#!/bin/bash
# Runs queued test scripts one at a time, oldest name first. Waits while a
# test match (an arena writing under elo/) is running; SPSA batches don't count.
Q=~/arhanpassant-data/elo/queue; D=~/arhanpassant-data/elo/done
while true; do
  if pgrep -f "^/Users/arhancanli/arhanpassant-data/bin/arena .*/arhanpassant-data/elo/" > /dev/null; then sleep 15; continue; fi
  next=$(ls "$Q"/*.sh 2>/dev/null | sort | head -1)
  if [ -z "$next" ]; then sleep 15; continue; fi
  base=$(basename "$next")
  mv "$next" "$D/$base"
  echo "$(date -u +%FT%TZ) start $base" >> ~/arhanpassant-data/elo/runner.log
  bash "$D/$base"
  echo "$(date -u +%FT%TZ) done $base" >> ~/arhanpassant-data/elo/runner.log
done
