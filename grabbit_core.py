#!/usr/bin/env python3
"""
grabbit_core — manifest (.grab v2), system capture, target-side resolution,
install planning/execution and migration bundles (.grab.run).

Standard library only: this runs on a freshly installed target machine before
anything else exists. Used by grabbit_gui.py and by `grabbit pack` (via the
CLI entry point at the bottom of this file).

.grab v2 keeps the v1 layout (header + PKG_LIST_START/END) and appends extra
sections after PKG_LIST_END, which v1 readers never reach:

    CAT_LIST_START      CAT name:src<TAB>category      (user|de|system|distro)
    SVC_LIST_START      SVC scope<TAB>unit<TAB>package  (scope: system|user)
    GRP_LIST_START      GRP group
    FILE_LIST_START     FILE kind<TAB>mode<TAB>size<TAB>key<TAB>dest<TAB>target
    *_LIST_END

FILE dest is "~/..." for files under the original $HOME (restored under the
new $HOME) or an absolute path (restored with sudo). kind is file|dir|link.
Keys starting "acct_" are sign-ins (git/GitHub CLI/SSH, Claude Code); text
files among them that name the old $HOME are rewritten to the new one.
"""

import fnmatch
import json
import os
import pwd
import grp
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

VERSION = "2"
APP_DIR = Path(__file__).resolve().parent
HOME = Path.home()
APP_FILES = ("grabbit", "grabbit-gui", "grabbit_gui.py", "grabbit_core.py",
             "install.sh", "grabbit-gui.desktop", "grabbit-bundle-stub.sh")
PAYLOAD_MARKER = "__GRABBIT_PAYLOAD_BELOW__"
# Package names end up on command lines; anything else in a .grab file is refused.
NAME_RE = re.compile(r"^[A-Za-z0-9@._+/-]+$")
# Package categories (the keys are what .grab files store):
#   user     = installed by you after the OS was set up
#   de       = desktop environment parts you added (KDE, GNOME, X11, ...)
#   hardware = drivers/firmware/microcode for a GPU or CPU vendor
#   os       = came with the OS: the installer's own choices, kernel/bootloader/base,
#              and the distro's own tools and branding. The new OS brings its own.
# DEFAULT_SELECTED is what starts ticked on restore (hardware is unticked anyway
# when the new machine lacks it).
CATEGORIES = ("user", "de", "hardware", "os")
CATEGORY_ALIASES = {"default": "os", "system": "os", "distro": "os"}   # older .grab files
DEFAULT_SELECTED = {"user": True, "de": True, "hardware": True, "os": False}


# ─────────────────────────────────────────────────────────────── model ───
@dataclass
class Package:
    name: str
    src: str
    category: str = "user"
    selected: bool = True
    via: str = ""          # resolved on the target: repo|aur|brew|flatpak|...|unavailable
    note: str = ""         # why it starts unticked on the target (already there, conflict, ...)

    @property
    def key(self):
        return f"{self.name}:{self.src}"


@dataclass
class Service:
    unit: str
    scope: str = "system"
    package: str = ""
    selected: bool = True


@dataclass
class LooseFile:
    kind: str              # file | dir | link
    mode: int
    size: int
    key: str               # path inside the bundle payload (files/<key>)
    dest: str              # "~/..." or absolute
    target: str = ""       # link target (same notation as dest)
    selected: bool = True


@dataclass
class Manifest:
    header: dict = field(default_factory=dict)
    packages: list = field(default_factory=list)
    services: list = field(default_factory=list)
    groups: list = field(default_factory=list)
    files: list = field(default_factory=list)
    group_selected: dict = field(default_factory=dict)


# ───────────────────────────────────────────────────────────── helpers ───
def run(argv, **kw):
    """Run and return stdout ('' on any failure)."""
    try:
        return subprocess.run(argv, capture_output=True, text=True, check=False, **kw).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def lines(argv, **kw):
    return [l.strip() for l in run(argv, **kw).splitlines() if l.strip()]


def have(cmd):
    return shutil.which(cmd) is not None


def os_release():
    data = {}
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                data[k] = v.strip().strip('"')
    except OSError:
        pass
    return data


FAMILIES = {
    "debian": ("apt", ("debian", "ubuntu", "linuxmint", "pop", "elementary", "kali", "raspbian", "neon", "zorin")),
    "arch": ("pacman", ("arch", "manjaro", "endeavouros", "garuda", "artix", "cachyos")),
    "fedora": ("dnf", ("fedora", "centos", "rhel", "rocky", "almalinux")),
    "suse": ("zypper", ("opensuse-tumbleweed", "opensuse-leap", "suse", "opensuse")),
    "alpine": ("apk", ("alpine",)),
}


def detect():
    """-> (distro_id, distro_name, family, pm)"""
    osr = os_release()
    did = osr.get("ID", "unknown").lower()
    like = osr.get("ID_LIKE", "").lower().split()
    for family, (pm, ids) in FAMILIES.items():
        if did in ids or did.startswith("opensuse") and family == "suse":
            return did, osr.get("NAME", did), family, pm
    for family, (pm, ids) in FAMILIES.items():
        if family in like or any(i in like for i in ids):
            return did, osr.get("NAME", did), family, pm
    for family, (pm, _) in FAMILIES.items():
        if have(pm):
            return did, osr.get("NAME", did), family, pm
    return did, osr.get("NAME", did), "unknown", "unknown"


def home_notation(path):
    p = str(path)
    h = str(HOME)
    return "~" + p[len(h):] if p == h or p.startswith(h + "/") else p


def expand(dest, home=None):
    home = str(home or HOME)
    return home + dest[1:] if dest.startswith("~") else dest


def human(n):
    for unit in ("B", "K", "M", "G"):
        if n < 1024 or unit == "G":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024


# ────────────────────────────────────────────────────── classification ───
BASE_PATTERNS = {
    "arch": ["linux", "linux-*", "linux-firmware*", "systemd", "systemd-*", "mkinitcpio*", "grub", "grub-*",
             "archlinux-*", "pacman", "pacman-*", "glibc", "filesystem", "base", "base-devel", "bash",
             "coreutils", "kmod", "hwdata", "iana-etc", "tzdata", "licenses", "pciutils", "usbutils",
             "inetutils", "iputils", "ca-certificates*", "openssl", "openssl-*", "intel-ucode", "amd-ucode",
             "efibootmgr", "dracut", "sudo", "man-db", "man-pages"],
    "debian": ["linux-*", "libc6", "systemd", "systemd-*", "dpkg", "dpkg-*", "apt", "apt-*", "debian-*",
               "perl-base", "ncurses-base", "base-files", "base-passwd"],
    "fedora": ["kernel", "kernel-*", "systemd", "systemd-*", "glibc", "bash", "coreutils", "dnf", "dnf-*",
               "rpm", "rpm-*", "fedora-release*"],
    "suse": ["kernel-*", "systemd", "systemd-*", "glibc", "bash", "coreutils", "zypper", "zypper-*"],
    "alpine": ["linux-*", "busybox", "alpine-baselayout", "apk-tools", "musl", "musl-*"],
}
DE_PATTERNS = [
    "sddm", "sddm-*", "lightdm", "lightdm-*", "gdm", "gdm-*", "ly", "greetd", "greetd-*",
    "kwin", "kwin-*", "mutter", "mutter-*", "xfwm4*", "marco*", "muffin*", "openbox*", "labwc*",
    "sway", "sway-*", "hyprland", "hyprland-*", "wlroots*", "weston*", "xwayland*", "xorg-*",
    "plasma-*", "plasma5-*", "kde-*", "kdeplasma-*", "kactivities-*", "frameworkintegration",
    "powerdevil", "systemsettings", "dolphin", "konsole", "kate", "spectacle", "breeze*", "kscreen",
    "gnome-*", "nautilus", "evolution-data-server", "xfce4-*", "xfdesktop", "thunar", "lxqt-*",
    "lxde-*", "pcmanfm*", "mate-*", "caja", "cinnamon-*", "nemo", "deepin-*", "dde-*", "budgie-*",
    "qt5-wayland", "qt6-wayland", "layer-shell-qt", "kwayland*", "xserver-xorg-*", "task-*-desktop",
    "ubuntu-desktop", "kubuntu-desktop", "xubuntu-desktop", "lubuntu-desktop", "patterns-kde-*",
    "patterns-gnome-*",
]
DOWNSTREAM_PATTERNS = {
    "endeavouros": ["eos-*", "endeavouros-*"], "manjaro": ["manjaro-*", "mhwd-*"],
    "garuda": ["garuda-*"], "artix": ["artix-*"], "cachyos": ["cachyos-*"],
    "ubuntu": ["ubuntu-*"], "linuxmint": ["mint-*", "linuxmint-*"], "pop": ["pop-*"],
    "elementary": ["elementary-*", "pantheon-*"], "kali": ["kali-*"], "neon": ["neon-*"],
}
OFFICIAL_PACMAN_REPOS = {"core", "extra", "multilib", "community", "testing", "core-testing",
                         "extra-testing", "multilib-testing", "community-testing"}


class Classifier:
    def __init__(self, family, distro_id):
        self.family, self.distro_id = family, distro_id
        self.base = set()
        self.de = set()
        self.downstream = set()
        if family == "arch":
            self.base.update(l.split()[1] for l in lines(["pacman", "-Qg", "base", "base-devel"]) if " " in l)
            for g in ("plasma", "kde-applications", "gnome", "xfce4", "lxqt", "mate", "cinnamon",
                      "deepin", "xorg", "budgie"):
                self.de.update(l.split()[1] for l in lines(["pacman", "-Qg", g]) if " " in l)
            for repo in self._extra_repos():
                self.downstream.update(l.split()[1] for l in lines(["pacman", "-Sl", repo]) if " " in l)
        for kind in ("base-excludes", "de-packages"):
            f = HOME / ".config/grabbit" / f"{kind}.{family}"
            if f.is_file():
                target = self.base if kind == "base-excludes" else self.de
                target.update(l.strip() for l in f.read_text().splitlines()
                              if l.strip() and not l.startswith("#"))

    @staticmethod
    def _extra_repos():
        try:
            names = re.findall(r"^\[([^\]]+)\]", Path("/etc/pacman.conf").read_text(), re.M)
        except OSError:
            return []
        return [n for n in names if n != "options" and n not in OFFICIAL_PACMAN_REPOS]

    def category(self, name, src):
        if src in ("brew", "flatpak", "snap", "pipx", "pip"):
            return "user"
        if name in self.base or any(fnmatch.fnmatch(name, p) for p in BASE_PATTERNS.get(self.family, [])):
            return "os"
        if any(fnmatch.fnmatch(name, p) for p in DOWNSTREAM_PATTERNS.get(self.distro_id, [])) \
                or name == self.distro_id:
            return "os"
        if name in self.de or any(fnmatch.fnmatch(name, p) for p in DE_PATTERNS):
            return "de"
        if name in self.downstream:
            return "os"
        return "user"


# ───────────────────────────────────────────────────────────── origin ───
# Which explicit packages the OS installer put there and which the user added
# later. A package never logged as installed, or last installed before the
# installer finished, came with the OS (offline installers copy an image, so
# nothing is logged for those at all).
INSTALLER_MARKERS = ("/var/log/Calamares.log", "/var/log/installer", "/var/log/archinstall",
                     "/var/log/anaconda")
PACMAN_LOG = "/var/log/pacman.log"
DPKG_LOGS = "/var/log/dpkg.log*"


def installer_end():
    """When the OS installer finished (epoch seconds), or None if no installer left a log."""
    for p in INSTALLER_MARKERS:
        try:
            return os.stat(p).st_mtime
        except OSError:
            pass
    return None


def _last_installs(events, end):
    """events: (epoch, name) of fresh installs. -> names installed after `end`, or None
    when the log can't tell (empty, or it starts a day after the install: rotated)."""
    if not events:
        return None
    t0 = min(t for t, _ in events)
    end = end if end is not None else t0 + 1800      # no installer log: its first half hour
    if t0 > end + 86400:
        return None
    last = {}
    for t, n in events:
        last[n] = max(t, last.get(n, 0))
    return {n for n, t in last.items() if t > end}


def pacman_added_after_install(log_path=None, end=None):
    try:
        text = Path(log_path or PACMAN_LOG).read_text(errors="replace")
    except OSError:
        return None
    events = []
    for m in re.finditer(r"^\[([^\]]+)\] \[ALPM\] installed (\S+) ", text, re.M):
        try:
            events.append((datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S%z").timestamp(), m.group(2)))
        except ValueError:
            pass
    return _last_installs(events, installer_end() if end is None else end)


def apt_added_after_install(log_glob=None, end=None):
    import glob
    import gzip
    events = []
    for f in glob.glob(log_glob or DPKG_LOGS):
        try:
            text = (gzip.open(f, "rt", errors="replace") if f.endswith(".gz") else open(f, errors="replace")).read()
        except OSError:
            continue
        for m in re.finditer(r"^(\S+ \S+) install (\S+?)(?::\S+)? ", text, re.M):
            try:
                events.append((datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp(), m.group(2)))
            except ValueError:
                pass
    return _last_installs(events, installer_end() if end is None else end)


def added_after_install(pm):
    """Names the user installed after setup, or None (unknown: fall back to patterns)."""
    return {"pacman": pacman_added_after_install, "apt": apt_added_after_install}.get(pm, lambda: None)()


# ──────────────────────────────────────────────────────────── hardware ───
# Packages that only make sense with a GPU/CPU vendor present. "driver" ones are
# what CachyOS's chwd installs for the detected GPU by itself.
HARDWARE = {
    ("gpu", "nvidia"): ["nvidia", "nvidia-*", "lib32-nvidia-*", "opencl-nvidia", "cuda", "cuda-*",
                        "xf86-video-nouveau", "xserver-xorg-video-nouveau", "akmod-nvidia*", "xorg-x11-drv-nvidia*"],
    ("gpu", "amd"): ["xf86-video-amdgpu", "xf86-video-ati", "vulkan-radeon", "lib32-vulkan-radeon", "amdvlk",
                     "lib32-amdvlk", "rocm-*", "hip-runtime-amd", "radeontop", "xserver-xorg-video-amdgpu",
                     "xserver-xorg-video-radeon", "firmware-amd-graphics"],
    ("gpu", "intel"): ["xf86-video-intel", "vulkan-intel", "lib32-vulkan-intel", "intel-media-driver",
                       "libva-intel-driver", "intel-compute-runtime", "intel-gpu-tools", "xserver-xorg-video-intel",
                       "intel-media-va-driver*", "i965-va-driver*"],
    ("cpu", "amd"): ["amd-ucode", "amd64-microcode"],
    ("cpu", "intel"): ["intel-ucode", "intel-microcode"],
}
GPU_DRIVERS = ["xf86-video-*", "vulkan-radeon", "lib32-vulkan-radeon", "vulkan-intel", "lib32-vulkan-intel",
               "amdvlk", "lib32-amdvlk", "nvidia", "nvidia-dkms", "nvidia-open*", "nvidia-utils",
               "lib32-nvidia-utils", "intel-media-driver", "libva-intel-driver"]
PCI_VENDORS = {"0x10de": "nvidia", "0x1002": "amd", "0x8086": "intel"}


def hardware_of(name):
    """-> (kind, vendor) the package is for, or None."""
    for key, pats in HARDWARE.items():
        if any(fnmatch.fnmatch(name, p) for p in pats):
            return key
    return None


def hardware():
    """{"gpu": {vendors}, "cpu": vendor} of this machine, from sysfs (no lspci needed)."""
    gpus = set()
    for d in Path("/sys/bus/pci/devices").glob("*"):
        try:
            if (d / "class").read_text().startswith("0x03"):
                gpus.add(PCI_VENDORS.get((d / "vendor").read_text().strip(), "other"))
        except OSError:
            pass
    try:
        info = Path("/proc/cpuinfo").read_text()
    except OSError:
        info = ""
    cpu = "amd" if "AuthenticAMD" in info else "intel" if "GenuineIntel" in info else ""
    return {"gpu": gpus, "cpu": cpu}


# ───────────────────────────────────────────────────────────── capture ───
def capture_packages(family, pm, classifier):
    found = []
    if pm == "pacman":
        found += [(n, "pacman") for n in lines(["pacman", "-Qqen"])]   # explicit, from a sync repo
        found += [(n, "aur") for n in lines(["pacman", "-Qqem"])]      # explicit, foreign (AUR/local)
    elif pm == "apt":
        found += [(n, "apt") for n in lines(["apt-mark", "showmanual"])]
    elif pm == "dnf":
        out = lines(["dnf", "repoquery", "--userinstalled", "--qf", "%{name}"])
        found += [(n, "dnf") for n in out]
    elif pm == "zypper":
        out = lines(["rpm", "-qa", "--qf", "%{NAME}\n"])
        found += [(n, "zypper") for n in out]
    elif pm == "apk":
        world = Path("/etc/apk/world")
        if world.is_file():
            found += [(re.split(r"[<>=~]", n)[0], "apk") for n in world.read_text().split()]

    brew = shutil.which("brew") or "/home/linuxbrew/.linuxbrew/bin/brew"
    if os.access(brew, os.X_OK):
        found += [(n, "brew") for n in lines([brew, "leaves", "--installed-on-request"])]
        found += [(n, "brew") for n in lines([brew, "list", "--cask"])]
    if have("flatpak"):
        found += [(n, "flatpak") for n in lines(["flatpak", "list", "--app", "--columns=application"])]
    if have("snap"):
        found += [(l.split()[0], "snap") for l in lines(["snap", "list"])[1:]
                  if l.split()[0] not in ("core", "core18", "core20", "core22", "core24", "snapd", "bare")]
    if have("pipx"):
        try:
            data = json.loads(run(["pipx", "list", "--json"]) or "{}")
            found += [(n, "pipx") for n in data.get("venvs", {})]
        except ValueError:
            pass
    py = shutil.which("python3")
    if py:
        out = run([py, "-m", "pip", "list", "--user", "--not-required", "--format=json"])
        try:
            found += [(d["name"], "pip") for d in json.loads(out or "[]")]
        except ValueError:
            pass

    added = added_after_install(pm)
    seen, pkgs = set(), []
    for name, src in found:
        if (name, src) in seen or not NAME_RE.match(name):
            continue
        seen.add((name, src))
        cat = classifier.category(name, src)
        native = src in ("pacman", "aur", "apt")
        if cat in ("user", "de") and added is not None and native and name not in added:
            cat = "os"              # the old installer chose it; the new one makes its own choice
        elif cat in ("user", "de") and hardware_of(name):
            cat = "hardware"
        pkgs.append(Package(name, src, cat))
    return pkgs


def pacman_owner(path):
    out = run(["pacman", "-Qoq", str(path)]).strip()
    return out.splitlines()[0] if out else ""


def pacman_required_by(pkg):
    """Installed packages that depend on `pkg` directly."""
    out = run(["pacman", "-Qi", pkg], env=dict(os.environ, LC_ALL="C"))
    m = re.search(r"^Required By\s*:\s*(.*?)(?=^\S)", out + "\nEnd", re.M | re.S)
    names = m.group(1).split() if m else []
    return [] if names == ["None"] else names


def capture_services(pm, packages, classifier=None):
    """Enabled units whose unit file belongs to a captured package, or to a
    dependency one pulled in (proton.VPN.service comes with proton-vpn-daemon, which
    proton-vpn-gtk-app requires): those are attached to that captured package, so
    the service follows its tick. Dependencies that came with the OS install
    (systemd, util-linux, avahi, pipewire, ...) are left out: the new OS runs its own."""
    cats = {p.name: p.category for p in packages}
    rank = {c: i for i, c in enumerate(CATEGORIES)}
    deps = set(lines(["pacman", "-Qqd"])) if pm == "pacman" else set()
    added = added_after_install(pm)       # None: can't tell, fall back to the classifier
    chooser = {}

    def chosen_for(owner):
        """The captured package that (transitively, 4 levels) requires `owner`, preferring
        the most likely ticked category."""
        if owner not in chooser:
            seen, frontier, found = {owner}, [owner], set()
            for _ in range(4):
                nxt = []
                for pkg in frontier:
                    for r in pacman_required_by(pkg):
                        if r in cats:
                            found.add(r)
                        elif r not in seen:
                            seen.add(r)
                            nxt.append(r)
                frontier = nxt
            chooser[owner] = min(found, key=lambda n: (rank.get(cats[n], 99), n)) if found else ""
        return chooser[owner]

    services = []
    for scope, cmd in (("system", ["systemctl"]), ("user", ["systemctl", "--user"])):
        for l in lines(cmd + ["list-unit-files", "--state=enabled", "--no-legend",
                              "--type=service,socket,timer,path"]):
            unit = l.split()[0]
            if "@" in unit and unit.endswith("@.service"):
                continue
            path = run(cmd + ["show", "-P", "FragmentPath", unit]).strip()
            owner = pacman_owner(path) if (pm == "pacman" and path) else ""
            if not owner:
                continue
            if owner in cats:
                services.append(Service(unit, scope, owner))
            elif owner in deps and (owner in added if added is not None else
                                    classifier is None or classifier.category(owner, "pacman") != "os"):
                via = chosen_for(owner)
                if via:
                    services.append(Service(unit, scope, via))
    return services


DEFAULT_GROUPS = {"wheel", "users", "sys", "adm", "audio", "video", "input", "storage", "network",
                  "power", "lp", "optical", "scanner", "rfkill", "sudo"}


def capture_groups():
    user = pwd.getpwuid(os.getuid()).pw_name
    primary = grp.getgrgid(os.getgid()).gr_name
    return [g.gr_name for g in grp.getgrall()
            if user in g.gr_mem and g.gr_name not in (primary, user)]


def _pip_entry_points():
    """Launchers pip --user wrote into ~/.local/bin (restored by reinstalling the package)."""
    names = set()
    for rec in HOME.glob(".local/lib/python3*/site-packages/*.dist-info/RECORD"):
        try:
            for l in rec.read_text().splitlines():
                p = l.split(",")[0]
                if "/bin/" in p:
                    names.add(Path(p).name)
        except OSError:
            pass
    return names


SKIP_NAMES = {"grabbit", "grabbit-gui", "grabbit_gui.py", "grabbit_core.py", "install.sh",
              "grabbit-bundle-stub.sh"}


def capture_files(pm):
    """Loose programs no package manager owns: ~/.local/bin, /usr/local/{bin,sbin},
    /opt/*, AppImages near $HOME, and rack's registry. Symlinks keep their target."""
    owned = (lambda p: bool(pacman_owner(p))) if pm == "pacman" else (lambda p: False)
    pip_bins = _pip_entry_points()
    out, keys = [], set()

    def add(path, dest=None):
        path = Path(path)
        dest = dest or home_notation(path)
        if any(f.dest == dest for f in out):
            return
        try:
            st = path.lstat()
        except OSError:
            return
        if stat.S_ISLNK(st.st_mode):
            real = Path(os.path.realpath(path))
            if not real.exists():
                return
            out.append(LooseFile("link", 0o777, 0, "", dest, home_notation(real)))
            if not owned(real) and "/.local/share/pipx/" not in str(real):
                add(real)
            return
        key = re.sub(r"[^A-Za-z0-9._-]", "_", dest.lstrip("~/")).lstrip("._") or "root"
        while key in keys:
            key += "_"
        keys.add(key)
        if stat.S_ISDIR(st.st_mode):
            size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file() and not f.is_symlink())
            if size == 0:
                keys.discard(key)
                return
            out.append(LooseFile("dir", stat.S_IMODE(st.st_mode), size, key, dest))
        elif stat.S_ISREG(st.st_mode) and os.access(path, os.R_OK):
            out.append(LooseFile("file", stat.S_IMODE(st.st_mode), st.st_size, key, dest))

    for d in (HOME / ".local/bin", Path("/usr/local/bin"), Path("/usr/local/sbin")):
        if d.is_dir():
            for f in sorted(d.iterdir()):
                if f.name in SKIP_NAMES or f.name in pip_bins:
                    continue
                if f.is_symlink() and "/.local/share/pipx/" in os.path.realpath(f):
                    continue
                if not owned(f):
                    add(f)
    if Path("/opt").is_dir():
        for f in sorted(Path("/opt").iterdir()):
            if not owned(f):
                add(f)
    for pattern in ("*.AppImage", "*.appimage", "*/*.AppImage", "*/*.appimage",
                    "*/*/*.AppImage", "*/*/*.appimage"):
        for f in HOME.glob(pattern):
            if "/." not in str(f.relative_to(HOME)):
                add(f)
    rack = HOME / ".local/share/rack"
    if rack.is_dir():
        for f in sorted(rack.glob("*.tsv")):
            add(f)
    return out


# ──────────────────────────────────────────────────────────── accounts ───
# Signed-in tools whose login lives in $HOME: git + GitHub CLI + SSH keys,
# Claude Code, KMail (Akonadi accounts + their KWallet passwords), the Railway,
# Stripe, Grok and Copilot CLIs, the adb key (paired devices stay authorized),
# plus the shell setup that puts those CLIs on PATH (bash startup files, fish
# conf.d + completions) and the command history of both shells. All of it is a credential or carries one, so a bundle holding
# any of it is written 0700. Their keys start with ACCOUNT_KEY.
ACCOUNT_KEY = "acct_"
ACCOUNT_PATHS = (
    "~/.gitconfig", "~/.git-credentials", "~/.config/git", "~/.config/gh", "~/.ssh",
    "~/.claude.json", "~/.claude/.credentials.json", "~/.claude/settings.json",
    "~/.claude/settings.local.json", "~/.claude/CLAUDE.md", "~/.claude/keybindings.json",
    "~/.claude/agents", "~/.claude/commands", "~/.claude/skills", "~/.claude/plugins",
    "~/.claude/projects", "~/.claude/history.jsonl",
    "~/.config/kmail2rc", "~/.config/emailidentities", "~/.config/mailtransports", "~/.config/akonadi",
    "~/.config/akonadi_*_resource_*rc", "~/.local/share/local-mail",
    "~/.railway/config.json", "~/.railway/env", "~/.railway/env.fish", "~/.railway/bin",
    "~/.config/stripe", "~/.grok/auth.json", "~/.grok/config.toml", "~/.grok/agent_id", "~/.grok/skills",
    "~/.copilot/config.json", "~/.android/adbkey", "~/.android/adbkey.pub",
    "~/.bashrc", "~/.bash_profile", "~/.bash_history",
    "~/.config/fish/conf.d", "~/.config/fish/completions",
)
# gh keeps its token in the system keyring, not in ~/.config/gh: it is read with
# `gh auth token` when the bundle is written and signed back in on restore.
GH_TOKEN_DEST = "~/.config/grabbit/gh-token"
# KMail's passwords live in KWallet, not in its config: read with kwallet-query when
# the bundle is written, written back on restore (never through KMail's password
# prompt, which drops it in kdepim-runtime 26.08.1).
KMAIL_WALLET_DEST = "~/.config/grabbit/kmail-wallet.json"
# fish's history, converted from ~/.bash_history when the bundle is written.
FISH_HISTORY_DEST = "~/.local/share/fish/fish_history"
# Histories merge into what the new machine already has instead of replacing it.
MERGE_ON_RESTORE = ("~/.bash_history", FISH_HISTORY_DEST)
# Restored text files that name the old $HOME by absolute path (rewritten to the new one).
REWRITE_HOME = ("~/.config/akonadi/akonadiserverrc", "~/.railway/env", "~/.railway/env.fish",
                "~/.railway/config.json", "~/.bashrc", "~/.bash_profile", "~/.grok/config.toml", "~/.config/akonadi_*rc", "~/.config/kmail2rc",
                "~/.config/emailidentities", "~/.config/mailtransports",
                "~/.gitconfig", "~/.ssh/config", "~/.claude.json", "~/.claude/settings.json",
                "~/.claude/settings.local.json", "~/.claude/history.jsonl", "~/.claude/plugins/*.json",
                "~/.claude/projects/*/memory/*.md", "~/.local/share/rack/*.tsv")


def gh_token():
    return run(["gh", "auth", "token", "--hostname", "github.com"]).strip() if have("gh") else ""


def kmail_wallet_entries():
    """(folder, key) of the KWallet passwords KMail's accounts use: one per IMAP
    resource that still has a config, one per mail transport."""
    home = Path(expand("~"))
    entries = [("imap", f.name) for f in sorted((home / ".config").glob("akonadi_imap_resource_*rc"))]
    try:
        ids = re.findall(r"^\[Transport (\d+)\]", (home / ".config/mailtransports").read_text(), re.M)
    except OSError:
        ids = []
    return entries + [("mailtransports", i) for i in ids]


def kmail_wallet_secrets():
    """{"entries": [[folder, key, password], ...]} read from kdewallet (may ask to unlock it)."""
    if not have("kwallet-query"):
        return None
    got = []
    for folder, key in kmail_wallet_entries():
        r = subprocess.run(["kwallet-query", "-r", key, "-f", folder, "kdewallet"], capture_output=True,
                           text=True, check=False)
        if r.returncode == 0 and r.stdout.rstrip("\n"):
            got.append([folder, key, r.stdout.rstrip("\n")])
    return {"entries": got} if got else None


def fish_history_from_bash(text, end=None):
    """~/.bash_history -> fish_history (consecutive repeats dropped; bash keeps no
    times here, so they count up to `end` one second apart)."""
    cmds = []
    for line in text.splitlines():
        line = line.rstrip()
        if line and not line.startswith("#") and (not cmds or cmds[-1] != line):
            cmds.append(line)
    end = int(end or time.time())
    out = []
    for i, cmd in enumerate(cmds):
        out.append(f"- cmd: {cmd.replace(chr(92), chr(92) * 2)}\n  when: {end - len(cmds) + i}\n")
    return "".join(out)


def capture_accounts():
    out = []
    dests = []
    for dest in ACCOUNT_PATHS:
        if "*" in dest:
            dests += sorted(home_notation(p) for p in Path(expand("~")).glob(dest[2:]))
        else:
            dests.append(dest)
    for dest in dests:
        path = Path(expand(dest))
        try:
            st = path.lstat()
        except OSError:
            continue
        key = ACCOUNT_KEY + re.sub(r"[^A-Za-z0-9._-]", "_", dest.lstrip("~/")).lstrip("._")
        if stat.S_ISDIR(st.st_mode):
            size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file() and not f.is_symlink())
            if size:
                out.append(LooseFile("dir", stat.S_IMODE(st.st_mode), size, key, dest))
        elif stat.S_ISREG(st.st_mode) and os.access(path, os.R_OK):
            out.append(LooseFile("file", stat.S_IMODE(st.st_mode), st.st_size, key, dest))
    token = gh_token()
    if token:
        out.append(LooseFile("file", 0o600, len(token) + 1, ACCOUNT_KEY + "gh-token", GH_TOKEN_DEST))
    fish_hist, bash_hist = Path(expand(FISH_HISTORY_DEST)), Path(expand("~/.bash_history"))
    if fish_hist.is_file():                 # fish already in use here: its own history travels
        out.append(LooseFile("file", 0o600, fish_hist.stat().st_size, ACCOUNT_KEY + "fish_history", FISH_HISTORY_DEST))
    elif bash_hist.is_file():               # otherwise it's made from bash's when the bundle is written
        out.append(LooseFile("file", 0o600, bash_hist.stat().st_size, ACCOUNT_KEY + "fish_history", FISH_HISTORY_DEST))
    if have("kwallet-query") and kmail_wallet_entries() and any(f.dest == "~/.config/mailtransports" or
                                                               "akonadi_imap" in f.dest for f in out):
        out.append(LooseFile("file", 0o600, 256, ACCOUNT_KEY + "kmail-wallet", KMAIL_WALLET_DEST))
    return out


def keep_original(dest, incoming):
    """Before a restored file replaces a different one already here (the new OS's own
    ~/.bashrc, ...), save it as <dest>.pre-grabbit."""
    if not os.path.isfile(dest) or os.path.islink(dest) or not os.path.isfile(incoming):
        return
    try:
        if Path(dest).read_bytes() != Path(incoming).read_bytes() and not os.path.exists(dest + ".pre-grabbit"):
            shutil.copy2(dest, dest + ".pre-grabbit")
    except OSError:
        pass


def claude_project_name(path):
    """Claude Code names ~/.claude/projects/<dir> after the project path."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def rewrite_home(restored, orig_home, home=None, log=print):
    """Old $HOME -> this one in the restored files REWRITE_HOME names (and in
    Claude Code's project directory names). restored: "~/..." dests just written."""
    home = str(home or HOME)
    if not orig_home or orig_home == home:
        return
    pairs = ((orig_home, home), (claude_project_name(orig_home), claude_project_name(home)))
    projects = Path(home) / ".claude/projects"
    if "~/.claude/projects" in restored and projects.is_dir():
        old = claude_project_name(orig_home)
        for d in sorted(projects.iterdir()):
            if d.name == old or d.name.startswith(old + "-"):
                new = projects / (claude_project_name(home) + d.name[len(old):])
                shutil.copytree(d, new, symlinks=True, dirs_exist_ok=True)
                shutil.rmtree(d)
                log(f"  renamed ~/.claude/projects/{d.name} -> {new.name}")
    for dest in restored:
        root = Path(expand(dest, home))
        for p in ([root] if root.is_file() else root.rglob("*") if root.is_dir() else []):
            if not p.is_file() or p.is_symlink():
                continue
            note = "~" + str(p)[len(home):]
            if not any(fnmatch.fnmatch(note, g) for g in REWRITE_HOME):
                continue
            try:
                text = p.read_text()
            except (OSError, UnicodeDecodeError):
                continue
            new = text
            for a, b in pairs:
                new = new.replace(a, b)
            if new != text:
                p.write_text(new)
                log(f"  {note}: {orig_home} -> {home}")


def capture(log=print, accounts=True):
    did, dname, family, pm = detect()
    log(f"Detected {dname} ({family}/{pm})")
    cls = Classifier(family, did)
    pkgs = capture_packages(family, pm, cls)
    log(f"Packages: {len(pkgs)}")
    names = {p.name for p in pkgs}
    services = capture_services(pm, pkgs, cls)
    log(f"Enabled services from those packages: {len(services)}")
    groups = capture_groups()
    log(f"Groups: {', '.join(groups) or 'none'}")
    hw = hardware()
    log(f"Hardware: GPU {', '.join(sorted(hw['gpu'])) or '?'}, CPU {hw['cpu'] or '?'}")
    files = capture_files(pm)
    log(f"Loose files: {len(files)} ({human(sum(f.size for f in files))})")
    if accounts:
        acct = capture_accounts()
        log(f"Sign-ins (git/GitHub/SSH, Claude Code): {len(acct)} ({human(sum(f.size for f in acct))})")
        files += acct
    header = {
        "Created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ORIG_DISTRO": did, "ORIG_FAMILY": family, "ORIG_PM": pm,
        "ORIG_DISTRO_NAME": f'"{dname}"', "ORIG_HOME": str(HOME),
        "ORIG_USER": pwd.getpwuid(os.getuid()).pw_name,
        "ORIG_SHELL": pwd.getpwuid(os.getuid()).pw_shell,
        "ORIG_GPU": ",".join(sorted(hw["gpu"])) or "unknown", "ORIG_CPU": hw["cpu"] or "unknown",
    }
    return Manifest(header, pkgs, services, groups, files)


# ──────────────────────────────────────────────────────── (de)serialize ───
def dumps(m):
    out = [f"# GRABBIT v{VERSION}"]
    out += [f"# {k}={v}" if k != "Created" else f"# Created: {v}" for k, v in m.header.items()]
    out += ["", "PKG_LIST_START"]
    out += [f"PKG {p.key}" for p in m.packages]
    out += ["PKG_LIST_END", "", "CAT_LIST_START"]
    out += [f"CAT {p.key}\t{p.category}" for p in m.packages]
    out += ["CAT_LIST_END", "", "SVC_LIST_START"]
    out += [f"SVC {s.scope}\t{s.unit}\t{s.package}" for s in m.services]
    out += ["SVC_LIST_END", "", "GRP_LIST_START"]
    out += [f"GRP {g}" for g in m.groups]
    out += ["GRP_LIST_END", "", "FILE_LIST_START"]
    out += [f"FILE {f.kind}\t{f.mode:o}\t{f.size}\t{f.key}\t{f.dest}\t{f.target}" for f in m.files]
    out += ["FILE_LIST_END", ""]
    return "\n".join(out)


def loads(text):
    m = Manifest()
    section = None
    cats = {}
    for raw in text.splitlines():
        line = raw.rstrip("\n")
        if line.startswith("# ") and "=" in line and section is None:
            k, v = line[2:].split("=", 1)
            m.header[k.strip()] = v.strip()
            continue
        if line.startswith("# Created:"):
            m.header["Created"] = line.split(":", 1)[1].strip()
            continue
        if line.endswith("_LIST_START"):
            section = line[:-len("_LIST_START")]
            continue
        if line.endswith("_LIST_END"):
            section = None
            continue
        if not section or not line.strip():
            continue
        tag, _, rest = line.partition(" ")
        if section == "PKG" and tag == "PKG" and ":" in rest:
            name, src = rest.strip().split(":", 1)
            if NAME_RE.match(name) and NAME_RE.match(src):
                m.packages.append(Package(name, src))
        elif section == "CAT" and tag == "CAT":
            key, _, cat = rest.partition("\t")
            cat = CATEGORY_ALIASES.get(cat.strip(), cat.strip())
            cats[key.strip()] = cat if cat in CATEGORIES else "user"
        elif section == "SVC" and tag == "SVC":
            parts = rest.split("\t")
            if len(parts) >= 2 and re.match(r"^[A-Za-z0-9@._:\\-]+$", parts[1]):
                m.services.append(Service(parts[1], parts[0], parts[2] if len(parts) > 2 else ""))
        elif section == "GRP" and tag == "GRP" and re.match(r"^[a-z_][a-z0-9_-]*$", rest.strip()):
            m.groups.append(rest.strip())
        elif section == "FILE" and tag == "FILE":
            parts = rest.split("\t")
            if len(parts) >= 5 and parts[0] in ("file", "dir", "link"):
                m.files.append(LooseFile(parts[0], int(parts[1], 8), int(parts[2]), parts[3], parts[4],
                                         parts[5] if len(parts) > 5 else ""))
    for p in m.packages:
        p.category = cats.get(p.key, "user")
        p.selected = DEFAULT_SELECTED.get(p.category, True)
    m.group_selected = {g: True for g in m.groups}
    return m


def load_path(path):
    """Read a .grab file or the manifest inside a .grab.run bundle (streamed)."""
    marker = ("\n" + PAYLOAD_MARKER + "\n").encode()
    with open(path, "rb") as fh:
        head = fh.read(64 * 1024)
        idx = head.find(marker)
        if idx < 0:
            return loads((head + fh.read()).decode("utf-8", "replace"))
        fh.seek(idx + len(marker))
        with tarfile.open(fileobj=fh, mode="r|*") as tar:
            for member in tar:
                if member.name == "manifest.grab":
                    return loads(tar.extractfile(member).read().decode())
    raise ValueError("bundle has no manifest.grab")


# ─────────────────────────────────────────────────────────────── bundle ───
def extract_bundle(path, dest_root=None):
    """Unpack a .grab.run payload; returns the directory (manifest.grab, app/, files/)."""
    data_home = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local/share")
    dest = Path(dest_root or data_home / "grabbit/bundles") / (
        Path(path).name.removesuffix(".run") + datetime.now().strftime("-%Y%m%d-%H%M%S"))
    dest.mkdir(parents=True, exist_ok=True)
    dest.chmod(0o700)   # may hold sign-ins
    marker = ("\n" + PAYLOAD_MARKER + "\n").encode()
    with open(path, "rb") as fh:
        head = fh.read(64 * 1024)
        idx = head.find(marker)
        if idx < 0:
            raise ValueError("not a grabbit bundle (payload marker missing)")
        fh.seek(idx + len(marker))
        with tarfile.open(fileobj=fh, mode="r|*") as tar:
            try:
                tar.extractall(dest, filter="tar")
            except TypeError:           # Python without extraction filters
                tar.extractall(dest)
    return dest


def build_bundle(manifest, out_path, log=print, include_files=True):
    stub = (APP_DIR / "grabbit-bundle-stub.sh").read_text()
    if not stub.rstrip().endswith(PAYLOAD_MARKER):
        raise RuntimeError("stub must end with the payload marker")
    files = manifest.files if include_files else []
    if not include_files:
        manifest = Manifest(manifest.header, manifest.packages, manifest.services, manifest.groups, [])
    out_path = Path(out_path)
    tmp = out_path.with_name(out_path.name + ".part")
    with open(tmp, "wb") as fh:
        fh.write(stub.rstrip().encode() + b"\n")
        with tarfile.open(fileobj=fh, mode="w|gz", compresslevel=1) as tar:
            def add_bytes(name, data, mode=0o644):
                info = tarfile.TarInfo(name)
                info.size, info.mode, info.mtime = len(data), mode, int(time.time())
                import io
                tar.addfile(info, io.BytesIO(data))
            add_bytes("manifest.grab", dumps(manifest).encode())
            for name in APP_FILES:
                src = APP_DIR / name
                if src.is_file():
                    tar.add(str(src), arcname=f"app/{name}")
            for f in files:
                if f.kind == "link":
                    continue
                log(f"  + {f.dest} ({human(f.size)})")
                if f.dest == FISH_HISTORY_DEST and not os.path.exists(expand(f.dest)):
                    try:
                        hist = Path(expand("~/.bash_history"))
                        add_bytes(f"files/{f.key}", fish_history_from_bash(hist.read_text(errors="replace"),
                                                                           hist.stat().st_mtime).encode(), 0o600)
                    except OSError as e:
                        log(f"  ! skipped the fish history: {e}")
                    continue
                if f.dest == KMAIL_WALLET_DEST:
                    secrets = kmail_wallet_secrets()
                    if secrets:
                        add_bytes(f"files/{f.key}", json.dumps(secrets).encode(), 0o600)
                    else:
                        log("  ! skipped KMail's passwords: KWallet gave none (is it unlocked?)")
                    continue
                if f.dest == GH_TOKEN_DEST:
                    token = gh_token()
                    if token:
                        add_bytes(f"files/{f.key}", (token + "\n").encode(), 0o600)
                    else:
                        log("  ! skipped the GitHub CLI token: `gh auth token` gave nothing")
                    continue
                try:
                    tar.add(expand(f.dest), arcname=f"files/{f.key}")
                except OSError as e:
                    log(f"  ! skipped {f.dest}: {e}")
    secret = any(f.key.startswith(ACCOUNT_KEY) for f in files)
    if secret:
        log("  ! this bundle holds sign-ins (tokens, SSH keys): it is written 0700, keep it private")
    os.chmod(tmp, 0o700 if secret else 0o755)
    tmp.replace(out_path)
    return out_path


# ──────────────────────────────────────────────────────────── resolve ───
def _http_get(url, timeout=30):
    """curl when present: it races IPv4/IPv6 (urllib waits out a dead IPv6 route, e.g. on a VPN)."""
    if have("curl"):
        r = subprocess.run(["curl", "-fsSL", "--max-time", str(timeout), url],
                           capture_output=True, text=True, check=False)
        if r.returncode == 0:
            return r.stdout
        raise OSError(r.stderr.strip() or f"curl exit {r.returncode}")
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode()


def aur_lookup(names, log=print):
    """{name: RPC info} for names that exist on the AUR (batched RPC, 150 per request)."""
    found = {}
    names = sorted(set(names))
    for i in range(0, len(names), 150):
        chunk = names[i:i + 150]
        url = "https://aur.archlinux.org/rpc/v5/info?" + urllib.parse.urlencode([("arg[]", n) for n in chunk])
        try:
            found.update((x["Name"], x) for x in json.loads(_http_get(url)).get("results", []))
        except Exception as e:  # network down: report and treat as unknown
            log(f"AUR lookup failed: {e}")
            return None
    return found


def resolve(manifest, family, pm, log=print, target_checks=True):
    """Set Package.via for this machine. Unavailable ones are deselected.
    target_checks: also untick what this machine already has / would conflict with
    (only meaningful when restoring here; a scan of the source machine skips it)."""
    native = {"repo"}
    if pm == "pacman":
        sync = set(lines(["pacman", "-Slq"]))
        if not sync:   # no sync databases yet: can't tell repo from AUR, trust the origin
            log("pacman has no sync databases (run pacman -Syu); using each package's original source")
            for p in manifest.packages:
                p.via = {"pacman": "repo", "aur": "aur"}.get(p.src, p.src)
            return manifest
        need_aur = [p.name for p in manifest.packages
                    if p.src in ("pacman", "aur", "apt", "dnf", "zypper", "apk") and p.name not in sync]
        aur = aur_lookup(need_aur, log) if need_aur else {}
        for p in manifest.packages:
            if p.src in ("brew", "flatpak", "snap", "pipx", "pip"):
                p.via = p.src
            elif p.name in sync:
                p.via = "repo"
            elif aur is None:
                p.via = "aur?"
            elif p.name in aur:
                p.via = "aur"
            else:
                p.via = "unavailable"
    else:
        for p in manifest.packages:
            p.via = p.src if p.src in ("brew", "flatpak", "snap", "pipx", "pip") else (
                "repo" if p.src in (pm, "apt", "pacman", "dnf", "zypper", "apk") else "unavailable")
            if p.src == "aur":
                p.via = "unavailable"
    for p in manifest.packages:
        if p.via == "unavailable":
            p.selected = False
    if not target_checks:
        return manifest
    try:
        if pm == "pacman":
            check_pacman_target(manifest, aur or {})
        elif pm == "apt":
            check_apt_target(manifest)
        check_hardware(manifest)
        check_cross_manager(manifest, pm)
    except OSError as e:   # a check that can't run leaves the choice to the user
        log(f"Could not check what this system already has: {e}")
    return manifest


# ──────────────────────────────────────────────── target-side conflicts ───
# Packages that do the same job without declaring a conflict, so pacman installs
# both and their services fight (both firewalls load nftables rules at boot;
# a second display manager's service can't take display-manager.service).
# If the target already has one, the bundle's other one starts unticked.
ROLE_GROUPS = {   # role -> {package: its service ("" = none known: installed is enough)}
    "firewall": {"firewalld": "firewalld.service", "ufw": "ufw.service"},
    "display manager": {"sddm": "sddm.service", "gdm": "gdm.service", "lightdm": "lightdm.service",
                        "lxdm": "lxdm.service", "ly": "ly.service", "greetd": "greetd.service",
                        "plasma-login-manager": ""},
    "power profiles": {"power-profiles-daemon": "power-profiles-daemon.service", "tlp": "tlp.service",
                       "auto-cpufreq": "auto-cpufreq.service", "tuned-ppd": "tuned-ppd.service"},
}
# Services that do one job: enabling a second one next to an enabled one fights it.
# (Display managers are caught by their shared display-manager.service alias.)
SERVICE_ROLES = {
    "firewall": {"firewalld.service", "ufw.service", "nftables.service", "iptables.service"},
    "time sync": {"systemd-timesyncd.service", "chronyd.service", "ntpd.service", "openntpd.service"},
    "network manager": {"NetworkManager.service", "systemd-networkd.service", "connman.service", "wicd.service"},
    "power profiles": {"power-profiles-daemon.service", "tlp.service", "auto-cpufreq.service", "tuned-ppd.service"},
}


def unit_on(unit, scope="system"):
    sc = ["systemctl"] + (["--user"] if scope == "user" else [])
    return (run(sc + ["is-enabled", unit]).strip() in ("enabled", "enabled-runtime")
            or run(sc + ["is-active", unit]).strip() == "active")


def unit_skip_reason(unit, scope="system"):
    """Why enabling `unit` here would be wrong ("" = go ahead)."""
    sc = ["systemctl"] + (["--user"] if scope == "user" else [])
    if run(sc + ["show", "-P", "LoadState", unit]).strip() != "loaded":
        return "not installed here"
    if run(sc + ["is-enabled", unit]).strip() in ("enabled", "enabled-runtime"):
        return "already enabled"
    for other in run(sc + ["show", "-P", "Conflicts", unit]).split():
        if other.endswith((".service", ".socket")) and other != unit and unit_on(other, scope):
            return f"it conflicts with {other}, which is on here"
    if scope == "system":
        for alias in re.findall(r"^\s*Alias\s*=\s*(\S+)", run(sc + ["cat", unit]), re.M):
            link = Path("/etc/systemd/system") / alias
            if link.is_symlink() and Path(os.path.realpath(link)).name != unit:
                return f"{alias} is already {Path(os.path.realpath(link)).name}"
    for role, units in SERVICE_ROLES.items():
        if unit in units:
            rivals = sorted(u for u in units if u != unit and unit_on(u, scope))
            if rivals:
                return f"this system's {role} is {rivals[0]}"
    return ""


def pacman_satisfied(specs):
    """The specs (names, provides, versioned deps) installed packages already meet."""
    specs = sorted(set(specs))
    unmet = set()
    for i in range(0, len(specs), 200):
        unmet.update(lines(["pacman", "-T"] + specs[i:i + 200]))
    return set(specs) - unmet


def pacman_sync_info(names):
    """{name: {"Conflicts With": [...], "Provides": [...]}} from the sync databases."""
    info, cur, key = {}, None, None
    out = run(["pacman", "-Si"] + sorted(set(names)), env=dict(os.environ, LC_ALL="C")) if names else ""
    for l in out.splitlines():
        if not l.strip():
            cur = key = None
            continue
        if l[:1].isspace() and cur is not None and key:      # wrapped value
            cur[key] += l.split()
            continue
        k, _, v = l.partition(":")
        k = k.strip()
        if k == "Name":
            cur = info.setdefault(v.strip(), {}) if v.strip() not in info else None   # first repo wins
            key = None
        elif cur is not None and k in ("Conflicts With", "Provides"):
            key = k
            cur[k] = [] if v.strip() == "None" else v.split()
        else:
            key = None
    return info


def check_pacman_target(manifest, aur_info):
    """Untick repo/AUR packages this machine already has (by name or provides), that
    conflict with something installed, or whose role an installed package fills."""
    installed = set(lines(["pacman", "-Qq"]))
    native = [p for p in manifest.packages if p.via in ("repo", "aur", "aur?")]
    todo = [p.name for p in native if p.name not in installed]
    provided = pacman_satisfied(todo)
    meta = pacman_sync_info([p.name for p in native if p.via == "repo" and p.name not in installed])
    for name, x in aur_info.items():
        meta.setdefault(name, {"Conflicts With": x.get("Conflicts") or [], "Provides": x.get("Provides") or []})
    met = pacman_satisfied({c for m in meta.values() for c in m.get("Conflicts With", [])})
    role_of = {pkg: role for role, pkgs in ROLE_GROUPS.items() for pkg in pkgs}
    # a dependency the install would pull in can conflict too
    bad_deps = pacman_conflicting_deps([p for p in native if p.via == "repo" and p.name not in installed],
                                       installed)
    for p in native:
        hits = [c for c in meta.get(p.name, {}).get("Conflicts With", []) if c in met]
        group = ROLE_GROUPS.get(role_of.get(p.name), {})
        rivals = sorted(x for x, unit in group.items()
                        if x != p.name and x in installed and (not unit or unit_on(unit)))
        if p.name in installed:
            p.note = "already installed"
        elif p.name in provided:
            p.note = "already provided by an installed package"
        elif hits:
            p.note = "conflicts with installed " + ", ".join(hits)
        elif p.name in bad_deps:
            p.note = bad_deps[p.name]
        elif rivals:
            p.note = f"this system's {role_of[p.name]} is {', '.join(rivals)}"
        if p.note:
            p.selected = False


def pacman_closure(names):
    """Every package `pacman -S names` would install (deps included), or None if pacman can't plan it."""
    out = subprocess.run(["pacman", "-Sp", "--print-format", "%n"] + sorted(names), capture_output=True,
                         text=True, check=False, env=dict(os.environ, LC_ALL="C"))
    return set(out.stdout.split()) if out.returncode == 0 else None


def pacman_conflicting_deps(pkgs, installed):
    """{candidate: reason} for repo candidates whose install pulls in a dependency
    that conflicts with an installed package."""
    names = [p.name for p in pkgs]
    if not names:
        return {}
    closure = pacman_closure(names)
    if closure is None:                                   # one bad apple: plan them one by one
        closure = set().union(*(pacman_closure([n]) or set() for n in names))
    deps = closure - installed          # candidates too: one may be another's dependency
    if not deps:
        return {}
    meta = pacman_sync_info(deps)
    met = pacman_satisfied({c for m in meta.values() for c in m.get("Conflicts With", [])})
    bad = {d: [c for c in m.get("Conflicts With", []) if c in met] for d, m in meta.items()}
    bad = {d: c for d, c in bad.items() if c}
    out = {}
    for n in names if bad else []:
        for d in sorted(((pacman_closure([n]) or set()) & set(bad)) - {n}):
            out[n] = f"needs {d}, which conflicts with installed {', '.join(bad[d])}"
            break
    return out


def apt_simulate(names):
    """-> (ok, packages the install would remove, last error line) from apt-get -s."""
    r = subprocess.run(["apt-get", "-s", "install"] + list(names), capture_output=True, text=True,
                       check=False, env=dict(os.environ, LC_ALL="C"))
    removes = [l.split()[1] for l in r.stdout.splitlines() if l.startswith("Remv ")]
    err = (r.stderr.strip().splitlines() or [""])[-1]
    return r.returncode == 0, removes, err


def check_apt_target(manifest):
    """Untick apt packages that are already installed, can't be installed here, or would
    make apt remove an installed package (apt-get -y would just do it)."""
    cands = [p for p in manifest.packages if p.via == "repo" and p.selected]
    for p in cands:
        if native_installed(p.name, "apt"):
            p.note = "already installed"
    todo = [p for p in cands if not p.note]
    ok, removes, _ = apt_simulate([p.name for p in todo]) if todo else (True, [], "")
    if not ok or removes:
        for p in todo:                                    # find the culprits
            ok, removes, err = apt_simulate([p.name])
            if removes:
                p.note = "would make apt remove installed " + ", ".join(removes[:4])
            elif not ok:
                p.note = "apt can't install it here: " + err.removeprefix("E: ")[:120]
    for p in cands:
        if p.note:
            p.selected = False


def check_hardware(manifest):
    """Untick drivers/microcode for hardware this machine lacks; on CachyOS leave GPU
    drivers to chwd, which installs the right ones for the detected GPU."""
    hw = hardware()
    chwd = have("chwd")
    for p in manifest.packages:
        need = hardware_of(p.name)
        if p.note or not need:
            continue
        kind, vendor = need
        if kind == "gpu" and hw["gpu"] and vendor not in hw["gpu"]:
            p.note = f"for {vendor} graphics; this machine has {', '.join(sorted(hw['gpu']))}"
        elif kind == "cpu" and hw["cpu"] and vendor != hw["cpu"]:
            p.note = f"microcode for {vendor} CPUs; this machine's CPU is {hw['cpu']}"
        elif kind == "gpu" and chwd and any(fnmatch.fnmatch(p.name, g) for g in GPU_DRIVERS):
            p.note = "CachyOS installs GPU drivers itself (chwd)"
        if p.note:
            p.selected = False


def native_installed(name, pm):
    if pm == "pacman":
        return bool(pacman_satisfied([name]))
    argv = {"apt": ["dpkg", "-s"], "dnf": ["rpm", "-q"], "zypper": ["rpm", "-q"], "apk": ["apk", "info", "-e"]}.get(pm)
    return bool(argv) and subprocess.run(argv + [name], capture_output=True, check=False).returncode == 0


def check_cross_manager(manifest, pm):
    """A brew/pipx/pip package the system package manager already supplies here (or that the
    bundle installs natively too) would be a second copy, shadowing the first on PATH."""
    native = {p.name for p in manifest.packages if p.selected and p.via in ("repo", "aur", "aur?")}
    for p in manifest.packages:
        if p.via not in ("brew", "pipx", "pip") or p.note:
            continue
        low = p.name.lower()
        if p.via == "pip":
            names = [f"python-{low}", f"python3-{low}"]
        else:
            names = [p.name, f"python-{low}"] if p.via == "pipx" else [p.name]
        if any(n in native for n in names):
            p.note = f"the bundle installs {next(n for n in names if n in native)} natively"
        elif any(native_installed(n, pm) for n in names):
            p.note = "already installed by " + pm
        elif p.via in ("brew", "pipx") and os.path.exists(f"/usr/bin/{p.name}"):
            p.note = f"/usr/bin/{p.name} already exists"
        if p.note:
            p.selected = False


# ──────────────────────────────────────────────────────────────── plan ───
@dataclass
class Step:
    label: str
    argv: list
    root: bool = False            # run through sudo -A
    items: list = field(default_factory=list)   # per-item fallback when the batch fails
    item_argv: object = None      # callable(item) -> argv
    env: dict = field(default_factory=dict)
    optional: bool = False        # failure is a warning, not a failed step


BREW = "/home/linuxbrew/.linuxbrew/bin/brew"
# None of these may remove an installed package to make room: apt-get -y would (so
# --no-remove makes it abort instead), zypper's non-interactive solver could force a
# resolution, dnf only removes with --allowerasing (never passed), pacman answers
# "remove conflicting package?" with its default No under --noconfirm. A refused
# batch falls back to one-by-one installs, which isolates the culprit.
INSTALL_FLAGS = {
    "apt": ["apt-get", "install", "-y", "--no-remove"], "dnf": ["dnf", "install", "-y"],
    "zypper": ["zypper", "--non-interactive", "install", "--no-force-resolution"], "apk": ["apk", "add"],
}


def plan(manifest, family, pm, bundle_dir=None, update_first=True, orig_home=None):
    sel = [p for p in manifest.packages if p.selected and p.via not in ("unavailable", "")]
    by = {}
    for p in sel:
        by.setdefault(p.via.rstrip("?"), []).append(p.name)
    steps = []
    user = pwd.getpwuid(os.getuid()).pw_name

    if pm == "pacman":
        repo = by.get("repo", [])
        if update_first or repo:
            steps.append(Step(f"Install {len(repo)} repo packages" + (" (with system update)" if update_first else ""),
                              ["pacman", "-S" + ("yu" if update_first else ""), "--needed", "--noconfirm"] + repo,
                              root=True, items=repo,
                              item_argv=lambda n: ["pacman", "-S", "--needed", "--noconfirm", n]))
        if by.get("aur") or by.get("snap"):
            steps.append(Step("Make sure paru (AUR helper) is installed", ["sh", "-c", ENSURE_PARU]))
        aur = by.get("aur", [])
        if aur:
            paru = ["paru", "-S", "--needed", "--noconfirm", "--skipreview", "--sudoflags", "-A"]
            steps.append(Step(f"Install {len(aur)} AUR packages", paru + aur, items=aur,
                              item_argv=lambda n, paru=paru: paru + [n]))
    elif pm in INSTALL_FLAGS and by.get("repo"):
        repo = by["repo"]
        if pm == "apt":
            steps.append(Step("Refresh apt", ["apt-get", "update"], root=True))
        steps.append(Step(f"Install {len(repo)} packages", INSTALL_FLAGS[pm] + repo, root=True, items=repo,
                          item_argv=lambda n: INSTALL_FLAGS[pm] + [n]))

    if by.get("brew"):
        steps.append(Step("Make sure Homebrew is installed", ["bash", "-c", ENSURE_BREW]))
        steps.append(Step(f"Install {len(by['brew'])} Homebrew packages", [BREW, "install"] + by["brew"],
                          items=by["brew"], item_argv=lambda n: [BREW, "install", n]))
    if by.get("flatpak"):
        fp_pkg = {"pacman": ["pacman", "-S", "--needed", "--noconfirm", "flatpak"]}.get(
            pm, INSTALL_FLAGS.get(pm, ["true"]) + ["flatpak"])
        steps.append(Step("Make sure flatpak is installed", fp_pkg, root=True))
        steps.append(Step("Add the Flathub remote", ["flatpak", "remote-add", "--if-not-exists", "flathub",
                                                     "https://dl.flathub.org/repo/flathub.flatpakrepo"], root=True))
        steps.append(Step(f"Install {len(by['flatpak'])} flatpaks",
                          ["flatpak", "install", "-y", "--noninteractive", "flathub"] + by["flatpak"], root=True,
                          items=by["flatpak"],
                          item_argv=lambda n: ["flatpak", "install", "-y", "--noninteractive", "flathub", n]))
    if by.get("snap"):
        snapd = (["paru", "-S", "--needed", "--noconfirm", "--skipreview", "--sudoflags", "-A", "snapd"]
                 if pm == "pacman" else INSTALL_FLAGS.get(pm, ["true"]) + ["snapd"])
        steps.append(Step("Make sure snapd is installed", snapd, root=(pm != "pacman")))
        steps.append(Step("Enable snapd", ["sh", "-c", "systemctl enable --now snapd.socket && "
                                                       "{ [ -e /snap ] || ln -s /var/lib/snapd/snap /snap; }"],
                          root=True))
        for n in by["snap"]:
            steps.append(Step(f"Install snap {n}", ["snap", "install", n], root=True))
    if by.get("pipx"):
        pipx_pkg = {"pacman": "python-pipx", "apt": "pipx", "dnf": "pipx"}.get(pm, "pipx")
        steps.append(Step("Make sure pipx is installed",
                          (["pacman", "-S", "--needed", "--noconfirm"] if pm == "pacman" else INSTALL_FLAGS.get(pm, ["true"]))
                          + [pipx_pkg], root=True))
        for n in by["pipx"]:
            steps.append(Step(f"pipx install {n}", ["pipx", "install", n]))
    if by.get("pip"):
        pip_pkg = {"pacman": "python-pip", "apt": "python3-pip", "dnf": "python3-pip"}.get(pm, "python3-pip")
        steps.append(Step("Make sure pip is installed",
                          (["pacman", "-S", "--needed", "--noconfirm"] if pm == "pacman" else INSTALL_FLAGS.get(pm, ["true"]))
                          + [pip_pkg], root=True))
        pip = ["python3", "-m", "pip", "install", "--user", "--break-system-packages"]
        steps.append(Step(f"pip install --user {len(by['pip'])} packages", pip + by["pip"], items=by["pip"],
                          item_argv=lambda n, pip=pip: pip + [n]))

    files = [f for f in manifest.files if f.selected]
    if files and bundle_dir:
        steps.append(Step(f"Restore {len(files)} files", ["restore-files"], items=files))
        if any(expand(f.dest).startswith(str(HOME / ".local/bin")) for f in files):
            steps.append(Step("Put ~/.local/bin on PATH", ["path-setup"], optional=True))
        if any(f.dest == GH_TOKEN_DEST for f in files):
            steps.append(Step("Sign the GitHub CLI in (bundled token)", ["gh-login"], optional=True))
        if any(f.dest == KMAIL_WALLET_DEST for f in files):
            steps.append(Step("Put KMail's passwords into KWallet", ["kmail-wallet"], optional=True))

    # a service whose package was captured but not chosen won't exist here: leave it out
    unchosen = {p.name for p in manifest.packages} - {p.name for p in sel}
    svcs = [s for s in manifest.services if s.selected and s.package not in unchosen]
    sys_units = [s.unit for s in svcs if s.scope == "system"]
    usr_units = [s.unit for s in svcs if s.scope == "user"]
    # each unit is checked on the spot (exists here? conflicts? job already done by another?)
    if sys_units:
        steps.append(Step(f"Enable {len(sys_units)} system services", ["enable-units", "system"],
                          root=True, items=sys_units, optional=True))
    if usr_units:
        steps.append(Step(f"Enable {len(usr_units)} user services", ["enable-units", "user"],
                          items=usr_units, optional=True))
    groups = [g for g in manifest.groups if manifest.group_selected.get(g, True)]
    if groups:
        steps.append(Step(f"Add {user} to groups: {', '.join(groups)}", ["add-groups"], items=groups,
                          optional=True))
    shell = manifest.header.get("ORIG_SHELL", "")
    if re.match(r"^/[\w/.+-]+$", shell or "") and shell != pwd.getpwuid(os.getuid()).pw_shell:
        steps.append(Step(f"Make {shell} your login shell again", ["login-shell", shell], root=True,
                          optional=True))
    for s in steps:
        s.env.setdefault("GRABBIT_ORIG_HOME", orig_home or manifest.header.get("ORIG_HOME", str(HOME)))
    return steps


ENSURE_PARU = r"""
command -v paru >/dev/null && exit 0
if pacman -Si paru >/dev/null 2>&1; then exec sudo -A pacman -S --needed --noconfirm paru; fi
sudo -A pacman -S --needed --noconfirm base-devel git || exit 1
d=$(mktemp -d) && git clone --depth 1 https://aur.archlinux.org/paru-bin.git "$d/paru" \
  && cd "$d/paru" && makepkg --noconfirm && sudo -A pacman -U --noconfirm ./*.pkg.tar.* ; rc=$?
rm -rf "$d"; exit $rc
"""
ENSURE_BREW = r"""
[ -x /home/linuxbrew/.linuxbrew/bin/brew ] && exit 0
command -v git >/dev/null && command -v curl >/dev/null || { echo "git and curl are needed"; exit 1; }
NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" || exit 1
line='eval "$(/home/linuxbrew/.linuxbrew/bin/brew shellenv)"'
for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
  [ -f "$rc" ] && ! grep -qF "$line" "$rc" && printf '\n%s\n' "$line" >> "$rc"
done
if command -v fish >/dev/null; then
  mkdir -p "$HOME/.config/fish/conf.d"
  echo '/home/linuxbrew/.linuxbrew/bin/brew shellenv | source' > "$HOME/.config/fish/conf.d/linuxbrew.fish"
fi
exit 0
"""
PATH_SETUP = r"""
case ":$PATH:" in *":$HOME/.local/bin:"*) echo "already on PATH"; exit 0;; esac
line='export PATH="$HOME/.local/bin:$PATH"'
for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
  [ -f "$rc" ] && ! grep -qF '.local/bin' "$rc" && printf '\n%s\n' "$line" >> "$rc" && echo "added to $rc"
done
if command -v fish >/dev/null; then
  mkdir -p "$HOME/.config/fish/conf.d"
  echo 'fish_add_path -g $HOME/.local/bin' > "$HOME/.config/fish/conf.d/local-bin.fish"; echo "added for fish"
fi
exit 0
"""


# ──────────────────────────────────────────────────────────────── sudo ───
class Sudo:
    """Holds the password for one run and hands it to sudo through SUDO_ASKPASS.

    The password lives only in this process and in the environment of the
    children it starts; the askpass helper is a 0700 script in $XDG_RUNTIME_DIR
    that prints it from that environment. Removed on close()."""

    def __init__(self):
        base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
        fd, self.askpass = tempfile.mkstemp(prefix="grabbit-askpass-", dir=base)
        with os.fdopen(fd, "w") as f:
            f.write('#!/bin/sh\nprintf \'%s\\n\' "$GRABBIT_ASKPASS_PW"\n')
        os.chmod(self.askpass, 0o700)
        self.password = None

    def env(self, extra=None):
        env = dict(os.environ)
        env.update(extra or {})
        env["SUDO_ASKPASS"] = self.askpass
        if self.password is not None:
            env["GRABBIT_ASKPASS_PW"] = self.password
        env.pop("SUDO_PROMPT", None)
        return env

    def check(self, password):
        """-> (ok, message). The password is verified with unix_chkpwd first: a
        typo there is not a PAM failure, whereas sudo -A retries a wrong
        password three times and trips faillock (a 10-minute lockout on Arch).
        Only a password that passed goes to sudo, which then succeeds first try."""
        user = pwd.getpwuid(os.getuid()).pw_name
        chk = shutil.which("unix_chkpwd") or "/usr/bin/unix_chkpwd"
        if os.access(chk, os.X_OK):
            r = subprocess.run([chk, user, "nullok"], input=(password + "\0").encode(),
                               capture_output=True, check=False)
            if r.returncode != 0:
                return False, "Wrong password — try again."
        self.password = password
        subprocess.run(["sudo", "-k"], check=False)
        r = subprocess.run(["sudo", "-A", "-v"], env=self.env(), capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, check=False)
        if r.returncode != 0:
            self.password = None
            msg = (r.stderr or "").strip().splitlines()
            return False, "sudo refused: " + (msg[-1] if msg else f"exit {r.returncode}")
        return True, ""

    def needs_password(self):
        return subprocess.run(["sudo", "-n", "true"], capture_output=True, check=False).returncode != 0

    def close(self):
        self.password = None
        try:
            os.unlink(self.askpass)
        except OSError:
            pass


# ─────────────────────────────────────────────────────────────── runner ───
class Runner:
    """Runs steps one after another in a thread, streaming output to log()."""

    def __init__(self, steps, sudo, bundle_dir=None, log=print, progress=None, done=None):
        self.steps, self.sudo, self.bundle_dir = steps, sudo, bundle_dir
        self.log, self.progress, self.done = log, progress or (lambda i, n, s: None), done or (lambda r: None)
        self.cancelled = False
        self.results = []   # (label, ok, detail)
        self.proc = None

    def start(self):
        t = threading.Thread(target=self._run, daemon=True)
        t.start()
        return t

    def cancel(self):
        self.cancelled = True

    def _exec(self, argv, root=False, env=None):
        if root:
            argv = ["sudo", "-A"] + argv
        self.log("$ " + " ".join(shlex.quote(a) for a in argv))
        try:
            self.proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL, text=True, bufsize=1,
                                         env=self.sudo.env(env), errors="replace")
        except OSError as e:
            self.log(f"  cannot run: {e}")
            return 127, ""
        tail = []
        for line in self.proc.stdout:
            line = line.rstrip()
            self.log("  " + line)
            tail = (tail + [line])[-40:]
        rc = self.proc.wait()
        return rc, "\n".join(tail)

    def _run(self):
        n = len(self.steps)
        for i, step in enumerate(self.steps):
            if self.cancelled:
                self.results.append((step.label, None, "cancelled"))
                continue
            self.progress(i, n, step.label)
            self.log(f"\n== {step.label}")
            try:
                ok, detail = self._run_step(step)
            except Exception as e:  # keep going with the next step
                ok, detail = False, f"internal error: {e}"
            if not ok and step.optional:
                self.log(f"  (optional step had problems: {detail})")
            self.results.append((step.label, ok, detail))
        self.progress(n, n, "Finished")
        self.done(self.results)

    def _run_step(self, step):
        kind = step.argv[0]
        if kind == "restore-files":
            return self._restore_files(step.items)
        if kind == "path-setup":
            rc, _ = self._exec(["sh", "-c", PATH_SETUP])
            return rc == 0, ""
        if kind == "enable-units":
            scope = step.argv[1]
            failed, skipped = [], []
            for unit in step.items:
                if self.cancelled:
                    break
                why = unit_skip_reason(unit, scope)
                if why:
                    self.log(f"  skip {unit}: {why}")
                    skipped.append(f"{unit} ({why})")
                    continue
                argv = ["systemctl"] + (["--user"] if scope == "user" else []) + ["enable", "--now", unit]
                if self._exec(argv, root=scope == "system")[0] != 0:
                    failed.append(unit)
            detail = "; ".join(([f"failed: {', '.join(failed)}"] if failed else [])
                               + ([f"skipped: {', '.join(skipped)}"] if skipped else []))
            return not failed, detail
        if kind == "login-shell":
            shell, user = step.argv[1], pwd.getpwuid(os.getuid()).pw_name
            listed = Path("/etc/shells").read_text().split() if Path("/etc/shells").is_file() else []
            if not os.access(shell, os.X_OK) or shell not in listed:
                return False, f"{shell} isn't an installed login shell here"
            rc, _ = self._exec(["usermod", "-s", shell, user], root=True)
            if rc == 0:
                self.log(f"  {user}'s login shell is now {shell} (applies to new terminals/logins)")
            return rc == 0, ""
        if kind == "kmail-wallet":
            return self._kmail_wallet()
        if kind == "gh-login":
            token = expand(GH_TOKEN_DEST)
            if not os.path.isfile(token):
                return False, "no token restored"
            if not have("gh"):
                return False, f"gh is not installed; token left in {token} (gh auth login --with-token < it)"
            # the keyring when there is one, else gh's own plain-text fallback
            rc, _ = self._exec(["sh", "-c", 'gh auth login --hostname github.com --with-token < "$1" '
                                            '&& gh auth status --hostname github.com', "sh", token])
            if rc != 0:
                return False, f"gh auth login failed; token left in {token}"
            os.unlink(token)
            return True, ""
        if kind == "add-groups":
            user = pwd.getpwuid(os.getuid()).pw_name
            existing = {g.gr_name for g in grp.getgrall()}
            have_g = [g for g in step.items if g in existing]
            missing = [g for g in step.items if g not in existing]
            for g in missing:
                self.log(f"  group {g} does not exist here (its package may not be installed) — skipped")
            if not have_g:
                return not missing, "no groups to add"
            rc, _ = self._exec(["usermod", "-aG", ",".join(have_g), user], root=True)
            if rc == 0:
                self.log("  log out and back in for new group memberships to apply")
            return rc == 0 and not missing, ", ".join(f"missing: {g}" for g in missing)

        rc, tail = self._exec(step.argv, root=step.root, env=step.env)
        if rc == 0:
            return True, ""
        if not step.items or not step.item_argv or self.cancelled:
            return False, f"exit {rc}"
        # Batch failed: pull out names pacman couldn't find, then retry one at a time.
        self.log(f"  batch failed (exit {rc}); retrying one by one to isolate the problem")
        failed = []
        for item in step.items:
            if self.cancelled:
                break
            rc, _ = self._exec(step.item_argv(item), root=step.root, env=step.env)
            if rc != 0:
                failed.append(item)
        return not failed, ("failed: " + ", ".join(failed)) if failed else "installed one by one"

    def _kmail_wallet(self):
        """Write the bundled KMail passwords straight into kdewallet (KWallet asks to
        create or unlock it if needed), then restart Akonadi so the accounts log in."""
        path = expand(KMAIL_WALLET_DEST)
        if not os.path.isfile(path):
            return False, "no KMail passwords restored"
        if not have("kwallet-query"):
            return False, f"kwallet-query is missing (install kwallet); passwords left in {path}"
        try:
            entries = json.loads(Path(path).read_text())["entries"]
        except (OSError, ValueError, KeyError) as e:
            return False, f"unreadable {path}: {e}"
        if have("akonadictl"):
            self._exec(["akonadictl", "stop"])
        failed = []
        for folder, key, pw in entries:
            r = subprocess.run(["kwallet-query", "-w", key, "-f", folder, "kdewallet"], input=pw, text=True,
                               capture_output=True, check=False)
            self.log(f"  {folder}/{key}: {'stored' if r.returncode == 0 else 'FAILED ' + r.stderr.strip()}")
            if r.returncode != 0:
                failed.append(f"{folder}/{key}")
        if have("akonadictl"):
            self._exec(["akonadictl", "start"])
        if failed:
            return False, f"not stored: {', '.join(failed)}; passwords left in {path}"
        os.unlink(path)
        return True, ""

    def _restore_files(self, files):
        if not self.bundle_dir:
            return False, "no bundle payload"
        orig_home = self.steps[0].env.get("GRABBIT_ORIG_HOME", str(HOME)) if self.steps else str(HOME)
        failed, restored = [], []
        for f in sorted(files, key=lambda f: f.kind == "link"):   # targets before links
            dest = expand(f.dest)
            system = not dest.startswith(str(HOME) + "/")
            try:
                if f.kind == "link":
                    target = expand(f.target)
                    argv = ["ln", "-sfn", target, dest]
                    mk = ["mkdir", "-p", os.path.dirname(dest)]
                    if system:
                        ok = self._exec(mk, root=True)[0] == 0 and self._exec(argv, root=True)[0] == 0
                    else:
                        os.makedirs(os.path.dirname(dest), exist_ok=True)
                        if os.path.lexists(dest):
                            os.unlink(dest)
                        os.symlink(target, dest)
                        ok = True
                    self.log(f"  link {f.dest} -> {f.target}")
                else:
                    src = os.path.join(self.bundle_dir, "files", f.key)
                    if system:
                        ok = (self._exec(["mkdir", "-p", os.path.dirname(dest)], root=True)[0] == 0 and
                              self._exec(["cp", "-a", "--no-preserve=ownership", "-T", src, dest], root=True)[0] == 0)
                    else:
                        os.makedirs(os.path.dirname(dest), exist_ok=True)
                        if f.dest not in MERGE_ON_RESTORE:
                            keep_original(dest, src)
                        if f.kind == "dir":
                            shutil.copytree(src, dest, symlinks=True, dirs_exist_ok=True)
                        elif f.dest in MERGE_ON_RESTORE and os.path.isfile(dest):
                            # old machine's history first, then whatever this one already has
                            mine = Path(dest).read_bytes()
                            merged = Path(src).read_bytes()
                            Path(dest).write_bytes(merged + (b"" if merged.endswith(b"\n") else b"\n") + mine)
                        else:
                            shutil.copy2(src, dest)
                            os.chmod(dest, f.mode)
                        restored.append(f.dest)
                        ok = True
                    self.log(f"  {f.kind} {f.dest} ({human(f.size)})")
            except OSError as e:
                self.log(f"  ! {f.dest}: {e}")
                ok = False
            if not ok:
                failed.append(f.dest)
        try:
            rewrite_home(restored, orig_home, log=self.log)
        except OSError as e:
            self.log(f"  ! rewriting {orig_home} paths: {e}")
            failed.append("(home path rewrite)")
        return not failed, ("failed: " + ", ".join(failed)) if failed else ""


# ─────────────────────────────────────────────────────────────────── CLI ───
def _cli(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="grabbit pack", description="Build a self-installing migration bundle")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pack", help="capture this system into a runnable .grab.run bundle")
    p.add_argument("out")
    p.add_argument("--no-files", action="store_true", help="packages/services/groups only")
    p.add_argument("--skip-file", action="append", default=[], metavar="GLOB",
                   help="leave out loose files whose path matches (repeatable)")
    p.add_argument("--max-file-mb", type=float, default=0, help="leave out loose files larger than this")
    p.add_argument("--no-accounts", action="store_true",
                   help="leave out sign-ins (git/GitHub CLI/SSH keys, Claude Code)")
    s = sub.add_parser("save", help="write a .grab v2 manifest (no files embedded)")
    s.add_argument("out")
    i = sub.add_parser("info", help="summarize a .grab or .grab.run")
    i.add_argument("path")
    a = ap.parse_args(argv)

    if a.cmd == "info":
        m = load_path(a.path)
        cats = {}
        for pk in m.packages:
            cats[pk.category] = cats.get(pk.category, 0) + 1
        print(f"{len(m.packages)} packages {cats}; {len(m.services)} services; groups {m.groups}; "
              f"{len(m.files)} files ({human(sum(f.size for f in m.files))})")
        return 0
    m = capture(accounts=a.cmd == "pack" and not a.no_accounts)
    if a.cmd == "save":
        Path(a.out).write_text(dumps(m))
        print(f"wrote {a.out}")
        return 0
    keep = []
    for f in m.files:
        path = expand(f.dest)
        if any(fnmatch.fnmatch(path, g) or fnmatch.fnmatch(f.dest, g) for g in a.skip_file):
            print(f"  - skipping {f.dest} (--skip-file)")
            continue
        if a.max_file_mb and f.size > a.max_file_mb * 1024 * 1024:
            print(f"  - skipping {f.dest} ({human(f.size)} > --max-file-mb)")
            continue
        keep.append(f)
    # Drop links whose target was skipped (unless the target is outside the bundle anyway)
    dests = {f.dest for f in keep}
    m.files = [f for f in keep if f.kind != "link" or f.target in dests
               or not any(f.target == x.dest for x in m.files)]
    print(f"Embedding {len([f for f in m.files if f.kind != 'link'])} files "
          f"({human(sum(f.size for f in m.files))})")
    out = build_bundle(m, a.out, include_files=not a.no_files)
    print(f"\nBundle written: {out} ({human(out.stat().st_size)})")
    print("On the new machine: mark it executable (Properties → Permissions) and double-click it,")
    print(f"or run:  bash {out.name}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
