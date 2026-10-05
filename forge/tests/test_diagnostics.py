import contextlib
import io
import json
import pathlib
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import diagnostics
import local_gate
import loop
import milestones
import opponents


class SupervisedDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.directory = self.root / "forge/milestones/fixture"
        self.directory.mkdir(parents=True)
        reference = self.root / "stockfish"
        reference.write_bytes(b"reference build")
        entries = [{"name": n, "path": str(reference), "sha256": local_gate.digest(reference)} for n in opponents.NAMES]
        self.matches = []
        for tc, target in [("10+0.1", 32), ("60+0.6", 4)]:
            for opponent in opponents.NAMES:
                self.matches.append({"opponent": opponent, "tc": tc, "target": target,
                                     "games": 0, "status": "queued", "batches": []})
        manifest = {"config": {"plan": [("10+0.1", 32), ("60+0.6", 4)], "opponents": entries},
                    "matches": [{k: m[k] for k in ["opponent", "tc", "target"]} for m in self.matches]}
        local_gate.save(str(self.directory / "manifest.json"), manifest)
        local_gate.save(str(self.directory.parent / "index.json"), {"runs": [{"identity": "fixture", "status": "running"}]})
        self.args = types.SimpleNamespace(data=str(self.root), loss_analysis_sample=32)
        self.state = {"selfplay_threads": 9, "selfplay_pid": 123}
        self.calls = []
        self.coverage()

    def tearDown(self):
        self.temp.cleanup()

    def report(self, complete=False):
        local_gate.save(str(self.directory / "report.json"), {
            "status": "complete" if complete else "running", "matches": self.matches})

    def coverage(self):
        for m in self.matches[:14]:
            self.batch(m)
        self.report()

    def batch(self, match):
        count = match["target"]
        path = self.directory / f"{match['opponent']}-{match['tc']}.json"
        records = path.with_suffix(".games.jsonl")
        game = {"white": "ArhanPassant", "black": match["opponent"], "fen": "fixture",
                "result": "0-1", "moves": "e2e4 e7e5"}
        records.write_text((json.dumps(game) + "\n") * count)
        local_gate.save(str(path), {"games": count, "wins": 0, "losses": count, "draws": 0,
                                   "penta": [count // 2, 0, 0, 0, 0], "reasons": {"adjudicated": count},
                                   "records_sha256": local_gate.digest(records)})
        match.update(games=count, status="complete", batches=[{"file": str(path), "sha256": local_gate.digest(path)}])

    def analyse(self, cmd, logfile):
        self.calls.append(cmd)
        games, reference = cmd[5], cmd[cmd.index("--stockfish") + 1]
        count = len(pathlib.Path(games).read_text().splitlines())
        local_gate.save(cmd[cmd.index("--out") + 1], {
            "schema_version": 2, "stockfish_nodes": 300_000,
            "stockfish_sha256": local_gate.digest(reference), "input_sha256": {games: local_gate.digest(games)},
            "losses_available": count, "losses_analysed": min(count, 32), "moves_analysed": 32})
        return 0

    def advance(self):
        with patch.object(local_gate, "run", self.analyse):
            return diagnostics.advance(self.args, self.state, 9, "log", lambda _: None)

    def test_complete_reports_survive_restart_and_do_not_replay(self):
        self.assertTrue(self.advance())
        self.assertFalse(self.advance())
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][self.calls[0].index("--threads") + 1], "9")
        self.assertEqual(self.state["loss_diagnostics"]["status"], "complete")

    def test_every_opponent_is_required_and_final_review_requires_every_budget(self):
        self.matches[13]["games"] = 0
        self.report(complete=True)
        self.assertIsNone(diagnostics.choose(self.root, 32))
        self.matches[13]["games"] = 32
        self.report()
        self.advance()
        self.report(complete=True)
        self.assertIsNone(diagnostics.choose(self.root, 32))
        for m in self.matches[14:]:
            self.batch(m)
        self.report(complete=True)
        self.assertEqual(diagnostics.choose(self.root, 32)[1]["stage"], "complete")

    def test_complete_report_recovers_the_crash_before_its_job_checkpoint(self):
        self.advance()
        path = pathlib.Path(self.state["loss_diagnostics"]["job"])
        job = milestones.read(path)
        job["status"] = "running"
        local_gate.save(str(path), job)
        with patch.object(local_gate, "run", side_effect=AssertionError("must reuse completed report")):
            self.assertTrue(diagnostics.advance(self.args, self.state, 9, "log", lambda _: None))
        self.assertEqual(milestones.read(path)["status"], "complete")

    def test_changed_game_records_fail_without_rating_or_promoting_anything(self):
        first = pathlib.Path(self.matches[0]["batches"][0]["file"]).with_suffix(".games.jsonl")
        first.write_text("changed records\n")
        with patch.object(local_gate, "run", side_effect=AssertionError("bad inputs must not run")):
            self.assertTrue(diagnostics.advance(self.args, self.state, 9, "log", lambda _: None))
        self.assertEqual(self.state["loss_diagnostics"]["status"], "failed")
        self.assertFalse(self.advance())  # cooldown; normal strength jobs can continue
        self.assertNotIn("activity", self.state)

    def test_reference_failure_has_three_bounded_retries(self):
        with patch.object(local_gate, "run", return_value=1), patch.object(diagnostics.time, "time", return_value=1):
            for _ in range(3):
                self.assertTrue(diagnostics.advance(self.args, self.state, 9, "log", lambda _: None))
                path = pathlib.Path(self.state["loss_diagnostics"]["job"])
                job = milestones.read(path)
                job["retry_after"] = 0
                local_gate.save(str(path), job)
            self.assertFalse(diagnostics.advance(self.args, self.state, 9, "log", lambda _: None))
        self.assertEqual(milestones.read(path)["attempts"], 3)

    def test_review_reserves_cores_even_when_search_and_external_matches_are_finished(self):
        for name in ["engine", "network", "book", "arena"]:
            (self.root / name).write_bytes(name.encode())
        local_gate.save(str(self.root / "forge/state.json"), {
            "generation": 1, "champion_net": str(self.root / "network"), "champion_version": "0.12.0",
            "trained_on": 0, "attempts": 0})
        argv = ["loop.py", "--data", str(self.root), "--engine", str(self.root / "engine"),
                "--arena", str(self.root / "arena"), "--book", str(self.root / "book"),
                "--milestone-catalog", "fixture", "--cpu-budget", "18", "--concurrency", "18",
                "--selfplay-threads", "18", "--once"]
        def ensure(args, state, threads):
            self.assertEqual(threads, 9)
            state.update(selfplay_threads=9, selfplay_pid=123)
        with patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
            with patch.object(loop.os, "cpu_count", return_value=18), patch.object(loop.signal, "signal"):
                with patch.object(loop, "stop_selfplay"), patch.object(loop, "storage_ready", return_value=True):
                    with patch.object(loop, "ensure_selfplay", ensure), patch.object(loop, "alive", return_value=True):
                        with patch.object(milestones, "enqueue"), patch.object(milestones, "pending", return_value=None):
                            with patch.object(local_gate, "run", self.analyse):
                                loop.main()
        self.assertEqual(self.calls[0][self.calls[0].index("--threads") + 1], "9")


if __name__ == "__main__":
    unittest.main()
