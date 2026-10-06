#!/bin/bash
# Put the newest engine build and champion network into the Lichess bot, then
# (re)start it unless the owner's STOPPED file is present.
#
#   bash forge/bot_update.sh BINARY NETWORK
set -euo pipefail
BOT=~/arhanpassant-lichess
BIN=$1; NET=$2
cp "$BIN" "$BOT/engines/arhanpassant.new" && mv "$BOT/engines/arhanpassant.new" "$BOT/engines/arhanpassant"
cp "$NET" "$BOT/engines/champion.nnue.new" && mv "$BOT/engines/champion.nnue.new" "$BOT/engines/champion.nnue"
printf 'uci\nisready\nquit\n' | "$BOT/engines/arhanpassant" | grep -E "^id name|readyok"
if [ -e "$BOT/STOPPED" ]; then
  echo "engine and network updated; bot left stopped ($BOT/STOPPED: $(cat "$BOT/STOPPED"))"
else
  bash "$BOT/bot.sh" restart
fi
