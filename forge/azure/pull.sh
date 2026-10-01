#!/bin/bash
# Download self-play chunks that are not yet on this machine.
set -euo pipefail
. ~/.arhanpassant/forge.env
DEST=${DEST:-$HOME/arhanpassant-data}
ACCOUNT=$(echo "$BASE" | sed -E 's#https://([^.]+)\..*#\1#')
mkdir -p "$DEST"
az storage blob download-batch --source forge --destination "$DEST" --pattern 'data/*' \
  --account-name "$ACCOUNT" --sas-token "$SAS" --overwrite false --max-connections 8 -o none
du -sh "$DEST"/data/* 2>/dev/null
