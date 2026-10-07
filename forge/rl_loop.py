"""The self-improvement loop: self-play → fine-tune → SPRT → promote, forever.

Every cycle it pulls new self-play from the Oracle bucket, counts the
positions played by the current champion since its promotion (Mac round
folders plus bucket folders of builds created after the promotion), and once
there are enough, fine-tunes the champion on this round's data (see recipe()
for the learning rate, epochs and how many earlier rounds are replayed). When no
epoch beats the champion on held-out games there is nothing to test and the
round keeps collecting. Otherwise the candidate is tested with an
SPRT at the front of the Mac test queue; if it passes it becomes the network
compiled into the engine (commit + push), the Mac self-play restarts on it in
new round folders, and the fleet VMs rebuild. If it fails, the round keeps
collecting and tries again with more data.

    nohup python3 forge/rl_loop.py >> ~/arhanpassant-data/rl/loop.log 2>&1 &

State: ~/arhanpassant-data/rl/state.json. Stop: touch ~/arhanpassant-data/rl/STOP.
"""

import datetime as dt
import glob
import json
import os
import subprocess
import time
import urllib.request

HOME = os.path.expanduser("~")
DATA = f"{HOME}/arhanpassant-data"
REPO = f"{HOME}/arhanpassant-elo"
RL = f"{DATA}/rl"
STATE = f"{RL}/state.json"
PAR_FILE = f"{HOME}/.oci/arhanpassant/par.txt"
IPS = f"{HOME}/.oci/arhanpassant/ips.txt"
SSH = ["ssh", "-i", f"{HOME}/.ssh/arhanpassant_oci", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20"]
PY = f"{HOME}/arhanpassant/.venv/bin/python"
THRESHOLD = int(os.environ.get("RL_THRESHOLD", 15_000_000))
MAC_THREADS, MAC_SEEDED = 6, 2


def now():
    return dt.datetime.now(dt.timezone.utc)


def parse_time(t):
    """Bucket and state timestamps (ISO 8601, 'Z' or offset) as aware datetimes."""
    return dt.datetime.fromisoformat(t.replace("Z", "+00:00"))


def log(msg):
    print(f"{now():%Y-%m-%dT%H:%M:%SZ} {msg}", flush=True)


def load():
    with open(STATE) as f:
        return json.load(f)


def save(s):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=1)
    os.replace(tmp, STATE)


def sh(cmd, **kw):
    return subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True, **kw)


def bucket_objects():
    par = open(PAR_FILE).read().strip()
    out, start = [], None
    while True:
        url = f"{par}?prefix=selfplay/&fields=name,size,timeCreated" + (f"&start={start}" if start else "")
        with urllib.request.urlopen(url, timeout=60) as r:
            d = json.load(r)
        out += d.get("objects", [])
        start = d.get("nextStartWith")
        if not start:
            return out


def sync_bucket(state):
    """Download objects of builds that started after the promotion; return their local paths."""
    par = open(PAR_FILE).read().strip()
    since = parse_time(state["since"])
    objs = bucket_objects()
    first = {}
    for o in objs:
        parts = o["name"].split("/")
        if len(parts) == 4:  # selfplay/<host>/<rev>/<file>
            key = (parts[1], parts[2])
            t = parse_time(o["timeCreated"])
            first[key] = min(first.get(key, t), t)
    fresh_keys = {k for k, t in first.items() if t > since}
    paths = []
    for o in objs:
        parts = o["name"].split("/")
        if len(parts) == 4 and (parts[1], parts[2]) in fresh_keys:
            local = f"{DATA}/selfplay/oci/{parts[1]}/{parts[2]}/{parts[3]}"
            if not os.path.exists(local) or os.path.getsize(local) != o["size"]:
                os.makedirs(os.path.dirname(local), exist_ok=True)
                urllib.request.urlretrieve(par + o["name"], local)
            paths.append(local)
    return paths


def positions(paths):
    return sum(os.path.getsize(p) // 32 for p in paths if os.path.exists(p))


def round_files(state, rnd):
    r = state["rounds"][str(rnd)]
    files = []
    for d in r["mac_dirs"]:
        files += glob.glob(f"{d}/*.bin")
    return files + r.get("oci_files", [])


def spawn(cmd, logfile):
    """Start a long-running job detached from this process (its own session, output to a log)."""
    with open(logfile, "a") as out:
        subprocess.Popen(["nice", "-n", "19", *cmd], cwd=DATA, stdin=subprocess.DEVNULL, stdout=out,
                         stderr=subprocess.STDOUT, start_new_session=True)


def start_mac_datagen(binary, rnd):
    # Any engine build's self-play, whatever the order of its arguments.
    sh(["pkill", "-f", "[a]p-net-[^ ]* datagen"])
    time.sleep(2)
    dirs = [f"{DATA}/selfplay/r{rnd}-mac", f"{DATA}/selfplay/r{rnd}-active"]
    for d in dirs:
        os.makedirs(d, exist_ok=True)
    seed = int(time.time())
    base = [binary, "datagen", "--nodes", "8000", "--positions-per-file", "1000000"]
    spawn([*base, "--threads", str(MAC_THREADS), "--seed", str(seed), "--out", dirs[0]], f"{RL}/datagen-r{rnd}.log")
    spawn([*base, "--threads", str(MAC_SEEDED), "--seed", str(seed + 17), "--book", f"{DATA}/active/seeds.epd",
           "--random-plies", "2", "--out", dirs[1]], f"{RL}/datagen-r{rnd}-active.log")
    return dirs


def restart_fleet():
    if not os.path.exists(IPS):
        return
    for line in open(IPS):
        parts = line.split()
        if len(parts) == 2:
            r = sh(SSH + [f"ubuntu@{parts[1]}", "sudo systemctl restart ap-node && echo ok"], timeout=120)
            log(f"fleet {parts[0]}: {r.stdout.strip() or r.stderr.strip()[:120]}")


def recipe(state):
    """Training settings, overridable in state.json ("recipe"). A fine-tune of a
    converged network needs a low learning rate: at 1e-4 a fresh optimizer knocks
    it off its optimum and no epoch beats the starting network (round 6, 10-07)."""
    r = {"lr": 3e-5, "epochs": 3, "warmup": 500, "replay": 0}
    r.update(state.get("recipe", {}))
    return r


def train(state, rnd):
    """Fine-tune the champion; returns the candidate, "same" when no epoch beat
    the champion on held-out games (the trainer then keeps the champion), or None."""
    rc = recipe(state)
    cand = f"{DATA}/nets/rl{rnd}.nnue"
    files = round_files(state, rnd)
    for back in range(1, rc["replay"] + 1):  # earlier rounds' data, if the recipe replays it
        if str(rnd - back) in state["rounds"]:
            files += round_files(state, rnd - back)
    listing = f"{RL}/r{rnd}-files.txt"
    with open(listing, "w") as f:
        f.write("\n".join(files))
    manifest = {p: os.path.getsize(p) // 32 * 32 for p in files if os.path.exists(p)}
    with open(f"{RL}/r{rnd}-manifest.json", "w") as f:
        json.dump(manifest, f)
    cmd = [PY, f"{REPO}/trainer/train.py", "--data", *manifest.keys(), "--manifest", f"{RL}/r{rnd}-manifest.json",
           "--hidden", str(state.get("hidden", 512)), "--input-buckets", str(state.get("input_buckets", 8)),
           "--output-buckets", "8", "--epochs", str(rc["epochs"]), "--lr", str(rc["lr"]), "--warmup", str(rc["warmup"]),
           "--factorize", "--init-nnue", state["champion"], "--out", cand, "--workers", "4", "--threads", "4"]
    with open(f"{RL}/train-r{rnd}.log", "w") as f:
        code = subprocess.run(["nice", "-n", "10", *cmd], stdout=f, stderr=subprocess.STDOUT).returncode
    if code != 0 or not os.path.exists(cand):
        return None
    with open(cand, "rb") as a, open(state["champion"], "rb") as b:
        return "same" if a.read() == b.read() else cand


def queue_sprt(state, rnd, cand):
    name = f"rl{rnd}-try{state['rounds'][str(rnd)]['tries']}"
    job = f"{DATA}/elo/queue/000-{name}.sh"
    with open(job, "w") as f:
        f.write(f'B={state["binary"]}\nCAND_OPTS="opt.EvalFile={cand}" BASE_OPTS="opt.EvalFile={state["champion"]}" '
                f'{DATA}/elo/sprt2.sh {name} $B $B 5+0.05 0 5 14\n')
    return name


def result(name):
    p = f"{DATA}/elo/{name}/result.json"
    if os.path.exists(p) and "complete" in open(f"{DATA}/elo/{name}/run.log").read():
        with open(p) as f:
            return json.load(f)
    return None


def promote(state, rnd, cand, res, trained):
    version = f"0.13.0-rl{rnd}"
    # `trained` is the count when training started; the round keeps collecting while the SPRT runs.
    prev = f" plus round {rnd - 1}'s" if str(rnd - 1) in state["rounds"] else ""
    summary = (f"RL round {rnd}: fine-tuned on {trained:,} fresh self-play positions{prev}, "
               f"{res['elo']:+.1f} Elo (95% [{res['elo_lo']:.1f}, {res['elo_hi']:.1f}], {res['games']:,} games at 5+0.05)")
    r = sh(["bash", f"{REPO}/forge/promote_net.sh", cand, version, summary], cwd=REPO)
    log(r.stdout.strip()[-300:] + r.stderr.strip()[-300:])
    if r.returncode != 0:
        return False
    sh(["git", "push", "-q", "origin", "HEAD"], cwd=REPO)
    state["champion"] = f"{DATA}/nets/champion-{version}.nnue"
    state["champion_version"] = version
    state["binary"] = f"{DATA}/bin/ap-net-{version}"
    state["since"] = now().strftime("%Y-%m-%dT%H:%M:%S.000+00:00")
    nxt = rnd + 1
    state["rounds"][str(nxt)] = {"mac_dirs": start_mac_datagen(state["binary"], nxt), "oci_files": [],
                                 "tries": 0, "positions": 0, "started": state["since"]}
    state["round"] = nxt
    save(state)
    restart_fleet()
    return True


def main():
    os.makedirs(RL, exist_ok=True)
    while not os.path.exists(f"{RL}/STOP"):
        state = load()
        rnd = state["round"]
        r = state["rounds"][str(rnd)]
        try:
            r["oci_files"] = sync_bucket(state)
        except Exception as e:  # network trouble: count what is local
            log(f"bucket sync failed: {e}")
        r["positions"] = positions(round_files(state, rnd))
        pending = r.get("pending")
        if pending:
            res = result(pending["name"])
            if res:
                log(f"round {rnd} {pending['name']}: {res['decision']} {res['elo']:+.1f} [{res['elo_lo']:.1f}, {res['elo_hi']:.1f}] {res['games']} games")
                r.setdefault("tests", []).append({"name": pending["name"], "decision": res["decision"], "elo": res["elo"],
                                                  "games": res["games"], "positions": pending["positions"]})
                r.pop("pending")
                save(state)
                if res["decision"] == "H1" and promote(state, rnd, pending["cand"], res, pending["positions"]):
                    log(f"promoted round {rnd}; round {rnd + 1} started")
                    continue
                r["next_try_at"] = int(r["positions"] * 1.5)
        elif r["positions"] >= max(THRESHOLD, r.get("next_try_at", 0)):
            r["tries"] += 1
            save(state)
            rc = recipe(state)
            replay = f" (+ {rc['replay']} earlier rounds)" if rc["replay"] else ""
            log(f"round {rnd} try {r['tries']}: training on {r['positions']:,} positions{replay}, lr {rc['lr']:g}, {rc['epochs']} epochs")
            cand = train(state, rnd)
            if cand == "same":
                # Nothing to test: wait for half as much data again.
                r.setdefault("tests", []).append({"name": f"rl{rnd}-try{r['tries']}", "decision": "no-gain", "elo": None,
                                                  "games": 0, "positions": r["positions"]})
                r["next_try_at"] = int(r["positions"] * 1.5)
                log(f"round {rnd}: no epoch beat the champion on held-out games; next try at {r['next_try_at']:,} positions")
            elif cand:
                name = queue_sprt(state, rnd, cand)
                r["pending"] = {"name": name, "cand": cand, "positions": r["positions"]}
                log(f"round {rnd}: queued SPRT {name}")
            else:
                log(f"round {rnd}: training failed (see {RL}/train-r{rnd}.log)")
        save(state)
        time.sleep(600)
    log("stopped (STOP file)")


if __name__ == "__main__":
    main()
