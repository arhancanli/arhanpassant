"""Build and verify the complete local benchmark suite, without cloud workers.

Run with the forge stopped so --jobs shares the owner's CPU budget. Upstream
tags, build commands, binary/network hashes and UCI transcripts are retained.
The catalog is published only when every opponent passes a real search.
"""

import argparse
import datetime as dt
import os
import pathlib
import queue
import re
import shutil
import subprocess
import threading
import time

import local_gate


SOURCES = {
    "stockfish-17.1": ("official-stockfish/Stockfish", "sf_17.1"),
    "koivisto-9.0": ("Luecx/Koivisto", "v9.0"),
    "demolito-2021": ("lucasart/Demolito", "20211004"),
    "ethereal-12.00": ("AndyGrant/Ethereal", "V12.00"),
    "laser-1.7": ("jeffreyan11/laser-chess-engine", "v1.7"),
    **{f"stash-{v}": ("mhouppin/stash-bot", f"v{v}") for v in ("28.0", "31.0", "34.0", "37.0")},
    **{f"weiss-{v}": ("TerjeKir/weiss", f"v{v}") for v in ("1.2", "1.3", "1.4", "2.0")},
}
NAMES = ["stockfish-19", *SOURCES]


def probe(binary, transcript, timeout=60):
    """Verify full-strength, one-thread UCI startup and NNUE search readiness."""
    child = subprocess.Popen([str(binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, bufsize=1)
    lines, received = [], queue.Queue()

    def reader():
        for line in child.stdout:
            received.put(line.rstrip())
        received.put(None)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()

    def send(command):
        lines.append("> " + command)
        child.stdin.write(command + "\n")
        child.stdin.flush()

    def until(prefix):
        deadline = time.monotonic() + timeout
        while True:
            line = received.get(timeout=max(0.01, deadline - time.monotonic()))
            if line is None:
                raise RuntimeError(f"{binary}: exited before {prefix}")
            lines.append(line)
            if "error" in line.lower() or "failed" in line.lower():
                raise RuntimeError(f"{binary}: {line}")
            if line.startswith(prefix):
                return line

    try:
        send("uci")
        until("uciok")
        advertised = {}
        for line in lines:
            match = re.match(r"option name (.+?) type (\w+)(.*)", line)
            if match:
                advertised[match[1]] = {"type": match[2], "definition": match[3].strip()}
        desired = {"Threads": "1", "Hash": "64", "Ponder": "false", "UCI_LimitStrength": "false",
                   "Skill Level": "20", "OwnBook": "false", "Book": "false", "NoobBook": "false",
                   "SyzygyPath": "", "SyzygyProbeLimit": "0"}
        options = {}
        for key, value in desired.items():
            name = next((n for n in advertised if n.lower() == key.lower()), None)
            if name is None:
                continue
            definition = advertised[name]
            if key in ("Book", "NoobBook") and definition["type"] != "check":
                continue
            if definition["type"] == "spin":
                bounds = re.search(r"min (-?\d+) max (-?\d+)", definition["definition"])
                if bounds and not int(bounds[1]) <= int(value) <= int(bounds[2]):
                    raise ValueError(f"unsupported fair match option: {name}={value}")
            options[name] = value
            send(f"setoption name {name} value {value}")
        if not any(n.lower() == "hash" for n in options):
            raise ValueError(f"{binary}: cannot verify equal 64 MB hash")
        send("isready")
        until("readyok")
        send("ucinewgame")
        send("position startpos")
        send("go movetime 250")
        bestmove = until("bestmove")
        if not re.match(r"bestmove [a-h][1-8][a-h][1-8][qrbn]?(?: |$)", bestmove):
            raise ValueError(f"{binary}: invalid starting-position bestmove")
        return {"uci_name": next((s[8:] for s in lines if s.startswith("id name ")), None),
                "options": options, "advertised_options": advertised, "bestmove": bestmove,
                "sha256": local_gate.digest(binary),
                "architecture": subprocess.check_output(["file", "-b", str(binary)], text=True).strip()}
    finally:
        try:
            if child.poll() is None:
                send("quit")
                child.wait(timeout=5)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            thread.join(timeout=5)
            child.stdin.close()
            child.stdout.close()
            pathlib.Path(transcript).write_text("\n".join(lines) + "\n")


def recipe(name, source, jobs):
    make = ["/usr/bin/make", f"-j{jobs}"]
    subdir, exe = "src", name.split("-")[0]
    if name.startswith("stockfish-"):
        args = ["build", "ARCH=apple-silicon", "COMP=clang"]
    elif name.startswith("koivisto-"):
        subdir, exe = "src_files", "../bin/Koivisto_9.0"
        args = ["build", "CXX=clang++", "CC=clang", "VALGRIND=0", "LTO=1", "NAMING=0"]
    elif name.startswith("ethereal-"):
        exe, args = "Ethereal", ["nopopcnt", "CC=clang"]
    elif name.startswith("laser-"):
        args = ["CC=clang++", "NOPOPCNT=true"]
    elif name.startswith("stash-"):
        # Stash 28 uses a different variable and executable spelling.
        if name in ("stash-28.0", "stash-31.0", "stash-34.0"):
            exe = "stash-bot"
            args = ["CC=clang", "ARCH=unknown", "native=yes", "CFLAGS=-Wall -Wextra -O3 -flto -march=native"]
        else:
            args = ["CC=clang", "ARCH=generic", "NATIVE=yes", "CPPFLAGS="]
    elif name.startswith("weiss-"):
        args = ["basic", "CC=clang", "WARN=-Wall -Wextra -Wshadow"]
    else:
        args = ["CC=clang"]
    return source / subdir, make + args, source / subdir / exe


def prepare(data, jobs):
    root = pathlib.Path(data) / "opponents"
    root.mkdir(exist_ok=True)
    catalog = {"created": dt.datetime.now(dt.timezone.utc).isoformat(),
               "description": "All thirteen historical benchmark opponents plus full-strength Stockfish 19, built for this Mac.",
               "opponents": []}
    for name in NAMES:
        directory = root / name
        directory.mkdir(exist_ok=True)
        installed = directory / "engine"
        evidence = {"name": name, "path": str(installed)}
        with (directory / "build.log").open("a") as logfile:
            if name == "stockfish-19":
                origin = pathlib.Path(data) / "anchors/stockfish/stockfish-macos-universal"
                if not installed.exists():
                    shutil.copy2(origin, installed)
                evidence["origin"] = str(origin)
            else:
                repo, tag = SOURCES[name]
                source = directory / "src"
                url = f"https://github.com/{repo}.git"
                if not source.exists():
                    subprocess.run(["git", "clone", "--depth", "1", "--branch", tag, url, str(source)],
                                   stdout=logfile, stderr=subprocess.STDOUT, check=True)
                evidence.update(repository=url, tag=tag,
                                source_commit=subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip())
                cwd, cmd, output = recipe(name, source, jobs)
                evidence["build_command"] = cmd
                if not installed.exists():
                    print(f"building {name}", flush=True)
                    subprocess.run(cmd, cwd=cwd, stdout=logfile, stderr=subprocess.STDOUT, check=True)
                    shutil.copy2(output, installed)
                evidence["networks"] = {str(p.relative_to(source)): local_gate.digest(p)
                                        for p in source.glob("**/*") if p.is_file() and p.suffix in (".nnue", ".net")}
            evidence.update(probe(installed, directory / "uci.log"))
            print(f"verified {name}: {evidence['uci_name']}", flush=True)
        catalog["opponents"].append(evidence)
        local_gate.save(str(root / "preparing.json"), catalog)
    local_gate.save(str(root / "catalog.json"), catalog)
    print(f"all {len(NAMES)} opponents ready", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", default=os.path.expanduser("~/arhanpassant-data"))
    ap.add_argument("--jobs", type=int, default=os.cpu_count())
    args = ap.parse_args()
    if not 1 <= args.jobs <= (os.cpu_count() or 1):
        ap.error("choose a build budget within the available CPU cores")
    prepare(os.path.abspath(args.data), args.jobs)
