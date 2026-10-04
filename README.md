# grabbit

grabbit captures what you've installed on a Linux machine and puts it back on
another one, on the same distro or a different one.

It works at three levels:

- **Lists:** `grabbit save` writes a plain-text `.grab` file of your explicitly
  installed packages and their sources (repo, AUR, Homebrew, Flatpak, Snap, pipx,
  pip). `grabbit load` installs them again, translating the commands if the
  target runs a different package manager.
- **Audit:** `grabbit-gui` lets you scan a system or open a file, filter it, tick
  exactly what you want, preview the plan and install it.
- **Migration:** the GUI (or `grabbit pack`) turns this whole machine into **one
  self-installing file**, after you've reviewed exactly what goes in. Double-click it on a fresh machine: it installs grabbit
  and its dependencies, opens the GUI with your list, and restores your
  packages, loose programs, services and group memberships. You're asked for
  your password once.

```sh
grabbit-gui --scan                   # review this machine, then Export Bundle
grabbit pack ~/my-pc.grab.run        # same bundle, no review (everything)
grabbit save ~/my-setup.grab         # just the package list
grabbit load ~/my-setup.grab         # install a list here
grabbit-gui                          # audit / pick / install in a window
```

## Features

- **Package managers:** apt, pacman, dnf, zypper and apk natively, plus AUR (via
  paru), Homebrew, Flatpak, Snap, pipx and `pip --user`.
- **Only what you installed:** `apt-mark showmanual`, `pacman -Qen`/`-Qem`, dnf
  user-installed and so on, rather than the whole base system. Every package
  keeps its original source.
- **Categories:** each package is tagged with one of four, so you can decide per
  group what comes back:
  - *Installed by you*: added after the OS was set up.
  - *Desktop environment*: KDE, GNOME, X11 and similar parts you added.
  - *Drivers & firmware*: drivers and microcode for a GPU or CPU vendor.
  - *Came with the OS*: the old installer's choices (network manager, firewall,
    audio, ...), the kernel, bootloader and base, and the distro's own tools and
    branding.

  *Installed by you* vs *Came with the OS* comes from the package logs
  (`pacman.log`, `dpkg.log`) compared with when the OS installer finished; where
  those can't tell, name patterns decide. Older `.grab` files' *system*,
  *old distro* and *came with old OS* tags all read as *Came with the OS*.
- **Review before you pack:** a GUI scan shows everything a bundle would carry
  (packages, loose programs, services, groups) with sizes; only what you leave
  ticked is exported.
- **Aware of the target machine:** before installing, grabbit checks where each
  package is available on this machine (repo, AUR, …). Packages that can't be
  had are marked and unticked instead of failing mid-run.
- **Batched and resilient:** one `pacman -Syu` transaction and one `paru` run for
  hundreds of packages. If a batch fails, it's retried package by package, so a
  single bad name doesn't sink the rest.
- **More than packages:** programs no package manager owns (`~/.local/bin`,
  `/usr/local`, `/opt`, AppImages, rack's registry), enabled systemd services
  and your group memberships.
- **Helpers installed on demand:** paru, Homebrew, Flatpak with the Flathub
  remote, snapd, pipx and pip are set up only when your selection needs them.
- **Cross-distro:** a file made with apt on Debian installs with pacman on Arch,
  and the other way round.
- **Few dependencies:** the CLI is Bash, and the GUI and migration engine use
  Python's standard library (Tkinter). Nothing else to install.

## Install

The installer copies `grabbit`, `grabbit-gui`, `grabbit_gui.py`, `grabbit_core.py`
and the bundle stub into `~/.local/bin`. It also installs Python/Tk for your
distro, adds optional `tkinterdnd2` for drag-and-drop, and creates a menu entry.

```sh
curl -fsSL https://raw.githubusercontent.com/Arrowstorm-Technologies-LLC/grabbit/main/install.sh | bash
# or, from a clone
./install.sh                 # --skip-deps / --skip-desktop to leave those out
```

Make sure `~/.local/bin` is on your PATH:

```sh
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc   # or ~/.zshrc
```

### Via rack

```sh
rack grabbit Arrowstorm-Technologies-LLC/grabbit     # install
rack -u grabbit                                       # update
```

grabbit has no GitHub releases. rack downloads the `main` branch archive and runs
`install.sh`.

## Migrating to a new machine

You don't need grabbit, Python's Tk or anything else on the new machine
beforehand.

**1. On the old machine**, build the bundle close to moving day, so it matches
what you have installed. Use the GUI, so you can see and choose what goes in:

1. `grabbit-gui --scan`, or **Scan Current System**. This captures packages with
   their categories, loose programs, services and groups. Everything starts
   ticked except *Came with the OS* packages.
2. Untick anything you don't want to take along, and tick any *Came with the OS*
   package you do want (e.g. a tool the old installer happened to include), on
   any of the three tabs. The
   tab titles count what's ticked, and the Files tab shows the total size.
   Unticking a symlink or its target unticks both.
3. **Export Bundle…** (also under **File**). It shows a summary (packages per
   category, files and their size, services, groups, what's left out), then
   asks where to save. Only ticked items go in, whatever the filters currently
   show. A service whose package you unticked stays out too.

A *Came with the OS* package you tick goes in, but it still starts unticked on
the new machine, so you get to decide again there.

For scripts, or when you don't need a review, the CLI packs everything:

```sh
grabbit pack ~/my-pc.grab.run                               # everything
grabbit pack ~/my-pc.grab.run --no-files                    # packages, services, groups only
grabbit pack ~/my-pc.grab.run --skip-file '*.AppImage'      # leave some loose files out (repeatable)
grabbit pack ~/my-pc.grab.run --max-file-mb 200             # ...or everything above a size
grabbit pack ~/my-pc.grab.run --no-accounts                 # leave the sign-ins out
```

**Sign-ins come along.** The scan also lists your git identity and logins as
files (each can be unticked): `~/.gitconfig`, `~/.git-credentials`,
`~/.config/gh` plus the GitHub CLI token (read from the keyring with
`gh auth token` when the bundle is written), `~/.ssh`, and Claude Code's login,
settings, skills, plugins, project memory and history (`~/.claude.json`,
`~/.claude/...`). On restore, gh is signed back in with the token (into the
keyring when there is one) and the token file is deleted; paths naming the old
`$HOME` are rewritten, so a different username on the new machine is fine.
A bundle holding sign-ins is written `0700`: treat it like a password.

`python3 ~/.local/bin/grabbit_core.py info my-pc.grab.run` prints a summary of
any bundle or `.grab` file.

**2. Copy it to the new machine and mark it executable once.** Right-click →
Properties → Permissions → *Allow executing file as program*. USB sticks and
downloads drop that bit. Alternatively, skip this and run `bash my-pc.grab.run`.

**3. Double-click it.** A terminal opens and:

- runs `pacman -Syu` and installs Python + Tk (other distros just install Python +
  Tk), asking for your password in the terminal;
- unpacks to `~/.local/share/grabbit/bundles/<name>-<date>/`;
- installs grabbit to `~/.local/bin`;
- opens the GUI on the bundle. The terminal can then be closed.

**4. In the GUI**, review the three tabs, then **Install Selected**:

| Tab | What's in it | How it's restored |
|---|---|---|
| **Packages** | repo, AUR, Homebrew (only the formulae you asked for, not their dependencies), Flatpak, Snap, pipx, `pip --user`; with *Category* and *Will install via* columns | `pacman -Syu` (one transaction), `paru` (one run), `brew`, `flatpak`, `snap`, `pipx`, `pip` |
| **Files** | programs no package manager owns: `~/.local/bin`, `/usr/local/{bin,sbin}`, `/opt/*`, AppImages in your home, rack's registry. Symlinks keep their targets, so e.g. `~/.local/bin/claude → ~/.local/share/claude/versions/…` comes back whole | copied to the same place; system paths through sudo. Paths under the old `$HOME` land under the new one, even with a different username. `~/.local/bin` is added to PATH for bash, zsh and fish |
| **Services & groups** | enabled systemd units whose unit file belongs to a captured package; your supplementary groups | `systemctl enable --now`, `usermod -aG` (log out to apply) |

What starts ticked:

- **Unticked by default:** *Came with the OS*. The new install makes its own
  choices there, and putting e.g. EndeavourOS's `grub`/`dracut` or its firewall
  on a CachyOS box would do harm.
- **Drivers & firmware:** the bundle records the old GPU/CPU vendor. Drivers and
  microcode for hardware the new machine doesn't have start unticked; on CachyOS,
  GPU drivers are left to its own `chwd`.
- **Also unticked:** packages marked *unavailable*, which this machine can't get
  from its repos or the AUR.
- **Follows its package:** a service whose package you untick is left out too.
- **Preview first:** **Preview Install Plan** shows every step and command
  before anything runs.

Installing runs in the background, with a progress bar, a live log, **Cancel**
(stops after the current step) and a summary at the end. The log is saved to
`~/.local/state/grabbit/install-<date>.log`.

## Passwords

- **In the terminal (bootstrap):** plain `sudo`, asking you directly.
- **In the GUI:** a password dialog, shown once per run.
  - The password is first checked with `unix_chkpwd`, the helper PAM itself
    uses, so a typo can't count against `pam_faillock`. sudo would retry a wrong
    askpass password three times, which on Arch-based systems locks the
    account for 10 minutes.
  - A verified password is then handed to sudo, pacman, paru (`--sudoflags -A`)
    and the Homebrew installer through a temporary `SUDO_ASKPASS` helper (mode
    0700, in `$XDG_RUNTIME_DIR`). It's deleted when the run ends.
  - The password itself is only held in memory and in the environment of the
    commands grabbit starts.
- **paru and makepkg always run as you**, never as root.

## The GUI

```sh
grabbit-gui                    # empty window: scan or open a file
grabbit-gui my-setup.grab      # open a list
grabbit-gui my-pc.grab.run     # open a bundle (unpacked first, so its files can be restored)
grabbit-gui --scan             # start with a scan of this machine
```

- **Scan Current System** captures this machine the same way `pack` does:
  every explicitly installed package tagged with its category, loose programs,
  services and groups, ticked by category (*Came with the OS* starts unticked).
  It runs in the background (about 10
  seconds).
- **Filters:** name search, per-source checkboxes and per-category checkboxes.
  Select, deselect or invert everything visible, or click the checkbox column
  per package.
- **Save / Save As:** writes the ticked, visible packages as `.grab` v2,
  keeping categories, services and groups.
- **Opening files:** drag and drop a `.grab` or `.grab.run` onto the window.
  This needs `tkinterdnd2`; otherwise click the drop zone. Open and save dialogs
  remember the last folder (`~/.config/grabbit/last_dir.txt`).
- **Export Bundle:** builds a `.grab.run` from a scan, with only what's ticked
  (see [Migrating](#migrating-to-a-new-machine)). With a file open instead of a
  scan, it offers to scan first, since loose files have to come from this
  machine.
- **Installing:** Preview Install Plan and Install Selected, as described above.

The menu entry (`grabbit-gui.desktop`) is installed by `install.sh`. It opens
`.grab` files from your file manager and has a *Scan Current System* action.

## CLI

```
grabbit save <file>                       your packages (no base system, no desktop environment)
grabbit -sp save <file>                   ...including distro/system packages
grabbit -de save <file>                   ...including desktop environment packages
grabbit -x aur,brew save <file>           ...without those sources
grabbit load <file>                       install a list here
grabbit -x aur load <file>                ...skipping those sources
grabbit pack <file.grab.run> [options]    self-installing migration bundle (see above)
grabbit install [--skip-deps]             install/update grabbit itself
```

`-x` takes a comma-separated list of sources (`aur`, `brew`, `snap`, `flatpak`,
`base`, …). On its own, `grabbit -x save` is a normal full capture.

`grabbit load` works in the terminal. For each package it checks the official
repos, the AUR, Snap, Flatpak and Homebrew. If a name exists in more than one
place, it asks you which to use; without a terminal it picks the official repo.
It then installs one package at a time with `sudo`. For a whole machine, use
the GUI or a bundle: they batch the work and resolve availability in about a
second.

### What gets captured

| Distro family | Packages |
|---|---|
| Debian/Ubuntu | `apt-mark showmanual` |
| Arch & derivatives (incl. CachyOS, EndeavourOS, Manjaro) | `pacman -Qen` (explicit, from a repo) + `pacman -Qem` (explicit, foreign → AUR) |
| Fedora | `dnf repoquery --userinstalled` |
| openSUSE | zypper |
| Alpine | `/etc/apk/world` (bundles) / `apk info` (`save`) |
| Anywhere | Homebrew (`brew leaves --installed-on-request` + casks), Flatpak apps, Snaps, pipx venvs, `pip --user --not-required` |

Dependencies aren't recorded; the package manager pulls them in again.
`grabbit save` leaves out base/system and desktop-environment packages unless
you pass `-sp`/`-de`. The GUI scan and `pack` keep everything and tag it
instead; the GUI's category checkboxes then show or hide each group. Per-distro lists can be extended with
`~/.config/grabbit/base-excludes.<family>` and `de-packages.<family>`.

## File format

A `.grab` file is plain text: `cat` it, diff it, keep it in git.

```
# GRABBIT v2
# ORIG_DISTRO=endeavouros
# ORIG_FAMILY=arch
# ORIG_PM=pacman
# ORIG_HOME=/home/me

PKG_LIST_START
PKG firefox:pacman
PKG brave-bin:aur
PKG_LIST_END

CAT_LIST_START
CAT firefox:pacman	user
CAT_LIST_END
SVC_LIST_START
SVC system	docker.service	docker
SVC_LIST_END
GRP_LIST_START
GRP docker
GRP_LIST_END
FILE_LIST_START
FILE link	777	0		~/.local/bin/claude	~/.local/share/claude/versions/2.1.283
FILE_LIST_END
```

v1 files are just the header plus the `PKG` block. v2 adds its sections *after*
`PKG_LIST_END`, where older readers stop, so every grabbit version can read
every file. Package names are validated (`[A-Za-z0-9@._+/-]`) before they reach
a command line, and anything else is dropped.

A `.grab.run` bundle is a Bash stub (`grabbit-bundle-stub.sh`) followed by a
tar.gz payload: `manifest.grab`, `app/` (grabbit itself) and `files/`.

## Examples

```sh
# Arch → fresh CachyOS, everything
grabbit pack ~/my-pc.grab.run            # then double-click it on the new machine

# Just the package list, same distro
grabbit save ~/arch-setup.grab
grabbit load ~/arch-setup.grab

# Ubuntu laptop → Arch desktop
grabbit -x brew save work.grab           # on Ubuntu
grabbit load work.grab                   # on Arch: apt packages install via pacman

# Only native packages
grabbit -x aur,brew save clean.grab
```

## Comparison

| Tool | Scope | Tracks source | Cross-distro | Restores more than packages | GUI |
|---|---|---|---|---|---|
| **grabbit** | native + AUR + brew + flatpak + snap + pipx + pip | yes | yes | loose programs, services, groups | yes (audit + install) |
| `pacman -Qe` lists | one distro | no | no | no | no |
| brew bundle | Homebrew | Homebrew | no | no | no |
| apt-clone / mintbackup | Debian family | partial | no | partial | some |
| Ansible / Nix | everything, declaratively | yes | yes | yes | varies (heavy) |

grabbit is for moving *your* machine, not for managing a fleet.

## Limitations

- **No settings:** bundles restore programs, not dotfiles or app data. Copy your
  home directory, or the parts you want, separately.
- **Names are taken as-is:** a package called something different on the target
  distro needs editing in the file (or shows up as *unavailable*).
- **What the target already has wins.** Before installing, grabbit unticks a
  package (the reason shows under *Will install via*; re-tick to override) when:
  - it's already installed, or provided by an installed package (Arch: `pacman -T`);
  - it declares a conflict with something installed (repo or AUR metadata), e.g.
    `pulseaudio` vs `pipewire-pulse`. Under `--noconfirm`, pacman would refuse it anyway;
  - one of its dependencies would conflict with something installed (Arch:
    the full `pacman -Sp` closure is checked);
  - apt would have to remove an installed package to install it (`apt-get -s`);
  - it does the same job as an installed package whose service is on, without
    declaring a conflict. That covers firewalls (`firewalld` vs `ufw`, which CachyOS
    enables), display managers and power-profile daemons. The table is
    `ROLE_GROUPS` in `grabbit_core.py`;
  - it's a Homebrew, pipx or pip copy of something the system package manager
    supplies here, or that the bundle installs natively.

  A service follows its package, so an unticked `firewalld` also leaves
  `firewalld.service` disabled. Each remaining service is checked again right
  before it's enabled, and skipped if it isn't installed, is already on,
  conflicts (`Conflicts=`) with an enabled unit, would take an alias another
  unit holds (`display-manager.service`), or another enabled service already
  does its job (firewall, time sync, network manager, power profiles:
  `SERVICE_ROLES`). `grabbit load` (CLI) skips the same package cases.
- **Nothing gets removed.** Installs never uninstall a package to make room:
  apt runs with `--no-remove`, zypper with `--no-force-resolution`, dnf without
  `--allowerasing`, and pacman answers its "remove conflicting package?"
  question with No under `--noconfirm`. A refused batch is retried one by one,
  which isolates the package at fault.
- **AUR packages only restore on Arch-based systems.**
- **Homebrew on Linux** installs to `/home/linuxbrew`. grabbit adds its
  `shellenv` line for bash, zsh and fish.
- **Flatpaks** come from Flathub. **Snaps on Arch** need `snapd` from the AUR,
  which grabbit builds.
- **Services** need systemd running (`--now` starts them). New group
  memberships apply after you log out and back in.
- **The executable bit:** a copied bundle usually needs it set again (step 2
  above).

## Tests

```sh
./tests/test_basic.sh        # syntax, parsing, feature checks + tests/test_core.py
python3 tests/test_core.py   # .grab v2 round trip, v1 compatibility, hostile names,
                             # install planning, bundle build/extract/restore (offline)
```

The migration path was tested end to end in a `cachyos/cachyos` container, as a
normal user with a sudo password, starting with no package databases, no Tk and
no paru:

- the bootstrap
- the GUI's install engine: repo and AUR packages (paru set up automatically),
  a fresh Homebrew install and formula, pipx, pip
- restoring home-directory and root-owned files
- the PATH setup

To repeat it:

```sh
docker run -d --name grabbit-test cachyos/cachyos sleep infinity
docker exec grabbit-test sh -c 'sed -i "/^\[options\]/a DisableSandbox" /etc/pacman.conf;
  useradd -m -G wheel tester; echo tester:test | chpasswd;
  echo "%wheel ALL=(ALL:ALL) ALL" > /etc/sudoers.d/wheel'
docker cp my-pc.grab.run grabbit-test:/home/tester/
docker exec -it -u tester -w /home/tester -e GRABBIT_BUNDLE_NO_GUI=1 grabbit-test \
  bash my-pc.grab.run --in-terminal
```

`GRABBIT_BUNDLE_NO_GUI=1` stops after unpacking and installing. `DisableSandbox`
is only needed because containers can't use pacman's network sandbox.

## License

MIT (same as the rack project)
