#!/bin/bash
# Add one self-play VM in a region (the code is already in blob storage).
#   forge/azure/add-region.sh eastus Standard_D4as_v7 x64 [--spot]
set -euo pipefail
cd "$(dirname "$0")"
region=$1; size=$2; arch=${3:-x64}; spot=${4:-}
image=Canonical:ubuntu-24_04-lts:server:latest
[ "$arch" = arm64 ] && image=Canonical:ubuntu-24_04-lts:server-arm64:latest
ci=$(mktemp)
python3 cloud-init.py "$ci"
name="ap-$region-$(echo "$size" | tr '[:upper:]_' '[:lower:]-' | sed 's/standard-//')"
extra=()
[ "$spot" = "--spot" ] && { name="$name-spot"; extra=(--priority Spot --eviction-policy Delete --max-price -1); }
az vm create -g "${FLEET_RG:-arhanpassant-fleet}" -n "$name" -l "$region" --size "$size" --image "$image" \
  --admin-username apforge --generate-ssh-keys --public-ip-sku Standard --nsg-rule NONE \
  --os-disk-size-gb 30 --storage-sku StandardSSD_LRS --custom-data "$ci" ${extra[@]+"${extra[@]}"} --no-wait -o none
rm -f "$ci"
echo "requested $name"
