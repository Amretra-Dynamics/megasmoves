#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$SCRIPT_DIR/MegasMoves.GtkApp"
BUILD_DIR="$APP_DIR/build"

echo "=== MegasMoves Linux GUI Operator ==="

# Set WSLg display variables if running inside WSL
if [ -d "/mnt/wslg" ]; then
    export DISPLAY="${DISPLAY:-:0}"
    export PULSE_SERVER="${PULSE_SERVER:-/mnt/wslg/PulseServer}"
    export GDK_BACKEND="x11"
    mkdir -p /mnt/shared_memory 2>/dev/null || true
    mount -t tmpfs tmpfs /mnt/shared_memory 2>/dev/null || true
fi

# Check dependencies
missing_pkgs=()
for pkg in cmake pkg-config g++ libgtk-4-dev libadwaita-1-dev; do
    if ! dpkg -s "$pkg" >/dev/null 2>&1; then
        missing_pkgs+=("$pkg")
    fi
done

if [ ${#missing_pkgs[@]} -ne 0 ]; then
    echo "Missing required build packages: ${missing_pkgs[*]}"
    echo "Please install them by running:"
    echo "  sudo apt update && sudo apt install -y ${missing_pkgs[*]}"
    exit 1
fi

echo "Building megasmoves_gtk..."
mkdir -p "$BUILD_DIR"
cd "$BUILD_DIR"
cmake ..
cmake --build . -j$(nproc)

echo "Launching MegasMoves Linux GUI..."
exec ./megasmoves_gtk "$@"
