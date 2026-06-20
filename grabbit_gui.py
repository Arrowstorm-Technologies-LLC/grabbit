#!/usr/bin/env python3
"""
grabbit-gui — GUI variant of grabbit for manual auditing of grab files.

Features:
- Scan current system for installed packages (like `grabbit save`).
- Open and audit existing .grab files.
- Apply filters (by name, by source type) instead of CLI -x.
- Select/deselect individual or groups of packages.
- Save audited selection as a new grab file.
- Preview and optionally execute load for selected packages (with cross-distro transposition).

Usage:
    ./grabbit-gui                 # using the launcher
    ./grabbit-gui myfile.grab
    python3 grabbit_gui.py

For desktop menu: install grabbit-gui.desktop to ~/.local/share/applications/
"""

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, filedialog, messagebox, scrolledtext
import fnmatch
import os
import sys
import subprocess
import platform
import re
from datetime import datetime
from pathlib import Path

# Optional drag and drop support
try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    HAS_DND = True
except ImportError:
    HAS_DND = False
    TkinterDnD = None

# Known sources from grabbit
KNOWN_SOURCES = ["apt", "pacman", "aur", "brew", "snap", "flatpak", "zypper", "dnf", "apk"]
EXTERNAL_SOURCES = frozenset({"aur", "brew", "snap", "flatpak"})

class GrabbitGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("grabbit-gui — Package Auditor")
        self.root.geometry("1000x700")

        # Data: list of dicts { 'name': str, 'src': str, 'selected': bool }
        self.packages = []
        self.current_file = None
        self.orig_distro = "unknown"
        self.orig_family = "unknown"
        self.orig_pm = "unknown"

        # Last directory for file dialogs + persistence
        self.config_dir = os.path.expanduser("~/.config/grabbit")
        os.makedirs(self.config_dir, exist_ok=True)
        self.last_dir_file = os.path.join(self.config_dir, "last_dir.txt")
        self.last_directory = self._load_last_directory()

        self._setup_ui()
        self._setup_drag_and_drop()
        self._detect_current_distro()

    def _setup_ui(self):
        # Top menu bar
        menubar = tk.Menu(self.root)
        filemenu = tk.Menu(menubar, tearoff=0)
        filemenu.add_command(label="Scan Current System (Prepare Save)", command=self.scan_system)
        filemenu.add_command(label="Open Grab File...", command=self.open_grab_file)
        filemenu.add_separator()
        filemenu.add_command(label="Save", command=self.save_grab_file)
        filemenu.add_command(label="Save As...", command=self.save_as_grab_file)
        filemenu.add_separator()
        filemenu.add_command(label="Exit", command=self.root.quit)
        menubar.add_cascade(label="File", menu=filemenu)

        actionmenu = tk.Menu(menubar, tearoff=0)
        actionmenu.add_command(label="Select All Visible", command=self.select_all_visible)
        actionmenu.add_command(label="Deselect All Visible", command=self.deselect_all_visible)
        actionmenu.add_command(label="Invert Selection", command=self.invert_selection)
        actionmenu.add_separator()
        actionmenu.add_command(label="Preview Load Commands", command=self.preview_load)
        actionmenu.add_command(label="Load Selected Packages", command=self.load_selected)
        menubar.add_cascade(label="Actions", menu=actionmenu)

        self.root.config(menu=menubar)

        # Main frame
        main_frame = ttk.Frame(self.root, padding=10)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Info bar
        self.info_var = tk.StringVar(value="No file loaded. Scan system or open a grab file to begin.")
        info_label = ttk.Label(main_frame, textvariable=self.info_var, relief=tk.SUNKEN, padding=5)
        info_label.pack(fill=tk.X, pady=(0, 5))

        # File navigation bar with explorer-style dialogs
        file_bar = ttk.Frame(main_frame)
        file_bar.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(file_bar, text="Grab File:").pack(side=tk.LEFT, padx=(0, 5))
        self.file_path_var = tk.StringVar(value="(no file loaded)")
        file_path_label = ttk.Label(file_bar, textvariable=self.file_path_var, relief=tk.SUNKEN, width=60)
        file_path_label.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)

        ttk.Button(file_bar, text="Open...", command=self.open_grab_file).pack(side=tk.LEFT, padx=2)
        ttk.Button(file_bar, text="Save", command=self.save_grab_file).pack(side=tk.LEFT, padx=2)
        ttk.Button(file_bar, text="Save As...", command=self.save_as_grab_file).pack(side=tk.LEFT, padx=2)

        # Drop zone for drag & drop
        drop_frame = ttk.Frame(main_frame)
        drop_frame.pack(fill=tk.X, pady=(0, 5))
        self.drop_label = ttk.Label(drop_frame, text="📥 Drop a .grab file here to open", 
                                    relief=tk.RAISED, padding=6, anchor="center")
        self.drop_label.pack(fill=tk.X)
        self.drop_label.bind("<Button-1>", lambda e: self.open_grab_file())

        # Filter frame
        filter_frame = ttk.LabelFrame(main_frame, text="Filters", padding=10)
        filter_frame.pack(fill=tk.X, pady=(0, 10))

        # Search
        ttk.Label(filter_frame, text="Search name:").grid(row=0, column=0, sticky=tk.W)
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *args: self.apply_filters())
        search_entry = ttk.Entry(filter_frame, textvariable=self.search_var, width=30)
        search_entry.grid(row=0, column=1, padx=5)

        # Source filters
        ttk.Label(filter_frame, text="Sources:").grid(row=0, column=2, sticky=tk.W, padx=(20, 5))
        self.source_vars = {}
        col = 3
        for src in KNOWN_SOURCES:
            var = tk.BooleanVar(value=True)
            var.trace_add("write", lambda *args: self.apply_filters())
            self.source_vars[src] = var
            cb = ttk.Checkbutton(filter_frame, text=src, variable=var)
            cb.grid(row=0, column=col, padx=2)
            col += 1

        # Buttons row
        btn_frame = ttk.Frame(filter_frame)
        btn_frame.grid(row=1, column=0, columnspan=10, pady=(10, 0), sticky=tk.W)

        ttk.Button(btn_frame, text="Select All Visible", command=self.select_all_visible).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="Deselect All Visible", command=self.deselect_all_visible).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="Invert Visible", command=self.invert_selection).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="Clear Filters", command=self.clear_filters).pack(side=tk.LEFT, padx=10)

        self.include_system_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            btn_frame,
            text="System Packages",
            variable=self.include_system_var,
            command=self._on_system_packages_toggle,
        ).pack(side=tk.LEFT, padx=10)

        # Package list - Treeview
        list_frame = ttk.Frame(main_frame)
        list_frame.pack(fill=tk.BOTH, expand=True)
        list_frame.grid_rowconfigure(0, weight=1)
        list_frame.grid_columnconfigure(0, weight=1)

        style = ttk.Style(self.root)
        tree_font = tkfont.nametofont("TkDefaultFont")
        # Extra height for Unicode checkbox glyphs (☑/☐) on Linux themes
        rowheight = max(tree_font.metrics("linespace") + 10, 30)
        style.configure("Grabbit.Treeview", rowheight=rowheight)
        style.configure("Grabbit.Treeview.Heading", font=tree_font)

        columns = ("selected", "name", "source")
        self.tree = ttk.Treeview(
            list_frame,
            columns=columns,
            show="headings",
            selectmode="extended",
            style="Grabbit.Treeview",
        )

        self.tree.heading("selected", text="Select", anchor=tk.CENTER)
        self.tree.heading("name", text="Package Name")
        self.tree.heading("source", text="Source")

        # stretch=False lets Select/Source resize independently; name absorbs extra width
        self.tree.column("selected", width=72, minwidth=56, anchor=tk.CENTER, stretch=False)
        self.tree.column("name", width=400, minwidth=160, stretch=True)
        self.tree.column("source", width=140, minwidth=80, stretch=False)

        vsb = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(list_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

        self.tree.bind("<Button-1>", self.on_tree_click)

        # Status bar
        self.status_var = tk.StringVar(value="Ready")
        status = ttk.Label(main_frame, textvariable=self.status_var, relief=tk.SUNKEN)
        status.pack(fill=tk.X, pady=(5, 0))

        # Bottom action buttons
        bottom_frame = ttk.Frame(main_frame)
        bottom_frame.pack(fill=tk.X, pady=(10, 0))

        ttk.Button(bottom_frame, text="Scan Current System", command=self.scan_system).pack(side=tk.LEFT, padx=5)
        ttk.Button(bottom_frame, text="Open Grab File...", command=self.open_grab_file).pack(side=tk.LEFT, padx=5)
        ttk.Button(bottom_frame, text="Save", command=self.save_grab_file).pack(side=tk.LEFT, padx=5)
        ttk.Button(bottom_frame, text="Save As...", command=self.save_as_grab_file).pack(side=tk.LEFT, padx=5)
        ttk.Button(bottom_frame, text="Preview Load Commands", command=self.preview_load).pack(side=tk.LEFT, padx=5)
        ttk.Button(bottom_frame, text="Load Selected Packages", command=self.load_selected).pack(side=tk.LEFT, padx=5)

        # Initial empty tree
        self.refresh_tree()

    def _detect_current_distro(self):
        """Detect current distro for load transposition."""
        self.current_family = "unknown"
        self.current_pm = "unknown"
        self.current_install_cmd = "echo 'Unknown package manager'"

        try:
            with open("/etc/os-release") as f:
                content = f.read().lower()
                if any(x in content for x in ["debian", "ubuntu", "mint", "pop"]):
                    self.current_family = "debian"
                    self.current_pm = "apt"
                    self.current_install_cmd = "sudo apt update && sudo apt install -y"
                elif "arch" in content or "endeavouros" in content or "manjaro" in content:
                    self.current_family = "arch"
                    self.current_pm = "pacman"
                    self.current_install_cmd = "sudo pacman -S --needed --noconfirm"
                elif any(x in content for x in ["fedora", "centos", "rhel", "rocky"]):
                    self.current_family = "fedora"
                    self.current_pm = "dnf"
                    self.current_install_cmd = "sudo dnf install -y"
                elif "opensuse" in content or "suse" in content:
                    self.current_family = "suse"
                    self.current_pm = "zypper"
                    self.current_install_cmd = "sudo zypper install -y"
                elif "alpine" in content:
                    self.current_family = "alpine"
                    self.current_pm = "apk"
                    self.current_install_cmd = "sudo apk add"
        except Exception:
            pass

        # Fallback by command presence
        if self.current_pm == "unknown":
            if self._command_exists("apt"):
                self.current_family = "debian"
                self.current_pm = "apt"
                self.current_install_cmd = "sudo apt update && sudo apt install -y"
            elif self._command_exists("pacman"):
                self.current_family = "arch"
                self.current_pm = "pacman"
                self.current_install_cmd = "sudo pacman -S --needed --noconfirm"
            elif self._command_exists("dnf"):
                self.current_family = "fedora"
                self.current_pm = "dnf"
                self.current_install_cmd = "sudo dnf install -y"
            elif self._command_exists("zypper"):
                self.current_family = "suse"
                self.current_pm = "zypper"
                self.current_install_cmd = "sudo zypper install -y"
            elif self._command_exists("apk"):
                self.current_family = "alpine"
                self.current_pm = "apk"
                self.current_install_cmd = "sudo apk add"

    def _command_exists(self, cmd):
        return subprocess.call(["which", cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0

    def _load_last_directory(self):
        if os.path.exists(self.last_dir_file):
            try:
                with open(self.last_dir_file, "r") as f:
                    d = f.read().strip()
                    if d and os.path.isdir(d):
                        return d
            except Exception:
                pass
        return os.path.expanduser("~")

    def _save_last_directory(self, directory):
        if directory and os.path.isdir(directory):
            try:
                with open(self.last_dir_file, "w") as f:
                    f.write(directory)
            except Exception:
                pass

    def _setup_drag_and_drop(self):
        """Setup drag and drop support for .grab files (requires tkinterdnd2 for full functionality)."""
        if HAS_DND and TkinterDnD is not None:
            try:
                self.root.drop_target_register(DND_FILES)
                self.root.dnd_bind('<<Drop>>', self._on_file_drop)
                if hasattr(self, 'drop_label'):
                    self.drop_label.configure(text="📥 Drag & drop a .grab file here to open it")
            except Exception:
                pass
        else:
            if hasattr(self, 'drop_label'):
                self.drop_label.configure(text="📥 Drop support: pip install tkinterdnd2 (then restart)")

    def _on_file_drop(self, event):
        """Handle dropped files."""
        try:
            files = self.root.tk.splitlist(event.data)
            for f in files:
                f = f.strip('{}')  # handle spaces in paths on some platforms
                if f.lower().endswith('.grab') and os.path.isfile(f):
                    self._load_grab_file_from_path(f)
                    return
            messagebox.showinfo("Drag & Drop", "Please drop a valid .grab file.")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to handle dropped file: {e}")

    def _load_grab_file_from_path(self, path):
        """Internal loader used by open dialog and drag-drop."""
        self.packages = []
        header = {}
        in_list = False

        try:
            with open(path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith("# "):
                        if "=" in line:
                            k, v = line[2:].split("=", 1)
                            header[k] = v.strip('"')
                    elif line == "PKG_LIST_START":
                        in_list = True
                    elif line == "PKG_LIST_END":
                        break
                    elif in_list and line.startswith("PKG "):
                        entry = line[4:].strip()
                        if ":" in entry:
                            name, src = entry.split(":", 1)
                            self.packages.append({
                                "name": name.strip(),
                                "src": src.strip(),
                                "selected": True
                            })
        except Exception as e:
            messagebox.showerror("Error", f"Failed to parse grab file:\n{e}")
            return

        self.current_file = path
        self.orig_distro = header.get("ORIG_DISTRO", "unknown")
        self.orig_family = header.get("ORIG_FAMILY", "unknown")
        self.orig_pm = header.get("ORIG_PM", "unknown")

        self.file_path_var.set(path)
        self.info_var.set(f"Loaded: {os.path.basename(path)} | Origin: {self.orig_distro} ({self.orig_family}/{self.orig_pm}) | {len(self.packages)} packages")
        self.apply_filters()
        self.status_var.set("Grab file loaded. Use filters and checkboxes to audit, then save or load selected packages.")

    def _build_base_package_list(self):
        """Mirror grabbit CLI base/system package lists for the current family."""
        family = self.current_family
        if getattr(self, "_base_list_cache_family", None) == family:
            return self._base_list_cache

        base = []

        if family == "debian":
            base = [
                "base-files", "bash", "coreutils", "debianutils", "diffutils", "findutils",
                "grep", "gzip", "hostname", "init-system-helpers", "libc-bin", "login",
                "mount", "ncurses-base", "passwd", "perl-base", "sed", "tar", "util-linux",
            ]
        elif family == "arch":
            try:
                out = subprocess.check_output(
                    ["pacman", "-Qg", "base", "base-devel"],
                    text=True,
                    stderr=subprocess.DEVNULL,
                )
                base.extend(line.split()[1] for line in out.strip().splitlines() if line.strip())
            except (subprocess.CalledProcessError, FileNotFoundError):
                pass
            base.extend([
                "linux", "linux-firmware", "linux-headers", "systemd", "systemd-sysvcompat",
                "pacman", "glibc", "filesystem", "archlinux-keyring", "mkinitcpio", "grub",
            ])
        elif family == "fedora":
            base = [
                "bash", "coreutils", "glibc", "rpm", "systemd", "dnf", "yum",
                "fedora-release", "kernel", "kernel-core",
            ]
        elif family == "suse":
            base = [
                "bash", "coreutils", "glibc", "systemd", "zypper", "suse-release", "kernel-default",
            ]
        elif family == "alpine":
            base = ["alpine-baselayout", "busybox", "apk-tools", "linux-lts", "linux-firmware"]

        config_dir = os.path.expanduser("~/.config/grabbit")
        user_excludes = os.path.join(config_dir, f"base-excludes.{family}")
        if os.path.isfile(user_excludes):
            with open(user_excludes, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        base.append(line)

        self._base_list_cache = list(dict.fromkeys(base))
        self._base_list_cache_family = family
        return self._base_list_cache

    def _package_is_base(self, name):
        family = self.current_family
        if name in self._build_base_package_list():
            return True

        patterns = {
            "arch": [
                "linux", "linux-*", "linux-firmware", "linux-firmware-*", "linux-headers",
                "linux-headers-*", "systemd", "systemd-*", "mkinitcpio", "mkinitcpio-*",
                "grub", "grub-*", "archlinux-*", "pacman", "pacman-*", "glibc", "filesystem",
                "base", "base-*", "bash", "coreutils", "systemd-sysvcompat", "kmod", "hwdata",
                "iana-etc", "tzdata", "licenses", "pciutils", "usbutils", "inetutils", "iputils",
                "ca-certificates", "ca-certificates-*", "openssl", "openssl-*",
            ],
            "debian": [
                "linux-*", "libc6", "systemd", "systemd-*", "dpkg", "dpkg-*", "apt", "apt-*",
                "ubuntu-*", "debian-*", "perl-base", "ncurses-base", "base-files", "base-passwd",
            ],
            "fedora": [
                "kernel", "kernel-*", "systemd", "systemd-*", "glibc", "bash", "coreutils",
                "dnf", "dnf-*", "rpm", "rpm-*", "fedora-release", "fedora-release-*",
            ],
            "suse": [
                "kernel-default", "kernel-*", "systemd", "systemd-*", "glibc", "bash",
                "coreutils", "zypper", "zypper-*",
            ],
            "alpine": [
                "linux-*", "linux-firmware", "linux-firmware-*", "busybox", "alpine-baselayout",
                "apk-tools", "musl", "musl-*",
            ],
        }

        for pattern in patterns.get(family, []):
            if fnmatch.fnmatch(name, pattern):
                return True
        return False

    def _filter_base_packages(self, packages, include_system=False):
        if include_system:
            return packages
        filtered = []
        for pkg in packages:
            if pkg["src"] in EXTERNAL_SOURCES:
                filtered.append(pkg)
                continue
            if not self._package_is_base(pkg["name"]):
                filtered.append(pkg)
        return filtered

    def _on_system_packages_toggle(self):
        """Re-scan with or without distro/system package filtering."""
        self.scan_system()

    def collect_current_packages(self, include_system=False):
        """Replicate grabbit's package collection logic in Python."""
        pkgs = []
        pm = self.current_pm

        try:
            if pm == "apt":
                out = subprocess.check_output(["apt-mark", "showmanual"], text=True, stderr=subprocess.DEVNULL)
                for line in out.strip().splitlines():
                    if line.strip():
                        pkgs.append((line.strip(), "apt"))
            elif pm == "pacman":
                # Explicit
                out = subprocess.check_output(["pacman", "-Qe"], text=True, stderr=subprocess.DEVNULL)
                for line in out.strip().splitlines():
                    pkg = line.split()[0]
                    pkgs.append((pkg, "pacman"))
                # AUR/foreign
                out = subprocess.check_output(["pacman", "-Qem"], text=True, stderr=subprocess.DEVNULL)
                for line in out.strip().splitlines():
                    pkg = line.split()[0]
                    pkgs.append((pkg, "aur"))
            elif pm == "dnf":
                try:
                    out = subprocess.check_output(["dnf", "repoquery", "--userinstalled"], text=True, stderr=subprocess.DEVNULL)
                    for line in out.strip().splitlines():
                        pkg = line.split("-")[0] if "-" in line else line
                        pkgs.append((pkg, "dnf"))
                except:
                    pass
            elif pm == "zypper":
                try:
                    out = subprocess.check_output(["zypper", "packages", "--installed-only"], text=True, stderr=subprocess.DEVNULL)
                    for line in out.strip().splitlines():
                        if line.startswith("i"):
                            parts = line.split()
                            if len(parts) > 4:
                                pkgs.append((parts[4], "zypper"))
                except:
                    pass
            elif pm == "apk":
                try:
                    out = subprocess.check_output(["apk", "info", "-v"], text=True, stderr=subprocess.DEVNULL)
                    for line in out.strip().splitlines():
                        pkg = line.split("-")[0]
                        pkgs.append((pkg, "apk"))
                except:
                    pass
        except subprocess.CalledProcessError:
            pass

        # Universal: Homebrew
        if self._command_exists("brew"):
            try:
                for mode in ["--formula", "--cask"]:
                    out = subprocess.check_output(["brew", "list", mode], text=True, stderr=subprocess.DEVNULL)
                    for line in out.strip().splitlines():
                        if line.strip():
                            pkgs.append((line.strip(), "brew"))
            except:
                pass

        # Snap
        if self._command_exists("snap"):
            try:
                out = subprocess.check_output(["snap", "list"], text=True, stderr=subprocess.DEVNULL)
                for line in out.strip().splitlines()[1:]:  # skip header
                    pkg = line.split()[0]
                    if pkg not in ("core", "snapd"):
                        pkgs.append((pkg, "snap"))
            except:
                pass

        # Flatpak
        if self._command_exists("flatpak"):
            try:
                out = subprocess.check_output(["flatpak", "list", "--app", "--columns=application"], text=True, stderr=subprocess.DEVNULL)
                for line in out.strip().splitlines():
                    if line.strip():
                        pkgs.append((line.strip(), "flatpak"))
            except:
                pass

        # Dedup while preserving order
        seen = set()
        unique = []
        for name, src in pkgs:
            key = (name, src)
            if key not in seen:
                seen.add(key)
                unique.append({"name": name, "src": src, "selected": True})
        return self._filter_base_packages(unique, include_system=include_system)

    def scan_system(self):
        """Scan current system and load into the auditor."""
        include_system = self.include_system_var.get()
        self.packages = self.collect_current_packages(include_system=include_system)
        if not self.packages:
            messagebox.showwarning("Scan", "No packages detected or unsupported package manager.")
            return
        self.current_file = None
        self.file_path_var.set("(scanned from system - not saved yet)")
        self.orig_distro = "current"
        self.orig_family = self.current_family
        self.orig_pm = self.current_pm
        if include_system:
            scope = "all packages (including system)"
        else:
            scope = "user-added packages (system excluded)"
        self.info_var.set(f"Scanned current system: {len(self.packages)} {scope}.")
        self.apply_filters()
        self.status_var.set(f"Loaded {len(self.packages)} packages from system scan. Audit and save desired selection.")

    def open_grab_file(self):
        path = filedialog.askopenfilename(
            title="Open Grab File",
            initialdir=self.last_directory,
            filetypes=[("Grab files", "*.grab"), ("All files", "*.*")]
        )
        if not path:
            return
        self.last_directory = os.path.dirname(path) or self.last_directory
        self._save_last_directory(self.last_directory)

        self._load_grab_file_from_path(path)

    def save_grab_file(self):
        """Save to current file if available, otherwise Save As."""
        if not self.packages:
            messagebox.showinfo("Save", "No packages loaded.")
            return

        selected = [p for p in self.get_filtered_packages() if p.get("selected", True)]
        if not selected:
            messagebox.showinfo("Save", "No packages selected to save.")
            return

        if self.current_file and not self.current_file.startswith("("):
            # Direct save
            self._write_grab_file(self.current_file)
        else:
            self.save_as_grab_file()

    def save_as_grab_file(self):
        if not self.packages:
            messagebox.showinfo("Save As", "No packages loaded.")
            return

        selected = [p for p in self.get_filtered_packages() if p.get("selected", True)]
        if not selected:
            messagebox.showinfo("Save As", "No packages selected to save.")
            return

        path = filedialog.asksaveasfilename(
            title="Save Audited Grab File As",
            initialdir=self.last_directory,
            defaultextension=".grab",
            filetypes=[("Grab files", "*.grab")]
        )
        if not path:
            return

        self.last_directory = os.path.dirname(path) or self.last_directory
        self._save_last_directory(self.last_directory)

        self._write_grab_file(path)

    def _write_grab_file(self, path):
        """Internal: write the current selected packages to path."""
        try:
            selected = [p for p in self.get_filtered_packages() if p.get("selected", True)]
            with open(path, "w") as f:
                f.write("# GRABBIT v1\n")
                f.write(f"# Created: {datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')}\n")
                f.write(f"# ORIG_DISTRO={self.orig_distro}\n")
                f.write(f"# ORIG_FAMILY={self.orig_family}\n")
                f.write(f"# ORIG_PM={self.orig_pm}\n")
                f.write(f'# ORIG_DISTRO_NAME="{self.orig_distro}"\n\n')
                f.write("PKG_LIST_START\n")
                for p in selected:
                    f.write(f"PKG {p['name']}:{p['src']}\n")
                f.write("PKG_LIST_END\n")
            self.current_file = path
            self.file_path_var.set(path)
            messagebox.showinfo("Saved", f"Saved {len(selected)} packages to {path}")
            self.status_var.set(f"Saved audited selection to {path}")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save: {e}")

    def get_filtered_packages(self):
        """Return packages after applying search and source filters."""
        search = self.search_var.get().lower().strip()
        active_sources = {s for s, var in self.source_vars.items() if var.get()}

        filtered = []
        for p in self.packages:
            if search and search not in p["name"].lower():
                continue
            if p["src"] not in active_sources:
                continue
            filtered.append(p)
        return filtered

    def apply_filters(self):
        self.refresh_tree()

    def refresh_tree(self):
        # Clear tree
        for item in self.tree.get_children():
            self.tree.delete(item)

        filtered = self.get_filtered_packages()

        for p in filtered:
            selected_char = "☑" if p.get("selected", True) else "☐"
            self.tree.insert("", "end", values=(
                selected_char,
                p["name"],
                p["src"]
            ), tags=(p["name"], p["src"]))

        self.status_var.set(f"Showing {len(filtered)} / {len(self.packages)} packages")

    def on_tree_click(self, event):
        # Find which column and row
        region = self.tree.identify_region(event.x, event.y)
        if region != "cell":
            return

        column = self.tree.identify_column(event.x)
        item = self.tree.identify_row(event.y)

        if not item:
            return

        # Only toggle on first column (selected)
        if column == "#1":
            values = self.tree.item(item, "values")
            name = values[1]
            src = values[2]

            # Find in packages and toggle
            for p in self.packages:
                if p["name"] == name and p["src"] == src:
                    p["selected"] = not p.get("selected", True)
                    break

            self.refresh_tree()  # rebuild to show new checkbox

    def select_all_visible(self):
        visible = self.get_filtered_packages()
        visible_names = {(p["name"], p["src"]) for p in visible}
        for p in self.packages:
            if (p["name"], p["src"]) in visible_names:
                p["selected"] = True
        self.refresh_tree()

    def deselect_all_visible(self):
        visible = self.get_filtered_packages()
        visible_names = {(p["name"], p["src"]) for p in visible}
        for p in self.packages:
            if (p["name"], p["src"]) in visible_names:
                p["selected"] = False
        self.refresh_tree()

    def invert_selection(self):
        visible = self.get_filtered_packages()
        visible_names = {(p["name"], p["src"]) for p in visible}
        for p in self.packages:
            if (p["name"], p["src"]) in visible_names:
                p["selected"] = not p.get("selected", True)
        self.refresh_tree()

    def clear_filters(self):
        self.search_var.set("")
        for var in self.source_vars.values():
            var.set(True)
        self.apply_filters()

    def get_selected_packages(self):
        return [p for p in self.packages if p.get("selected", True)]

    def preview_load(self):
        selected = self.get_selected_packages()
        if not selected:
            messagebox.showinfo("Preview", "No packages selected.")
            return

        preview = scrolledtext.ScrolledText(self.root, width=90, height=25)
        preview.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        top = tk.Toplevel(self.root)
        top.title("Load Preview - Commands to be executed")
        top.geometry("900x500")

        text = scrolledtext.ScrolledText(top, wrap=tk.WORD)
        text.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        text.insert(tk.END, f"Current system: {self.current_family}/{self.current_pm}\n")
        text.insert(tk.END, f"Original grab file distro: {self.orig_distro} ({self.orig_family}/{self.orig_pm})\n\n")
        text.insert(tk.END, "The following commands will be executed for selected packages:\n\n")

        for p in selected:
            cmd = self._get_install_command(p["name"], p["src"])
            text.insert(tk.END, f"# {p['name']} (from {p['src']})\n{cmd}\n\n")

        text.insert(tk.END, "\nNote: Review carefully before running. Some commands require sudo.")

        def do_load():
            top.destroy()
            self.load_selected()

        btn_frame = ttk.Frame(top)
        btn_frame.pack(fill=tk.X, padx=10, pady=5)
        ttk.Button(btn_frame, text="Run These Commands", command=do_load).pack(side=tk.RIGHT)
        ttk.Button(btn_frame, text="Close", command=top.destroy).pack(side=tk.RIGHT, padx=5)

    def _get_install_command(self, name, src):
        """Simplified version of grabbit's get_install_command with current distro."""
        fam = self.current_family
        cmd_base = self.current_install_cmd

        if src == "apt":
            if fam == "debian":
                return f"sudo apt update && sudo apt install -y {name}"
            else:
                return f"{cmd_base} {name}"
        elif src in ("pacman", "dnf", "zypper", "apk"):
            if fam in ("arch", "fedora", "suse", "alpine"):
                return f"{cmd_base} {name}"
            else:
                return f"{cmd_base} {name}"
        elif src == "aur":
            if fam == "arch":
                return f"paru -S --needed --noconfirm {name}"
            else:
                return f"# Cannot install AUR package '{name}' on non-Arch system"
        elif src == "brew":
            return f"brew install {name}"
        elif src == "snap":
            return f"sudo snap install {name}"
        elif src == "flatpak":
            return f"flatpak install -y flathub {name}"
        else:
            return f"{cmd_base} {name}"

    def load_selected(self):
        selected = self.get_selected_packages()
        if not selected:
            messagebox.showinfo("Load", "No packages selected.")
            return

        if not messagebox.askyesno("Confirm Load", 
                f"Load/Install {len(selected)} selected packages?\n\n"
                "This will run package manager commands (may require sudo privileges)."):
            return

        results = []
        for p in selected:
            cmd = self._get_install_command(p["name"], p["src"])
            if cmd.startswith("#"):
                results.append(f"SKIPPED: {p['name']} - {cmd}")
                continue

            self.status_var.set(f"Installing {p['name']} ...")
            self.root.update()

            try:
                # Run the command. This may prompt for sudo password in terminal if needed.
                # For better UX in GUI, one could use a terminal emulator, but we keep it simple.
                proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=300)
                if proc.returncode == 0:
                    results.append(f"✓ {p['name']}: success")
                else:
                    results.append(f"✗ {p['name']}: {proc.stderr.strip()[:200] or 'failed'}")
            except Exception as e:
                results.append(f"✗ {p['name']}: {str(e)}")

        # Show results
        result_win = tk.Toplevel(self.root)
        result_win.title("Load Results")
        txt = scrolledtext.ScrolledText(result_win, width=80, height=30)
        txt.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        txt.insert(tk.END, "\n".join(results))
        self.status_var.set("Load operation completed. See results window.")

if __name__ == "__main__":
    # Support command line: grabbit-gui /path/to/file.grab
    initial_file = sys.argv[1] if len(sys.argv) > 1 else None

    if HAS_DND and TkinterDnD is not None:
        root = TkinterDnD.Tk()
    else:
        root = tk.Tk()

    app = GrabbitGUI(root)

    # Handle simple CLI flags
    if "--scan" in sys.argv or "-s" in sys.argv:
        app.scan_system()
    elif initial_file and os.path.isfile(initial_file):
        app._load_grab_file_from_path(initial_file)

    root.mainloop()
