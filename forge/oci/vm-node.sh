#!/bin/bash
# ArhanPassant fleet node: build the engine at $BRANCH, then generate
# self-play data on every core and upload finished chunks to the bucket.
# A file ~/HOLD stops new work (manual jobs such as SPRT/SPSA run instead).
# Self-play runs at idle priority (SCHED_IDLE): it only gets cycles nothing else
# wants, so a Lichess bot on the same VM always has the CPU first and self-play
# fills whatever the bot leaves idle (no cores are reserved).
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
cargo build --release -p arhanpassant -p arhanpassant-arena >> ~/build.log 2>&1 || { log "build failed at $REV"; sleep 600; exit 1; }
cp target/release/arhanpassant ~/ap-$REV && cp target/release/arena ~/arena 2>/dev/null
# Use the profile-guided build of the same code when it builds and searches identically (same bench nodes).
# It is built as ~/pgo-$REV, outside the ~/ap-* names that the bot and the test runner pick up.
if bash forge/pgo_build.sh ~/pgo-$REV >> ~/build.log 2>&1 \
   && [ "$(~/pgo-$REV bench 2>&1 | tail -1 | awk '{print $1}')" = "$(~/ap-$REV bench 2>&1 | tail -1 | awk '{print $1}')" ]; then
  mv ~/pgo-$REV ~/ap-$REV
  log "built $REV (profile-guided)"
else
  rm -f ~/pgo-$REV
  log "built $REV"
fi
mkdir -p ~/data/$REV
upload_loop() {
  while true; do
    # A chunk nobody has written to for 10 minutes is complete (datagen appends a game every few seconds).
    # Objects go under selfplay/<host>/<build>/ so each network's data stays apart.
    find ~/data -name '*.bin' -mmin +10 | sort | while read -r f; do
      [ -e "$f.up" ] && continue
      rel=${f#$HOME/data/}
      if curl -fsS -X PUT --data-binary @"$f" "$PAR""selfplay/$HOST/$rel" -o /dev/null; then
        touch "$f.up"; log "uploaded $rel"
      fi
    done
    sleep 300
  done
}
upload_loop &
# Search per self-play move: 5,000 nodes gives 60% more positions than 8,000 for 5% noisier targets.
NODES=$(cat ~/DATAGEN_NODES 2>/dev/null || echo 5000)
THREADS=$(nproc)
while true; do
  if [ -e ~/HOLD ]; then sleep 60; continue; fi
  log "datagen $THREADS threads at idle priority, $NODES nodes, with $REV"
  chrt --idle 0 ~/ap-$REV datagen --threads "$THREADS" --nodes "$NODES" --seed $(od -An -N4 -tu4 /dev/urandom | tr -d ' ') \
    --out ~/data/$REV --positions-per-file 250000 --hours 6 \
    --set corr_joint=1 --set corr_cont=128 >> ~/datagen.log 2>&1
done
