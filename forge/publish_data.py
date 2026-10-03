"""Publish the engine's live numbers for the website to the `data` branch.

    python forge/publish_data.py

Writes site.json (current version, the promotion ledger, every search-change
test, the latest absolute-strength measurement, and the last 14 days of
Lichess games and gauntlet results per time control) on the repository's
`data` branch. The website reads it from raw.githubusercontent.com, so the
numbers stay current between site deployments. Counts only: no opponent
names or game records.
"""

import datetime as dt
import json
import os
import subprocess
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.expanduser("~/arhanpassant-data")


def load(path, default):
    try:
        return json.load(open(path))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def git(*args, cwd=ROOT):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def main():
    state = load(os.path.join(DATA, "forge", "state.json"), {})
    site = {
        "updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "champion": state.get("champion_version"),
        "ledger": load(os.path.join(ROOT, "forge", "ledger.json"), {}).get("versions", []),
        "tests": load(os.path.join(ROOT, "forge", "tests.json"), {}).get("tests", []),
        "anchors": load(os.path.join(ROOT, "forge", "anchors.json"), None),
        "lichess": load(os.path.join(DATA, "lichess", "summary.json"), {"days": []})["days"][-14:],
        "gauntlet": [{k: d[k] for k in ("date", "network", "byMode")}
                     for d in load(os.path.join(DATA, "gauntlet", "summary.json"), {"days": []})["days"][-14:]],
    }
    with tempfile.TemporaryDirectory() as tmp:
        work = os.path.join(tmp, "data")
        exists = subprocess.run(["git", "ls-remote", "--exit-code", "--heads", "origin", "data"], cwd=ROOT,
                                capture_output=True).returncode == 0
        if exists:
            git("fetch", "-q", "origin", "data")
            git("worktree", "add", "-q", "-B", "data-publish", work, "FETCH_HEAD")
        else:
            git("worktree", "add", "-q", "--detach", work)
            git("checkout", "-q", "--orphan", "data-publish", cwd=work)
            git("rm", "-rfq", ".", cwd=work)
        try:
            with open(os.path.join(work, "site.json"), "w") as f:
                json.dump(site, f, indent=1)
                f.write("\n")
            with open(os.path.join(work, "README.md"), "w") as f:
                f.write("Live numbers for https://arhanpassant.com, written by `forge/publish_data.py` on the main branch.\n")
            git("add", "-A", cwd=work)
            if git("status", "--porcelain", cwd=work):
                git("commit", "-q", "-m", f"Live numbers {site['updated'][:10]}", cwd=work)
                git("push", "-q", "origin", "HEAD:data", cwd=work)
                print("published", site["updated"])
            else:
                print("nothing changed")
        finally:
            git("worktree", "remove", "--force", work)
            subprocess.run(["git", "branch", "-D", "data-publish"], cwd=ROOT, capture_output=True)


def publish_quietly(log=print):
    """Publish, but never let a publishing problem stop the caller's real work."""
    try:
        main()
    except Exception as e:  # network or git trouble
        log(f"could not publish live numbers ({e})")


if __name__ == "__main__":
    main()
