#!/usr/bin/env bash
set -Eeuo pipefail

REPO_URL="${BUZZMOO_REPO_URL:-https://github.com/ornab74/buzzmoo_cooks.git}"
APP_DIR="${BUZZMOO_COOKS_DIR:-$HOME/buzzmoo_cooks}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

say() {
  printf '\n\033[1;36m%s\033[0m\n' "$1"
}

warn() {
  printf '\n\033[1;33m%s\033[0m\n' "$1"
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1
}

sudo_if_needed() {
  if [ "${EUID:-$(id -u)}" -eq 0 ]; then
    "$@"
  else
    sudo "$@"
  fi
}

install_os_packages() {
  say "Installing system packages"
  if need_cmd apt-get; then
    sudo_if_needed apt-get update
    sudo_if_needed apt-get install -y python3 python3-venv python3-tk git curl ffmpeg
  elif need_cmd dnf; then
    sudo_if_needed dnf install -y python3 python3-tkinter git curl ffmpeg
  elif need_cmd pacman; then
    sudo_if_needed pacman -Sy --needed python tk git curl ffmpeg
  elif need_cmd zypper; then
    sudo_if_needed zypper install -y python3 python3-tk git curl ffmpeg
  else
    warn "Unknown Linux package manager. Install python3, python3-venv, tkinter, git, curl, and ffmpeg manually."
  fi
}

sync_repo() {
  say "Syncing Buzzmoo Cooks from $REPO_URL"
  if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" fetch --all --prune
    git -C "$APP_DIR" pull --ff-only
  elif [ -e "$APP_DIR" ]; then
    printf 'Target exists and is not a git repo: %s\n' "$APP_DIR" >&2
    exit 1
  else
    git clone "$REPO_URL" "$APP_DIR"
  fi
}

install_python_env() {
  say "Creating pinned Python environment"
  cd "$APP_DIR"
  "$PYTHON_BIN" -m venv .venv
  # shellcheck disable=SC1091
  . .venv/bin/activate
  python -m pip install --upgrade pip setuptools wheel
  python -m pip install --upgrade --pre -r requirements.txt
  python -m py_compile main.py
}

write_launchers() {
  say "Writing launchers"
  cat > "$APP_DIR/run.sh" <<'RUNNER'
#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")"
. .venv/bin/activate
python main.py
RUNNER
  chmod +x "$APP_DIR/run.sh"

  mkdir -p "$HOME/.local/share/applications"
  cat > "$HOME/.local/share/applications/buzzmoo-cooks.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Buzzmoo Cooks
Comment=Twitch stream to encrypted AI recipe vault
Exec=$APP_DIR/run.sh
Path=$APP_DIR
Terminal=false
Categories=AudioVideo;Utility;
DESKTOP
}

install_os_packages
sync_repo
install_python_env
write_launchers

say "Done"
printf 'Run it with:\n  %s/run.sh\n\n' "$APP_DIR"
printf 'Data vault defaults to:\n  %s/.superagent_data\n\n' "$APP_DIR"
