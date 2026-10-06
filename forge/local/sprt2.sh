#!/bin/bash
# sprt.sh NAME CAND_BIN BASE_BIN TC ELO0 ELO1 CONC [extra engine opts for both...]
# Candidate-only options: CAND_OPTS="opt.x=1 ..." ; baseline-only: BASE_OPTS="..."
set -u
NAME=$1; CAND=$2; BASE=$3; TC=$4; E0=$5; E1=$6; CONC=$7; shift 7
DIR=~/arhanpassant-data/elo/$NAME
mkdir -p "$DIR"
COMMON="opt.Hash=16 opt.Threads=1 $*"
echo "start $(date -u +%FT%TZ) $NAME tc=$TC sprt=[$E0,$E1] conc=$CONC cand=$CAND base=$BASE common='$COMMON' cand_opts='${CAND_OPTS:-}' base_opts='${BASE_OPTS:-}'" >> "$DIR/run.log"
~/arhanpassant-data/bin/arena \
  --engine name=cand cmd="$CAND" $COMMON ${CAND_OPTS:-} \
  --engine name=base cmd="$BASE" $COMMON ${BASE_OPTS:-} \
  --tc "$TC" --book ~/arhanpassant/tools/books/UHO_4060_v4.epd --concurrency "$CONC" \
  --games ${MAXGAMES:-8000} --sprt "$E0,$E1" --seed $RANDOM --out "$DIR/result.json" --games-out "$DIR/games.jsonl" >> "$DIR/run.log" 2>&1
echo "complete $(date -u +%FT%TZ)" >> "$DIR/run.log"
