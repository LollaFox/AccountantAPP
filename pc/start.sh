#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=macos_paths.sh
source "$script_dir/macos_paths.sh"

python_bin=$(receipt_sync_resolve_python)
data_dir="${RECEIPT_SYNC_DATA_DIR:-$(receipt_sync_default_data_dir)}"
model_cache="${RECEIPT_SYNC_MODEL_CACHE:-$(receipt_sync_default_model_cache)}"
port="${RECEIPT_SYNC_PORT:-8765}"
review_port="${RECEIPT_SYNC_REVIEW_PORT:-8764}"

certificate_directory="$data_dir/tls"
certificate_path="$certificate_directory/receipt-sync-cert.pem"
key_path="$certificate_directory/receipt-sync-key.pem"

if [[ ! -f "$certificate_path" || ! -f "$key_path" ]]; then
  "$script_dir/generate_certificate.sh" "$certificate_directory"
fi

export PYTHONUNBUFFERED=1

echo "Computer review: http://127.0.0.1:$review_port"
echo "iPhone HTTPS port: $port"
exec "$python_bin" "$script_dir/receipt_sync_server.py" \
  --data-dir "$data_dir" \
  --model-cache "$model_cache" \
  --host 0.0.0.0 \
  --port "$port" \
  --cert "$certificate_path" \
  --key "$key_path" \
  --review-port "$review_port"
