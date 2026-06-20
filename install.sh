#!/usr/bin/env bash
# grabbit global installer — deploys CLI/GUI and installs distro-specific dependencies.
#
# Usage:
#   ./install.sh                  Install to ~/.local/bin (default)
#   INSTALL_DIR=/usr/local/bin ./install.sh
#   ./install.sh --skip-deps      Binaries only (no apt/pacman/dnf packages)
#   ./install.sh --skip-desktop   Skip .desktop file installation
#
# rack: when a release has no binary asset, rack runs this script from the source archive.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
INSTALL_DIR="${INSTALL_DIR:-${RACK_DIR:-$HOME/.local/bin}}"
INSTALL_DIR="${INSTALL_DIR/#\~/$HOME}"
GITHUB_RAW="${GRABBIT_GITHUB_RAW:-https://raw.githubusercontent.com/Arrowstorm-Technologies-LLC/grabbit/main}"
MARKER="$HOME/.config/grabbit/install-ok"

SKIP_DEPS=false
SKIP_DESKTOP=false

for arg in "$@"; do
  case "$arg" in
    --skip-deps) SKIP_DEPS=true ;;
    --skip-desktop) SKIP_DESKTOP=true ;;
    -h|--help)
      sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
  esac
done

if [[ -t 1 ]]; then
  BOLD="\033[1m"; DIM="\033[2m"; GREEN="\033[1;32m"
  YELLOW="\033[1;33m"; CYAN="\033[1;36m"; RED="\033[1;31m"; RESET="\033[0m"
else
  BOLD="" DIM="" GREEN="" YELLOW="" CYAN="" RED="" RESET=""
fi

step() { echo -e "\n${CYAN}::${RESET} ${BOLD}$*${RESET}"; }
info() { echo -e "   ${DIM}$*${RESET}"; }
ok()   { echo -e "   ${GREEN}✔${RESET} $*"; }
warn() { echo -e "   ${YELLOW}⚠${RESET}  $*"; }
die()  { echo -e "\n${RED}✖ error:${RESET} $*" >&2; exit 1; }

detect_distro() {
  if [[ -f /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
  fi

  DISTRO_ID="${ID:-unknown}"
  DISTRO_NAME="${NAME:-Unknown}"

  case "$DISTRO_ID" in
    debian|ubuntu|linuxmint|pop|elementary|kali|raspbian)
      FAMILY="debian"; PM="apt"
      GUI_PKGS=(python3 python3-tk)
      ;;
    arch|manjaro|endeavouros|garuda|artix)
      FAMILY="arch"; PM="pacman"
      GUI_PKGS=(python tk)
      ;;
    fedora|centos|rhel|rocky|almalinux)
      FAMILY="fedora"; PM="dnf"
      GUI_PKGS=(python3 python3-tkinter)
      ;;
    opensuse*|suse)
      FAMILY="suse"; PM="zypper"
      GUI_PKGS=(python3 python3-tk)
      ;;
    alpine)
      FAMILY="alpine"; PM="apk"
      GUI_PKGS=(python3 py3-tkinter)
      ;;
    *)
      if command -v apt >/dev/null 2>&1; then
        FAMILY="debian"; PM="apt"
        GUI_PKGS=(python3 python3-tk)
      elif command -v pacman >/dev/null 2>&1; then
        FAMILY="arch"; PM="pacman"
        GUI_PKGS=(python tk)
      elif command -v dnf >/dev/null 2>&1; then
        FAMILY="fedora"; PM="dnf"
        GUI_PKGS=(python3 python3-tkinter)
      elif command -v zypper >/dev/null 2>&1; then
        FAMILY="suse"; PM="zypper"
        GUI_PKGS=(python3 python3-tk)
      elif command -v apk >/dev/null 2>&1; then
        FAMILY="alpine"; PM="apk"
        GUI_PKGS=(python3 py3-tkinter)
      else
        die "Unsupported or unknown Linux distribution: $DISTRO_NAME ($DISTRO_ID)"
      fi
      ;;
  esac

  info "Detected: $DISTRO_NAME ($FAMILY / $PM)"
}

pm_pkg_installed() {
  local pkg="$1"
  case "$PM" in
    apt)    dpkg -s "$pkg" >/dev/null 2>&1 ;;
    pacman) pacman -Qi "$pkg" >/dev/null 2>&1 ;;
    dnf)    rpm -q "$pkg" >/dev/null 2>&1 ;;
    zypper) rpm -q "$pkg" >/dev/null 2>&1 ;;
    apk)    apk info -e "$pkg" >/dev/null 2>&1 ;;
    *) return 1 ;;
  esac
}

install_missing_packages() {
  local -a needed=()
  local pkg
  for pkg in "$@"; do
    if pm_pkg_installed "$pkg"; then
      info "$pkg already installed"
    else
      needed+=("$pkg")
    fi
  done

  if [[ ${#needed[@]} -eq 0 ]]; then
    ok "All distro GUI packages present"
    return 0
  fi

  step "Installing distro packages: ${needed[*]}"
  case "$PM" in
    apt)
      sudo apt update
      sudo apt install -y "${needed[@]}"
      ;;
    pacman) sudo pacman -S --needed --noconfirm "${needed[@]}" ;;
    dnf)    sudo dnf install -y "${needed[@]}" ;;
    zypper) sudo zypper install -y "${needed[@]}" ;;
    apk)    sudo apk add "${needed[@]}" ;;
  esac
  ok "Installed: ${needed[*]}"
}

find_python_with_tk() {
  local candidate
  for candidate in python3 /usr/bin/python3; do
    if command -v "$candidate" >/dev/null 2>&1 \
        && "$candidate" -c "import tkinter" >/dev/null 2>&1; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  return 1
}

ensure_gui_dependencies() {
  detect_distro
  step "Checking GUI dependencies for $FAMILY"
  install_missing_packages "${GUI_PKGS[@]}"

  local python
  if ! python="$(find_python_with_tk)"; then
    die "Tkinter still unavailable after installing ${GUI_PKGS[*]}. Install Tk manually for your distro."
  fi
  ok "Python with tkinter: $python"

  if "$python" -c "import tkinterdnd2" >/dev/null 2>&1; then
    ok "tkinterdnd2 already available (drag-and-drop enabled)"
    return 0
  fi

  step "Installing optional drag-and-drop support (tkinterdnd2)"
  if "$python" -m pip --version >/dev/null 2>&1; then
    if "$python" -m pip install --user tkinterdnd2 >/dev/null 2>&1; then
      ok "Installed tkinterdnd2 via pip"
      return 0
    fi
    warn "pip install tkinterdnd2 failed — GUI will work without drag-and-drop"
    return 0
  fi

  local pip_pkg=""
  case "$PM" in
    apt)    pip_pkg="python3-pip" ;;
    pacman) pip_pkg="python-pip" ;;
    dnf)    pip_pkg="python3-pip" ;;
    zypper) pip_pkg="python3-pip" ;;
    apk)    pip_pkg="py3-pip" ;;
  esac

  if [[ -n "$pip_pkg" ]] && ! pm_pkg_installed "$pip_pkg"; then
    install_missing_packages "$pip_pkg"
  fi

  if "$python" -m pip install --user tkinterdnd2 >/dev/null 2>&1; then
    ok "Installed tkinterdnd2 via pip"
  else
    warn "Could not install tkinterdnd2 — GUI will work without drag-and-drop"
  fi
}

fetch_or_copy() {
  local name="$1" dest="$2"
  if [[ -f "$SCRIPT_DIR/$name" ]]; then
    cp "$SCRIPT_DIR/$name" "$dest"
  else
    curl -fsSL "$GITHUB_RAW/$name" -o "$dest"
  fi
}

install_binaries() {
  step "Installing grabbit binaries to $INSTALL_DIR"
  mkdir -p "$INSTALL_DIR"

  for name in grabbit grabbit-gui grabbit_gui.py install.sh; do
    fetch_or_copy "$name" "$INSTALL_DIR/$name"
    info "Installed $name"
  done

  chmod +x "$INSTALL_DIR/install.sh"

  chmod +x "$INSTALL_DIR/grabbit" "$INSTALL_DIR/grabbit-gui"
  ok "CLI and GUI launchers installed"
}

install_desktop_entry() {
  local apps_dir="$HOME/.local/share/applications"
  step "Installing desktop entry"
  mkdir -p "$apps_dir"
  if [[ -f "$SCRIPT_DIR/grabbit-gui.desktop" ]]; then
    cp "$SCRIPT_DIR/grabbit-gui.desktop" "$apps_dir/"
  else
    curl -fsSL "$GITHUB_RAW/grabbit-gui.desktop" -o "$apps_dir/grabbit-gui.desktop"
  fi
  if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$apps_dir" >/dev/null 2>&1 || true
  fi
  ok "Desktop entry installed (search for grabbit-gui in your app menu)"
}

write_marker() {
  mkdir -p "$(dirname "$MARKER")"
  date '+%Y-%m-%d %H:%M:%S' >"$MARKER"
}

main() {
  echo -e "${BOLD}grabbit installer${RESET}"

  install_binaries

  if [[ "$SKIP_DEPS" == false ]]; then
    ensure_gui_dependencies
  else
    warn "Skipping dependency installation (--skip-deps)"
  fi

  if [[ "$SKIP_DESKTOP" == false ]]; then
    install_desktop_entry
  fi

  write_marker

  echo ""
  ok "grabbit installed successfully"
  info "Ensure $INSTALL_DIR is in your PATH"
  info "Run: grabbit save ~/my-setup.grab  or  grabbit-gui"
}

main "$@"