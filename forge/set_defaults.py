"""Set tunable defaults in engine/src/params.rs from a JSON object {name: value}.

Only the default (first number after the name) changes; ranges and comments
stay. Unknown names or values outside a tunable's range are an error.

    python3 forge/set_defaults.py accepted.json
"""
import json
import re
import sys

PATH = "engine/src/params.rs"


def main():
    values = json.load(open(sys.argv[1]))
    src = open(PATH).read()
    for name, value in values.items():
        value = int(value)
        pat = re.compile(rf"^(\s*{re.escape(name)}:\s*)(-?\d+)(,\s*(-?\d+),\s*(-?\d+);)", re.M)
        m = pat.search(src)
        if not m:
            sys.exit(f"unknown tunable {name}")
        lo, hi = int(m.group(4)), int(m.group(5))
        if not lo <= value <= hi:
            sys.exit(f"{name}={value} outside [{lo}, {hi}]")
        src = src[:m.start()] + m.group(1) + str(value) + m.group(3) + src[m.end():]
        print(f"{name}: {m.group(2)} -> {value}")
    open(PATH, "w").write(src)


if __name__ == "__main__":
    main()
