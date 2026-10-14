#!/bin/sh
# Native macOS/Linux download installer. Does not require Python or read credentials.
set -eu
site=${MEGAS_INSTALL_SITE:-https://megasmoves.io}
site=${site%/}
preview=0
case "$site" in
  https://megasmoves.io) ;;
  http://127.0.0.1:*|http://localhost:*|"http://[::1]:"*)
    port=${site##*:}
    case "$port" in ''|*[!0-9]*) echo 'Invalid local preview origin.' >&2; exit 1 ;; esac
    preview=1 ;;
  *) echo 'Use the official HTTPS site or a local preview origin.' >&2; exit 1 ;;
esac
case "$(uname -s)-$(uname -m)" in
  Darwin-arm64) platform=macos-arm64 ;;
  Darwin-x86_64) platform=macos-amd64 ;;
  Linux-x86_64) platform=linux-amd64 ;;
  *) echo 'No verified native download for this operating system and architecture.' >&2; exit 1 ;;
esac
verify_only=0
case "${1:-}" in '' ) ;; --verify-only) verify_only=1 ;; *) echo 'Usage: install.sh [--verify-only]' >&2; exit 1 ;; esac
archive_tool=unzip
archive_kind=cli-zip
archive_suffix=zip
case "$platform" in linux-*) archive_tool=tar; archive_kind=cli-tar-gz; archive_suffix=tar.gz ;; esac
for utility in curl "$archive_tool" od tr wc mktemp; do
  command -v "$utility" >/dev/null 2>&1 || { echo "Required system utility missing: $utility" >&2; exit 1; }
done
if command -v sha256sum >/dev/null 2>&1; then hash_tool=sha256sum
elif command -v shasum >/dev/null 2>&1; then hash_tool=shasum
else echo 'A SHA-256 utility is required.' >&2; exit 1
fi
stage=$(mktemp -d "${TMPDIR:-/tmp}/MegasMoves-install.XXXXXX")
manifest="$stage/manifest.txt"
archive="$stage/download.zip"
binary="$stage/MegasMoves"
cleanup() { rm -f "$manifest" "$archive" "$binary"; rmdir "$stage" 2>/dev/null || true; }
trap cleanup EXIT HUP INT TERM
curl -fS --connect-timeout 15 --max-time 30 --max-filesize 16384 --max-redirs 0 "$site/install-manifests/$platform.txt" -o "$manifest"
format= scope= recorded_platform= version= filename= sha256= size= kind= executable= release_tag=
seen=' '
while IFS='=' read -r key value; do
  value=$(printf '%s' "$value" | tr -d '\r')
  case "$seen" in *" $key "*) echo 'Duplicate native manifest field.' >&2; exit 1 ;; esac
  seen="$seen$key "
  case "$key" in
    format) format=$value ;; scope) scope=$value ;; platform) recorded_platform=$value ;;
    version) version=$value ;; filename) filename=$value ;; sha256) sha256=$value ;;
    size_bytes) size=$value ;; kind) kind=$value ;; executable) executable=$value ;;
    release_tag) release_tag=$value ;;
    *) echo 'Unrecognized native manifest field.' >&2; exit 1 ;;
  esac
done < "$manifest"
expected_scope=public-release
[ "$preview" -eq 0 ] || expected_scope=local-development-preview
[ "$format" = megas-native-install-v1 ] && [ "$scope" = "$expected_scope" ] && [ "$recorded_platform" = "$platform" ] && [ "$kind" = "$archive_kind" ] && [ "$executable" = megasmoves ] || { echo 'Native release manifest is unavailable or invalid.' >&2; exit 1; }
case "$filename" in MegasMoves-"$platform"-*."$archive_suffix") ;; *) echo 'Invalid artifact filename.' >&2; exit 1 ;; esac
case "$filename" in *[!A-Za-z0-9._-]*) echo 'Invalid filename characters.' >&2; exit 1 ;; esac
case "$version" in ''|*[!A-Za-z0-9.-]*) echo 'Invalid version.' >&2; exit 1 ;; esac
case "$sha256" in *[!a-f0-9]*) echo 'Invalid checksum.' >&2; exit 1 ;; esac
[ "${#sha256}" -eq 64 ] || { echo 'Invalid checksum length.' >&2; exit 1; }
case "$size" in ''|*[!0-9]*) echo 'Invalid size.' >&2; exit 1 ;; esac
[ "$size" -ge 2 ] && [ "$size" -le 2147483648 ] || { echo 'Native download exceeds the size limit.' >&2; exit 1; }
download_base="$site/downloads"
[ "$preview" -eq 0 ] || download_base=http://127.0.0.1:3106/megasmoves/downloads
if [ -n "$release_tag" ]; then
  case "$release_tag" in *[!A-Za-z0-9._-]*) echo 'Invalid release tag.' >&2; exit 1 ;; esac
  [ "$preview" -eq 1 ] || download_base="https://github.com/Amretra-Dynamics/megasmoves/releases/download/$release_tag"
fi
echo "Downloading Megas Moves $version ($platform)..."
curl -fSL --proto-redir '=https' --connect-timeout 15 --max-time 600 --max-filesize "$size" --max-redirs 3 "$download_base/$filename" -o "$archive"
[ "$(wc -c < "$archive" | tr -d ' ')" = "$size" ] || { echo 'Download size does not match the manifest.' >&2; exit 1; }
if [ "$hash_tool" = sha256sum ]; then actual=$(sha256sum "$archive")
else actual=$(shasum -a 256 "$archive")
fi
actual=${actual%% *}
[ "$actual" = "$sha256" ] || { echo 'Download checksum does not match the manifest.' >&2; exit 1; }
# Extract a single named entry; never unpack arbitrary archive paths.
if [ "$archive_tool" = tar ]; then tar -xOzf "$archive" megasmoves > "$binary"
else unzip -p "$archive" megasmoves > "$binary"
fi
header=$(od -An -tx1 -N8 "$binary" | tr -d ' \n')
case "$platform:$header" in
  macos-arm64:cffaedfe0c000001|macos-amd64:cffaedfe07000001) ;;
  linux-amd64:7f454c460201*)
    machine=$(od -An -tx1 -j18 -N2 "$binary" | tr -d ' \n')
    [ "$machine" = 3e00 ] || { echo 'Linux binary is not x64.' >&2; exit 1; } ;;
  *) echo 'Native binary architecture does not match this device.' >&2; exit 1 ;;
esac
if [ "$verify_only" -eq 1 ]; then echo "Verified $filename; no installation performed."; exit 0; fi
install_bin="$HOME/.local/share/MegasMoves/bin"
command_bin="$HOME/.local/bin"
mkdir -p "$install_bin" "$command_bin"
chmod 755 "$binary"
# Update the same installation; preserve account data and a non-executable rollback file.
if [ -f "$install_bin/MegasMoves" ]; then cp "$install_bin/MegasMoves" "$install_bin/MegasMoves.previous"; chmod 600 "$install_bin/MegasMoves.previous"; fi
cp "$binary" "$install_bin/MegasMoves.new"
chmod 755 "$install_bin/MegasMoves.new"
mv -f "$install_bin/MegasMoves.new" "$install_bin/MegasMoves"
cat > "$command_bin/megas" <<'LAUNCHER'
#!/bin/sh
exec "$HOME/.local/share/MegasMoves/bin/MegasMoves" "$@"
LAUNCHER
chmod 755 "$command_bin/megas"
profile="$HOME/.profile"
case "$platform" in macos-*) profile="$HOME/.zprofile" ;; esac
marker='# Megas Moves terminal command'
if ! grep -F "$marker" "$profile" >/dev/null 2>&1; then
  printf '\n%s\n%s\n' "$marker" 'export PATH="$HOME/.local/bin:$PATH"' >> "$profile"
fi
echo 'Megas Moves native runtime is installed. Open a new terminal and run megas --help.'
