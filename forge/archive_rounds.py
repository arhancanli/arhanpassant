"""Compress self-play data that training no longer reads (rounds older than the
replay window) to .bin.xz, about 4x smaller. Nothing is deleted until its
compressed copy has been read back and matches byte for byte.

    nice -n 19 python3 forge/archive_rounds.py [--dry-run]

Restore a file with: python3 -c "import lzma,shutil,sys; shutil.copyfileobj(lzma.open(sys.argv[1]), open(sys.argv[1][:-3], 'wb'))" FILE.bin.xz
"""

import glob
import hashlib
import json
import lzma
import os
import sys

DATA = os.path.expanduser("~/arhanpassant-data")
STATE = f"{DATA}/rl/state.json"


def files_of(r):
    out = []
    for d in r["mac_dirs"]:
        out += glob.glob(f"{d}/*.bin")
    return out + [p for p in r.get("oci_files", []) if os.path.exists(p)]


def digest_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def compress(path):
    xz = path + ".xz"
    tmp = xz + ".tmp"
    with open(path, "rb") as src, lzma.open(tmp, "wb", preset=6) as dst:
        for chunk in iter(lambda: src.read(1 << 22), b""):
            dst.write(chunk)
    h = hashlib.sha256()
    with lzma.open(tmp, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    if h.hexdigest() != digest_file(path):
        os.remove(tmp)
        raise RuntimeError(f"{path}: compressed copy does not match")
    os.replace(tmp, xz)
    saved = os.path.getsize(path) - os.path.getsize(xz)
    os.remove(path)
    return saved


def main():
    dry = "--dry-run" in sys.argv
    state = json.load(open(STATE))
    replay = state.get("recipe", {}).get("replay", 1)
    keep_from = state["round"] - replay  # rounds >= this are still trained on
    total, n = 0, 0
    for k, r in sorted(state["rounds"].items(), key=lambda x: int(x[0])):
        if int(k) >= keep_from:
            continue
        for p in files_of(r):
            if dry:
                total += os.path.getsize(p)
                n += 1
                continue
            total += compress(p)
            n += 1
    verb = "would compress" if dry else "compressed"
    print(f"{verb} {n} files of rounds before {keep_from}" + ("" if dry else f", saved {total / 1e9:.2f} GB")
          + (f" ({total / 1e9:.2f} GB raw)" if dry else ""))


if __name__ == "__main__":
    main()
