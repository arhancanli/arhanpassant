import fcntl
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

import chess
import chess.engine

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "trainer"))
import paired
import teacher
import teacher_corpus as corpus

BLACK = bytes.fromhex("000204000020400005d800000000000000000000000000007701020100140000")


class TeacherCorpusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        (self.root / "selfplay").mkdir()
        self.source = self.root / "selfplay/source.bin"
        self.source.write_bytes(BLACK * 8)
        self.state = self.root / "state.json"
        self.state.write_text(json.dumps({"sizes": {str(self.source): 32}}))
        self.engine = self.root / "engine"
        self.engine.write_bytes(b"frozen reference executable")
        self.out = self.root / "study"
        self.calls = []
        self.labeler = patch.object(teacher, "label_chunk", side_effect=self.labels)
        self.labeler.start()
        self.addCleanup(self.labeler.stop)

    def tearDown(self):
        self.temp.cleanup()

    def labels(self, engine, items, nodes):
        self.calls.extend(i["index"] for i in items)
        rows = []
        for item in items:
            row = teacher.scored_row(chess.Board(item["fen"]), {
                "pv": [chess.Move.from_uci("g7f7")], "depth": 10, "nodes": nodes,
                "score": chess.engine.PovScore(chess.engine.Cp(300), chess.BLACK),
                "wdl": chess.engine.PovWdl(chess.engine.Wdl(600, 300, 100), chess.BLACK)})
            rows.append({**row, "index": item["index"]})
        return rows

    def prepare(self, count=5, chunk=2):
        return corpus.prepare(self.root, self.state, self.engine, self.out,
                              count=count, seed=7, chunk_size=chunk, floor_gib=0)

    def advance(self, max_chunks=1):
        return corpus.advance(self.out, workers=1, cpu_budget=1, max_chunks=max_chunks, floor_gib=0)

    def test_bounded_resume_keeps_offsets_and_partial_last_chunk_and_frozen_copies(self):
        manifest = self.prepare()
        self.assertEqual(manifest["distinct_board_turn"], 1)
        inputs = (self.out / "inputs.json").read_bytes()
        self.assertEqual(self.advance()["completed_records"], 2)
        # Retirement or subsequent appends to a live source cannot alter copies.
        self.source.unlink()
        self.state.write_text("{}")
        self.assertEqual(self.advance()["completed_records"], 4)
        final = self.advance()
        self.assertEqual(final["status"], "complete")
        self.assertEqual(final["summary"]["labelled"], 5)
        self.assertEqual(final["summary"]["distinct_board_turn"], 1)
        self.assertEqual(self.calls, list(range(5)))
        self.assertEqual((self.out / "inputs.json").read_bytes(), inputs)
        old = (self.out / "corpus/baseline.bin").read_bytes()
        self.assertEqual(old, BLACK * 5)
        for raw in paired.records(self.out / "corpus/teacher.bin"):
            self.assertEqual(raw[:24] + raw[26:], BLACK[:24] + BLACK[26:])
        self.advance()
        self.assertEqual(self.calls, list(range(5)))

    def test_interrupted_worker_preserves_completed_chunk_and_retries_same_inputs(self):
        self.prepare(count=4)
        original = self.labels
        def fail(engine, items, nodes):
            if items[0]["index"] >= 2:
                raise RuntimeError("interrupted reference")
            return original(engine, items, nodes)
        with patch.object(teacher, "label_chunk", side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, "interrupted reference"):
                self.advance(2)
        preserved = teacher.digest(self.out / "chunks/00000000/report.json")
        final = self.advance()
        self.assertEqual(final["status"], "complete")
        self.assertEqual(self.calls, [0, 1, 2, 3])
        self.assertEqual(teacher.digest(self.out / "chunks/00000000/report.json"), preserved)
        self.assertEqual(len(list((self.out / "chunks").glob("*.interrupted-*"))), 1)

    def test_checkpoint_crash_after_chunk_rename_recovers_without_relabeling(self):
        self.prepare(count=4)
        save = paired.save
        def fail_progress(path, value):
            if path.name == "progress.json":
                raise RuntimeError("checkpoint crash")
            save(path, value)
        with patch.object(paired, "save", side_effect=fail_progress):
            with self.assertRaisesRegex(RuntimeError, "checkpoint crash"):
                self.advance()
        self.assertTrue((self.out / "chunks/00000000/report.json").exists())
        self.assertFalse((self.out / "progress.json").exists())
        self.assertEqual(self.advance()["status"], "complete")
        self.assertEqual(self.calls, [0, 1, 2, 3])

    def test_storage_pause_preserves_chunks_and_finishes_without_resampling(self):
        self.prepare(count=4)
        self.advance()
        with patch.object(paired, "headroom", side_effect=ValueError("disk headroom")):
            with self.assertRaisesRegex(ValueError, "disk headroom"):
                self.advance()
        self.assertEqual(self.calls, [0, 1])
        self.assertEqual(self.advance()["status"], "complete")
        self.assertEqual(self.calls, [0, 1, 2, 3])

    def test_reference_or_frozen_input_mutation_fails_before_engine_starts(self):
        self.prepare()
        self.engine.write_bytes(b"changed executable")
        with self.assertRaisesRegex(ValueError, "frozen corpus input changed"):
            self.advance()
        self.assertFalse(self.calls)
        self.engine.write_bytes(b"frozen reference executable")
        with (self.out / "inputs.json").open("a") as file:
            file.write(" ")
        with self.assertRaisesRegex(ValueError, "frozen corpus input changed"):
            self.advance()
        self.assertFalse(self.calls)

    def test_completed_artifact_corruption_and_non_score_changes_are_rejected(self):
        self.prepare()
        self.advance()
        path = self.out / "chunks/00000000"
        raw = bytearray((path / "teacher.bin").read_bytes())
        raw[26] = 0
        (path / "teacher.bin").write_bytes(raw)
        with self.assertRaisesRegex(ValueError, "artifact changed"):
            self.advance()
        report = corpus.load(path / "report.json")
        report["artifacts_sha256"]["teacher.bin"] = teacher.digest(path / "teacher.bin")
        paired.save(path / "report.json", report)
        progress = corpus.load(self.out / "progress.json")
        progress["chunks"][0]["report_sha256"] = teacher.digest(path / "report.json")
        paired.save(self.out / "progress.json", progress)
        with self.assertRaisesRegex(ValueError, "paired bytes changed"):
            self.advance()
        self.assertEqual(self.calls, [0, 1])

    def test_completed_report_metadata_mutation_is_detected_by_saved_hash(self):
        self.prepare()
        self.advance()
        path = self.out / "chunks/00000000/report.json"
        report = corpus.load(path)
        report["summary"]["rows"][0]["teacher_white_cp"] += 1
        paired.save(path, report)
        with self.assertRaisesRegex(ValueError, "chunk report changed"):
            self.advance()
        self.assertEqual(self.calls, [0, 1])

    def test_duplicate_result_coverage_cannot_commit_a_chunk(self):
        self.prepare()
        def duplicate(engine, items, nodes):
            rows = self.labels(engine, items, nodes)
            return [rows[0], rows[0]]
        with patch.object(teacher, "label_chunk", side_effect=duplicate):
            with self.assertRaisesRegex(ValueError, "coverage incomplete"):
                self.advance()
        self.assertFalse((self.out / "chunks/00000000").exists())

    def test_simultaneous_writer_and_changed_reference_options_are_rejected(self):
        self.prepare()
        with (self.out / "corpus.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                self.advance()
        with patch.object(teacher, "OPTIONS", {**teacher.OPTIONS, "Threads": 2}):
            with self.assertRaisesRegex(ValueError, "reference configuration changed"):
                self.advance()
        self.assertFalse(self.calls)

    def test_no_live_outputs_or_excess_cpu_budget(self):
        self.out = self.root / "selfplay" / "study"
        with self.assertRaisesRegex(ValueError, "outside live self-play"):
            self.prepare()
        self.out = self.root / "study"
        self.prepare()
        with self.assertRaisesRegex(ValueError, "reserved CPU"):
            corpus.advance(self.out, workers=2, cpu_budget=1, floor_gib=0)
        self.assertFalse(self.calls)


if __name__ == "__main__":
    unittest.main()
