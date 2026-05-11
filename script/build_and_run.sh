#!/usr/bin/env bash
set -euo pipefail

APP_NAME="Anki Card Updater"
ENTRYPOINT="AnkiCardGen_LiquidGlass.py"
VENV_DIR=".venv-build"
DIST_DIR="dist"
APP_BUNDLE="$DIST_DIR/$APP_NAME.app"

usage() {
  printf 'Usage: %s [--build-only] [--verify]\n' "$0"
}

BUILD_ONLY=0
VERIFY=0
for arg in "$@"; do
  case "$arg" in
    --build-only) BUILD_ONLY=1 ;;
    --verify) VERIFY=1 ;;
    -h|--help) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
done

cd "$(dirname "$0")/.."

if pgrep -x "$APP_NAME" >/dev/null 2>&1; then
  pkill -x "$APP_NAME" || true
  sleep 1
fi

if [ ! -x "$VENV_DIR/bin/python" ]; then
  python3 -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/python" -m pip install --disable-pip-version-check -r requirements.txt

rm -rf "$APP_BUNDLE"

PYINSTALLER_ARGS=(
  --noconfirm
  --clean
  --windowed
  --name "$APP_NAME"
  --osx-bundle-identifier "com.local.ankicardupdater"
  --collect-all flet
  --collect-all flet_desktop
)

"$VENV_DIR/bin/python" -m PyInstaller "${PYINSTALLER_ARGS[@]}" "$ENTRYPOINT"

PLIST="$APP_BUNDLE/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleDisplayName $APP_NAME" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Set :LSMinimumSystemVersion 13.0" "$PLIST" 2>/dev/null || true

if [ "$BUILD_ONLY" -eq 1 ]; then
  printf 'Built %s\n' "$APP_BUNDLE"
  exit 0
fi

/usr/bin/open -n "$APP_BUNDLE"

if [ "$VERIFY" -eq 1 ]; then
  sleep 3
  pgrep -x "$APP_NAME" >/dev/null
  printf 'Launched %s\n' "$APP_NAME"
fi
