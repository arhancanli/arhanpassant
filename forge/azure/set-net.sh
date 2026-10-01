#!/bin/bash
# Point the whole self-play fleet at a network: upload it and update the
# control file. Each node picks it up at the start of its next chunk.
#
#   forge/azure/set-net.sh ~/arhanpassant-data/nets/champion-0.2.0.nnue gen1 [nodes]

set -euo pipefail
. ~/.arhanpassant/forge.env

net=${1:?usage: set-net.sh NET.nnue TAG [NODES]}
tag=${2:?usage: set-net.sh NET.nnue TAG [NODES]}
nodes=${3:-5000}
name=$(basename "$net")

put() {
  curl -fsS -X PUT -H "x-ms-blob-type: BlockBlob" -H "x-ms-version: 2021-08-06" \
    --data-binary @"$2" "$BASE/$1?$SAS" -o /dev/null
  echo "uploaded $1"
}

head -c 4 "$net" | grep -q APNN || { echo "$net is not an ArhanPassant network" >&2; exit 1; }
put "nets/$name" "$net"
tmp=$(mktemp)
printf 'SRC=src/current.tar.gz\nNET=nets/%s\nNODES=%s\nHOURS=0.5\nTAG=%s\nPAUSE=0\n' "$name" "$nodes" "$tag" > "$tmp"
put control/node.env "$tmp"
rm -f "$tmp"
echo "Fleet will use $name for tag $tag from each node's next chunk."
