#!/bin/bash
# Ship a network that passed the gate as a new engine version.
#
#   forge/promote.sh NET.nnue VERSION SPRT.json "change description"
#
# Embeds the network, bumps the version, runs the tests and perft suite,
# rebuilds the WebAssembly engine for the site, records the promotion in both
# ledgers and commits. Pushing and tagging are left as printed commands.

set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.cargo/bin:$PATH"

net=${1:?usage: promote.sh NET.nnue VERSION SPRT.json CHANGE}
version=${2:?version}
sprt=${3:?sprt result json}
change=${4:?change description}
web=${WEB:-$HOME/arhanpassant-web}

head -c 4 "$net" | grep -q APNN || { echo "$net is not an ArhanPassant network" >&2; exit 1; }
python3 - "$sprt" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
assert r["decision"] == "H1", f"gate decision is {r['decision']}, not H1"
print(f"gate passed: {r['games']} games, elo {r['elo']:+.1f} [{r['elo_lo']:+.1f}, {r['elo_hi']:+.1f}], llr {r['sprt']['llr']:.2f}")
PY

mkdir -p engine/nets
cp "$net" engine/nets/default.nnue
sed -i '' -E "s/^version = \"[0-9.]+\"/version = \"$version\"/" Cargo.toml
grep -q "^version = \"$version\"" Cargo.toml

cargo build --release --workspace
cargo test --release --workspace
cargo test --release -p arhanpassant --test perft -- --ignored
echo "uci" | ./target/release/arhanpassant | grep -q "EvalFile type string default <embedded>"
bench=$(./target/release/arhanpassant bench | tail -1)
echo "bench: $bench"

cargo build --release -p arhanpassant-wasm --target wasm32-unknown-unknown
cp target/wasm32-unknown-unknown/release/arhanpassant_wasm.wasm "$web/public/engine/arhanpassant.wasm"
# The same engine with WebAssembly SIMD, about twice as fast; the site's worker picks it when the browser supports it.
RUSTFLAGS="-C target-feature=+simd128" CARGO_TARGET_DIR=target/wasm-simd cargo build --release -p arhanpassant-wasm --target wasm32-unknown-unknown
cp target/wasm-simd/wasm32-unknown-unknown/release/arhanpassant_wasm.wasm "$web/public/engine/arhanpassant-simd.wasm"

python3 - "$sprt" "$version" "$change" "$web/src/data/ledger.json" forge/ledger.json <<'PY'
import datetime, json, sys
sprt, version, change, *ledgers = sys.argv[1:]
r = json.load(open(sprt))
entry = {
    "version": version,
    "date": datetime.date.today().isoformat(),
    "change": change,
    "status": "promoted",
    "games": r["games"],
    "elo": round(r["elo"], 1),
    "eloLo": round(r["elo_lo"], 1),
    "eloHi": round(r["elo_hi"], 1),
    "llr": round(r["sprt"]["llr"], 2),
}
for path in ledgers:
    led = json.load(open(path))
    led["versions"] = [v for v in led["versions"] if v["version"] != version] + [entry]
    with open(path, "w") as f:
        json.dump(led, f, indent=2)
        f.write("\n")
print("ledgers updated")
PY

git add engine/nets/default.nnue Cargo.toml Cargo.lock forge/ledger.json
git commit -q -m "v$version: $change"
(cd "$web" && git add public/engine/arhanpassant.wasm public/engine/arhanpassant-simd.wasm src/data/ledger.json && git commit -q -m "Engine v$version: $change")
echo
echo "Committed v$version in both repos. Next:"
echo "  git -C $(pwd) push && git -C $(pwd) tag -a v$version -m 'v$version' && git -C $(pwd) push origin v$version"
echo "  git -C $web push   (then deploy the site)"
