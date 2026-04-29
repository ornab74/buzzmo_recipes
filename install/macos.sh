#!/usr/bin/env bash
set -Eeuo pipefail

REPO_URL="${BUZZMOO_REPO_URL:-https://github.com/ornab74/buzzmoo_cooks.git}"
APP_DIR="${BUZZMOO_COOKS_DIR:-$HOME/buzzmoo_cooks}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

say() {
  printf '\n\033[1;36m%s\033[0m\n' "$1"
}

ensure_homebrew() {
  if command -v brew >/dev/null 2>&1; then
    return
  fi
  say "Installing Homebrew"
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  if [ -x /opt/homebrew/bin/brew ]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
  elif [ -x /usr/local/bin/brew ]; then
    eval "$(/usr/local/bin/brew shellenv)"
  fi
}

install_os_packages() {
  say "Installing macOS packages"
  ensure_homebrew
  brew update
  brew install git ffmpeg python@3.12 tcl-tk
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
  if command -v python3.12 >/dev/null 2>&1; then
    PYTHON_BIN="python3.12"
  fi
  "$PYTHON_BIN" -m venv .venv
  # shellcheck disable=SC1091
  . .venv/bin/activate
  python -m pip install --upgrade pip setuptools wheel
  python -m pip install --upgrade --pre -r requirements.txt
  python -m py_compile main.py
}

write_launchers() {
  say "Writing launchers"
  cat > "$APP_DIR/run.command" <<'RUNNER'
#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")"
. .venv/bin/activate
python main.py
RUNNER
  chmod +x "$APP_DIR/run.command"
}

install_os_packages
sync_repo
install_python_env
write_launchers

say "Done"
printf 'Run it with:\n  open "%s/run.command"\n\n' "$APP_DIR"
printf 'Data vault defaults to:\n  %s/.superagent_data\n\n' "$APP_DIR"
