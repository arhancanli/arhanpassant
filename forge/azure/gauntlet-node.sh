#!/bin/bash
# Run on a forge VM (as root): pause self-play, play the champion against
# Stockfish at a fixed UCI_Elo, then resume self-play.
#   gauntlet-node.sh NET_BLOB_NAME LEVEL GAMES TC
set -u
NET=${1:?net}; LEVEL=${2:?level}; GAMES=${3:-160}; TC=${4:-10+0.1}
. /etc/arhanpassant.env
export HOME=/root
. /root/.cargo/env
G=/opt/arhanpassant/gauntlet
mkdir -p "$G" && cd "$G"
systemctl stop arhanpassant-node
(cd /opt/arhanpassant/src && cargo build --release -p arhanpassant-arena > "$G/build.log" 2>&1)
[ -f "$NET" ] || curl -fsS "$BASE/nets/$NET?$SAS" -o "$NET"
if [ ! -x stockfish/stockfish-linux-x86-64-universal ]; then
  curl -fsSL -o sf.tgz https://github.com/official-stockfish/Stockfish/releases/download/sf_19/stockfish-linux-x86-64-universal.tar.gz
  tar xzf sf.tgz
fi
if [ ! -f 2moves_v1.epd ]; then
  curl -fsSL -o b.zip https://github.com/official-stockfish/books/raw/master/2moves_v1.epd.zip
  python3 -c "import zipfile; zipfile.ZipFile('b.zip').extractall('.')"
fi
systemd-run --unit="ap-gauntlet-$LEVEL" --collect bash -c "
  /opt/arhanpassant/src/target/release/arena \
    --engine name=arhanpassant cmd=/opt/arhanpassant/arhanpassant opt.EvalFile=$G/$NET \
    --engine name=stockfish19-elo$LEVEL cmd=$G/stockfish/stockfish-linux-x86-64-universal opt.UCI_LimitStrength=true opt.UCI_Elo=$LEVEL \
    --tc $TC --book $G/2moves_v1.epd --concurrency 3 --games $GAMES --seed $LEVEL --quiet \
    --out $G/result-$LEVEL.json > $G/log-$LEVEL.txt 2>&1
  systemctl start arhanpassant-node"
echo "gauntlet $LEVEL started on $(hostname)"
