#!/usr/bin/env bash
# Local SparkHub CA and a server certificate signed by it. ./install.sh runs this; by hand:
#   ops/tls/make-local-cert.sh NAME_OR_IP...       (the first DNS name is the subject; IPs become IP entries)
# Output in data/tls/: ca.crt (give this to clients), ca.key (never leaves the Spark), server.crt, server.key.
# Re-run to renew the server certificate (the CA is kept, so clients keep trusting it).
set -euo pipefail
[ $# -ge 1 ] || { echo "usage: $0 NAME_OR_IP..." >&2; exit 2; }
D="$(cd "$(dirname "$0")/../.." && pwd)/data/tls"
mkdir -p "$D"; chmod 700 "$D"; cd "$D"
SAN=""; CN=""
for n in "$@"; do
  if [[ $n =~ ^[0-9.]+$ || $n == *:* ]]; then SAN="$SAN,IP:$n"; else SAN="$SAN,DNS:$n"; [ -n "$CN" ] || CN="$n"; fi
done
SAN="${SAN#,}"; CN="${CN:-localhost}"
if [ ! -f ca.key ]; then
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 825 \
    -subj "/CN=SparkHub local CA ($(hostname -s))" -keyout ca.key -out ca.crt \
    -addext "basicConstraints=critical,CA:TRUE,pathlen:0" -addext "keyUsage=critical,keyCertSign,cRLSign"
fi
openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -subj "/CN=$CN" -keyout server.key -out server.csr
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -days 180 -out server.crt -extfile <(printf '%s\n' \
  "basicConstraints=CA:FALSE" "keyUsage=critical,digitalSignature" "extendedKeyUsage=serverAuth" \
  "subjectAltName=$SAN")
rm -f server.csr; chmod 600 ca.key server.key; chmod 644 ca.crt server.crt
openssl x509 -in server.crt -noout -subject -enddate -ext subjectAltName
