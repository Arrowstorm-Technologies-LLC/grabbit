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
- `-x` modifier lets you exclude (or ignore on load) certain source types: `aur`, `brew`, `base`, etc.
- Pure Bash with only standard tools + whatever package manager you already have.

## Install

```sh
curl -Lo ~/.local/bin/grabbit https://raw.githubusercontent.com/Arrowstorm-Technologies-LLC/grabbit/main/grabbit
chmod +x ~/.local/bin/grabbit
```

Make sure `~/.local/bin` is in your PATH.

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

- Debian family → `apt-mark showmanual`
- Arch family → `pacman -Qe` + `pacman -Qem` (foreign = AUR)
- Fedora family → best-effort dnf user-installed list
- Homebrew → all formulae and casks

It does **not** try to capture every single dependency (those will be pulled in automatically during load).

### The proprietary file

The file created by `save` is plain text and contains:

- Origin distro and package manager
- List of packages + their source (`apt`, `aur`, `brew`, etc.)

You can read it with `cat`, version control it, or copy it anywhere.

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
- No support (yet) for Snap, Flatpak, pip, cargo, etc. Only the main native managers + AUR + Homebrew.
- Base package exclusion relies on each distro's "explicit/manual" concept and is not 100% perfect.

## License

MIT (same as the rack project)