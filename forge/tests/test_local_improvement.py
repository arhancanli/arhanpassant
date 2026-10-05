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

    def test_mid_batch_binary_changes_are_not_counted_as_the_original_build(self):
        def changed(cmd, logfile):
            self.arena(cmd, logfile)
            (self.root / "engine").write_bytes(b"replacement build")
            return 0
        with patch.object(local_gate, "run", changed):
            with self.assertRaisesRegex(RuntimeError, "gate input changed while testing"):
                local_gate.gate(**self.kw)
        saved = json.loads(pathlib.Path(self.kw["out"]).read_text())
        self.assertEqual(saved["games"], 0)

    def test_engine_symlink_is_resolved_before_launching_matches(self):
        link = self.root / "current"
        link.symlink_to(self.root / "engine")
        replacement = self.root / "replacement"
        replacement.write_bytes(b"replacement build")
        def switched(cmd, logfile):
            link.unlink()
            link.symlink_to(replacement)
            self.assertIn(f"cmd={(self.root / 'engine').resolve()}", cmd)
            return self.arena(cmd, logfile)
        with patch.object(local_gate, "run", switched):
            result = local_gate.gate(**{**self.kw, "engine": str(link)})
        self.assertEqual(result["games"], 4)


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
                      "elo_hi": 38.8, "sprt": {"llr": 2.98},
                      "config": {"champion_options": {}, "candidate_options": {"cont4": "1"}}}
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

    def test_multithreaded_matches_fit_the_cpu_budget_and_can_reserve_all_cores(self):
        args = types.SimpleNamespace(cpu_budget=18, concurrency=18, selfplay_threads=18)
        item = {"opts": {"smp_vote": "1"}, "common_options": {"Threads": "3"}}
        self.assertEqual(loop.selfplay_workers(args, item), 9)
        state = {"selfplay_pid": 23, "selfplay_threads": 9}
        with patch.object(loop, "alive", return_value=True):
            self.assertEqual(loop.gate_workers(args, state, 3), 3)
            with self.assertRaises(ValueError):
                loop.gate_workers(args, state, 18)
        with patch.object(loop, "alive", return_value=False):
            self.assertEqual(loop.gate_workers(args, state, 3), 6)
        item["common_options"]["Threads"] = "18"
        self.assertEqual(loop.selfplay_workers(args, item), 0)
        with patch.object(loop, "stop_selfplay") as stop:
            loop.ensure_selfplay(args, state, 0)
            stop.assert_called_once_with(args, state)
        for value in ("0", "19", "bad"):
            item["common_options"]["Threads"] = value
            with self.assertRaises(ValueError):
                loop.search_threads_for(args, item)
        with self.assertRaises(ValueError):
            loop.search_threads_for(args, {"opts": {"Threads": "3"}})

    def test_common_match_options_are_equal_and_are_not_promoted_as_search_settings(self):
        engine, net = self.root / "engine", self.root / "nets/champion.nnue"
        engine.write_bytes(b"engine")
        net.write_bytes(b"network")
        args = types.SimpleNamespace(data=str(self.root), engine=str(engine), arena="arena", book="book",
                                     concurrency=18, cpu_budget=18, max_games=12000, tc="8+0.08",
                                     batch_games=64, no_publish=True)
        item = {"name": "smp-vote-3t", "change": "helper voting", "opts": {"smp_vote": "1"},
                "common_options": {"Threads": "3"}}
        loop.save_json(str(self.root / "forge/queue.json"), {"pending": [item], "done": []})
        loop.save_json(str(self.root / "forge/search.json"), {"accepted": {"tm_nodes": "1"}, "history": []})
        def passed(**kw):
            self.assertEqual(kw["cand_opts"], {"tm_nodes": "1", "Threads": "3", "smp_vote": "1"})
            self.assertEqual(kw["champ_opts"], {"tm_nodes": "1", "Threads": "3"})
            self.assertEqual(kw["concurrency"], 3)
            return {"decision": "H1", "games": 1104, "elo": 26.5, "elo_lo": 14.2,
                    "elo_hi": 38.8, "sprt": {"llr": 2.98}}
        state = {"champion_net": str(net), "selfplay_pid": 23, "selfplay_threads": 9}
        with patch.object(loop, "ROOT", str(self.root)), patch.object(local_gate, "gate", passed):
            with patch.object(loop, "alive", return_value=True), contextlib.redirect_stdout(io.StringIO()):
                loop.search_batch(args, state, str(self.root / "forge/log"))
        accepted = json.loads((self.root / "forge/search.json").read_text())["accepted"]
        self.assertEqual(accepted, {"tm_nodes": "1", "smp_vote": "1"})

    def test_binary_comparison_uses_both_saved_builds_and_the_same_options(self):
        engine, baseline, net = self.root / "engine", self.root / "baseline", self.root / "nets/champion.nnue"
        for path in (engine, baseline, net):
            path.write_bytes(path.name.encode())
        args = types.SimpleNamespace(data=str(self.root), engine="unused-default", arena="arena", book="book",
                                     concurrency=18, cpu_budget=18, max_games=12000, tc="8+0.08",
                                     batch_games=64, no_publish=True)
        item = {"name": "neon-output", "change": "vector output", "opts": {},
                "candidate_engine": str(engine), "baseline_engine": str(baseline)}
        loop.save_json(str(self.root / "forge/queue.json"), {"pending": [item], "done": []})
        loop.save_json(str(self.root / "forge/search.json"), {"accepted": {"tm_nodes": "1"}, "history": []})
        def running(**kw):
            self.assertEqual(kw["engine"], str(engine))
            self.assertEqual(kw["baseline_engine"], str(baseline))
            self.assertEqual(kw["candidate"], str(net))
            self.assertEqual(kw["champion"], str(net))
            self.assertEqual(kw["cand_opts"], kw["champ_opts"])
            self.assertIn("-vs-", kw["out"])
            return {"decision": "running"}
        with patch.object(local_gate, "gate", running):
            loop.search_batch(args, {"champion_net": str(net)}, str(self.root / "forge/log"))

    def test_ready_training_finishes_the_checkpointed_search_after_restart(self):
        engine, arena, net, book = (self.root / n for n in ("engine", "arena", "network", "book"))
        for path in (engine, arena, net, book):
            path.write_bytes(path.name.encode())
        data = self.root / "selfplay/gen1/data.bin"
        data.parent.mkdir(parents=True)
        data.write_bytes(bytes(4 * 32))
        item = {"name": "cont4", "change": "continuation", "opts": {"cont4": "1"}}
        state = {"generation": 1, "champion_net": str(net), "champion_version": "0.12.0",
                 "trained_on": 0, "attempts": 0, "sizes": {}}
        loop.save_json(str(self.root / "forge/state.json"), state)
        loop.save_json(str(self.root / "forge/queue.json"), {"pending": [item], "done": []})
        loop.save_json(str(self.root / "forge/search.json"), {"accepted": {}, "history": []})
        args = types.SimpleNamespace(data=str(self.root), engine=str(engine), arena=str(arena), book=str(book),
                                     concurrency=1, cpu_budget=2, selfplay_threads=1, tc="8+0.08",
                                     max_games=12, batch_games=4)
        _, _, _, out = loop.search_evidence(args, state, item)
        def arena_batch(cmd, logfile):
            local_gate.save(cmd[cmd.index("--out") + 1], {
                "seed": int(cmd[cmd.index("--seed") + 1]), "games": 4, "wins": 1, "losses": 1,
                "draws": 2, "penta": [0, 1, 0, 1, 0], "seconds": 1, "reasons": {"adjudicated": 4}})
            return 0
        with patch.object(local_gate, "run", arena_batch):
            local_gate.gate(engine=str(engine), arena=str(arena), candidate=str(net), champion=str(net),
                            book=str(book), tc="8+0.08", concurrency=1, max_games=12,
                            elo0=0, elo1=5, cand_opts=item["opts"], champ_opts={},
                            out=out, logfile=str(self.root / "log"), log=lambda _: None,
                            batch_games=4, max_batches=1)
        # A freshly started controller has no live generator. Planning its
        # upcoming allocation must preserve the old gate's concurrency.
        self.assertTrue(loop.search_has_progress(args, state, item, ready=True))
        argv = ["loop.py", "--data", str(self.root), "--engine", str(engine), "--arena", str(arena),
                "--book", str(book), "--cpu-budget", "2", "--selfplay-threads", "1", "--concurrency", "1",
                "--min-new", "1", "--max-games", "12", "--batch-games", "4", "--min-free-gb", "0",
                "--local-search", "--once", "--no-publish"]
        with patch.object(sys, "argv", argv), patch.object(loop, "ensure_selfplay"), patch.object(loop.signal, "signal"):
            with patch.object(loop, "run", side_effect=AssertionError("training preempted the active test")):
                with patch.object(local_gate, "run", arena_batch), contextlib.redirect_stdout(io.StringIO()):
                    loop.main()
        saved = json.loads((self.root / "forge/state.json").read_text())
        self.assertEqual(saved["training_deferred_for"], "cont4")
        self.assertEqual(saved["attempts"], 0)
        self.assertEqual(json.loads(pathlib.Path(out).read_text())["games"], 8)
        changed = {**item, "opts": {"cont4": "0"}}
        self.assertFalse(loop.search_has_progress(args, state, changed, ready=True))
        # A name in the queue alone must not postpone a ready training round.
        self.assertFalse(loop.search_has_progress(args, state, {**item, "name": "unstarted"}, ready=True))
        saved_gate = json.loads(pathlib.Path(out).read_text())
        saved_gate["games"] = 0
        local_gate.save(out, saved_gate)
        self.assertFalse(loop.search_has_progress(args, state, item, ready=True))

    def test_acceptance_gap_is_recovered_before_a_new_network_changes_the_test_identity(self):
        engine, net = self.root / "engine", self.root / "network"
        engine.write_bytes(b"engine")
        net.write_bytes(b"network")
        args = types.SimpleNamespace(data=str(self.root), engine=str(engine))
        state = {"champion_net": str(net)}
        item = {"name": "cont4", "opts": {"cont4": "1"}}
        _, _, _, out = loop.search_evidence(args, state, item)
        local_gate.save(out, {"decision": "H1", "games": 1104,
                              "config": {"champion_options": {}, "candidate_options": {"cont4": "1"}}})
        loop.save_json(str(self.root / "forge/search.json"), {
            "accepted": {"cont4": "1"}, "history": [{"evidence": pathlib.Path(out).name}]})
        self.assertTrue(loop.search_has_progress(args, state, item, ready=True))
        self.assertFalse(loop.accepted_checkpoint(json.loads(pathlib.Path(out).read_text()),
                                                  {**item, "opts": {"cont4": "0"}},
                                                  json.loads((self.root / "forge/search.json").read_text()), out))


if __name__ == "__main__":
    unittest.main()
