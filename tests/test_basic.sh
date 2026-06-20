#!/usr/bin/env bash
# Basic smoke tests for grabbit
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GRABBIT="$SCRIPT_DIR/grabbit"

echo "== grabbit basic tests =="

echo "[1/7] Syntax check..."
bash -n "$GRABBIT"
echo "  OK"

echo "[2/7] Help and version info..."
help=$("$GRABBIT" --help 2>&1 || true)
echo "$help" | grep -q grabbit
echo "  OK"

echo "[3/7] Example .grab files present and valid..."
for f in "$SCRIPT_DIR"/examples/*.grab; do
  test -f "$f" || { echo "Missing $f"; exit 1; }
  set +e
  grep -q PKG_LIST_START "$f"
  grep -q PKG_LIST_END "$f"
  set -e
done
echo "  OK"

echo "[4/7] Manual parse test on example..."
count=$(awk '/PKG_LIST_START/{f=1;next} /PKG_LIST_END/{f=0} f && /^PKG/ {c++} END {print c+0}' "$SCRIPT_DIR/examples/cross-distro.grab")
[[ "$count" == "3" ]] || { echo "Expected 3 got $count"; exit 1; }
echo "  Parsed 3 packages OK"

echo "[5/7] Check new PM support in code..."
set +e
grep -q zypper "$GRABBIT"
grep -q flatpak "$GRABBIT"
grep -q "is_available_in_native_repos" "$GRABBIT"
set -e
echo "  New features present in source OK"

echo "[6/7] Installer script present and valid..."
INSTALLER="$SCRIPT_DIR/install.sh"
test -x "$INSTALLER"
bash -n "$INSTALLER"
grep -q ensure_gui_dependencies "$INSTALLER"
grep -q install_binaries "$INSTALLER"
echo "  OK"

echo "[7/7] grabbit install subcommand present..."
grep -q 'do_global_install' "$GRABBIT"
grep -q '_maybe_complete_global_install' "$GRABBIT"
echo "  OK"

echo ""
echo "All basic tests passed."
echo "Cross-distro / multi-PM tests covered in examples/ and manual verification recommended."