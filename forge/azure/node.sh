#!/bin/bash
# ArhanPassant forge node.
#
# Builds the engine from the source tarball in blob storage, then generates
# self-play training data in fixed-length chunks and uploads each chunk.
# A small control file in the same container steers every node without
# logging in: which network to play with, nodes per move, data tag, pause.
#
# Reads BASE (container URL) and SAS (token, no leading '?') from
# /etc/arhanpassant.env. Runs under systemd with Restart=always.

set -u
. /etc/arhanpassant.env

WORK=/opt/arhanpassant
LOG=/var/log/arhanpassant-node.log
HOST=$(hostname)
export HOME=/root
mkdir -p "$WORK"
cd "$WORK" || exit 1

log() { echo "$(date -u +%FT%TZ) $*" >> "$LOG"; }
url() { echo "$BASE/$1?$SAS"; }
fetch() { curl -fsS --retry 5 --retry-delay 5 "$(url "$1")" -o "$2"; }
put() {
  curl -fsS --retry 5 --retry-delay 10 -X PUT \
    -H "x-ms-blob-type: BlockBlob" -H "x-ms-version: 2021-08-06" \
    --data-binary @"$2" "$(url "$1")" > /dev/null
}

if [ ! -x "$HOME/.cargo/bin/cargo" ]; then
  log "installing rust"
  curl -fsS https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain stable >> "$LOG" 2>&1
fi
. "$HOME/.cargo/env"

build() {
  local tarball=$1
  log "building $tarball"
  rm -rf src && mkdir src
  fetch "$tarball" src.tar.gz || return 1
  tar -xzf src.tar.gz -C src || return 1
  (cd src && RUSTFLAGS="-C target-cpu=native" cargo build --release -p arhanpassant >> "$LOG" 2>&1) || return 1
  cp src/target/release/arhanpassant ./arhanpassant.new && mv ./arhanpassant.new ./arhanpassant
  echo "$tarball" > built-from
  ./arhanpassant bench 8 | tail -1 > bench.txt
  log "built: $(cat bench.txt)"
}

# Play one batch of a running distributed gate (control/gate.json) and upload
# its result; returns non-zero when no gate is running or the batch failed.
gate_batch() {
  fetch control/gate.json gate.json 2>/dev/null || return 1
  local fields
  fields=$(python3 -c "import json; g = json.load(open('gate.json')); print(g.get('status', ''), g['id'], g['candidate'], g['champion'], g['tc'], g.get('pairs', 16))" 2>/dev/null) || return 1
  read -r status gid cand champ gtc pairs <<< "$fields"
  [ "$status" = "running" ] || return 1
  mkdir -p nets
  for n in "$cand" "$champ"; do
    [ -f "nets/$(basename "$n")" ] || fetch "$n" "nets/$(basename "$n")" || return 1
  done
  if [ ! -f book.epd ]; then
    curl -fsSL -o book.zip https://github.com/official-stockfish/books/raw/master/UHO_4060_v4.epd.zip || return 1
    python3 -c "import zipfile; z = zipfile.ZipFile('book.zip'); open('book.epd', 'wb').write(z.read(z.namelist()[0]))" || return 1
  fi
  if [ ! -x src/target/release/arena ]; then
    (cd src && cargo build --release -p arhanpassant-arena >> "$LOG" 2>&1) || return 1
  fi
  local conc=$(( $(nproc) > 1 ? $(nproc) - 1 : 1 ))
  local seed
  seed=$(od -An -N4 -tu4 /dev/urandom | tr -d ' ')
  log "gate $gid batch: $pairs pairs, concurrency $conc"
  src/target/release/arena \
    --engine name=candidate cmd=./arhanpassant "opt.EvalFile=nets/$(basename "$cand")" \
    --engine name=champion cmd=./arhanpassant "opt.EvalFile=nets/$(basename "$champ")" \
    --tc "$gtc" --book book.epd --concurrency "$conc" --games $((2 * pairs)) --seed "$seed" \
    --quiet --out gate-batch.json >> "$LOG" 2>&1 || return 1
  put "gates/$gid/$HOST-$(date -u +%Y%m%dT%H%M%S).json" gate-batch.json || return 1
  return 0
}

chunk=0
while true; do
  # Self-update: run the newest node script from storage at each chunk boundary.
  if fetch bootstrap/node.sh node.sh.new 2>/dev/null && ! cmp -s node.sh.new /usr/local/bin/arhanpassant-node \
      && bash -n node.sh.new; then
    install -m 755 node.sh.new /usr/local/bin/arhanpassant-node
    log "node script updated; restarting it"
    exec /usr/local/bin/arhanpassant-node
  fi
  # Defaults, then whatever the control file says.
  SRC=src/current.tar.gz; NET=; NODES=5000; HOURS=0.5; TAG=gen0; PAUSE=0
  if fetch control/node.env control.env 2>/dev/null; then
    # shellcheck disable=SC1091
    . ./control.env
  fi
  if [ "$PAUSE" = "1" ]; then
    log "paused"
    sleep 120
    continue
  fi
  if [ ! -x ./arhanpassant ] || [ "$(cat built-from 2>/dev/null)" != "$SRC" ]; then
    build "$SRC" || { log "build failed"; sleep 120; continue; }
  fi
  # A running gate takes priority over self-play.
  if gate_batch; then
    continue
  fi
  NETARG=()
  if [ -n "$NET" ]; then
    if [ ! -f "nets/$(basename "$NET")" ]; then
      mkdir -p nets
      fetch "$NET" "nets/$(basename "$NET").part" && mv "nets/$(basename "$NET").part" "nets/$(basename "$NET")" \
        || { log "net fetch failed"; sleep 60; continue; }
    fi
    NETARG=(--net "nets/$(basename "$NET")")
  fi
  chunk=$((chunk + 1))
  seed=$(od -An -N8 -tu8 /dev/urandom | tr -d ' ')
  out="chunk-$chunk"
  rm -rf "$out"
  log "chunk $chunk tag=$TAG nodes=$NODES net=${NET:-hce} seed=$seed"
  ./arhanpassant datagen --threads "$(nproc)" --hours "$HOURS" --nodes "$NODES" \
    --seed "$seed" --out "$out" "${NETARG[@]}" >> "$LOG" 2>&1
  name="data/$TAG/$HOST-$(date -u +%Y%m%dT%H%M%S)-$chunk.bin.gz"
  cat "$out"/*.bin | gzip -1 > upload.bin.gz
  bytes=$(stat -c %s upload.bin.gz)
  if put "$name" upload.bin.gz; then
    log "uploaded $name ($bytes bytes)"
    rm -rf "$out" upload.bin.gz
  else
    log "upload failed for $name; keeping $out"
  fi
  printf '{"host":"%s","chunk":%d,"tag":"%s","net":"%s","bench":"%s","updated":"%s"}\n' \
    "$HOST" "$chunk" "$TAG" "${NET:-hce}" "$(cat bench.txt 2>/dev/null)" "$(date -u +%FT%TZ)" > status.json
  put "status/$HOST.json" status.json || true
done
