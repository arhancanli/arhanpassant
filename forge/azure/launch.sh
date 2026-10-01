#!/bin/bash
# Start the ArhanPassant self-play fleet on Azure.
#
# Uploads the engine source (git's tracked files at HEAD), the node script and
# a default control file to the forge storage container, then requests one
# 4-core ARM VM plus one 2-core spot VM in each region. Every VM builds the
# engine and streams self-play data back to the container (see node.sh).
#
# Needs ~/.arhanpassant/forge.env with BASE (container URL) and SAS.
# Stop the fleet with forge/azure/fleet-down.sh (keeps the stored data).

set -euo pipefail
cd "$(dirname "$0")/../.."
. ~/.arhanpassant/forge.env

FLEET_RG=${FLEET_RG:-arhanpassant-fleet}
REGIONS=(${REGIONS:-centralindia westus3 westus2 eastus2 northcentralus eastus mexicocentral canadacentral northeurope centralus uksouth francecentral})
IMAGE=Canonical:ubuntu-24_04-lts:server-arm64:latest

put() {
  curl -fsS -X PUT -H "x-ms-blob-type: BlockBlob" -H "x-ms-version: 2021-08-06" \
    --data-binary @"$2" "$BASE/$1?$SAS" -o /dev/null
  echo "uploaded $1"
}

tmp=$(mktemp -d)
git archive --format=tar.gz -o "$tmp/src.tar.gz" HEAD
put src/current.tar.gz "$tmp/src.tar.gz"
put bootstrap/node.sh forge/azure/node.sh
if ! curl -fsS "$BASE/control/node.env?$SAS" -o /dev/null 2>/dev/null; then
  printf 'SRC=src/current.tar.gz\nNET=\nNODES=5000\nHOURS=0.5\nTAG=gen0\nPAUSE=0\n' > "$tmp/node.env"
  put control/node.env "$tmp/node.env"
fi

python3 - "$tmp/cloud-init.yaml" <<'PY'
import os, sys
env = open(os.path.expanduser("~/.arhanpassant/forge.env")).read()
indent = lambda text, n: "".join(" " * n + line + "\n" for line in text.splitlines())
bootstrap = """#!/bin/bash
. /etc/arhanpassant.env
curl -fsS --retry 10 --retry-delay 10 "$BASE/bootstrap/node.sh?$SAS" -o /usr/local/bin/arhanpassant-node
chmod +x /usr/local/bin/arhanpassant-node
systemctl daemon-reload
systemctl enable --now arhanpassant-node
"""
unit = """[Unit]
Description=ArhanPassant self-play node
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/local/bin/arhanpassant-node
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
"""
doc = "#cloud-config\npackage_update: true\npackages: [build-essential, curl]\nwrite_files:\n"
doc += "  - path: /etc/arhanpassant.env\n    permissions: '0600'\n    content: |\n" + indent(env, 6)
doc += "  - path: /usr/local/sbin/ap-bootstrap.sh\n    permissions: '0700'\n    content: |\n" + indent(bootstrap, 6)
doc += "  - path: /etc/systemd/system/arhanpassant-node.service\n    content: |\n" + indent(unit, 6)
doc += "runcmd:\n  - [bash, /usr/local/sbin/ap-bootstrap.sh]\n"
open(sys.argv[1], "w").write(doc)
PY

az group create -n "$FLEET_RG" -l eastus --tags project=arhanpassant expires=2026-10-03 -o none
common=(-g "$FLEET_RG" --image "$IMAGE" --admin-username apforge --generate-ssh-keys
        --public-ip-sku Standard --nsg-rule NONE --os-disk-size-gb 30 --storage-sku StandardSSD_LRS
        --custom-data "$tmp/cloud-init.yaml" --no-wait -o none)
for r in "${REGIONS[@]}"; do
  az vm create -n "ap-$r" -l "$r" --size Standard_D4ps_v6 "${common[@]}" \
    && echo "requested ap-$r (4 cores)" || echo "FAILED ap-$r"
  az vm create -n "ap-$r-spot" -l "$r" --size Standard_D2ps_v6 --priority Spot \
    --eviction-policy Delete --max-price -1 "${common[@]}" \
    && echo "requested ap-$r-spot (2 cores, spot)" || echo "FAILED ap-$r-spot"
done
rm -rf "$tmp"
echo "Fleet requested. Check with: az vm list -g $FLEET_RG -d -o table"
