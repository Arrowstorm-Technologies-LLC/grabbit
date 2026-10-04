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
import queue
import re
import threading
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import grabbit_core as core  # noqa: E402

# Optional drag and drop support
try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    HAS_DND = True
except ImportError:
    HAS_DND = False
    TkinterDnD = None

# Known sources from grabbit
KNOWN_SOURCES = ["apt", "pacman", "aur", "brew", "snap", "flatpak", "pipx", "pip", "zypper", "dnf", "apk"]
EXTERNAL_SOURCES = frozenset({"aur", "brew", "snap", "flatpak", "pipx", "pip"})
CATEGORY_LABELS = {"user": "yours", "de": "desktop", "system": "system", "distro": "old distro"}

class GrabbitGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("grabbit-gui — Package Auditor")
        self.root.geometry("1200x820")

        # Data: list of dicts { 'name': str, 'src': str, 'selected': bool }
        self.packages = []
        self.current_file = None
        self.orig_distro = "unknown"
        self.orig_family = "unknown"
        self.orig_pm = "unknown"
        # v2 extras: services, groups, loose files, and the unpacked bundle they come from
        self.extras = core.Manifest()
        self.header = {}
        self.bundle_dir = None
        self.is_scan = False
        self._ui_queue = queue.Queue()

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
        filemenu.add_command(label="Export Migration Bundle (.grab.run)...", command=self.export_bundle)
        filemenu.add_separator()
        filemenu.add_command(label="Exit", command=self.root.quit)
        menubar.add_cascade(label="File", menu=filemenu)

        actionmenu = tk.Menu(menubar, tearoff=0)
        actionmenu.add_command(label="Select All Visible", command=self.select_all_visible)
        actionmenu.add_command(label="Deselect All Visible", command=self.deselect_all_visible)
        actionmenu.add_command(label="Invert Selection", command=self.invert_selection)
        actionmenu.add_separator()
        actionmenu.add_command(label="Preview Install Plan", command=self.preview_load)
        actionmenu.add_command(label="Install Selected...", command=self.load_selected)
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

        # Category row (from .grab v2 files; a system scan tags everything "yours")
        cat_frame = ttk.Frame(filter_frame)
        cat_frame.grid(row=1, column=0, columnspan=14, pady=(8, 0), sticky=tk.W)
        ttk.Label(cat_frame, text="Show categories:").pack(side=tk.LEFT, padx=(0, 5))
        self.category_vars = {}
        for cat in core.CATEGORIES:
            var = tk.BooleanVar(value=True)
            var.trace_add("write", lambda *args: self.apply_filters())
            self.category_vars[cat] = var
            ttk.Checkbutton(cat_frame, text=CATEGORY_LABELS[cat], variable=var).pack(side=tk.LEFT, padx=2)

        # Buttons row
        btn_frame = ttk.Frame(filter_frame)
        btn_frame.grid(row=2, column=0, columnspan=14, pady=(10, 0), sticky=tk.W)

        ttk.Button(btn_frame, text="Select All Visible", command=self.select_all_visible).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="Deselect All Visible", command=self.deselect_all_visible).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="Invert Visible", command=self.invert_selection).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="Clear Filters", command=self.clear_filters).pack(side=tk.LEFT, padx=10)

        # Bottom action buttons
        bottom_frame = ttk.Frame(main_frame)
        bottom_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=(10, 0))

        ttk.Button(bottom_frame, text="Scan Current System", command=self.scan_system).pack(side=tk.LEFT, padx=5)
        ttk.Button(bottom_frame, text="Open Grab File...", command=self.open_grab_file).pack(side=tk.LEFT, padx=5)
        ttk.Button(bottom_frame, text="Save", command=self.save_grab_file).pack(side=tk.LEFT, padx=5)
        ttk.Button(bottom_frame, text="Save As...", command=self.save_as_grab_file).pack(side=tk.LEFT, padx=5)
        ttk.Button(bottom_frame, text="Preview Install Plan", command=self.preview_load).pack(side=tk.LEFT, padx=5)
        ttk.Button(bottom_frame, text="Install Selected...", command=self.load_selected).pack(side=tk.LEFT, padx=5)
        ttk.Button(bottom_frame, text="Export Bundle...", command=self.export_bundle).pack(side=tk.LEFT, padx=5)

        # Status bar
        self.status_var = tk.StringVar(value="Ready")
        status = ttk.Label(main_frame, textvariable=self.status_var, relief=tk.SUNKEN)
        status.pack(side=tk.BOTTOM, fill=tk.X, pady=(5, 0))

        # Tabs: packages, loose files, services & groups
        self.notebook = ttk.Notebook(main_frame)
        self.notebook.pack(fill=tk.BOTH, expand=True)
        list_frame = ttk.Frame(self.notebook)
        self.notebook.add(list_frame, text="Packages")
        list_frame.grid_rowconfigure(0, weight=1)
        list_frame.grid_columnconfigure(0, weight=1)

        style = ttk.Style(self.root)
        tree_font = tkfont.nametofont("TkDefaultFont")
        # Extra height for Unicode checkbox glyphs (☑/☐) on Linux themes
        rowheight = max(tree_font.metrics("linespace") + 10, 30)
        style.configure("Grabbit.Treeview", rowheight=rowheight)
        style.configure("Grabbit.Treeview.Heading", font=tree_font)

        columns = ("selected", "name", "source", "category", "via")
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
        self.tree.heading("category", text="Category")
        self.tree.heading("via", text="Will install via")

        # stretch=False lets Select/Source resize independently; name absorbs extra width
        self.tree.column("selected", width=72, minwidth=56, anchor=tk.CENTER, stretch=False)
        self.tree.column("name", width=400, minwidth=160, stretch=True)
        self.tree.column("source", width=90, minwidth=70, stretch=False)
        self.tree.column("category", width=100, minwidth=70, stretch=False)
        self.tree.column("via", width=320, minwidth=80, stretch=False)

        vsb = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(list_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

        self.tree.bind("<Button-1>", self.on_tree_click)

        self._setup_extra_tabs()

        # Initial empty tree
        self.refresh_tree()

    def _parse_os_release(self):
        data = {}
        try:
            with open("/etc/os-release", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    data[key] = value.strip().strip('"')
        except OSError:
            pass
        return data

    def _detect_current_distro(self):
        """Detect current distro for load transposition."""
        self.current_family = "unknown"
        self.current_pm = "unknown"
        self.current_distro_id = "unknown"
        self.current_distro_name = "Unknown"
        self.current_install_cmd = "echo 'Unknown package manager'"

        os_release = self._parse_os_release()
        distro_id = os_release.get("ID", "unknown").lower()
        self.current_distro_id = distro_id
        self.current_distro_name = os_release.get("NAME", "Unknown")

        case_map = {
            "debian": ("debian", "apt", "sudo apt update && sudo apt install -y"),
            "ubuntu": ("debian", "apt", "sudo apt update && sudo apt install -y"),
            "linuxmint": ("debian", "apt", "sudo apt update && sudo apt install -y"),
            "pop": ("debian", "apt", "sudo apt update && sudo apt install -y"),
            "elementary": ("debian", "apt", "sudo apt update && sudo apt install -y"),
            "kali": ("debian", "apt", "sudo apt update && sudo apt install -y"),
            "raspbian": ("debian", "apt", "sudo apt update && sudo apt install -y"),
            "arch": ("arch", "pacman", "sudo pacman -S --needed --noconfirm"),
            "endeavouros": ("arch", "pacman", "sudo pacman -S --needed --noconfirm"),
            "manjaro": ("arch", "pacman", "sudo pacman -S --needed --noconfirm"),
            "garuda": ("arch", "pacman", "sudo pacman -S --needed --noconfirm"),
            "artix": ("arch", "pacman", "sudo pacman -S --needed --noconfirm"),
            "fedora": ("fedora", "dnf", "sudo dnf install -y"),
            "centos": ("fedora", "dnf", "sudo dnf install -y"),
            "rhel": ("fedora", "dnf", "sudo dnf install -y"),
            "rocky": ("fedora", "dnf", "sudo dnf install -y"),
            "almalinux": ("fedora", "dnf", "sudo dnf install -y"),
            "opensuse-tumbleweed": ("suse", "zypper", "sudo zypper install -y"),
            "opensuse-leap": ("suse", "zypper", "sudo zypper install -y"),
            "suse": ("suse", "zypper", "sudo zypper install -y"),
            "alpine": ("alpine", "apk", "sudo apk add"),
        }

        if distro_id in case_map:
            family, pm, install_cmd = case_map[distro_id]
            self.current_family = family
            self.current_pm = pm
            self.current_install_cmd = install_cmd
        elif distro_id.startswith("opensuse"):
            self.current_family = "suse"
            self.current_pm = "zypper"
            self.current_install_cmd = "sudo zypper install -y"

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
                if f.lower().endswith(('.grab', '.run')) and os.path.isfile(f):
                    self._load_grab_file_from_path(f)
                    return
            messagebox.showinfo("Drag & Drop", "Please drop a valid .grab file.")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to handle dropped file: {e}")

    def _load_grab_file_from_path(self, path):
        """Open a .grab (v1/v2) or a .grab.run bundle (unpacked so its files can be restored)."""
        try:
            if path.endswith(".run"):
                self.status_var.set("Unpacking bundle...")
                self.root.update_idletasks()
                self.bundle_dir = str(core.extract_bundle(path))
                manifest = core.load_path(os.path.join(self.bundle_dir, "manifest.grab"))
            else:
                self.bundle_dir = None
                manifest = core.load_path(path)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to read grab file:\n{e}")
            return
        self._show_manifest(manifest, path)

    def _show_manifest(self, manifest, label, scan=False):
        """scan=True: this machine's own capture. Everything starts ticked (the
        audit decides what goes into a bundle); files come from their real paths."""
        self.is_scan = scan
        if scan:
            self.bundle_dir = None
            for p in manifest.packages:
                p.selected = True
        self.packages = [{"name": p.name, "src": p.src, "selected": p.selected,
                          "category": p.category, "via": ""} for p in manifest.packages]
        self.extras = manifest
        self.header = manifest.header
        self.current_file = label
        self.orig_distro = manifest.header.get("ORIG_DISTRO", "unknown")
        self.orig_family = manifest.header.get("ORIG_FAMILY", "unknown")
        self.orig_pm = manifest.header.get("ORIG_PM", "unknown")
        self.file_path_var.set(label)
        self.files_note.configure(text=(
            "Programs on this machine that no package manager tracks. Ticked ones are embedded in the "
            "bundle when you export it (File → Export Migration Bundle)." if scan else
            "Programs no package manager tracks (from the bundle). Ticked ones are copied back to the "
            "same place; system paths use your password."))
        extra = ""
        if manifest.files or manifest.services or manifest.groups:
            extra = (f" | {len(manifest.files)} files, {len(manifest.services)} services, "
                     f"{len(manifest.groups)} groups")
        self.info_var.set(f"Loaded: {os.path.basename(label)} | Origin: {self.orig_distro} "
                          f"({self.orig_family}/{self.orig_pm}) | {len(self.packages)} packages{extra}")
        self.apply_filters()
        self._refresh_extra_tabs()
        self._resolve_in_background()

    def _resolve_in_background(self):
        """Work out where each package comes from on THIS machine (repo / AUR / ...)."""
        if not self.packages:
            return
        self.status_var.set("Checking where each package is available on this machine...")
        pkgs = [core.Package(p["name"], p["src"], p.get("category", "user")) for p in self.packages]
        m = core.Manifest(packages=pkgs)

        def work():
            logs = []
            core.resolve(m, self.current_family, self.current_pm, log=logs.append)
            self._ui_queue.put(("resolved", m, logs))
        threading.Thread(target=work, daemon=True).start()
        self._poll_queue()

    def _poll_queue(self):
        try:
            while True:
                msg = self._ui_queue.get_nowait()
                if msg[0] == "resolved":
                    _, m, logs = msg
                    res = {p.key: p for p in m.packages}
                    n_unavail = n_here = 0
                    for p in self.packages:
                        r = res.get(f"{p['name']}:{p['src']}")
                        p["via"], p["note"] = (r.via, r.note) if r else ("", "")
                        if p["via"] == "unavailable":
                            p["selected"] = False
                            n_unavail += 1
                        elif p["note"]:      # already here / conflicts / same role: untick, user may re-tick
                            p["selected"] = False
                            n_here += 1
                    self.refresh_tree()
                    note = f" AUR lookup problem: {logs[0]}" if logs else ""
                    self.status_var.set(f"Availability checked: {n_unavail} not available here, {n_here} already "
                                        f"covered or conflicting (both unticked; see 'Will install via').{note}")
                    return
        except queue.Empty:
            pass
        self.root.after(150, self._poll_queue)

    def scan_system(self):
        """Capture this machine (packages with categories, loose files, services,
        groups) in the background and show it with everything ticked, ready to
        audit and export as a bundle."""
        self.status_var.set("Scanning this machine (packages, loose files, services, groups)...")
        self.info_var.set("Scanning...")
        q = queue.Queue()

        def work():
            try:
                q.put(("ok", core.capture(log=lambda line: q.put(("log", line)))))
            except Exception as e:
                q.put(("err", e))

        def pump():
            try:
                while True:
                    kind, val = q.get_nowait()
                    if kind == "log":
                        self.status_var.set(f"Scanning... {val}")
                    elif kind == "err":
                        messagebox.showerror("Scan", f"Scan failed:\n{val}")
                        return
                    else:
                        self._show_manifest(val, "(scanned from this machine - not saved yet)", scan=True)
                        return
            except queue.Empty:
                pass
            self.root.after(150, pump)
        threading.Thread(target=work, daemon=True).start()
        pump()

    def open_grab_file(self):
        path = filedialog.askopenfilename(
            title="Open Grab File",
            initialdir=self.last_directory,
            filetypes=[("Grab files", "*.grab *.grab.run"), ("All files", "*.*")]
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
        """Write the selected (visible) packages as .grab v2, keeping categories,
        services and groups. Embedded files only exist inside a bundle, so they
        are not written to a plain .grab."""
        try:
            selected = [p for p in self.get_filtered_packages() if p.get("selected", True)]
            header = dict(self.header) if self.header else {}
            header.update({
                "Created": datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
                "ORIG_DISTRO": header.get("ORIG_DISTRO", self.orig_distro if self.orig_distro != "current"
                                          else self.current_distro_id),
                "ORIG_FAMILY": self.orig_family, "ORIG_PM": self.orig_pm,
            })
            header.setdefault("ORIG_DISTRO_NAME", f'"{self.current_distro_name}"')
            names = {p["name"] for p in selected}
            m = core.Manifest(
                header,
                [core.Package(p["name"], p["src"], p.get("category", "user")) for p in selected],
                [sv for sv in self.extras.services if sv.selected and sv.package in names],
                [g for g in self.extras.groups if self.extras.group_selected.get(g, True)])
            with open(path, "w") as f:
                f.write(core.dumps(m))
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
        active_sources |= {p["src"] for p in self.packages if p["src"] not in self.source_vars}

        filtered = []
        for p in self.packages:
            if search and search not in p["name"].lower():
                continue
            if p["src"] not in active_sources:
                continue
            if not self.category_vars.get(p.get("category", "user"), tk.BooleanVar(value=True)).get():
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
                p["src"],
                CATEGORY_LABELS.get(p.get("category", "user"), p.get("category", "")),
                p.get("via", "") + (f"  ({p['note']})" if p.get("note") else ""),
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
        for var in self.category_vars.values():
            var.set(True)
        self.apply_filters()

    def get_selected_packages(self):
        return [p for p in self.packages if p.get("selected", True)]

    # ───────────────────────────────────────── files / services & groups tabs ───
    def _setup_extra_tabs(self):
        files_frame = ttk.Frame(self.notebook)
        self.notebook.add(files_frame, text="Files")
        self.files_note = ttk.Label(files_frame, text="Programs no package manager tracks. Scan this machine "
                                    "or open a bundle to see them.", wraplength=900)
        self.files_note.pack(fill=tk.X, padx=5, pady=5)
        self.files_tree = self._make_tree(files_frame, (("selected", "Select", 72), ("dest", "Path", 420),
                                                         ("kind", "Kind", 60), ("size", "Size", 80),
                                                         ("target", "Link target", 300)))
        self.files_tree.bind("<Button-1>", lambda e: self._toggle_extra(e, self.files_tree))

        svc_frame = ttk.Frame(self.notebook)
        self.notebook.add(svc_frame, text="Services & groups")
        ttk.Label(svc_frame, text="Services are enabled (and started) after the packages are installed; "
                  "group membership needs a log-out to apply.", wraplength=900).pack(fill=tk.X, padx=5, pady=5)
        self.svc_tree = self._make_tree(svc_frame, (("selected", "Select", 72), ("type", "Type", 80),
                                                     ("name", "Name", 320), ("scope", "Scope", 80),
                                                     ("package", "From package", 200)))
        self.svc_tree.bind("<Button-1>", lambda e: self._toggle_extra(e, self.svc_tree))

    def _make_tree(self, parent, cols):
        frame = ttk.Frame(parent)
        frame.pack(fill=tk.BOTH, expand=True)
        frame.grid_rowconfigure(0, weight=1)
        frame.grid_columnconfigure(0, weight=1)
        tree = ttk.Treeview(frame, columns=[c[0] for c in cols], show="headings", style="Grabbit.Treeview")
        for cid, text, width in cols:
            tree.heading(cid, text=text)
            tree.column(cid, width=width, stretch=(cid in ("dest", "name")),
                        anchor=tk.CENTER if cid == "selected" else tk.W)
        vsb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        return tree

    def _refresh_extra_tabs(self):
        m = self.extras
        self.files_tree.delete(*self.files_tree.get_children())
        for i, f in enumerate(m.files):
            self.files_tree.insert("", "end", iid=f"f{i}", values=(
                "☑" if f.selected else "☐", f.dest, f.kind, core.human(f.size) if f.size else "", f.target))
        self.svc_tree.delete(*self.svc_tree.get_children())
        for i, sv in enumerate(m.services):
            self.svc_tree.insert("", "end", iid=f"s{i}", values=(
                "☑" if sv.selected else "☐", "service", sv.unit, sv.scope, sv.package))
        for g in m.groups:
            self.svc_tree.insert("", "end", iid=f"g:{g}", values=(
                "☑" if m.group_selected.get(g, True) else "☐", "group", g, "", ""))
        tabs = self.notebook.tabs()
        ticked = [f for f in m.files if f.selected]
        size = sum(f.size for f in ticked)
        self.notebook.tab(tabs[1], text=f"Files ({len(ticked)}/{len(m.files)}"
                                        + (f", {core.human(size)})" if size else ")"))
        n_svc = sum(sv.selected for sv in m.services) + sum(m.group_selected.get(g, True) for g in m.groups)
        self.notebook.tab(tabs[2], text=f"Services & groups ({n_svc}/{len(m.services) + len(m.groups)})")

    def _toggle_extra(self, event, tree):
        if tree.identify_region(event.x, event.y) != "cell" or tree.identify_column(event.x) != "#1":
            return
        iid = tree.identify_row(event.y)
        if not iid:
            return
        m = self.extras
        if iid.startswith("f"):
            f = m.files[int(iid[1:])]
            f.selected = not f.selected
            # a link is useless without its target and vice versa: keep pairs together
            for other in m.files:
                if other.kind == "link" and other.target == f.dest or f.kind == "link" and other.dest == f.target:
                    other.selected = f.selected
        elif iid.startswith("s"):
            sv = m.services[int(iid[1:])]
            sv.selected = not sv.selected
        elif iid.startswith("g:"):
            g = iid[2:]
            m.group_selected[g] = not m.group_selected.get(g, True)
        self._refresh_extra_tabs()

    # ─────────────────────────────────────────────────────── install plan ───
    def _build_plan(self):
        pkgs = []
        for p in self.get_selected_packages():
            via = p.get("via") or ("repo" if p["src"] in ("pacman", "apt", "dnf", "zypper", "apk") else p["src"])
            pkg = core.Package(p["name"], p["src"], p.get("category", "user"), True, via)
            pkgs.append(pkg)
        m = core.Manifest(self.header, pkgs, self.extras.services, self.extras.groups,
                          self.extras.files if self.bundle_dir else [], self.extras.group_selected)
        return core.plan(m, self.current_family, self.current_pm, bundle_dir=self.bundle_dir,
                         update_first=self.current_pm == "pacman",
                         orig_home=self.header.get("ORIG_HOME"))

    def preview_load(self):
        if any(p.get("via") == "" for p in self.packages) and self.packages:
            messagebox.showinfo("Preview", "Still checking package availability — try again in a moment.")
            return
        steps = self._build_plan()
        if not steps:
            messagebox.showinfo("Preview", "Nothing selected to install.")
            return
        top = tk.Toplevel(self.root)
        top.title("Install plan")
        top.geometry("900x520")
        text = scrolledtext.ScrolledText(top, wrap=tk.WORD)
        text.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        text.insert(tk.END, f"This machine: {self.current_distro_name} ({self.current_family}/{self.current_pm})\n")
        text.insert(tk.END, f"File from: {self.orig_distro} ({self.orig_family}/{self.orig_pm})\n\n")
        for i, st in enumerate(steps, 1):
            who = "as root (sudo)" if st.root else "as you"
            text.insert(tk.END, f"{i}. {st.label}  [{who}]\n")
            if st.argv[0] in ("restore-files", "add-groups", "path-setup"):
                for it in st.items:
                    text.insert(tk.END, f"     {getattr(it, 'dest', it)}\n")
            elif st.argv[:2] in (["sh", "-c"], ["bash", "-c"]):
                text.insert(tk.END, "     (built-in helper script; installs it only if missing)\n")
            else:
                cmd = " ".join(st.argv)
                text.insert(tk.END, f"     {cmd[:600]}{' ...' if len(cmd) > 600 else ''}\n")
            text.insert(tk.END, "\n")
        text.configure(state=tk.DISABLED)
        btns = ttk.Frame(top)
        btns.pack(fill=tk.X, padx=10, pady=5)
        ttk.Button(btns, text="Install", command=lambda: (top.destroy(), self.load_selected())).pack(side=tk.RIGHT)
        ttk.Button(btns, text="Close", command=top.destroy).pack(side=tk.RIGHT, padx=5)

    def _ask_password(self, sudo):
        """Modal password dialog; returns True once sudo accepts it (3 tries)."""
        result = {"ok": False}
        dlg = tk.Toplevel(self.root)
        dlg.title("Administrator password")
        dlg.transient(self.root)
        dlg.resizable(False, False)
        user = os.environ.get("USER", "")
        ttk.Label(dlg, text=f"Installing needs administrator rights.\nEnter the password for {user}:",
                  padding=12).pack()
        pw = tk.StringVar()
        entry = ttk.Entry(dlg, textvariable=pw, show="•", width=32)
        entry.pack(padx=12)
        msg = ttk.Label(dlg, text="", foreground="#c0392b", padding=(12, 4))
        msg.pack()
        tries = {"n": 0}

        def submit(*_):
            msg.configure(text="Checking...")
            dlg.update_idletasks()
            good, why = sudo.check(pw.get())
            if good:
                result["ok"] = True
                dlg.destroy()
                return
            tries["n"] += 1
            pw.set("")
            if why.startswith("sudo refused") or tries["n"] >= 5:
                messagebox.showerror("Password", why if why.startswith("sudo") else "Too many attempts.",
                                     parent=dlg)
                dlg.destroy()
            else:
                msg.configure(text=why)

        row = ttk.Frame(dlg, padding=12)
        row.pack(fill=tk.X)
        ttk.Button(row, text="OK", command=submit).pack(side=tk.RIGHT)
        ttk.Button(row, text="Cancel", command=dlg.destroy).pack(side=tk.RIGHT, padx=5)
        entry.bind("<Return>", submit)
        dlg.after(50, lambda: (dlg.grab_set(), entry.focus_set()))
        self.root.wait_window(dlg)
        return result["ok"]

    def load_selected(self):
        if self.packages and any(p.get("via") == "" for p in self.packages):
            messagebox.showinfo("Install", "Still checking package availability — try again in a moment.")
            return
        steps = self._build_plan()
        if not steps:
            messagebox.showinfo("Install", "Nothing selected to install.")
            return
        n_pkgs = len(self.get_selected_packages())
        n_files = len([f for f in self.extras.files if f.selected]) if self.bundle_dir else 0
        n_svc = len([sv for sv in self.extras.services if sv.selected])
        if not messagebox.askyesno("Install", f"Install {n_pkgs} packages, restore {n_files} files and enable "
                                   f"{n_svc} services?\n\n{len(steps)} steps. "
                                   "Use Preview Install Plan to see every command."):
            return
        sudo = core.Sudo()
        if sudo.needs_password() and not self._ask_password(sudo):
            sudo.close()
            return
        self._run_steps(steps, sudo)

    def _run_steps(self, steps, sudo):
        top = tk.Toplevel(self.root)
        top.title("Installing")
        top.geometry("980x600")
        status = tk.StringVar(value="Starting...")
        ttk.Label(top, textvariable=status, padding=8, font=("TkDefaultFont", 11, "bold")).pack(fill=tk.X)
        bar = ttk.Progressbar(top, maximum=len(steps))
        bar.pack(fill=tk.X, padx=10)
        log = scrolledtext.ScrolledText(top, wrap=tk.NONE, font=("monospace", 9))
        log.pack(fill=tk.BOTH, expand=True, padx=10, pady=8)
        btns = ttk.Frame(top)
        btns.pack(fill=tk.X, padx=10, pady=(0, 8))
        state = {"done": False}
        log_dir = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "grabbit"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"install-{datetime.now():%Y%m%d-%H%M%S}.log"
        log_file = open(log_path, "w", encoding="utf-8")
        q = queue.Queue()

        runner = core.Runner(
            steps, sudo, bundle_dir=self.bundle_dir,
            log=lambda line: q.put(("log", line)),
            progress=lambda i, n, label: q.put(("progress", i, n, label)),
            done=lambda results: q.put(("done", results)))

        def cancel():
            if state["done"]:
                top.destroy()
                return
            if messagebox.askyesno("Cancel", "Stop after the current step?", parent=top):
                runner.cancel()
                status.set("Cancelling after the current step...")
        close_btn = ttk.Button(btns, text="Cancel", command=cancel)
        close_btn.pack(side=tk.RIGHT)
        top.protocol("WM_DELETE_WINDOW", cancel)

        def pump():
            try:
                for _ in range(500):
                    msg = q.get_nowait()
                    if msg[0] == "log":
                        log_file.write(msg[1] + "\n")
                        log.insert(tk.END, msg[1] + "\n")
                        log.see(tk.END)
                    elif msg[0] == "progress":
                        _, i, n, label = msg
                        bar["value"] = i
                        status.set(f"Step {min(i + 1, n)}/{n}: {label}")
                    elif msg[0] == "done":
                        state["done"] = True
                        sudo.close()
                        log_file.close()
                        self._show_results(top, status, log, msg[1], log_path)
                        close_btn.configure(text="Close")
                        return
            except queue.Empty:
                pass
            top.after(100, pump)

        runner.start()
        pump()

    def _show_results(self, top, status, log, results, log_path):
        ok = [r for r in results if r[1]]
        bad = [r for r in results if r[1] is False]
        skipped = [r for r in results if r[1] is None]
        status.set(f"Done: {len(ok)} step(s) OK, {len(bad)} with problems"
                   + (f", {len(skipped)} cancelled" if skipped else ""))
        log.insert(tk.END, "\n══════ Summary ══════\n")
        for label, good, detail in results:
            mark = "✔" if good else ("–" if good is None else "✖")
            log.insert(tk.END, f"{mark} {label}{('  — ' + detail) if detail else ''}\n")
        log.insert(tk.END, f"\nFull log: {log_path}\n")
        log.see(tk.END)

    # ───────────────────────────────────────────────────── export bundle ───
    def _audited_manifest(self):
        """What is ticked right now, as a manifest (all ticked packages, whatever
        the filters show). Services of unticked packages are dropped."""
        pkgs = [core.Package(p["name"], p["src"], p.get("category", "user"))
                for p in self.packages if p.get("selected", True)]
        names = {p.name for p in pkgs}
        m = self.extras
        files = [f for f in m.files if f.selected]
        # a link without its target (or vice versa) is pointless: both or neither
        dests = {f.dest for f in files}
        files = [f for f in files if f.kind != "link" or f.target in dests
                 or not any(x.dest == f.target for x in m.files)]
        return core.Manifest(
            dict(self.header), pkgs,
            [sv for sv in m.services if sv.selected and sv.package in names],
            [g for g in m.groups if m.group_selected.get(g, True)], files)

    def export_bundle(self):
        """Build a .grab.run from the audited scan of this machine."""
        if not self.is_scan:
            if messagebox.askyesno(
                    "Export migration bundle",
                    "A bundle is built from a scan of THIS machine (its loose files have to come from "
                    "here).\n\nScan now? Review the three tabs, untick what you don't want to take "
                    "along, then choose Export again."):
                self.scan_system()
            return
        m = self._audited_manifest()
        if not (m.packages or m.files):
            messagebox.showinfo("Export migration bundle", "Nothing is ticked.")
            return
        cats = {}
        for p in m.packages:
            cats[p.category] = cats.get(p.category, 0) + 1
        size = sum(f.size for f in m.files)
        left_out = (len(self.packages) - len(m.packages), len(self.extras.files) - len(m.files))
        summary = (f"Packages: {len(m.packages)}  ("
                   + ", ".join(f"{n} {CATEGORY_LABELS.get(g, g)}" for g, n in sorted(cats.items())) + ")\n"
                   f"Files: {len(m.files)}  ({core.human(size)})\n"
                   f"Services: {len(m.services)}   Groups: {', '.join(m.groups) or 'none'}\n\n"
                   f"Left out: {left_out[0]} packages, {left_out[1]} files.\n\n"
                   + ("Includes sign-ins (git/GitHub CLI/SSH keys, Claude Code): the bundle is a "
                      "credential, keep it private.\n\n"
                      if any(f.key.startswith(core.ACCOUNT_KEY) for f in m.files) else "") +
                   "On the new machine, system and old-distro packages still start unticked, "
                   "so you can decide again there.")
        if not messagebox.askokcancel("Export migration bundle", summary):
            return
        path = filedialog.asksaveasfilename(
            title="Export migration bundle", initialdir=self.last_directory,
            initialfile=f"{platform.node() or 'machine'}-{datetime.now():%Y%m%d}.grab.run",
            defaultextension=".run", filetypes=[("grabbit bundle", "*.grab.run")])
        if not path:
            return
        self.last_directory = os.path.dirname(path) or self.last_directory
        self._save_last_directory(self.last_directory)

        top = tk.Toplevel(self.root)
        top.title("Exporting bundle")
        top.geometry("820x420")
        status = tk.StringVar(value=f"Writing {os.path.basename(path)} ({core.human(size)} of files)...")
        ttk.Label(top, textvariable=status, padding=8).pack(fill=tk.X)
        log = scrolledtext.ScrolledText(top, wrap=tk.NONE)
        log.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        q = queue.Queue()

        def work():
            try:
                out = core.build_bundle(m, path, log=lambda l: q.put(l))
                q.put(f"\nWrote {out} ({core.human(out.stat().st_size)})")
                q.put("On the new machine: mark it executable (Properties → Permissions), then double-click it.")
                q.put(("done", True))
            except Exception as e:
                q.put(f"\nFAILED: {e}")
                q.put(("done", False))

        def pump():
            try:
                while True:
                    item = q.get_nowait()
                    if isinstance(item, tuple):
                        status.set("Done." if item[1] else "Export failed — see below.")
                        return
                    log.insert(tk.END, item + "\n")
                    log.see(tk.END)
            except queue.Empty:
                pass
            top.after(150, pump)
        threading.Thread(target=work, daemon=True).start()
        pump()


if __name__ == "__main__":
    # grabbit-gui [file.grab | bundle.grab.run] | --bundle <unpacked dir> | --scan
    args = sys.argv[1:]
    bundle_dir = args[args.index("--bundle") + 1] if "--bundle" in args and args.index("--bundle") + 1 < len(args) else None
    initial_file = next((a for a in args if not a.startswith("-") and a != bundle_dir), None)

    if HAS_DND and TkinterDnD is not None:
        root = TkinterDnD.Tk()
    else:
        root = tk.Tk()

    app = GrabbitGUI(root)

    if bundle_dir:
        manifest = core.load_path(os.path.join(bundle_dir, "manifest.grab"))
        app.bundle_dir = bundle_dir
        app._show_manifest(manifest, os.path.join(bundle_dir, "manifest.grab"))
        app.status_var.set("Bundle opened. Review the three tabs, then Install Selected.")
    elif "--scan" in args or "-s" in args:
        app.scan_system()
    elif initial_file and os.path.isfile(initial_file):
        app._load_grab_file_from_path(initial_file)

    root.mainloop()
