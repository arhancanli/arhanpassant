"""Compress the self-play files of finished RL rounds in place (FILE.bin -> FILE.bin.xz, xz preset 1, ~3.3x),
checking each archive against the original before the original is removed. Training reads only plain .bin files,
so compress only rounds no longer replayed (the loop replays the last recipe["replay"] rounds).

    python3 forge/compress_rounds.py 5 6
"""
import hashlib
import importlib.util
import lzma
import os
import sys
from concurrent.futures import ProcessPoolExecutor


def compress(path):
    data = open(path, "rb").read()
    packed = lzma.compress(data, preset=1)
    if hashlib.sha256(lzma.decompress(packed)).digest() != hashlib.sha256(data).digest():
        return path, 0, "verify failed"
    tmp = path + ".xz.tmp"
    with open(tmp, "wb") as f:
        f.write(packed)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path + ".xz")
    os.remove(path)
    return path, len(data) - len(packed), None


def main():
    spec = importlib.util.spec_from_file_location("rl", os.path.join(os.path.dirname(__file__), "rl_loop.py"))
    rl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rl)
    state = rl.load()
    paths = [p for rnd in sys.argv[1:] for p in rl.round_files(state, int(rnd)) if os.path.exists(p)]
    saved = failed = 0
    with ProcessPoolExecutor(2) as pool:
        for path, freed, err in pool.map(compress, paths):
            if err:
                failed += 1
                print(f"{path}: {err}", flush=True)
            saved += freed
    print(f"rounds {' '.join(sys.argv[1:])}: {len(paths) - failed} files compressed, {saved / 1e9:.2f} GB freed"
          + (f", {failed} failed (kept)" if failed else ""), flush=True)


if __name__ == "__main__":
    main()
