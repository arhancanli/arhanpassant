import pathlib
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

import chess
import chess.engine

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "trainer"))
import teacher


BLACK = bytes.fromhex("000204000020400005d800000000000000000000000000007701020100140000")
CASTLE = bytes.fromhex("110004000000001053d000000000000000000000000000007701020000140000")
EP = bytes.fromhex("000200001800400085d000000000000000000000000000007701020000140000")


class TeacherTests(unittest.TestCase):
    def test_white_score_and_clocks_decode_independently_of_side_to_move(self):
        board, score, result = teacher.decode(BLACK)
        self.assertEqual(board.fen(), "8/6k1/5p2/8/8/2P5/1K6/8 b - - 0 20")
        self.assertEqual((score, result), (375, 2))
        self.assertIsNone(teacher.skip_reason(board))
        changed = teacher.replace_score(BLACK, -600)
        self.assertEqual(struct.unpack_from("<h", changed, 24)[0], -600)
        self.assertEqual(changed[:24] + changed[26:], BLACK[:24] + BLACK[26:])

    def test_omitted_history_castling_and_en_passant_are_excluded(self):
        historical = bytearray(BLACK)
        historical[28] = 7
        for record, reason in [(historical, "repetition"), (CASTLE, "castling"), (EP, "en-passant")]:
            board, _, _ = teacher.decode(record)
            self.assertIn(reason, teacher.skip_reason(board))

    def test_corrupt_piece_side_result_and_reserved_bytes_are_rejected(self):
        for offset, value in [(8, 7), (26, 3), (27, 2), (31, 1)]:
            corrupt = bytearray(BLACK)
            corrupt[offset] = value
            with self.assertRaises(ValueError):
                teacher.decode(corrupt)

    def test_impossible_en_passant_does_not_exclude_an_unambiguous_position(self):
        for fen in ["8/6k1/3N4/3pP3/8/8/1K6/8 w - - 0 20",
                    "8/3b2k1/8/3pP3/8/8/1K6/8 w - - 0 20"]:
            board = chess.Board(fen)
            self.assertTrue(board.is_valid())
            self.assertIsNone(teacher.skip_reason(board))

    def test_teacher_black_score_becomes_white_relative_and_pv_must_be_legal(self):
        board, _, _ = teacher.decode(BLACK)
        move = chess.Move.from_uci("g7f7")
        info = {"pv": [move], "score": chess.engine.PovScore(chess.engine.Cp(300), chess.BLACK)}
        self.assertEqual(teacher.scored_row(board, info)["teacher_white_cp"], -300)
        info["score"] = chess.engine.PovScore(chess.engine.Mate(3), chess.BLACK)
        self.assertEqual(teacher.scored_row(board, info)["teacher_white_cp"], -31997)
        with self.assertRaisesRegex(ValueError, "illegal reference PV"):
            teacher.scored_row(board, {**info, "pv": [chess.Move.from_uci("a1a8")]})

    def test_bounds_never_enter_the_paired_training_data(self):
        board, score, result = teacher.decode(BLACK)
        items = [{"index": i, "raw_hex": BLACK.hex(), "fen": board.fen(),
                  "original_white_cp": score, "result": result} for i in range(2)]
        base = teacher.scored_row(board, {"pv": [chess.Move.from_uci("g7f7")],
            "score": chess.engine.PovScore(chess.engine.Cp(300), chess.BLACK)})
        rows = [{**base, "index": 1, "upperbound": True}, {**base, "index": 0}]
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp)
            summary = teacher.write_pairs(out, items, rows)
            self.assertEqual((summary["labelled"], summary["excluded_bounds"]), (1, 1))
            self.assertEqual((out / "baseline.bin").read_bytes(), BLACK)
            new = (out / "teacher.bin").read_bytes()
            self.assertEqual(new[:24] + new[26:], BLACK[:24] + BLACK[26:])

    def test_interrupted_bound_keeps_the_previous_complete_score_and_pv_together(self):
        board, _, _ = teacher.decode(BLACK)
        exact = {"pv": [chess.Move.from_uci("g7f7")], "depth": 10, "nodes": 70000,
                 "score": chess.engine.PovScore(chess.engine.Cp(300), chess.BLACK)}
        bound = {"pv": [chess.Move.from_uci("g7h7")], "depth": 11, "nodes": 99000,
                 "score": chess.engine.PovScore(chess.engine.Cp(500), chess.BLACK), "lowerbound": True}
        row = teacher.completed_result(board, iter([exact, bound]))
        self.assertEqual((row["teacher_white_cp"], row["move"], row["depth"], row["nodes"]), (-300, "g7f7", 10, 70000))
        self.assertFalse(row["lowerbound"])
        self.assertTrue(row["used_earlier_completed_iteration"])
        self.assertEqual(row["final_raw_result"]["teacher_white_cp"], -500)
        self.assertTrue(row["final_raw_result"]["lowerbound"])
        only_bound = teacher.completed_result(board, iter([bound]))
        self.assertTrue(only_bound["lowerbound"])
        self.assertFalse(only_bound["used_earlier_completed_iteration"])

    def test_wdl_orientation_draws_and_inverse_scale_are_preserved(self):
        board, _, _ = teacher.decode(BLACK)
        info = {"pv": [chess.Move.from_uci("g7f7")],
                "score": chess.engine.PovScore(chess.engine.Cp(300), chess.BLACK),
                "wdl": chess.engine.PovWdl(chess.engine.Wdl(600, 300, 100), chess.BLACK)}
        row = teacher.scored_row(board, info)
        self.assertEqual(row["teacher_wdl_white"], [100, 300, 600])
        self.assertEqual(row["teacher_expected_score_white"], 0.25)
        for expected in [0, 0.0005, 0.25, 0.5, 0.75, 0.9995, 1]:
            cp = teacher.expected_score_cp(expected)
            self.assertLessEqual(abs(teacher.probability(cp) - expected), 0.000313)
            self.assertEqual(cp, -teacher.expected_score_cp(1 - expected))
        for invalid in [-0.1, 1.1, float("nan"), float("inf")]:
            with self.assertRaises(ValueError):
                teacher.expected_score_cp(invalid)

    def test_wdl_encoding_changes_only_scores_and_requires_same_iteration_wdl(self):
        board, score, result = teacher.decode(BLACK)
        item = {"index": 0, "raw_hex": BLACK.hex(), "fen": board.fen(),
                "original_white_cp": score, "result": result}
        info = {"pv": [chess.Move.from_uci("g7f7")],
                "score": chess.engine.PovScore(chess.engine.Cp(300), chess.BLACK),
                "wdl": chess.engine.PovWdl(chess.engine.Wdl(600, 300, 100), chess.BLACK)}
        exact = {**info, "depth": 10, "nodes": 70000}
        interrupted = {**info, "depth": 11, "nodes": 99000, "lowerbound": True,
                       "wdl": chess.engine.PovWdl(chess.engine.Wdl(900, 100, 0), chess.BLACK)}
        row = {**teacher.completed_result(board, iter([exact, interrupted])), "index": 0}
        self.assertEqual(row["teacher_expected_score_white"], 0.25)
        self.assertEqual(row["final_raw_result"]["teacher_expected_score_white"], 0.05)
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp)
            summary = teacher.write_pairs(out, [item], [row], "wdl")
            new = (out / "teacher.bin").read_bytes()
            self.assertEqual(new[:24] + new[26:], BLACK[:24] + BLACK[26:])
            self.assertEqual(struct.unpack_from("<h", new, 24)[0], -439)
            self.assertEqual(summary["rows"][0]["teacher_white_cp"], -300)
            self.assertEqual(summary["rows"][0]["target_white_cp"], -439)
        with tempfile.TemporaryDirectory() as tmp:
            del row["teacher_expected_score_white"]
            with self.assertRaisesRegex(ValueError, "no WDL target"):
                teacher.write_pairs(pathlib.Path(tmp), [item], [row], "wdl")

    def test_sampling_uses_only_fresh_complete_records_and_keeps_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "selfplay").mkdir()
            file = root / "selfplay" / "source.bin"
            file.write_bytes(CASTLE + BLACK + BLACK + b"partial")
            items, summary = teacher.fresh_sample(root, {"sizes": {str(file): 32}}, 2, 7)
            self.assertEqual(summary["available_fresh_records"], 2)
            self.assertEqual({r["record_index"] for r in items}, {1, 2})
            self.assertTrue(all(r["raw_hex"] == BLACK.hex() for r in items))

    def test_duplicate_draws_cannot_inflate_a_corpus_or_require_a_bulk_draw_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "selfplay").mkdir()
            (root / "selfplay/source.bin").write_bytes(CASTLE + BLACK + BLACK)
            with patch.object(teacher.random.Random, "sample", side_effect=AssertionError("bulk allocation")):
                with patch.object(teacher.random.Random, "randrange", side_effect=[0, 1, 1, 2]):
                    items, summary = teacher.fresh_sample(root, {}, 2, 7)
            self.assertEqual([r["record_index"] for r in items], [1, 2])
            self.assertEqual(summary["skipped"]["duplicate accepted offset"], 1)
            self.assertEqual(summary["skipped"]["castling rights unavailable"], 1)


if __name__ == "__main__":
    unittest.main()
