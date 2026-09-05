#!/usr/bin/env bash
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
status=0
./ios/install-iphone.sh "$@" || status=$?
echo
read -r -p "Press Enter to close. " || true
exit "$status"
