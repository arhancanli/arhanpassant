"""Prepare and run isolated, matched fine-tuning controls for teacher labels.

Group held-out positions by board and side to move, excluding clocks and scores.
Keep identical anchors and batches for original-target and teacher-target models.
A pilot validates mechanics only; model promotion requires a fresh strength gate.
"""

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import struct

REC = 32


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def save(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def board_key(raw):
    if len(raw) != REC or raw[26] > 2 or raw[27] > 1 or raw[31]:
        raise ValueError("invalid record metadata")
    count = bin(int.from_bytes(raw[:8], "little")).count("1")
    if not 2 <= count <= 32:
        raise ValueError("invalid occupied-square count")
    pieces = bytearray(raw[8:8 + (count + 1) // 2])
    for i in range(count):
        if ((pieces[i // 2] >> (4 * (i % 2))) & 7) > 5:
            raise ValueError("invalid piece code")
    if count % 2:
        pieces[-1] &= 15
    return raw[:8] + pieces + raw[27:28]


def held_out(key, seed, fraction):
    draw = int.from_bytes(hashlib.sha256(struct.pack("<Q", seed) + key).digest()[:8], "little")
    return draw < int(fraction * 2**64)


def records(path, limit=0, prefix=False):
    length = path.stat().st_size
    if length % REC and not prefix:
        raise ValueError(f"partial record in {path}")
    n = min(length // REC, limit) if prefix or limit else length // REC
    with path.open("rb") as f:
        for _ in range(n):
            raw = f.read(REC)
            if len(raw) != REC:
                raise ValueError(f"input shortened while reading {path}")
            yield raw


def headroom(path, estimated_bytes, floor_gib):
    if not math.isfinite(floor_gib) or floor_gib < 0:
        raise ValueError("invalid free-space floor")
    if shutil.disk_usage(path).free - estimated_bytes < floor_gib * 2**30:
        raise ValueError("insufficient disk headroom above configured free-space floor")


def experimental_output(out):
    if "selfplay" in out.resolve().parts:
        raise ValueError("experimental outputs must stay outside live self-play")


def prefix_digest(path, length):
    h = hashlib.sha256()
    with path.open("rb") as f:
        while length:
            block = f.read(min(length, 1 << 20))
            if not block:
                raise ValueError("anchor prefix shortened")
            h.update(block)
            length -= len(block)
    return h.hexdigest()


def prepare(corpus, validation, out, anchors=(), max_anchors=0,
            seed=20261031, fraction=0.1, floor_gib=8):
    if not 0 < fraction < 1 or not 0 <= seed < 2**64:
        raise ValueError("invalid held-out fraction or seed")
    if anchors and max_anchors < 1:
        raise ValueError("anchor input requires a bounded positive record budget")
    corpus, validation, out = Path(corpus), Path(validation), Path(out)
    experimental_output(out)
    proof = json.loads(validation.read_text())
    source = json.loads((corpus / "report.json").read_text())
    if proof.get("status") != "complete" or source.get("status") != "complete":
        raise ValueError("incomplete teacher corpus validation")
    if Path(proof["corpus_report"]).resolve() != (corpus / "report.json").resolve():
        raise ValueError("validation belongs to another corpus")
    if source["summary"].get("target_mode") != "wdl":
        raise ValueError("paired study requires explicit WDL targets")
    hashes = proof["artifacts_sha256"]
    paths = [corpus / name for name in ["baseline.bin", "teacher.bin", "report.json"]]
    for path in paths:
        if digest(path) != hashes[path.name]:
            raise ValueError(f"teacher corpus changed: {path.name}")
    count = paths[0].stat().st_size // REC
    if count != source["summary"]["labelled"] or paths[1].stat().st_size != count * REC:
        raise ValueError("paired record counts differ")
    headroom(out.parent, (count + max_anchors) * REC * 2, floor_gib)
    out.mkdir(parents=True, exist_ok=False)
    report = {"status": "running", "started": dt.datetime.now(dt.timezone.utc).isoformat(),
              "teacher_validation": str(validation.resolve()), "validation_sha256": digest(validation),
              "source_sha256": {str(p.resolve()): digest(p) for p in paths},
              "script_sha256": digest(__file__), "split_seed": seed, "held_out_fraction": fraction,
              "anchor_budget": max_anchors, "anchors": [],
              "limitations": "Held out from this fine-tuning study and its replay, not guaranteed absent from historical champion pretraining. Conditional teacher sample; no model/strength claim."}
    groups = {"train": set(), "val": set()}
    counts = {"teacher_train": 0, "teacher_val": 0, "anchor_train": 0, "anchor_val": 0,
              "anchors_excluded_teacher_board": 0}
    try:
        with contextlib.ExitStack() as stack:
            files = {(side, split): stack.enter_context((out / f"{side}-{split}.bin").open("xb"))
                     for side in ["baseline", "teacher"] for split in ["train", "val"]}
            teacher_keys = set()
            for old, new in zip(records(paths[0]), records(paths[1])):
                if old[:24] != new[:24] or old[26:] != new[26:]:
                    raise ValueError("paired corpus changed non-score bytes")
                key = board_key(old)
                teacher_keys.add(key)
                split = "val" if held_out(key, seed, fraction) else "train"
                groups[split].add(key)
                counts[f"teacher_{split}"] += 1
                files["baseline", split].write(old)
                files["teacher", split].write(new)
            remaining = max_anchors
            for path in map(Path, anchors):
                if remaining <= 0:
                    break
                frozen_length = min(path.stat().st_size // REC, remaining) * REC
                h = hashlib.sha256()
                used = 0
                for raw in records(path, frozen_length // REC, prefix=True):
                    h.update(raw)
                    used += 1
                    key = board_key(raw)
                    if key in teacher_keys:
                        counts["anchors_excluded_teacher_board"] += 1
                        continue
                    split = "val" if held_out(key, seed, fraction) else "train"
                    groups[split].add(key)
                    counts[f"anchor_{split}"] += 1
                    for side in ["baseline", "teacher"]:
                        files[side, split].write(raw)
                report["anchors"].append({"path": str(path.resolve()), "prefix_bytes": frozen_length,
                                          "prefix_sha256": h.hexdigest(), "records_read": used})
                if prefix_digest(path, frozen_length) != h.hexdigest():
                    raise ValueError("anchor prefix changed while preparing")
                remaining -= used
        if groups["train"] & groups["val"]:
            raise ValueError("held-out board leaked into training")
        if not counts["teacher_train"] or not counts["teacher_val"]:
            raise ValueError("need teacher records in both splits")
        for path in paths:
            if digest(path) != hashes[path.name]:
                raise ValueError("teacher input changed during preparation")
        report.update(status="complete", counts=counts,
                      distinct_groups={split: len(g) for split, g in groups.items()},
                      files={p.name: {"bytes": p.stat().st_size, "sha256": digest(p)} for p in out.glob("*.bin")})
    except BaseException as error:
        report.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        report["finished"] = dt.datetime.now(dt.timezone.utc).isoformat()
        save(out / "prepared.json", report)
    return report


def train_controls(prepared, checkpoint, champion, engine, out, epochs=2,
                   batch=256, lr=1e-5, seed=20261031, workers=4, threads=4,
                   outcome_weight=0.3, floor_gib=8, pilot=False):
    # Keep Torch optional for preparation and lightweight CI regression tests.
    import numpy as np
    import torch
    import train
    import check_pipeline
    from stream import prefetch
    prepared, checkpoint, champion, engine, out = map(Path, [prepared, checkpoint, champion, engine, out])
    engine = engine.resolve()
    experimental_output(out)
    manifest = json.loads((prepared / "prepared.json").read_text())
    if manifest.get("status") != "complete":
        raise ValueError("study preparation incomplete")
    if min(epochs, batch, workers, threads) < 1 or not math.isfinite(lr) or lr <= 0:
        raise ValueError("invalid training budget")
    if workers + threads + 1 > (os.cpu_count() or 1) or not 0 <= outcome_weight <= 1:
        raise ValueError("invalid CPU or outcome-weight budget")
    inputs = [prepared / name for name in manifest["files"]]
    for path in inputs:
        expected = manifest["files"][path.name]
        if path.stat().st_size != expected["bytes"] or digest(path) != expected["sha256"]:
            raise ValueError("prepared corpus changed")
    headroom(out.parent, checkpoint.stat().st_size * 6, floor_gib)
    out.mkdir(parents=True, exist_ok=False)
    frozen = {str(p.resolve()): digest(p) for p in inputs + [prepared / "prepared.json", checkpoint, champion, engine]}
    report = {"status": "running", "started": dt.datetime.now(dt.timezone.utc).isoformat(),
              "inputs_sha256": frozen, "script_sha256": digest(__file__), "pilot": pilot,
              "settings": {"epochs": epochs, "batch": batch, "lr": lr, "seed": seed,
                           "workers": workers, "threads": threads, "outcome_weight": outcome_weight},
              "models": {}, "limitations": "Fixed-epoch matched fine-tuning controls. Same board groups, anchors, initialization, shuffled batch order and optimizer settings. MPS arithmetic may not be bitwise deterministic. Pilot/held-out losses cannot establish engine strength. No promotion."}
    try:
        torch.set_num_threads(threads)
        torch.set_num_interop_threads(1)
        weights = torch.load(checkpoint, map_location="cpu", weights_only=True)
        hidden = weights["ft.weight"].shape[1]
        ib, ob = weights["ft.weight"].shape[0] // 768, weights["out.weight"].shape[0]
        train.set_layout(ib, ob)
        initial = train.Net(hidden)
        initial.load_state_dict(weights, strict=True)
        initial_path = out / "initial.nnue"
        train.export(initial, initial_path)
        if digest(initial_path) != digest(champion):
            raise ValueError("checkpoint does not reproduce the accepted champion NNUE")
        report["initial_export_matches_champion"] = True
        report["layout"] = {"hidden": hidden, "input_buckets": ib, "output_buckets": ob}
        device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")
        report["device"] = str(device)

        class FullRows(train.Dataset):
            def chunks(self, blocks, batch_size, rng=None, buffer_blocks=64):
                order = list(blocks)
                if rng is not None:
                    rng.shuffle(order)
                for i in range(0, len(order), buffer_blocks):
                    buf = np.concatenate([self.read(b) for b in order[i:i + buffer_blocks]])
                    if rng is not None:
                        buf = buf[rng.permutation(len(buf))]
                    # Include partial last batches so held-out rows never vanish.
                    for j in range(0, len(buf), batch_size):
                        yield buf[j:j + batch_size]

        datasets = {}
        for side in ["baseline", "teacher"]:
            datasets[side] = {split: FullRows([str(prepared / f"{side}-{split}.bin")], val_every=1 if split == "val" else 10**12)
                              for split in ["train", "val"]}
        def evaluate(net):
            net.eval()
            sums = {"original_target_mse": 0., "teacher_target_mse": 0., "outcome_mse": 0.}
            count = 0
            old, new = datasets["baseline"]["val"], datasets["teacher"]["val"]
            with torch.no_grad():
                for a, b in zip(old.chunks(old.val_blocks, batch), new.chunks(new.val_blocks, batch)):
                    if len(a) != len(b) or not np.array_equal(a[:, :24], b[:, :24]) or not np.array_equal(a[:, 26:], b[:, 26:]):
                        raise ValueError("held-out paired rows differ")
                    inputs, score, outcome = train.to_inputs(train.decode(a), device)
                    pred = torch.sigmoid(net(*inputs))
                    _, teacher_score, _ = train.to_inputs(train.decode(b), device)
                    for name, target in [("original_target_mse", (1 - outcome_weight) * torch.sigmoid(score / train.SCALE) + outcome_weight * outcome),
                                         ("teacher_target_mse", (1 - outcome_weight) * torch.sigmoid(teacher_score / train.SCALE) + outcome_weight * outcome),
                                         ("outcome_mse", outcome)]:
                        sums[name] += float(torch.sum((pred - target) ** 2))
                    count += len(a)
            if count != old.n_val:
                raise ValueError("held-out evaluation incomplete")
            net.train()
            return {"records": count, **{k: v / count for k, v in sums.items()}}
        report["initial_held_out"] = evaluate(initial.to(device))
        del initial
        for side in ["baseline", "teacher"]:
            torch.manual_seed(seed)
            rng = np.random.default_rng(seed)
            net = train.Net(hidden).to(device)
            net.load_state_dict(weights, strict=True)
            opt = torch.optim.AdamW(net.parameters(), lr=lr, weight_decay=0.)
            data = datasets[side]["train"]
            epochs_report = []
            batch_hash = hashlib.sha256()
            for epoch in range(1, epochs + 1):
                total, n = 0., 0
                for length, decoded in prefetch(data.batches(data.train_blocks, batch, rng, workers=workers)):
                    features, score, outcome = train.to_inputs(decoded, device)
                    # Fingerprint the paired batch's features and outcomes, not differing scores.
                    for part in decoded[:4] + (decoded[5],):
                        batch_hash.update(np.ascontiguousarray(part).tobytes())
                    target = (1 - outcome_weight) * torch.sigmoid(score / train.SCALE) + outcome_weight * outcome
                    loss = torch.mean((torch.sigmoid(net(*features)) - target) ** 2)
                    if not torch.isfinite(loss):
                        raise ValueError("nonfinite training loss")
                    opt.zero_grad(set_to_none=True)
                    loss.backward()
                    opt.step()
                    with torch.no_grad():
                        net.out.weight.clamp_(-1.98, 1.98)
                    total += float(loss.detach()) * length
                    n += length
                if n != data.n_train:
                    raise ValueError("training epoch skipped records")
                entry = {"epoch": epoch, "records": n, "train_mse": total / n, "held_out": evaluate(net)}
                epochs_report.append(entry)
                print(f"{side} epoch {epoch}: {json.dumps(entry)}", flush=True)
            network = out / f"{side}.nnue"
            train.export(net, network)
            torch.save(net.cpu().state_dict(), out / f"{side}.pt")
            raw = np.fromfile(prepared / "baseline-val.bin", dtype=np.uint8).reshape(-1, REC)[:96]
            fens = [check_pipeline.fen_of(r) for r in raw]
            native = check_pipeline.engine_evals(str(engine), str(network), fens)
            integer = check_pipeline.emulate(str(network), raw)
            if native != integer:
                raise ValueError("exported network disagrees with native integer evaluation")
            report["models"][side] = {"epochs": epochs_report, "batch_features_outcomes_sha256": batch_hash.hexdigest(),
                                       "network_sha256": digest(network), "integer_native_agreement": len(raw)}
            del net, opt
        if report["models"]["baseline"]["batch_features_outcomes_sha256"] != report["models"]["teacher"]["batch_features_outcomes_sha256"]:
            raise ValueError("paired controls saw different batch orders or features")
        for path, expected in frozen.items():
            if digest(path) != expected:
                raise ValueError("study input changed during training")
        report["status"] = "complete"
        report["artifacts_sha256"] = {p.name: digest(p) for p in out.iterdir() if p.is_file()}
    except BaseException as error:
        report.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        report["finished"] = dt.datetime.now(dt.timezone.utc).isoformat()
        save(out / "study.json", report)
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--corpus", type=Path, required=True)
    prep.add_argument("--validation", type=Path, required=True)
    prep.add_argument("--out", type=Path, required=True)
    prep.add_argument("--anchor", type=Path, action="append", default=[])
    prep.add_argument("--max-anchors", type=int, default=0)
    prep.add_argument("--seed", type=int, default=20261031)
    prep.add_argument("--held-out-fraction", type=float, default=0.1)
    prep.add_argument("--min-free-gb", type=float, default=8)
    fit = sub.add_parser("train")
    for name in ["prepared", "checkpoint", "champion", "engine", "out"]:
        fit.add_argument("--" + name, type=Path, required=True)
    for name, default in [("epochs", 2), ("batch", 256), ("seed", 20261031), ("workers", 4), ("threads", 4)]:
        fit.add_argument("--" + name, type=int, default=default)
    fit.add_argument("--lr", type=float, default=1e-5)
    fit.add_argument("--outcome-weight", type=float, default=0.3)
    fit.add_argument("--min-free-gb", type=float, default=8)
    fit.add_argument("--pilot", action="store_true")
    args = ap.parse_args()
    if args.command == "prepare":
        report = prepare(args.corpus, args.validation, args.out, args.anchor, args.max_anchors,
                         args.seed, args.held_out_fraction, args.min_free_gb)
        print(json.dumps(report["counts"], indent=2))
    else:
        report = train_controls(args.prepared, args.checkpoint, args.champion, args.engine, args.out,
                                args.epochs, args.batch, args.lr, args.seed, args.workers, args.threads,
                                args.outcome_weight, args.min_free_gb, args.pilot)
        print(json.dumps({"status": report["status"], "device": report["device"], "layout": report["layout"]}))


if __name__ == "__main__":
    main()
