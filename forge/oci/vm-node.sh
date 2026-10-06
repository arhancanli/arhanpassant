#!/bin/bash
# ArhanPassant fleet node: build the engine at $BRANCH, then generate
# self-play data on every core and upload finished chunks to the bucket.
# A file ~/HOLD stops new work (manual jobs such as SPRT/SPSA run instead).
set -u
source ~/.cargo/env
BRANCH=${BRANCH:-engine/elo-20261006}
PAR=$(cat ~/par.txt)
HOST=$(hostname)
log() { echo "$(date -u +%FT%TZ) $*" >> ~/node.log; }
cd ~
if [ ! -d src ]; then git clone --depth 50 -b "$BRANCH" https://github.com/arhancanli/arhanpassant.git src; fi
cd src && git fetch --depth 50 origin "$BRANCH" && git checkout -q -B run "origin/$BRANCH"
REV=$(git rev-parse --short HEAD)
cargo build --release -p arhanpassant -p arena >> ~/build.log 2>&1 || { log "build failed at $REV"; sleep 600; exit 1; }
cp target/release/arhanpassant ~/ap-$REV && cp target/release/arena ~/arena 2>/dev/null
log "built $REV"
mkdir -p ~/data
upload_loop() {
  while true; do
    # A chunk nobody has written to for 10 minutes is complete (datagen appends a game every few seconds).
    find ~/data -name '*.bin' -mmin +10 | sort | while read -r f; do
      [ -e "$f.up" ] && continue
      if curl -fsS -X PUT --data-binary @"$f" "$PAR""selfplay/$HOST/$(basename "$f")" -o /dev/null; then
        touch "$f.up"; log "uploaded $(basename "$f")"
      fi
    done
    sleep 300
  done
}
upload_loop &
THREADS=$(nproc)
while true; do
  if [ -e ~/HOLD ]; then sleep 60; continue; fi
  log "datagen $THREADS threads with $REV"
  ~/ap-$REV datagen --threads "$THREADS" --nodes 8000 --seed $(od -An -N4 -tu4 /dev/urandom | tr -d ' ') \
    --out ~/data --positions-per-file 250000 --hours 6 \
    --set corr_joint=1 --set corr_cont=128 >> ~/datagen.log 2>&1
done
