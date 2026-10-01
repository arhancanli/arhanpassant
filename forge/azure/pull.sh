#!/bin/bash
# Download new self-play chunks and unpack them where the trainer looks:
# ~/arhanpassant-data/selfplay/fleet-<tag>/<chunk>.bin
set -euo pipefail
. ~/.arhanpassant/forge.env
DATA=${DATA:-$HOME/arhanpassant-data}
ACCOUNT=$(echo "$BASE" | sed -E 's#https://([^.]+)\..*#\1#')
inbox="$DATA/inbox"
mkdir -p "$inbox"
az storage blob download-batch --source forge --destination "$inbox" --pattern 'data/*' \
  --account-name "$ACCOUNT" --sas-token "$SAS" --overwrite false --max-connections 8 -o none
count=0
for gz in $(find "$inbox/data" -name '*.bin.gz' 2>/dev/null); do
  tag=$(basename "$(dirname "$gz")")
  out="$DATA/selfplay/fleet-$tag/$(basename "$gz" .gz)"
  [ -f "$out" ] && continue
  mkdir -p "$(dirname "$out")"
  gunzip -c "$gz" > "$out.part" && mv "$out.part" "$out"
  count=$((count + 1))
done
echo "unpacked $count new chunks"
du -sh "$DATA"/selfplay/fleet-* 2>/dev/null || true
