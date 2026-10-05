import pathlib
import sys
import unittest
from unittest.mock import MagicMock, patch

import chess
import chess.engine

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import blunders


class LossDiagnosticsTests(unittest.TestCase):
    def analyse(self, game, replies):
        sf = MagicMock()
        sf.options = {"UCI_LimitStrength": None, "Skill Level": None}
        seen = []
        def search(board, limit, **kwargs):
            seen.append((board.fen(), limit.nodes, kwargs))
            cp, move = replies[len(seen) - 1]
            return {"score": chess.engine.PovScore(chess.engine.Cp(cp), board.turn),
                    "pv": [chess.Move.from_uci(move)]}
        sf.analyse.side_effect = search
        sf.__enter__.return_value = sf
        with patch.object(chess.engine.SimpleEngine, "popen_uci", return_value=sf):
            rows = blunders.analyse_game(game, "stockfish", 300_000)
        sf.configure.assert_called_once_with({"Hash": 64, "Threads": 1,
                                             "UCI_LimitStrength": False, "Skill Level": 20})
        return rows, seen

    def game(self, moves="e2e4", white="ArhanPassant", black="Stockfish"):
        return {"white": white, "black": black, "fen": chess.STARTING_FEN, "moves": moves}

    def test_recommended_move_has_zero_loss_without_a_child_evaluation(self):
        rows, seen = self.analyse(self.game(), [(90, "e2e4")])
        self.assertEqual(len(seen), 1)
        self.assertEqual((rows[0]["drop"], rows[0]["eval_best"], rows[0]["eval_played"]), (0, 90, 90))

    def test_alternatives_have_the_same_position_budget_and_fresh_search_state(self):
        rows, seen = self.analyse(self.game("d2d4"), [(90, "e2e4"), (100, "e2e4"), (-100, "d2d4")])
        self.assertEqual({fen for fen, _, _ in seen}, {chess.STARTING_FEN})
        self.assertEqual([n for _, n, _ in seen], [300_000] * 3)
        self.assertEqual(len({id(kw["game"]) for _, _, kw in seen}), 3)
        self.assertEqual([kw["root_moves"] for _, _, kw in seen],
                         [None, [chess.Move.from_uci("e2e4")], [chess.Move.from_uci("d2d4")]])
        self.assertGreater(rows[0]["drop"], 10)
        self.assertEqual((rows[0]["eval_best"], rows[0]["eval_played"]), (100, -100))

    def test_reference_disagreement_is_counted_without_a_false_move_penalty(self):
        rows, _ = self.analyse(self.game("d2d4"), [(90, "e2e4"), (50, "e2e4"), (100, "d2d4")])
        self.assertTrue(rows[0]["reference_disagreement"])
        self.assertEqual(rows[0]["drop"], 0)

    def test_mixed_case_black_name_and_only_our_moves(self):
        rows, seen = self.analyse(self.game("e2e4 e7e5 g1f3", "Stockfish", "ARHANPASSANT"), [(12, "e7e5")])
        self.assertEqual(len(seen), 1)
        self.assertEqual((rows[0]["played"], rows[0]["opponent"], rows[0]["eval_played"]),
                         ("e7e5", "Stockfish", 12))
        self.assertEqual(chess.Board(seen[0][0]).turn, chess.BLACK)

    def test_terminal_position_never_launches_a_reference_search(self):
        game = self.game("g6g7")
        game["fen"] = "7k/6Q1/5K2/8/8/8/8/8 b - - 0 1"
        rows, seen = self.analyse(game, [])
        self.assertEqual((rows, seen), ([], []))

    def test_reference_must_respect_the_forced_move(self):
        with self.assertRaisesRegex(ValueError, "requested legal root move"):
            self.analyse(self.game("d2d4"), [(90, "e2e4"), (100, "e2e4"), (-100, "e2e4")])

    def test_game_without_our_engine_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "does not contain"):
            blunders.analyse_game(self.game(white="Laser", black="Stockfish"), "stockfish", 1)


if __name__ == "__main__":
    unittest.main()
