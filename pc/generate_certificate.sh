#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <output-directory>" >&2
  exit 1
fi

output_directory=$1
mkdir -p "$output_directory"

certificate_path="$output_directory/receipt-sync-cert.pem"
key_path="$output_directory/receipt-sync-key.pem"
fingerprint_path="$output_directory/receipt-sync-sha256.txt"
config_path="$output_directory/.receipt-sync-openssl.cnf"

hostname_short=$(scutil --get LocalHostName 2>/dev/null || hostname -s 2>/dev/null || echo "")
san_dns="DNS:localhost"
if [[ "$hostname_short" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]]; then
  san_dns="$san_dns,DNS:$hostname_short"
fi

cat > "$config_path" <<EOF
[req]
distinguished_name = req_distinguished_name
x509_extensions = v3_req
prompt = no

[req_distinguished_name]
CN = Receipt Sync

[v3_req]
basicConstraints = CA:FALSE
keyUsage = digitalSignature
extendedKeyUsage = serverAuth
subjectAltName = ${san_dns},IP:127.0.0.1
EOF

openssl genrsa -out "$key_path" 3072
openssl req -x509 -new -key "$key_path" -sha256 -days 1825 -batch \
  -config "$config_path" -extensions v3_req \
  -subj "/CN=Receipt Sync" \
  -out "$certificate_path"
rm -f "$config_path"

fingerprint=$(
  openssl x509 -in "$certificate_path" -outform DER \
    | shasum -a 256 \
    | awk '{print toupper($1)}'
)
printf '%s\n' "$fingerprint" > "$fingerprint_path"
chmod 600 "$key_path"
chmod 644 "$certificate_path" "$fingerprint_path"
echo "HTTPS certificate created. SHA-256: $fingerprint"
