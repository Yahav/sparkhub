#!/bin/sh
# certbot --manual-auth-hook for a one-off DNS-01 certificate (port 80 blocked, 2026-10-23).
# Writes the TXT value where it can be read (data/acme-dns-challenge.txt), then waits until DNS serves it.
OUT="$(cd "$(dirname "$0")/../.." && pwd)/data/acme-dns-challenge.txt"
printf '_acme-challenge.%s TXT %s\n' "$CERTBOT_DOMAIN" "$CERTBOT_VALIDATION" > "$OUT"
chmod 644 "$OUT"
i=0
while [ $i -lt 180 ]; do            # 180 x 10 s = 30 min
  # the server room blocks some outbound DNS at times; ask several resolvers, any one is enough
  for R in @8.8.8.8 @ns01.domaincontrol.com @ns02.domaincontrol.com ""; do
    if dig +short +time=3 +tries=1 $R "_acme-challenge.$CERTBOT_DOMAIN" TXT 2>/dev/null | grep -qF "$CERTBOT_VALIDATION"; then
      echo "TXT record visible via ${R:-system resolver}" >> "$OUT"
      sleep 30                       # let both GoDaddy nameservers serve it
      exit 0
    fi
  done
  i=$((i + 1)); sleep 10
done
echo "timed out waiting for the TXT record" >> "$OUT"
exit 1
