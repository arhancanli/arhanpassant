"""Work through a queue of search changes on the fleet, one test at a time.

Each queued change is tested against the settings accepted so far; a change
that passes (H1) joins them, so later tests build on it. Networks are not
handled here: the forge loop trains, gates and promotes those, and both share
the fleet by taking turns (forge/fleet.py waits for a running gate to finish).

    python forge/test_queue.py add NAME "what it changes" opt=value [opt=value ...] [bounds=-5,0]
    python forge/test_queue.py run          # keeps running; picks up new items
    python forge/test_queue.py show

State, in ~/arhanpassant-data/forge/: queue.json (pending and done items) and
search.json (the accepted settings, which the forge loop also uses in its gates).
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fleet_test  # noqa: E402

FORGE = os.path.expanduser("~/arhanpassant-data/forge")
QUEUE = os.path.join(FORGE, "queue.json")
SEARCH = os.path.join(FORGE, "search.json")


def load(path, default):
    try:
        return json.load(open(path))
    except FileNotFoundError:
        return default


def save(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def accepted():
    return load(SEARCH, {"accepted": {}})["accepted"]


def add(name, change, opts):
    """Queue a change. bounds=ELO0,ELO1 overrides the default [0, 5]; [-5, 0] asks
    "does it at least not lose strength?" for a change whose value lies elsewhere."""
    item = {"name": name, "change": change, "opts": opts}
    if "bounds" in opts:
        item["bounds"] = [float(x) for x in opts.pop("bounds").split(",")]
    q = load(QUEUE, {"pending": [], "done": []})
    q["pending"].append(item)
    save(QUEUE, q)


def run():
    said_idle = False
    while True:
        q = load(QUEUE, {"pending": [], "done": []})
        if not q["pending"]:
            if not said_idle:
                fleet_test.log("queue empty; waiting for new items")
                said_idle = True
            time.sleep(60)
            continue
        said_idle = False
        item = q["pending"][0]
        base = accepted()
        elo0, elo1 = item.get("bounds", [0.0, 5.0])
        entry = fleet_test.run_test(item["name"], item["change"], {**base, **item["opts"]}, dict(base), elo0=elo0, elo1=elo1)
        q = load(QUEUE, {"pending": [], "done": []})  # re-read: items may have been added meanwhile
        q["pending"] = [i for i in q["pending"] if i != item]
        if entry is None:
            q["pending"].append(item)  # the fleet stalled: try again later
            save(QUEUE, q)
            time.sleep(600)
            continue
        q["done"].append({**item, "decision": entry["decision"], "elo": entry["elo"], "games": entry["games"]})
        save(QUEUE, q)
        if entry["decision"] == "H1":
            s = load(SEARCH, {"accepted": {}, "history": []})
            s["accepted"].update(item["opts"])
            s.setdefault("history", []).append({"name": item["name"], "opts": item["opts"], "elo": entry["elo"]})
            save(SEARCH, s)
            fleet_test.log(f"accepted {item['name']}: settings now {s['accepted']}")


def main():
    cmd = sys.argv[1:2]
    if cmd == ["add"] and len(sys.argv) >= 5:
        add(sys.argv[2], sys.argv[3], fleet_test.options(sys.argv[4:]))
    elif cmd == ["run"]:
        run()
    elif cmd == ["show"]:
        print(json.dumps({"accepted": accepted(), **load(QUEUE, {"pending": [], "done": []})}, indent=2))
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
