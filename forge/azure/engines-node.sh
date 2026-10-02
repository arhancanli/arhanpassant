#!/bin/bash
# Run on a forge VM (as root, through `az vm run-command`): matches against
# other open-source engines.
#
#   engines-node.sh build OPP[,OPP...]                    fetch and build opponents, check each speaks UCI
#   engines-node.sh play RUN NET TC GAMES OPP[,OPP...]    pause self-play, play GAMES against each opponent,
#                                                         upload results and games, resume self-play
#   engines-node.sh status RUN                            progress of a play run on this VM
#
# Opponents are named <engine>-<version>; recipes below. Results go to
# engines/RUN/<host>-<opponent>.json and .games.txt in the fleet container.
set -u
. /etc/arhanpassant.env
export HOME=/root
. /root/.cargo/env 2>/dev/null || true
WORK=/opt/arhanpassant
E=$WORK/engines
mkdir -p "$E"
# Keep a copy: the detached match job calls it to build opponents it is missing.
[ "$0" != "$E/engines-node.sh" ] && cp "$0" "$E/engines-node.sh" 2>/dev/null
url() { echo "$BASE/$1?$SAS"; }
put() {
  curl -fsS --retry 5 --retry-delay 10 -X PUT -H "x-ms-blob-type: BlockBlob" \
    -H "Content-Type: application/octet-stream" --data-binary "@$2" "$(url "$1")" -o /dev/null
}

# Build an engine from a GitHub tag tarball: make in the directory holding the
# makefile, then take the executable the build produced.
build_tag() {  # name repo tag subdir [make args...]
  local name=$1 repo=$2 tag=$3 sub=$4; shift 4
  local d=$E/$name
  rm -rf "$d" && mkdir -p "$d" && cd "$d" || return 1
  curl -fsSL "https://github.com/$repo/archive/refs/tags/$tag.tar.gz" -o src.tgz || return 1
  tar xzf src.tgz && rm src.tgz
  local top; top=$(ls -d */ | head -1)
  cd "$d/$top$sub" || return 1
  # Old releases meet newer compilers: warnings must not stop the build.
  find "$d" -iname 'makefile*' -exec sed -i 's/-Werror//g' {} +
  touch "$d/.stamp"
  make -j"$(nproc)" "$@" > "$d/build.log" 2>&1 || { tail -20 "$d/build.log"; return 1; }
  local exe; exe=$(find "$d" -type f -perm -u+x -newer "$d/.stamp" ! -name '*.o' ! -name '*.sh' ! -name '*.py' \
                   -exec sh -c 'head -c4 "$1" | grep -q ELF' _ {} \; -print | head -1)
  [ -n "$exe" ] && cp "$exe" "$d/engine"
}

fetch_bin() {  # name url [path inside tar]
  local name=$1 u=$2 inner=${3:-}
  local d=$E/$name
  rm -rf "$d" && mkdir -p "$d" && cd "$d" || return 1
  if [ -n "$inner" ]; then
    curl -fsSL "$u" -o pkg.tar && tar xf pkg.tar && cp "$inner" engine && rm -rf pkg.tar
  else
    curl -fsSL "$u" -o engine
  fi
  chmod +x engine
}

build_one() {
  case $1 in
    stockfish-17.1) fetch_bin "$1" https://github.com/official-stockfish/Stockfish/releases/download/sf_17.1/stockfish-ubuntu-x86-64-avx2.tar stockfish/stockfish-ubuntu-x86-64-avx2 ;;
    koivisto-9.0)   fetch_bin "$1" https://github.com/Luecx/Koivisto/releases/download/v9.0/Koivisto_9.0-linux-avx2-pgo ;;
    stash-*)        build_tag "$1" mhouppin/stash-bot "v${1#stash-}" src CC=gcc ;;
    weiss-*)        build_tag "$1" TerjeKir/weiss "v${1#weiss-}" src CC=gcc ;;
    ethereal-1[2-4]*) build_tag "$1" AndyGrant/Ethereal "$( [ "${1#ethereal-}" = 12.75 ] || [ "${1#ethereal-}" = 13.00 ] || [ "${1#ethereal-}" = 14.00 ] && echo v || echo V)${1#ethereal-}" src CC=gcc ;;
    ethereal-*)     build_tag "$1" AndyGrant/Ethereal "V${1#ethereal-}" src CC=gcc ;;
    laser-1.7)      build_tag "$1" jeffreyan11/laser-chess-engine v1.7 src ;;
    demolito-2021)  build_tag "$1" lucasart/Demolito 20211004 src CC=gcc pext ;;
    *) echo "no recipe for $1"; return 1 ;;
  esac
}

check_uci() {
  timeout 20 sh -c "printf 'uci\nisready\nquit\n' | '$1'" 2>/dev/null | grep -q '^uciok'
}

case "${1:-}" in
  build)
    IFS=, read -ra OPPS <<< "${2:?opponents}"
    for o in "${OPPS[@]}"; do
      if [ -x "$E/$o/engine" ] && check_uci "$E/$o/engine"; then echo "$o: ready"; continue; fi
      if build_one "$o" >/dev/null 2>&1 && check_uci "$E/$o/engine"; then echo "$o: built"; else echo "$o: FAILED"; tail -3 "$E/$o/build.log" 2>/dev/null; fi
    done
    ;;
  play)
    RUN=${2:?run}; NET=${3:?net}; TC=${4:?tc}; GAMES=${5:?games}; IFS=, read -ra OPPS <<< "${6:?opponents}"
    OPTS=${OPTS:-"opt.corr_np=128"}   # accepted settings that are not yet defaults in the fleet build
    R=$E/runs/$RUN; mkdir -p "$R"; cd "$R" || exit 1
    [ -f "$WORK/nets/$NET" ] || curl -fsS "$(url "nets/$NET")" -o "$WORK/nets/$NET" || { echo "no net $NET"; exit 1; }
    BOOK=$E/popularpos_lichess_v3.epd   # 200,000 positions common in Lichess games
    if [ ! -f "$BOOK" ]; then
      curl -fsSL --retry 6 --retry-delay 10 --retry-all-errors -o "$E/b.zip" https://github.com/official-stockfish/books/raw/master/popularpos_lichess_v3.epd.zip &&
        python3 -c "import zipfile; zipfile.ZipFile('$E/b.zip').extractall('$E')" && rm -f "$E/b.zip"
    fi
    [ -f "$BOOK" ] || { echo "no book"; exit 1; }
    [ -x "$WORK/src/target/release/arena" ] || (cd "$WORK/src" && cargo build --release -p arhanpassant-arena > "$R/arena-build.log" 2>&1)
    cat > "$R/run.sh" <<EOF
#!/bin/bash
. /etc/arhanpassant.env
url() { echo "\$BASE/\$1?\$SAS"; }
put() { curl -fsS --retry 5 --retry-delay 10 -X PUT -H "x-ms-blob-type: BlockBlob" -H "Content-Type: application/octet-stream" --data-binary "@\$2" "\$(url "\$1")" -o /dev/null; }
systemctl stop arhanpassant-node
for o in ${OPPS[*]}; do
  [ -x "$E/\$o/engine" ] || bash $E/engines-node.sh build "\$o" >> "$R/build.log" 2>&1
  [ -x "$E/\$o/engine" ] || continue
  $WORK/src/target/release/arena \\
    --engine name=arhanpassant cmd=$WORK/arhanpassant opt.EvalFile=$WORK/nets/$NET opt.Hash=64 $OPTS \\
    --engine name=\$o cmd=$E/\$o/engine opt.Hash=64 \\
    --tc $TC --book $BOOK --concurrency \$(nproc) --games $GAMES --seed \$RANDOM --quiet \\
    --out "$R/\$o.json" --games-out "$R/\$o.games.txt" > "$R/\$o.log" 2>&1
  put "engines/$RUN/\$(hostname)-\$o.json" "$R/\$o.json"
  put "engines/$RUN/\$(hostname)-\$o.games.txt" "$R/\$o.games.txt"
done
touch "$R/done"
systemctl start arhanpassant-node
EOF
    chmod +x "$R/run.sh"
    systemd-run --unit="ap-engines-$RUN" --collect "$R/run.sh" > /dev/null 2>&1 || { echo "systemd-run failed"; exit 1; }
    echo "$(hostname): run $RUN started vs ${OPPS[*]}, $GAMES games each at $TC"
    ;;
  status)
    R=$E/runs/${2:?run}
    for f in "$R"/*.log; do [ -f "$f" ] && echo "$(basename "$f" .log): $(tail -c 300 "$f" | tr '\r' '\n' | grep -v '^$' | tail -1)"; done
    [ -f "$R/done" ] && echo "done"
    ;;
  *) echo "usage: engines-node.sh build|play|status ..."; exit 1 ;;
esac
