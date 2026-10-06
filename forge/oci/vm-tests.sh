#!/bin/bash
# Test runner for a fleet VM: runs ~/queue/*.sh one at a time (oldest name
# first). Each job calls `sprt NAME TC ELO0 ELO1` with CAND_OPTS / BASE_OPTS
# (and optionally CAND / BASE binaries); results land in ~/elo/NAME and are
# uploaded to the bucket under elo/<host>/NAME/.
set -u
PAR=$(cat ~/par.txt)
HOST=$(hostname)
BOOK=~/src/tools/books/UHO_4060_v4.epd
mkdir -p ~/queue ~/done ~/elo
latest() { ls -t ~/ap-* 2>/dev/null | head -1; }

sprt() {
  local name=$1 tc=$2 e0=$3 e1=$4
  local cand=${CAND:-$(latest)} base=${BASE:-$(latest)} conc=${CONC:-$(( $(nproc) - 1 ))}
  local dir=~/elo/$name
  mkdir -p "$dir"
  echo "start $(date -u +%FT%TZ) $name tc=$tc sprt=[$e0,$e1] conc=$conc cand=$cand base=$base cand_opts='${CAND_OPTS:-}' base_opts='${BASE_OPTS:-}'" >> "$dir/run.log"
  ~/arena --engine name=cand cmd="$cand" opt.Hash=16 opt.Threads=1 ${COMMON:-} ${CAND_OPTS:-} \
          --engine name=base cmd="$base" opt.Hash=16 opt.Threads=1 ${COMMON:-} ${BASE_OPTS:-} \
          --tc "$tc" --book "$BOOK" --concurrency "$conc" --games "${MAXGAMES:-8000}" --sprt "$e0,$e1" \
          --seed $RANDOM --out "$dir/result.json" --games-out "$dir/games.jsonl" >> "$dir/run.log" 2>&1
  echo "complete $(date -u +%FT%TZ)" >> "$dir/run.log"
  for f in result.json run.log; do
    curl -fsS -X PUT --data-binary @"$dir/$f" "$PAR""elo/$HOST/$name/$f" -o /dev/null
  done
}

while true; do
  next=$(ls ~/queue/*.sh 2>/dev/null | sort | head -1)
  if [ -z "$next" ]; then sleep 20; continue; fi
  base=$(basename "$next")
  mv "$next" ~/done/"$base"
  echo "$(date -u +%FT%TZ) start $base" >> ~/tests.log
  ( source ~/done/"$base" )
  echo "$(date -u +%FT%TZ) done $base" >> ~/tests.log
done
