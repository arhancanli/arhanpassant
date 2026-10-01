#!/bin/bash
# Download new self-play chunks straight into the trainer's data folder:
# ~/arhanpassant-data/selfplay/fleet-<tag>/<chunk>.bin (no compressed copies kept).
# A manifest of finished chunks makes repeated runs cheap.
set -euo pipefail
. ~/.arhanpassant/forge.env
DATA=${DATA:-$HOME/arhanpassant-data}
ACCOUNT=$(echo "$BASE" | sed -E 's#https://([^.]+)\..*#\1#')
manifest="$DATA/forge/pulled.txt"
mkdir -p "$DATA/forge"
touch "$manifest"
count=0
for blob in $(az storage blob list -c forge --account-name "$ACCOUNT" --sas-token "$SAS" --prefix data/ --query "[].name" -o tsv); do
  grep -qxF "$blob" "$manifest" && continue
  tag=$(echo "$blob" | cut -d/ -f2)
  out="$DATA/selfplay/fleet-$tag/$(basename "$blob" .gz)"
  mkdir -p "$(dirname "$out")"
  if curl -fsS --retry 3 "$BASE/$blob?$SAS" | gunzip -c > "$out.part"; then
    mv "$out.part" "$out"
    echo "$blob" >> "$manifest"
    count=$((count + 1))
  else
    rm -f "$out.part"
    echo "failed: $blob" >&2
  fi
done
echo "$(date -u +%FT%TZ) pulled $count new chunks"
