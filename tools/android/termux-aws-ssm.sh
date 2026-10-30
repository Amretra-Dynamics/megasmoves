#!/data/data/com.termux/files/usr/bin/bash
# Megas Moves Android bootstrap: AWS SSM shell + localhost XRDP tunnel.
# Run in the official Termux app; credentials stay on the phone.
set -eo pipefail
umask 077

ID="$MEGAS_INSTANCE_ID"
REGION="ap-south-2"
PORT="50001"
if [ -n "$MEGAS_REGION" ]; then REGION="$MEGAS_REGION"; fi
if [ -n "$MEGAS_LOCAL_PORT" ]; then PORT="$MEGAS_LOCAL_PORT"; fi
if ! [[ "$ID" =~ ^i-[0-9a-f]{8,17}$ ]]; then
  echo "Set MEGAS_INSTANCE_ID to the authorized AWS instance ID." >&2; exit 2
fi
if ! [[ "$REGION" =~ ^[a-z]{2}-[a-z]+-[0-9]+$ ]]; then
  echo "Invalid AWS region." >&2; exit 2
fi
if ! [[ "$PORT" =~ ^[0-9]+$ ]] || [ "$PORT" -lt 1024 ] || [ "$PORT" -gt 65535 ]; then
  echo "Invalid local RDP port." >&2; exit 2
fi
if [ "$(uname -m)" != "aarch64" ]; then
  echo "This setup requires 64-bit ARM Android (aarch64)." >&2; exit 2
fi
if ! command -v pkg >/dev/null 2>&1 || ! command -v proot-distro >/dev/null 2>&1; then
  echo "Preparing Termux dependencies..."
  pkg update -y
  pkg install -y curl ca-certificates coreutils proot-distro
fi
if ! proot-distro list --quiet | grep -Fxq megas-ubuntu; then
  echo "Installing isolated Ubuntu 24.04 on Android..."
  proot-distro install ubuntu:24.04 --name megas-ubuntu
fi

echo "Setting up AWS CLI + Session Manager inside Ubuntu..."
proot-distro login megas-ubuntu -- /bin/bash -s -- "$ID" "$REGION" "$PORT" <<'UBUNTU'
set -eo pipefail
umask 077
ID="$1"; REGION="$2"; PORT="$3"
if [ "$(dpkg --print-architecture)" != "arm64" ]; then
  echo "Ubuntu container must be ARM64." >&2; exit 2
fi
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl unzip less groff
INSTALL_AWS=1
if command -v aws >/dev/null 2>&1; then
  VERSION="$(aws --version 2>&1 || true)"
  MINOR="$(printf '%s' "$VERSION" | sed -n 's/.*aws-cli\/2\.\([0-9][0-9]*\)\..*/\1/p')"
  if [ -n "$MINOR" ] && [ "$MINOR" -ge 32 ]; then INSTALL_AWS=0; fi
fi
if [ "$INSTALL_AWS" -eq 1 ]; then
  TMP="$(mktemp -d)"
  curl -fL --retry 3 -sS https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip -o "$TMP/aws.zip"
  (cd "$TMP" && unzip -q aws.zip && \
    if [ -d /usr/local/aws-cli/v2/current ]; then \
      ./aws/install --bin-dir /usr/local/bin --install-dir /usr/local/aws-cli --update; \
    else ./aws/install --bin-dir /usr/local/bin --install-dir /usr/local/aws-cli; fi)
  rm -rf "$TMP"
fi
if ! command -v session-manager-plugin >/dev/null 2>&1; then
  TMP="$(mktemp -d)"
  curl -fL --retry 3 -sS https://s3.amazonaws.com/session-manager-downloads/plugin/latest/ubuntu_arm64/session-manager-plugin.deb -o "$TMP/ssm.deb"
  apt-get install -y -qq "$TMP/ssm.deb"
  rm -rf "$TMP"
fi
command -v aws >/dev/null
command -v session-manager-plugin >/dev/null
printf 'ID=%q\nREGION=%q\nPORT=%q\n' "$ID" "$REGION" "$PORT" > /etc/megas-ssm.conf
chmod 600 /etc/megas-ssm.conf
cat > /usr/local/bin/megas-ssm <<'MEGAS'
#!/usr/bin/env bash
set -eo pipefail
umask 077
. /etc/megas-ssm.conf
PROFILE=megas-ssm-rdp-hyd
ACTION="$1"
export AWS_PAGER=""
if [ "$ACTION" = "login" ]; then
  exec aws login --remote --profile "$PROFILE" --region "$REGION"
fi
if [ "$ACTION" = "sso" ]; then
  aws configure sso --profile "$PROFILE"
  exec aws sso login --use-device-code --profile "$PROFILE"
fi
if ! aws sts get-caller-identity --profile "$PROFILE" --region "$REGION" >/dev/null 2>&1; then
  echo "AWS login required. Complete the sign-in URL/code on your Android browser."
  if ! aws login --remote --profile "$PROFILE" --region "$REGION"; then
    echo "AWS console sign-in did not complete. IAM Identity Center users: run megassso." >&2
    echo "Your AWS identity needs ssm:StartSession permission for the selected instance." >&2
    exit 4
  fi
  aws sts get-caller-identity --profile "$PROFILE" --region "$REGION" >/dev/null
fi
case "$ACTION" in
  rdp)
    if (echo >/dev/tcp/127.0.0.1/"$PORT") 2>/dev/null; then
      echo "Local port $PORT is already in use. Check the existing tunnel." >&2; exit 5
    fi
    echo "Starting secure AWS SSM RDP tunnel: Android 127.0.0.1:$PORT -> Ubuntu 127.0.0.1:3389"
    echo "Keep this Termux session running, then open Windows App: PC 127.0.0.1:$PORT, user devone."
    exec aws ssm start-session --target "$ID" --region "$REGION" \
      --profile "$PROFILE" --document-name AWS-StartPortForwardingSession \
      --parameters "portNumber=3389,localPortNumber=$PORT"
    ;;
  shell)
    echo "Opening AWS SSM shell on your authorized Ubuntu instance..."
    exec aws ssm start-session --target "$ID" --region "$REGION" --profile "$PROFILE"
    ;;
  *)
    echo "Unknown mode." >&2; exit 2
    ;;
esac
MEGAS
chmod 700 /usr/local/bin/megas-ssm
echo "AWS CLI: $(aws --version 2>&1)"
echo "Session Manager plugin: $(command -v session-manager-plugin)"
UBUNTU

mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/megas" <<'TERMUX'
#!/data/data/com.termux/files/usr/bin/bash
set -eo pipefail
MODE=rdp
case "$0" in
  */megassh) MODE=shell ;;
  */megaslogin) MODE=login ;;
  */megassso) MODE=sso ;;
esac
if [ "$MODE" = rdp ] && command -v termux-wake-lock >/dev/null 2>&1; then
  termux-wake-lock || true
  trap 'termux-wake-unlock 2>/dev/null || true' EXIT
fi
proot-distro login megas-ubuntu -- /usr/local/bin/megas-ssm "$MODE"
TERMUX
chmod 700 "$HOME/.local/bin/megas"
ln -sf megas "$HOME/.local/bin/megassh"
ln -sf megas "$HOME/.local/bin/megaslogin"
ln -sf megas "$HOME/.local/bin/megassso"
if ! grep -Fq '# Megas Moves Termux commands' "$HOME/.bashrc" 2>/dev/null; then
  printf '\n# Megas Moves Termux commands\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$HOME/.bashrc"
fi
echo "Installed commands: megas (RDP), megassh (shell), megaslogin (AWS login), megassso (IAM Identity Center)."
echo "Launching your AWS SSM RDP tunnel..."
"$HOME/.local/bin/megas"
