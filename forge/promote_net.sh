#!/bin/bash
# Promote a network that passed its SPRT: keep a versioned copy, make it the
# network compiled into the engine, rebuild, and print the new bench signature.
#
#   bash forge/promote_net.sh NET.nnue VERSION "SPRT summary"
set -euo pipefail
cd "$(dirname "$0")/.."
NET=$1; VERSION=$2; SUMMARY=$3
DATA=~/arhanpassant-data
cp "$NET" "$DATA/nets/champion-$VERSION.nnue"
cp "$NET" engine/nets/default.nnue
export PATH="$HOME/.cargo/bin:$PATH"
cargo build --release -p arhanpassant 2>&1 | tail -1
BENCH=$(./target/release/arhanpassant bench 2>&1 | tail -1 | awk '{print $1}')
cp target/release/arhanpassant "$DATA/bin/ap-net-$VERSION"
echo "bench $BENCH"
git add engine/nets/default.nnue
git commit -q -m "Network $VERSION: $SUMMARY

Bench: $BENCH nodes."
echo "committed $(git log --oneline -1)"
