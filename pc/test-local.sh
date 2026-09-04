#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=macos_paths.sh
source "$script_dir/macos_paths.sh"

python_bin=$(receipt_sync_resolve_python)
test_data="${RECEIPT_SYNC_TEST_DATA:-$HOME/Library/Application Support/ReceiptSync-LocalTest}"
model_cache="${RECEIPT_SYNC_MODEL_CACHE:-$(receipt_sync_default_model_cache)}"

export PYTHONUNBUFFERED=1

echo "Local review page: http://127.0.0.1:8764"
echo "Test data: $test_data"
echo "Keep this window open while testing. Press Ctrl+C to stop."
exec "$python_bin" "$script_dir/receipt_sync_server.py" \
  --data-dir "$test_data" \
  --model-cache "$model_cache" \
  --host 127.0.0.1 \
  --port 8764 \
  --allow-insecure-http
