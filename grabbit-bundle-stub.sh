#!/usr/bin/env bash
# grabbit migration bundle — self-installing.
#
# Double-click this file (it must be marked executable: Properties → Permissions)
# or run:  bash <this-file>
#
# It opens a terminal, installs what grabbit needs (Python + Tk; asks for your
# password there), unpacks itself to ~/.local/share/grabbit/bundles/, installs
# grabbit to ~/.local/bin, and opens the grabbit GUI with the bundled package
# list so you can choose what to restore.
#
# Everything after the marker line at the bottom is a tar.gz payload.
set -euo pipefail

SELF="$(readlink -f "$0")"
MARKER="__GRABBIT_PAYLOAD_BELOW__"

# ── no terminal (double-clicked)? reopen in one so prompts and progress are visible
if [[ "${1:-}" != "--in-terminal" && ! -t 0 ]]; then
  for term in "${TERMINAL:-}" konsole gnome-terminal kgx ptyxis xfce4-terminal mate-terminal \
              tilix kitty alacritty wezterm foot xterm x-terminal-emulator; do
    [[ -n "$term" ]] && command -v "$term" >/dev/null 2>&1 || continue
    case "$term" in
      gnome-terminal|kgx|ptyxis|tilix) exec "$term" -- bash "$SELF" --in-terminal ;;
      xfce4-terminal|mate-terminal)    exec "$term" -x bash "$SELF" --in-terminal ;;
      wezterm)                         exec wezterm start -- bash "$SELF" --in-terminal ;;
      *)                               exec "$term" -e bash "$SELF" --in-terminal ;;
    esac
  done
  if command -v kdialog >/dev/null 2>&1; then
    kdialog --error "No terminal emulator found. Run this from a terminal:  bash '$SELF'"
  elif command -v zenity >/dev/null 2>&1; then
    zenity --error --text="No terminal emulator found. Run this from a terminal:  bash '$SELF'"
  fi
  exit 1
fi
[[ "${1:-}" == "--in-terminal" ]] && shift

if [[ -t 1 ]]; then
  B="\033[1m" C="\033[1;36m" G="\033[1;32m" Y="\033[1;33m" R="\033[1;31m" N="\033[0m"
else
  B="" C="" G="" Y="" R="" N=""
fi
step() { echo -e "\n${C}::${N} ${B}$*${N}"; }
ok()   { echo -e "   ${G}✔${N} $*"; }
warn() { echo -e "   ${Y}⚠${N}  $*"; }
fail() {
  echo -e "\n${R}✖ $*${N}" >&2
  read -r -p "Press Enter to close…" _ </dev/tty || true
  exit 1
}
trap 'fail "Unexpected error on line $LINENO"' ERR

echo -e "${B}grabbit migration bundle${N}  ($(basename "$SELF"))"

# ── distro + GUI dependencies
. /etc/os-release 2>/dev/null || true
if command -v pacman >/dev/null; then PM=pacman; DEPS=(python tk)
elif command -v apt-get >/dev/null; then PM=apt; DEPS=(python3 python3-tk)
elif command -v dnf >/dev/null; then PM=dnf; DEPS=(python3 python3-tkinter)
elif command -v zypper >/dev/null; then PM=zypper; DEPS=(python3 python3-tk)
elif command -v apk >/dev/null; then PM=apk; DEPS=(python3 py3-tkinter)
else fail "No supported package manager found (pacman/apt/dnf/zypper/apk)."
fi
ok "${PRETTY_NAME:-${NAME:-Linux}} ($PM)"

have_tk() { command -v python3 >/dev/null && python3 -c 'import tkinter' >/dev/null 2>&1; }
if [[ "$PM" == pacman ]]; then
  # Always sync here: the GUI checks what the repos offer, and a fresh install's
  # databases can be stale or missing. -Syu, never -Sy alone (partial upgrades).
  step "Updating the system and installing Python + Tk — enter your password if asked"
  sudo pacman -Syu --needed --noconfirm "${DEPS[@]}" || fail "pacman failed (see above)"
elif have_tk; then
  ok "Python + Tk already present"
else
  step "Installing Python + Tk (${DEPS[*]}) — enter your password if asked"
  case "$PM" in
    apt)    sudo apt-get update && sudo apt-get install -y "${DEPS[@]}" ;;
    dnf)    sudo dnf install -y "${DEPS[@]}" ;;
    zypper) sudo zypper --non-interactive install "${DEPS[@]}" ;;
    apk)    sudo apk add "${DEPS[@]}" ;;
  esac
fi
have_tk || fail "Python with Tk is still not available."
ok "Python + Tk ready"

# ── unpack
DEST="${XDG_DATA_HOME:-$HOME/.local/share}/grabbit/bundles/$(basename "$SELF" .run)-$(date +%Y%m%d-%H%M%S)"
step "Unpacking to $DEST"
mkdir -p "$DEST" && chmod 700 "$DEST"   # may hold sign-ins
line=$(awk -v m="$MARKER" '$0 == m { print NR + 1; exit }' "$SELF")
[[ -n "$line" ]] || fail "Payload marker missing — the file is damaged."
tail -n +"$line" "$SELF" | tar -xz -C "$DEST" || fail "Could not unpack the payload (damaged or incomplete copy?)"
ok "Unpacked ($(du -sh "$DEST" | cut -f1))"

# ── install grabbit itself (CLI + GUI + menu entry); deps were handled above
step "Installing grabbit to ~/.local/bin"
bash "$DEST/app/install.sh" --skip-deps >/dev/null || fail "grabbit install failed"
ok "grabbit installed"

# ── open the GUI with the bundle (GRABBIT_BUNDLE_NO_GUI=1: stop here, for tests)
if [[ -n "${GRABBIT_BUNDLE_NO_GUI:-}" ]]; then ok "Unpacked to $DEST (GUI skipped)"; exit 0; fi
step "Opening the grabbit GUI"
nohup setsid python3 "$DEST/app/grabbit_gui.py" --bundle "$DEST" >"$DEST/gui.log" 2>&1 < /dev/null &
sleep 2
if kill -0 $! 2>/dev/null; then
  ok "GUI started. Choose what to restore there; it will ask for your password once."
  echo -e "\n   This window can be closed."
  read -r -t 30 -p "   (closes by itself in 30 s, or press Enter) " _ </dev/tty || true
else
  cat "$DEST/gui.log" >&2 || true
  fail "The GUI did not start (log above)."
fi
exit 0
__GRABBIT_PAYLOAD_BELOW__
