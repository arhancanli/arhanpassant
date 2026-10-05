import contextlib
import copy
import io
import json
import pathlib
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import local_gate
import loop
import macbook


class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        for name in ("engine", "arena", "candidate", "champion", "book"):
            (self.root / name).write_bytes(name.encode())
        self.kw = dict(engine=str(self.root / "engine"), arena=str(self.root / "arena"),
                       candidate=str(self.root / "candidate"), champion=str(self.root / "champion"),
                       book=str(self.root / "book"), tc="8+0.08", concurrency=2, max_games=12,
                       elo0=0.0, elo1=5.0, cand_opts={"cont4": "1"}, champ_opts={},
                       out=str(self.root / "gate.json"), logfile=str(self.root / "gate.log"),
                       log=lambda _: None, batch_games=4, max_batches=1)
        self.seeds = []

    def tearDown(self):
        self.tmp.cleanup()

    def arena(self, cmd, logfile):
        seed = int(cmd[cmd.index("--seed") + 1])
        self.seeds.append(seed)
        self.batch(cmd[cmd.index("--out") + 1], seed)
        return 0

    def batch(self, path, seed, config=None):
        value = {"seed": seed, "games": 4, "wins": 1, "losses": 1, "draws": 2,
                 "penta": [0, 1, 0, 1, 0], "seconds": 3, "reasons": {"adjudicated": 4}}
        if config:
            value["gate_config"] = config
        local_gate.save(path, value)

    def test_restart_reuses_completed_pairs_without_counting_them_twice(self):
        with patch.object(local_gate, "run", self.arena):
            first = local_gate.gate(**self.kw)
            second = local_gate.gate(**self.kw)
            third = local_gate.gate(**self.kw)
            again = local_gate.gate(**self.kw)
        self.assertEqual((first["games"], second["games"], third["games"], again["games"]), (4, 8, 12, 12))
        self.assertEqual(self.seeds, [first["seed"] + i for i in range(3)])
        self.assertEqual(third["decision"], "inconclusive")

    def test_completed_batch_survives_crash_before_aggregate_checkpoint(self):
        with patch.object(local_gate, "run", self.arena):
            first = local_gate.gate(**self.kw)
        self.batch(self.kw["out"] + ".batch.json", first["seed"] + 1, first["config"])
        with patch.object(local_gate, "run", side_effect=AssertionError("must reuse saved batch")):
            resumed = local_gate.gate(**self.kw)
        self.assertEqual(resumed["games"], 8)

    def test_changed_network_starts_a_new_gate_and_keeps_old_evidence(self):
        with patch.object(local_gate, "run", self.arena):
            local_gate.gate(**self.kw)
            (self.root / "candidate").write_bytes(b"new network")
            restarted = local_gate.gate(**self.kw)
        self.assertEqual(restarted["games"], 4)
        self.assertEqual(len(list(self.root.glob("gate.json.superseded-*"))), 1)

    def test_failed_games_and_partial_pairs_cannot_be_promoted(self):
        self.batch(str(self.root / "bad.json"), 1)
        valid = json.loads((self.root / "bad.json").read_text())
        local_gate.validate_batch(valid, 4)
        for reason in ("engine failure", "illegal move", "loses on time"):
            broken = {**valid, "reasons": {reason: 1}}
            with self.assertRaises(ValueError):
                local_gate.validate_batch(broken, 4)
        with self.assertRaises(ValueError):
            local_gate.validate_batch({**valid, "games": 2}, 4)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        (self.root / "forge").mkdir()
        (self.root / "nets").mkdir()
        self.args = types.SimpleNamespace(data=str(self.root), no_publish=False, fleet=False,
                                         max_data_gb=512 / 2**30, min_free_gb=0)

    def tearDown(self):
        self.tmp.cleanup()

    def test_disk_retention_only_removes_complete_already_trained_chunks(self):
        old = self.root / "selfplay/gen1/old.bin"
        fresh = self.root / "selfplay/gen2/new.bin"
        growing = self.root / "selfplay/gen1/growing.bin"
        for path, n in ((old, 300), (fresh, 300), (growing, 100)):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(bytes(n))
        state = {"sizes": {str(old): 300, str(growing): 50}}
        with patch.object(loop.shutil, "disk_usage", return_value=types.SimpleNamespace(free=100 * 2**30)):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(loop.storage_ready(self.args, state))
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        self.assertEqual(growing.stat().st_size, 100)

    def test_promotion_commits_the_champion_before_publication_and_clears_pending(self):
        candidate = self.root / "nets/candidate.nnue"
        candidate.write_bytes(b"tested network")
        original = {"champion_version": "0.11.0", "generation": 10,
                    "pending": {"candidate": str(candidate)}}
        result = {"decision": "H1", "games": 1104, "elo": 26.5, "elo_lo": 14.2,
                  "elo_hi": 38.8, "sprt": {"llr": 2.98}}
        observed = []
        def publish(log):
            saved = json.loads((self.root / "forge/state.json").read_text())
            observed.append(saved["champion_version"])
            self.assertNotIn("pending", saved)
        ledger = str(self.root / "ledger.json")
        with patch.object(loop, "LEDGER", ledger), patch.object(loop.publish_data, "publish_quietly", publish):
            with contextlib.redirect_stdout(io.StringIO()):
                loop.promote_if_passed(self.args, copy.deepcopy(original), str(candidate), result, "candidate")
                # Recover the gap between the ledger write and state write.
                loop.promote_if_passed(self.args, copy.deepcopy(original), str(candidate), result, "candidate")
        self.assertEqual(observed, ["0.12.0", "0.12.0"])
        self.assertEqual(len(json.loads(pathlib.Path(ledger).read_text())["versions"]), 1)
        self.assertEqual((self.root / "nets/champion-0.12.0.nnue").read_bytes(), candidate.read_bytes())

    def test_acceptance_recovers_after_a_crash_before_dequeuing(self):
        engine, net = self.root / "engine", self.root / "nets/champion.nnue"
        engine.write_bytes(b"engine")
        net.write_bytes(b"network")
        args = types.SimpleNamespace(data=str(self.root), engine=str(engine), arena="arena", book="book",
                                     concurrency=8, cpu_budget=16, max_games=12000, tc="8+0.08",
                                     batch_games=64, no_publish=True)
        state = {"champion_net": str(net), "selfplay_threads": 8}
        item = {"name": "cont4", "change": "continuation", "opts": {"cont4": "1"}}
        queue = {"pending": [item], "done": []}
        loop.save_json(str(self.root / "forge/queue.json"), queue)
        loop.save_json(str(self.root / "forge/search.json"), {"accepted": {}, "history": []})
        def passed(**kw):
            result = {"decision": "H1", "games": 1104, "elo": 26.5, "elo_lo": 14.2,
                      "elo_hi": 38.8, "sprt": {"llr": 2.98}, "config": {"champion_options": {}}}
            local_gate.save(kw["out"], result)
            return result
        with patch.object(loop, "ROOT", str(self.root)), patch.object(local_gate, "gate", passed):
            with contextlib.redirect_stdout(io.StringIO()):
                loop.search_batch(args, state, str(self.root / "forge/log"))
        # Restore just the queue to model a crash after acceptance was saved.
        loop.save_json(str(self.root / "forge/queue.json"), queue)
        with patch.object(loop, "ROOT", str(self.root)):
            with patch.object(local_gate, "gate", side_effect=AssertionError("accepted gate must not rerun")):
                loop.search_batch(args, state, str(self.root / "forge/log"))
        saved = json.loads((self.root / "forge/queue.json").read_text())
        self.assertEqual(saved["pending"], [])
        self.assertEqual(len(saved["done"]), 1)
        self.assertEqual(json.loads((self.root / "forge/search.json").read_text())["accepted"], {"cont4": "1"})
        self.assertEqual(len(json.loads((self.root / "forge/tests.json").read_text())["tests"]), 1)

    def test_service_replacement_retries_the_launchd_unload_race(self):
        busy = types.SimpleNamespace(returncode=5, stderr="Input/output error", stdout="")
        ready = types.SimpleNamespace(returncode=0, stderr="", stdout="")
        with patch.object(macbook, "launch", side_effect=[busy, ready]) as launch:
            with patch.object(macbook.time, "sleep"):
                macbook.bootstrap("gui/501")
        self.assertEqual(launch.call_count, 2)

    def test_search_reclaims_cores_when_a_generator_dies_or_disk_pauses(self):
        args = types.SimpleNamespace(cpu_budget=18, concurrency=18)
        state = {"selfplay_pid": 23, "selfplay_threads": 9}
        with patch.object(loop, "alive", return_value=True):
            self.assertEqual(loop.gate_workers(args, state), 9)
        with patch.object(loop, "alive", return_value=False):
            self.assertEqual(loop.gate_workers(args, state), 18)
            loop.stop_selfplay(self.args, state)
        self.assertNotIn("selfplay_pid", state)
        self.assertEqual(state["selfplay_threads"], 0)


if __name__ == "__main__":
    unittest.main()
