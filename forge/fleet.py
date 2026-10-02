"""Talk to the cloud self-play fleet through its blob container.

- set_net(): point every node's self-play at a network.
- remote_gate(): run an SPRT gate on the fleet. Nodes play batches of game
  pairs (forge/azure/node.sh, gate_batch) and upload pentanomial results; this
  sums them and decides with forge/sprt.py, the Python twin of the arena's test.

Reads BASE and SAS from ~/.arhanpassant/forge.env.
"""

import datetime as dt
import json
import os
import time
import urllib.parse
import urllib.request
from defusedxml import ElementTree as ET

import sprt


def _env():
    env = {}
    for line in open(os.path.expanduser("~/.arhanpassant/forge.env")):
        if "=" in line:
            k, v = line.strip().split("=", 1)
            env[k] = v.strip().strip("'")
    return env["BASE"], env["SAS"]


def _url(name, query=""):
    base, sas = _env()
    return f"{base}/{urllib.parse.quote(name)}?{query}{sas}"


def put(name, data):
    req = urllib.request.Request(_url(name), data=data, method="PUT",
                                 headers={"x-ms-blob-type": "BlockBlob", "x-ms-version": "2021-08-06"})
    with urllib.request.urlopen(req, timeout=120) as r:
        assert r.status in (200, 201), r.status


def get(name):
    with urllib.request.urlopen(_url(name), timeout=120) as r:
        return r.read()


def list_names(prefix):
    base, sas = _env()
    names, marker = [], ""
    while True:
        q = f"{base}?restype=container&comp=list&prefix={urllib.parse.quote(prefix)}"
        if marker:
            q += f"&marker={urllib.parse.quote(marker)}"
        with urllib.request.urlopen(f"{q}&{sas}", timeout=120) as r:
            root = ET.fromstring(r.read())
        names += [b.findtext("Name") for b in root.iter("Blob")]
        marker = root.findtext("NextMarker") or ""
        if not marker:
            return names


def set_net(net_path, tag, nodes=8000, hours=0.15):
    name = os.path.basename(net_path)
    put(f"nets/{name}", open(net_path, "rb").read())
    control = f"SRC=src/current.tar.gz\nNET=nets/{name}\nNODES={nodes}\nHOURS={hours}\nTAG={tag}\nPAUSE=0\n"
    put("control/node.env", control.encode())


def nodes_alive(max_age_minutes=45):
    """Number of nodes that reported status recently."""
    alive = 0
    now = dt.datetime.now(dt.timezone.utc)
    for n in list_names("status/"):
        try:
            s = json.loads(get(n))
            t = dt.datetime.fromisoformat(s["updated"].replace("Z", "+00:00"))
            alive += (now - t).total_seconds() < max_age_minutes * 60
        except Exception:
            pass
    return alive


def remote_gate(candidate, champion, tc="8+0.08", elo0=0.0, elo1=5.0, alpha=0.05, beta=0.05,
                max_games=12000, pairs=16, stall_minutes=30, log=print):
    """Run a distributed SPRT; returns an arena-style result dict, or None if the fleet stalls."""
    gid = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
    for path in (candidate, champion):
        put(f"nets/{os.path.basename(path)}", open(path, "rb").read())
    gate = {"id": gid, "status": "running", "candidate": f"nets/{os.path.basename(candidate)}",
            "champion": f"nets/{os.path.basename(champion)}", "tc": tc, "pairs": pairs, "elo0": elo0, "elo1": elo1}
    put("control/gate.json", json.dumps(gate).encode())
    lower, upper = sprt.bounds(alpha, beta)
    penta, wins, losses, draws, seen = [0] * 5, 0, 0, 0, set()
    last_new, last_log, started = time.time(), 0.0, time.time()
    decision, llr = None, 0.0
    try:
        while True:
            for name in list_names(f"gates/{gid}/"):
                if name in seen:
                    continue
                r = json.loads(get(name))
                seen.add(name)
                last_new = time.time()
                penta = [a + b for a, b in zip(penta, r["penta"])]
                wins, losses, draws = wins + r["wins"], losses + r["losses"], draws + r["draws"]
            games = wins + losses + draws
            if games:
                llr = sprt.llr(penta, elo0, elo1)
                if llr >= upper:
                    decision = "H1"
                elif llr <= lower:
                    decision = "H0"
                elif games >= max_games:
                    decision = "inconclusive"
            if time.time() - last_log > 300 or decision:
                e, lo, hi = sprt.elo_estimate(penta) if games else (0, 0, 0)
                log(f"fleet gate {gid}: {games} games from {len(seen)} batches, elo {e:+.1f} [{lo:+.1f}, {hi:+.1f}], llr {llr:.2f}")
                last_log = time.time()
            if decision:
                break
            if time.time() - last_new > stall_minutes * 60:
                log(f"fleet gate {gid}: no batches for {stall_minutes} minutes; giving up")
                return None
            time.sleep(30)
    finally:
        gate["status"] = "done"
        gate["decision"] = decision
        put("control/gate.json", json.dumps(gate).encode())
    e, lo, hi = sprt.elo_estimate(penta)
    return {
        "engine": os.path.basename(candidate), "baseline": os.path.basename(champion), "tc": tc, "where": "fleet",
        "batches": len(seen), "games": wins + losses + draws, "wins": wins, "losses": losses, "draws": draws,
        "penta": penta, "elo": e, "elo_lo": lo, "elo_hi": hi,
        "sprt": {"elo0": elo0, "elo1": elo1, "alpha": alpha, "beta": beta, "llr": llr, "lower": lower, "upper": upper},
        "decision": decision, "seconds": round(time.time() - started),
    }
