#!/bin/bash
# Profile-guided build of the engine: an instrumented build runs a short
# benchmark and self-play, and the release build is optimised for that profile
# (about 4% more nodes per second on the Mac). The search itself is unchanged,
# so the bench node count matches the plain build's.
#
#   bash forge/pgo_build.sh OUT
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=$1
export PATH="$HOME/.cargo/bin:$PATH"
PROFDATA=$(ls "$(rustc --print sysroot)"/lib/rustlib/*/bin/llvm-profdata 2>/dev/null | head -1)
if [ -z "$PROFDATA" ]; then
  echo "llvm-profdata missing: rustup component add llvm-tools" >&2
  exit 1
fi
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
FLAGS=${RUSTFLAGS:-}
RUSTFLAGS="$FLAGS -Cprofile-generate=$WORK/raw" CARGO_TARGET_DIR=target/pgo-gen cargo build -q --release -p arhanpassant
target/pgo-gen/release/arhanpassant bench 13 > /dev/null
target/pgo-gen/release/arhanpassant datagen --threads 2 --nodes 5000 --seed 7 --out "$WORK/selfplay" --hours 0.005 \
  --set corr_joint=1 --set corr_cont=128 > /dev/null 2>&1
"$PROFDATA" merge -o "$WORK/merged.profdata" "$WORK/raw"
RUSTFLAGS="$FLAGS -Cprofile-use=$WORK/merged.profdata" CARGO_TARGET_DIR=target/pgo-use cargo build -q --release -p arhanpassant
cp target/pgo-use/release/arhanpassant "$OUT"
