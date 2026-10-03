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
CATEGORIES = ("user", "de", "system", "distro")
# Preselected on restore. system = the old machine's kernel/bootloader/base (the new
# install has its own); distro = the old distro's own packages (branding, tools).
DEFAULT_SELECTED = {"user": True, "de": True, "system": False, "distro": False}


# ─────────────────────────────────────────────────────────────── model ───
@dataclass
class Package:
    name: str
    src: str
    category: str = "user"
    selected: bool = True
    via: str = ""          # resolved on the target: repo|aur|brew|flatpak|...|unavailable

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
            return "system"
        if any(fnmatch.fnmatch(name, p) for p in DOWNSTREAM_PATTERNS.get(self.distro_id, [])) \
                or name == self.distro_id:
            return "distro"
        if name in self.de or any(fnmatch.fnmatch(name, p) for p in DE_PATTERNS):
            return "de"
        if name in self.downstream:
            return "distro"
        return "user"


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

    seen, pkgs = set(), []
    for name, src in found:
        if (name, src) in seen or not NAME_RE.match(name):
            continue
        seen.add((name, src))
        pkgs.append(Package(name, src, classifier.category(name, src)))
    return pkgs


def pacman_owner(path):
    out = run(["pacman", "-Qoq", str(path)]).strip()
    return out.splitlines()[0] if out else ""


def capture_services(pm, package_names):
    """Enabled units whose unit file belongs to one of the captured packages."""
    services = []
    for scope, cmd in (("system", ["systemctl"]), ("user", ["systemctl", "--user"])):
        for l in lines(cmd + ["list-unit-files", "--state=enabled", "--no-legend",
                              "--type=service,socket,timer,path"]):
            unit = l.split()[0]
            if "@" in unit and unit.endswith("@.service"):
                continue
            path = run(cmd + ["show", "-P", "FragmentPath", unit]).strip()
            owner = pacman_owner(path) if (pm == "pacman" and path) else ""
            if owner and owner in package_names:
                services.append(Service(unit, scope, owner))
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
# Signed-in tools whose login lives in $HOME: git + GitHub CLI + SSH keys, and
# Claude Code. All of it is a credential or carries one, so a bundle holding
# any of it is written 0700. Their keys start with ACCOUNT_KEY.
ACCOUNT_KEY = "acct_"
ACCOUNT_PATHS = (
    "~/.gitconfig", "~/.git-credentials", "~/.config/git", "~/.config/gh", "~/.ssh",
    "~/.claude.json", "~/.claude/.credentials.json", "~/.claude/settings.json",
    "~/.claude/settings.local.json", "~/.claude/CLAUDE.md", "~/.claude/keybindings.json",
    "~/.claude/agents", "~/.claude/commands", "~/.claude/skills", "~/.claude/plugins",
    "~/.claude/projects", "~/.claude/history.jsonl",
)
# gh keeps its token in the system keyring, not in ~/.config/gh: it is read with
# `gh auth token` when the bundle is written and signed back in on restore.
GH_TOKEN_DEST = "~/.config/grabbit/gh-token"
# Restored text files that name the old $HOME by absolute path (rewritten to the new one).
REWRITE_HOME = ("~/.gitconfig", "~/.ssh/config", "~/.claude.json", "~/.claude/settings.json",
                "~/.claude/settings.local.json", "~/.claude/history.jsonl", "~/.claude/plugins/*.json",
                "~/.claude/projects/*/memory/*.md", "~/.local/share/rack/*.tsv")


def gh_token():
    return run(["gh", "auth", "token", "--hostname", "github.com"]).strip() if have("gh") else ""


def capture_accounts():
    out = []
    for dest in ACCOUNT_PATHS:
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
    return out


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
    services = capture_services(pm, names)
    log(f"Enabled services from those packages: {len(services)}")
    groups = capture_groups()
    log(f"Groups: {', '.join(groups) or 'none'}")
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
            cats[key.strip()] = cat.strip() if cat.strip() in CATEGORIES else "user"
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
    """Names that exist on the AUR (batched RPC, 150 per request)."""
    found = set()
    names = sorted(set(names))
    for i in range(0, len(names), 150):
        chunk = names[i:i + 150]
        url = "https://aur.archlinux.org/rpc/v5/info?" + urllib.parse.urlencode([("arg[]", n) for n in chunk])
        try:
            found.update(x["Name"] for x in json.loads(_http_get(url)).get("results", []))
        except Exception as e:  # network down: report and treat as unknown
            log(f"AUR lookup failed: {e}")
            return None
    return found


def resolve(manifest, family, pm, log=print):
    """Set Package.via for this machine. Unavailable ones are deselected."""
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
        aur = aur_lookup(need_aur, log) if need_aur else set()
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
    return manifest


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
INSTALL_FLAGS = {
    "apt": ["apt-get", "install", "-y"], "dnf": ["dnf", "install", "-y"],
    "zypper": ["zypper", "--non-interactive", "install"], "apk": ["apk", "add"],
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

    # a service whose package was captured but not chosen won't exist here: leave it out
    unchosen = {p.name for p in manifest.packages} - {p.name for p in sel}
    svcs = [s for s in manifest.services if s.selected and s.package not in unchosen]
    sys_units = [s.unit for s in svcs if s.scope == "system"]
    usr_units = [s.unit for s in svcs if s.scope == "user"]
    if sys_units:
        steps.append(Step(f"Enable {len(sys_units)} system services", ["systemctl", "enable", "--now"] + sys_units,
                          root=True, items=sys_units, item_argv=lambda u: ["systemctl", "enable", "--now", u],
                          optional=True))
    if usr_units:
        steps.append(Step(f"Enable {len(usr_units)} user services",
                          ["systemctl", "--user", "enable", "--now"] + usr_units, items=usr_units,
                          item_argv=lambda u: ["systemctl", "--user", "enable", "--now", u], optional=True))
    groups = [g for g in manifest.groups if manifest.group_selected.get(g, True)]
    if groups:
        steps.append(Step(f"Add {user} to groups: {', '.join(groups)}", ["add-groups"], items=groups,
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
                        if f.kind == "dir":
                            shutil.copytree(src, dest, symlinks=True, dirs_exist_ok=True)
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
