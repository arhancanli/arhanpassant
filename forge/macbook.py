"""Install, inspect or stop the supervised local engine improvement loop.

    python3 forge/macbook.py install --cpu-budget 16
    python3 forge/macbook.py status
    python3 forge/macbook.py stop
    python3 forge/macbook.py start
"""

import argparse
import json
import os
import pathlib
import plistlib
import subprocess
import sys
import time

import local_gate

ROOT = pathlib.Path(__file__).resolve().parent.parent
LABEL = "com.arhanpassant.engine-forge"
PLIST = pathlib.Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def profile(data, cpu_budget, min_new, no_publish=False):
    data = pathlib.Path(data).resolve()
    cmd = [str(ROOT / ".venv/bin/python"), str(ROOT / "forge/loop.py"), "--data", str(data),
           "--engine", str(data / "bin/ap-current"), "--arena", str(data / "bin/arena"),
           "--cpu-budget", str(cpu_budget), "--selfplay-threads", str(cpu_budget),
           "--concurrency", str(cpu_budget), "--selfplay-nodes", "8000", "--min-new", str(min_new),
           "--local-search", "--batch-games", "64", "--train-workers", str(min(8, cpu_budget)),
           "--train-threads", str(min(4, cpu_budget)), "--max-data-gb", "14", "--min-free-gb", "8"]
    if no_publish:
        cmd.append("--no-publish")
    return {"Label": LABEL, "ProgramArguments": cmd, "WorkingDirectory": str(ROOT),
            "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 30,
            # launchd's Background and Standard classes throttle sustained
            # compute. The owner-selected core budget and nice levels bound
            # this explicitly requested workload instead.
            "ProcessType": "Interactive", "ExitTimeOut": 20,
            "SoftResourceLimits": {"NumberOfFiles": 8192},
            "EnvironmentVariables": {"PATH": f"{pathlib.Path.home()}/.cargo/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                                     "PYTHONUNBUFFERED": "1", "OMP_NUM_THREADS": "4", "OPENBLAS_NUM_THREADS": "1",
                                     "ARHANPASSANT_EXECUTION_PROFILE": f"macbook-interactive-{cpu_budget}"},
            "StandardOutPath": str(data / "forge/loop.out"),
            "StandardErrorPath": str(data / "forge/loop.out")}


def launch(*args, check=True):
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, check=check)


def bootstrap(domain):
    # bootout can return before launchd has released the old service. Retry
    # that transient error rather than leaving an installed controller off.
    for attempt in range(20):
        result = launch("bootstrap", domain, str(PLIST), check=False)
        if result.returncode == 0:
            return
        if result.returncode != 5:
            break
        time.sleep(0.25)
    raise RuntimeError(f"launchctl bootstrap failed: {result.stderr.strip() or result.stdout.strip()}")


def read(path, default):
    try:
        return json.loads(pathlib.Path(path).read_text())
    except FileNotFoundError:
        return default


def status(data):
    data = pathlib.Path(data)
    service = launch("print", f"gui/{os.getuid()}/{LABEL}", check=False)
    state = read(data / "forge/state.json", {})
    queue = read(data / "forge/queue.json", {"pending": []})
    files = list((data / "selfplay").glob("**/*.bin"))
    snapshot = state.get("sizes", {})
    sizes = {str(p): p.stat().st_size for p in files if p.exists()}
    generator = subprocess.run(["ps", "-p", str(state.get("selfplay_pid", 0)), "-o", "command="],
                               capture_output=True, text=True).stdout.strip()
    out = {"supervised": service.returncode == 0, "champion": state.get("champion_version"),
           "phase": state.get("phase"), "cpu_budget": state.get("cpu_budget"),
           "selfplay_running": " datagen " in generator,
           "selfplay_threads": state.get("selfplay_threads", 0) if " datagen " in generator else 0,
           "positions": sum(v // 32 for v in sizes.values()),
           "fresh_positions": sum(max(0, n - snapshot.get(p, 0)) // 32 for p, n in sizes.items()),
           "pending_search_tests": [p["name"] for p in queue["pending"]],
           "pending_network": state.get("pending"), "error": state.get("error")}
    if state.get("pending"):
        candidate = pathlib.Path(state["pending"]["candidate"]).name
        result = read(data / "forge" / f"sprt-{candidate}.json", {})
        out["network_test"] = {k: result.get(k) for k in ("games", "elo", "elo_lo", "elo_hi", "sprt", "decision")}
    if queue["pending"] and state.get("phase") == "search tests and self-play":
        import re
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", queue["pending"][0]["name"])
        item = queue["pending"][0]
        engine = os.path.expanduser(item.get("candidate_engine", str(data / "bin/ap-current")))
        baseline = os.path.expanduser(item.get("baseline_engine", engine))
        identity = local_gate.build_identity(engine, state["champion_net"], baseline)
        result = read(data / "forge/tests" / f"local-{name}-{identity}.json", {})
        if result:
            out["search_test"] = {k: result.get(k) for k in ("games", "elo", "elo_lo", "elo_hi", "sprt", "decision")}
    print(json.dumps(out, indent=2))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", choices=["install", "start", "stop", "status"])
    ap.add_argument("--data", default=str(pathlib.Path.home() / "arhanpassant-data"))
    ap.add_argument("--cpu-budget", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    ap.add_argument("--min-new", type=int, default=40_000_000)
    ap.add_argument("--no-publish", action="store_true")
    args = ap.parse_args()
    args.data = str(pathlib.Path(args.data).expanduser().resolve())
    if sys.platform != "darwin":
        ap.error("launchd supervision is for macOS; run forge/loop.py directly elsewhere")
    if not 1 <= args.cpu_budget <= (os.cpu_count() or 1) or args.min_new < 1:
        ap.error("choose a positive CPU budget within the available cores and a positive data threshold")
    domain, service = f"gui/{os.getuid()}", f"gui/{os.getuid()}/{LABEL}"
    if args.command == "status":
        status(args.data)
    elif args.command == "stop":
        launch("disable", service)
        launch("bootout", service, check=False)
        print("engine improvement service stopped and disabled")
    elif args.command == "start":
        launch("enable", service)
        if launch("print", service, check=False).returncode:
            bootstrap(domain)
        launch("kickstart", service)
        print("engine improvement service started")
    else:
        config = profile(args.data, args.cpu_budget, args.min_new, args.no_publish)
        for path in (config["ProgramArguments"][0], pathlib.Path(args.data) / "bin/ap-current",
                     pathlib.Path(args.data) / "bin/arena", ROOT / "tools/books/UHO_4060_v4.epd"):
            if not pathlib.Path(path).is_file():
                ap.error(f"required file missing: {path}")
        (pathlib.Path(args.data) / "forge").mkdir(parents=True, exist_ok=True)
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        tmp = PLIST.with_suffix(".plist.tmp")
        tmp.write_bytes(plistlib.dumps(config))
        launch("bootout", service, check=False)
        os.replace(tmp, PLIST)
        launch("enable", service)
        bootstrap(domain)
        print(f"installed {LABEL}: {args.cpu_budget} cores; {PLIST}")


if __name__ == "__main__":
    main()
