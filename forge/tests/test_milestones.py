import contextlib
import io
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
import milestones
import opponents


class MilestoneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        entries = []
        for name in opponents.NAMES:
            path = self.root / name
            path.write_bytes(name.encode())
            entries.append({"name": name, "path": str(path), "sha256": local_gate.digest(path),
                            "options": {"Threads": "1", "Hash": "64", "UCI_LimitStrength": "false"}})
        for name in ("engine", "network", "book", "arena"):
            (self.root / name).write_bytes(name.encode())
        catalog = self.root / "opponents/catalog.json"
        local_gate.save(str(catalog), {"opponents": entries})
        self.args = types.SimpleNamespace(data=str(self.root), milestone_catalog=str(catalog),
                                         engine=str(self.root / "engine"), arena=str(self.root / "arena"),
                                         book=str(self.root / "book"))
        self.state = {"champion_net": str(self.root / "network"), "champion_version": "0.12.0"}
        self.commands = []
        self.plan = patch.object(milestones, "PLAN", [("10+0.1", 8), ("60+0.6", 4)])
        self.budget = patch.object(milestones, "BATCH_GAMES", 4)
        self.plan.start()
        self.budget.start()

    def tearDown(self):
        self.budget.stop()
        self.plan.stop()
        self.tmp.cleanup()

    def snapshot(self):
        index = milestones.enqueue(self.args, self.state, {"cont4": "1"})
        directory = self.root / "forge/milestones" / index["runs"][0]["identity"]
        manifest = milestones.read(directory / "manifest.json")
        return directory, manifest

    def arena(self, cmd, logfile):
        self.commands.append(cmd)
        count = int(cmd[cmd.index("--games") + 1])
        batch = {"seed": int(cmd[cmd.index("--seed") + 1]), "games": count,
                 "wins": 0, "losses": 0, "draws": count, "penta": [0, 0, count // 2, 0, 0],
                 "seconds": 2, "reasons": {"adjudicated": count}}
        local_gate.save(cmd[cmd.index("--out") + 1], batch)
        pathlib.Path(cmd[cmd.index("--games-out") + 1]).write_text("{}\n" * count)
        return 0

    def test_every_opponent_is_required_and_later_networks_keep_previous_snapshot(self):
        directory, manifest = self.snapshot()
        self.assertEqual(len(manifest["matches"]), 28)
        (self.root / "network").write_bytes(b"later network")
        index = milestones.enqueue(self.args, self.state, {"cont4": "1"})
        self.assertEqual(len(index["runs"]), 2)
        self.assertEqual(pathlib.Path(manifest["paths"]["network"]).read_bytes(), b"network")
        self.assertEqual(len(milestones.enqueue(self.args, self.state, {"cont4": "1"})["runs"]), 2)
        catalog = milestones.read(self.args.milestone_catalog)
        catalog["opponents"].pop()
        local_gate.save(self.args.milestone_catalog, catalog)
        with self.assertRaisesRegex(ValueError, "every benchmark opponent"):
            milestones.enqueue(self.args, self.state, {})

    def test_external_nnue_is_preserved_and_complete_pairs_resume_once(self):
        directory, manifest = self.snapshot()
        match = manifest["matches"][0]
        with patch.object(local_gate, "run", self.arena):
            for _ in range(3):
                result = milestones.advance_match(manifest, match, directory, 9, "log")
        self.assertEqual(result["games"], 8)
        self.assertEqual(len(self.commands), 2)
        command = self.commands[0]
        sides = [i for i, value in enumerate(command) if value == "--engine"]
        self.assertTrue(any(s.startswith("opt.EvalFile=") for s in command[sides[0]:sides[1]]))
        self.assertFalse(any(s.startswith("opt.EvalFile=") for s in command[sides[1]:]))
        self.assertIn("opt.UCI_LimitStrength=false", command)
        self.assertEqual(result["status"], "complete")
        self.assertLess(result["score_ci95"][0], .5)
        self.assertGreater(result["score_ci95"][1], .5)

    def test_crash_after_batch_before_aggregate_recovers_without_replaying(self):
        directory, manifest = self.snapshot()
        match = manifest["matches"][0]
        save = local_gate.save
        def interrupted(path, value):
            if path == str(directory / match["file"]):
                raise RuntimeError("crash before aggregate")
            save(path, value)
        with patch.object(local_gate, "run", self.arena), patch.object(local_gate, "save", interrupted):
            with self.assertRaisesRegex(RuntimeError, "crash before aggregate"):
                milestones.advance_match(manifest, match, directory, 9, "log")
        with patch.object(local_gate, "run", side_effect=AssertionError("must recover")):
            result = milestones.advance_match(manifest, match, directory, 9, "log")
        self.assertEqual(result["games"], 4)
        self.assertEqual(len(self.commands), 1)

    def test_failed_games_changed_binaries_and_missing_records_are_not_counted(self):
        directory, manifest = self.snapshot()
        match = manifest["matches"][0]
        def broken(cmd, logfile):
            self.arena(cmd, logfile)
            path = cmd[cmd.index("--out") + 1]
            batch = milestones.read(path)
            batch["reasons"] = {"engine failure": 4}
            local_gate.save(path, batch)
            return 0
        with patch.object(local_gate, "run", broken), self.assertRaisesRegex(ValueError, "unreliable"):
            milestones.advance_match(manifest, match, directory, 9, "log")
        self.assertFalse((directory / match["file"]).exists())
        def missing_records(cmd, logfile):
            self.arena(cmd, logfile)
            pathlib.Path(cmd[cmd.index("--games-out") + 1]).write_text("{}\n")
            return 0
        with patch.object(local_gate, "run", missing_records), self.assertRaisesRegex(ValueError, "record count"):
            milestones.advance_match(manifest, match, directory, 9, "log")
        self.assertFalse((directory / match["file"]).exists())
        def changed(cmd, logfile):
            self.arena(cmd, logfile)
            pathlib.Path(manifest["config"]["opponents"][0]["path"]).write_bytes(b"replacement")
            return 0
        with patch.object(local_gate, "run", changed), self.assertRaisesRegex(RuntimeError, "input changed"):
            milestones.advance_match(manifest, match, directory, 9, "log")
        self.assertFalse((directory / match["file"]).exists())

    def test_round_robin_covers_the_suite_and_completion_requires_all_opponents(self):
        directory, _ = self.snapshot()
        with patch.object(local_gate, "run", self.arena):
            for _ in opponents.NAMES:
                milestones.advance(self.args, self.state, 9, "log", lambda _: None)
        report = milestones.read(directory / "report.json")
        self.assertEqual(report["status"], "running")
        self.assertTrue(all(m["games"] == 4 for m in report["matches"][:14]))
        self.assertTrue(all(m["games"] == 0 for m in report["matches"][14:]))
        with patch.object(local_gate, "run", self.arena):
            for _ in range(28):
                milestones.advance(self.args, self.state, 9, "log", lambda _: None)
        self.assertIsNone(milestones.pending(self.args.data))
        report = milestones.read(directory / "report.json")
        self.assertEqual((report["games"], report["status"]), (168, "complete"))

    def test_profile_and_multicore_search_share_one_eighteen_core_controller(self):
        config = macbook.profile(self.root, 18, 20_000_000)
        self.assertIn("--milestone-catalog", config["ProgramArguments"])
        args = types.SimpleNamespace(cpu_budget=18, concurrency=18, selfplay_threads=18)
        item = {"opts": {"smp_vote": "1"}, "common_options": {"Threads": "3"}}
        playing = loop.selfplay_workers(args, item)
        state = {"selfplay_threads": playing, "selfplay_pid": 123}
        with patch.object(loop, "alive", return_value=True):
            self.assertEqual(playing + loop.gate_workers(args, state, 3) * 3, 18)
            self.assertEqual(playing + loop.gate_workers(args, state), 18)

    def test_controller_advances_search_and_external_games_without_extra_cores(self):
        local_gate.save(str(self.root / "forge/state.json"), {**self.state, "generation": 1,
                                                              "trained_on": 0, "attempts": 0})
        local_gate.save(str(self.root / "forge/queue.json"), {
            "pending": [{"name": "smp", "opts": {"smp_vote": "1"}, "common_options": {"Threads": "3"}}]})
        calls = []
        def ensure(args, state, threads):
            self.assertEqual(threads, 9)
            state.update(selfplay_threads=threads, selfplay_pid=123)
        def search(args, state, logfile):
            calls.append("search")
            self.assertEqual(loop.gate_workers(args, state, 3), 3)
        def external(args, state, concurrency, logfile, log):
            calls.append("external")
            self.assertEqual(concurrency + state["selfplay_threads"], 18)
        argv = ["loop.py", "--data", str(self.root), "--engine", self.args.engine, "--arena", self.args.arena,
                "--book", self.args.book, "--cpu-budget", "18", "--selfplay-threads", "18", "--concurrency", "18",
                "--milestone-catalog", self.args.milestone_catalog, "--local-search", "--once", "--min-free-gb", "0"]
        with patch.object(sys, "argv", argv), patch.object(loop.signal, "signal"), contextlib.redirect_stdout(io.StringIO()):
            with patch.object(loop, "stop_selfplay"), patch.object(loop, "alive", return_value=True):
                with patch.object(loop, "ensure_selfplay", ensure), patch.object(loop, "search_batch", search):
                    with patch.object(milestones, "advance", external):
                        loop.main()
        self.assertEqual(calls, ["search", "external"])


if __name__ == "__main__":
    unittest.main()
