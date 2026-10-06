"""Durable launch budgets and immutable evidence for rejected arena batches."""
import datetime as dt
import hashlib
import json
import os
import pathlib
import shutil

MAX_ATTEMPTS = 3


class RetryLimit(RuntimeError):
    def __init__(self, journal_path):
        self.journal_path = str(journal_path)
        super().__init__(f"arena batch exhausted {MAX_ATTEMPTS} attempts; inspect {journal_path}")


def read(path, default=None):
    try:
        return json.loads(pathlib.Path(path).read_text())
    except FileNotFoundError:
        return default


def save(path, value):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = pathlib.Path(str(path) + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    os.replace(temporary, path)


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def failed(journal_path, journal, outputs, archive_root, error):
    archive = pathlib.Path(archive_root) / f"{len(journal['attempts']):04d}"
    archive.mkdir(parents=True, exist_ok=True)
    files = []
    for source in map(pathlib.Path, outputs):
        if not source.exists():
            continue
        value = digest(source)
        target = archive / source.name
        if not target.exists():
            temporary = pathlib.Path(str(target) + ".tmp")
            shutil.copy2(source, temporary)
            if digest(temporary) != value:
                raise RuntimeError(f"arena evidence changed while archiving: {source}")
            os.replace(temporary, target)
        if digest(target) != value:
            raise RuntimeError(f"arena evidence archive changed: {target}")
        files.append({"file": str(target), "sha256": value, "bytes": source.stat().st_size})
    journal["attempts"][-1].update(status="failed", error=str(error), files=files,
                                  finished=dt.datetime.now(dt.timezone.utc).isoformat())
    save(archive / "attempt.json", journal["attempts"][-1])
    save(journal_path, journal)


def begin(journal_path, identity, cmd, outputs, archive_root):
    identity = {**identity, "max_attempts": MAX_ATTEMPTS}
    journal = read(journal_path, {**identity, "attempts": []})
    if any(journal.get(k) != v for k, v in identity.items()):
        raise ValueError("arena attempt journal does not match this batch")
    if journal["attempts"] and journal["attempts"][-1]["status"] == "running":
        failed(journal_path, journal, outputs, archive_root, "interrupted unsealed arena attempt")
    elif not journal["attempts"] and any(pathlib.Path(p).exists() for p in outputs):
        journal["attempts"].append({"status": "running", "command": None, "legacy": True})
        failed(journal_path, journal, outputs, archive_root, "unsealed legacy arena outputs")
    if len(journal["attempts"]) >= MAX_ATTEMPTS:
        raise RetryLimit(journal_path)
    journal["attempts"].append({"status": "running", "command": cmd,
                                "started": dt.datetime.now(dt.timezone.utc).isoformat()})
    save(journal_path, journal)
    for path in map(pathlib.Path, outputs):
        path.unlink(missing_ok=True)
    return journal


def passed(journal_path, journal):
    journal["attempts"][-1].update(status="passed", finished=dt.datetime.now(dt.timezone.utc).isoformat())
    save(journal_path, journal)
