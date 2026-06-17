# grabbit

grabbit is a portable Bash script that captures the packages you've actually installed on a Linux system (excluding most of what the distro shipped with) and can later restore them on the *same* or a *completely different* distro.

It knows how to talk to apt, pacman, dnf, AUR (via paru), and Homebrew. When you load a grabbit file on a different distribution it automatically rewrites the install commands (e.g. `apt` → `pacman`).

```sh
grabbit save ~/my-setup.grab          # comprehensive list of what you added
grabbit -x aur,brew save minimal.grab # same but skip AUR and Homebrew

# later, on another machine (even different distro)
grabbit load ~/my-setup.grab
grabbit -x aur load minimal.grab      # restore while ignoring AUR packages
```

## Features

- Works on Debian/Ubuntu, Arch, Fedora and similar families.
- Detects explicitly installed packages (using `apt-mark showmanual`, `pacman -Qe`, etc.) so you don't get the entire base system.
- Recognises AUR packages and Homebrew formulae/casks.
- Records the *original source* of each package.
- On load, ensures required helpers exist (automatically installs paru and/or Homebrew when needed).
- Transposes commands across distros: a file created with apt on Debian will use pacman on Arch (and vice versa).
- `-x` modifier lets you exclude (or ignore on load) certain source types: `aur`, `brew`, `base`, `snap`, `flatpak`, etc.
- Pure Bash with only standard tools + whatever package manager you already have.
- Supports zypper, apk, snap, flatpak in addition to apt/pacman/dnf/brew/aur.
- Prefers native mainline repos during cross-distro loads when possible.

## GUI Variant

A graphical interface is available for manual auditing of grab files:

```sh
./grabbit_gui.py
# or
python3 grabbit_gui.py
```

In the GUI you can:

- Scan your current system (equivalent to `grabbit save`)
- Open an existing `.grab` file
- Use the search box and per-source checkboxes to filter the package list (replaces the `-x` CLI flag)
- Toggle individual packages or use "Select All Visible", "Deselect All Visible", "Invert"
- Save the audited selection as a new grab file
- Preview the exact commands that would be run for the selected packages (with transposition applied)
- Execute the load for the audited selection

The GUI uses only Python's standard library (`tkinter`). On most desktop Linux distributions you may need to install the Tk package:

- Debian/Ubuntu: `sudo apt install python3-tk`
- Arch: `sudo pacman -S tk`
- Fedora: `sudo dnf install python3-tkinter`

## Install

```sh
curl -Lo ~/.local/bin/grabbit https://raw.githubusercontent.com/Arrowstorm-Technologies-LLC/grabbit/main/grabbit
chmod +x ~/.local/bin/grabbit
```

Make sure `~/.local/bin` is in your PATH.

### GUI Launcher & Desktop Integration

To run the GUI from command line:

```sh
# After cloning or downloading the repo
./grabbit-gui                # or
./grabbit-gui my-setup.grab
```

To make `grabbit-gui` available globally:

```sh
mkdir -p ~/.local/bin
cp grabbit-gui ~/.local/bin/
cp grabbit_gui.py ~/.local/bin/
chmod +x ~/.local/bin/grabbit-gui
```

For desktop/start menu integration, copy the .desktop file:

```sh
mkdir -p ~/.local/share/applications
cp grabbit-gui.desktop ~/.local/share/applications/
update-desktop-database ~/.local/share/applications/
```

You can then search for "grabbit-gui" in your application launcher.

The GUI also supports:
- Drag & drop of `.grab` files (best with `pip install tkinterdnd2`)
- "Save" (to current file) and "Save As..."
- Remembers the last used directory for open/save dialogs
- Command line: `grabbit-gui somefile.grab` or `grabbit-gui --scan`

## Usage

```
grabbit save <file>
grabbit -x save <file>
grabbit load <file>
grabbit -x load <file>
```

### Filtering with -x

`-x` followed by a comma-separated list of types tells grabbit to skip those sources:

```sh
grabbit -x aur,brew save mylist.grab     # capture only native packages
grabbit -x aur load mylist.grab          # restore everything except AUR
```

When you just write `grabbit -x save ...` (no types after `-x`) it performs a full capture (the normal behaviour).

### What gets saved?

grabbit tries hard to record only packages *you* asked for:

- Debian/Ubuntu etc. → `apt-mark showmanual`
- Arch etc. → `pacman -Qe` + `pacman -Qem` (AUR)
- Fedora etc. → dnf user-installed
- openSUSE → zypper
- Alpine → apk
- Universal: Snap, Flatpak, Homebrew (formulae + casks)

Improved base exclusion heuristics are applied per distro (known base groups and meta-packages are filtered).

It does **not** try to capture every single dependency (those will be pulled in automatically during load).

### The proprietary file

The file created by `save` is plain text and contains:

- Origin distro and package manager
- List of packages + their source (`apt`, `aur`, `brew`, `snap`, `flatpak`, `zypper`, `apk`, etc.)

You can read it with `cat`, version control it, or copy it anywhere.

### Sophisticated source selection on load

When restoring, grabbit queries the current system for all places a package name is available:

- Official/mainline repos of the current package manager
- AUR (on Arch, via RPC query)
- Snap, Flatpak, Homebrew (if commands present)

If exactly one option, it uses it (with transposition where appropriate).

If **multiple options** share the same name (e.g. a package exists in mainline *and* AUR, or cross-distro name collision), grabbit will **prompt you interactively** to choose:

```
Multiple installation options found for 'foo' (original source was: aur)
   1) official (pacman)
   2) AUR (paru)
   3) ...
Select option: 
```

Non-interactive runs (no tty) will auto-prefer the first "official" match if available.

This handles ambiguous names gracefully while preferring user control.

## How load works

1. Detects your current distro and package manager.
2. If the file mentions AUR packages and you're on Arch, makes sure `paru` is installed.
3. If the file mentions Homebrew packages, makes sure Homebrew is installed.
4. For every package, builds the correct install command for *this* machine:
   - Same distro family → uses the original style.
   - Different family → rewrites the command (`apt install` becomes `pacman -S`, etc.).
5. Runs the commands (dependencies are handled by the native package manager or `paru`).

## Examples

### Typical Arch user backup

```sh
grabbit save ~/arch-setup.grab
# ... copy the file to a new Arch install
grabbit load ~/arch-setup.grab
```

### Move from Ubuntu laptop to Arch desktop (partial)

```sh
# on Ubuntu
grabbit -x brew save work.grab

# on Arch desktop
grabbit load work.grab          # will use pacman for the old apt packages
```

### Only native packages, no helpers

```sh
grabbit -x aur,brew save clean.grab
grabbit load clean.grab
```

## Limitations

- Package names are used as-is. If a package has a completely different name on the target distro you will need to adjust the file manually or the install will fail for that entry.
- Some packages are not available everywhere (e.g. most AUR packages only make sense on Arch).
- Homebrew on Linux installs to `/home/linuxbrew` by default.
- The script will ask for sudo when needed.
- Base package exclusion heuristics are improved but not perfect for every edge case.
- Snap/Flatpak support installs from default remotes (flathub for flatpak).

## Examples and tests

See the `examples/` directory for sample `.grab` files (including cross-distro).

Run `./tests/test_basic.sh` for basic smoke tests (syntax, parsing, feature presence).

## License

MIT (same as the rack project)