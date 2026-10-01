#!/bin/bash
# Fleet status: what each node last reported and how much data has arrived.
set -euo pipefail
. ~/.arhanpassant/forge.env
ACCOUNT=$(echo "$BASE" | sed -E 's#https://([^.]+)\..*#\1#')
echo "== nodes"
for b in $(az storage blob list -c forge --account-name "$ACCOUNT" --sas-token "$SAS" --prefix status/ --query "[].name" -o tsv); do
  curl -fsS "$BASE/$b?$SAS"
done
echo "== data"
az storage blob list -c forge --account-name "$ACCOUNT" --sas-token "$SAS" --prefix data/ \
  --query "[].[name, properties.contentLength]" -o tsv |
  awk -F'\t' '{split($1, p, "/"); n[p[2]]++; s[p[2]] += $2} END {for (t in n) printf "%s: %d chunks, %.1f MB compressed\n", t, n[t], s[t] / 1e6}'
