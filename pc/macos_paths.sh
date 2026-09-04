# Shared macOS paths and Python lookup. Sourced by the Mac launchers; not executed directly.

receipt_sync_default_data_dir() {
  printf '%s\n' "${HOME}/Library/Application Support/ReceiptSync"
}

receipt_sync_default_model_cache() {
  printf '%s\n' "${HOME}/Library/Application Support/ReceiptSync/paddle_models"
}

receipt_sync_resolve_python() {
  if [[ -n "${RECEIPT_SYNC_PYTHON:-}" ]]; then
    if [[ ! -x "$RECEIPT_SYNC_PYTHON" ]]; then
      echo "RECEIPT_SYNC_PYTHON is not executable: $RECEIPT_SYNC_PYTHON" >&2
      return 1
    fi
    printf '%s\n' "$RECEIPT_SYNC_PYTHON"
    return 0
  fi

  local script_dir
  script_dir=$(cd "$(dirname "${BASH_SOURCE[1]}")" && pwd)
  local venv_python="$script_dir/.venv/bin/python"
  if [[ -x "$venv_python" ]]; then
    printf '%s\n' "$venv_python"
    return 0
  fi

  if command -v python3 >/dev/null 2>&1; then
    echo "Warning: $venv_python not found. The review page can start, but receipt OCR needs $script_dir/setup_macos.sh" >&2
    command -v python3
    return 0
  fi

  echo "PaddleOCR Python was not found." >&2
  echo "Run: $script_dir/setup_macos.sh" >&2
  echo "Or set RECEIPT_SYNC_PYTHON to a Python that has paddleocr installed." >&2
  return 1
}
