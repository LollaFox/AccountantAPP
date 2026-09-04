#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=macos_paths.sh
source "$script_dir/macos_paths.sh"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This setup script is for macOS." >&2
  exit 1
fi

choose_python() {
  if [[ -n "${RECEIPT_SYNC_PYTHON:-}" ]]; then
    printf '%s\n' "$RECEIPT_SYNC_PYTHON"
    return 0
  fi
  local candidate
  for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)'; then
        command -v "$candidate"
        return 0
      fi
    fi
  done
  echo "Python 3.9 or newer is required. Install it with Xcode or Homebrew, then re-run." >&2
  return 1
}

base_python=$(choose_python)
venv_dir="$script_dir/.venv"
echo "Creating virtual environment with: $base_python"
"$base_python" -m venv "$venv_dir"
# shellcheck disable=SC1091
source "$venv_dir/bin/activate"
python -m pip install --upgrade pip
echo "Installing PaddlePaddle CPU (macOS)..."
python -m pip install "paddlepaddle==3.2.1" -i https://www.paddlepaddle.org.cn/packages/stable/cpu/
echo "Installing PaddleOCR 3.7.0..."
python -m pip install "paddleocr==3.7.0"
python -c "import paddleocr, paddle; print('paddle', paddle.__version__); print('paddleocr ready')"
echo
echo "macOS computer host is ready."
echo "Start with: $script_dir/start.sh"
echo "Or double-click start-service.command in the project root."
