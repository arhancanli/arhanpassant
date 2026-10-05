import json
import pathlib
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "trainer"))
import paired

BLACK = bytes.fromhex("000204000020400005d800000000000000000000000000007701020100140000")


def fixture_records():
    result = []
    occupancy = int.from_bytes(BLACK[:8], "little") & ~(1 << 18)
    for square in range(16, 24):
        for side in [0, 1]:
            raw = bytearray(BLACK)
            raw[:8] = (occupancy | (1 << square)).to_bytes(8, "little")
            raw[27] = side
            result.append(bytes(raw))
    return result


class PairedTrainingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.corpus = self.root / "corpus"
        self.corpus.mkdir()
        self.rows = fixture_records()
        self.write_corpus(self.rows)
        self.out = self.root / "prepared"

    def tearDown(self):
        self.tmp.cleanup()

    def write_corpus(self, rows, teacher_rows=None):
        if teacher_rows is None:
            teacher_rows = [r[:24] + struct.pack("<h", -439) + r[26:] for r in rows]
        (self.corpus / "baseline.bin").write_bytes(b"".join(rows))
        (self.corpus / "teacher.bin").write_bytes(b"".join(teacher_rows))
        (self.corpus / "report.json").write_text(json.dumps({"status": "complete", "summary": {"labelled": len(rows), "target_mode": "wdl"}}))
        proof = {"status": "complete", "corpus_report": str(self.corpus / "report.json"),
                 "artifacts_sha256": {p.name: paired.digest(p) for p in self.corpus.iterdir()}}
        self.validation = self.root / "validation.json"
        self.validation.write_text(json.dumps(proof))

    def prepare(self, **kwargs):
        return paired.prepare(self.corpus, self.validation, self.out, seed=1, fraction=0.5, floor_gib=0, **kwargs)

    def test_board_group_ignores_scores_outcomes_clocks_and_unused_padding(self):
        changed = bytearray(BLACK)
        changed[24:27] = bytes([255, 128, 0])
        changed[28:31] = bytes([99, 40, 0])
        changed[23] = 255
        self.assertEqual(paired.board_key(changed), paired.board_key(BLACK))
        changed[27] = 0
        self.assertNotEqual(paired.board_key(changed), paired.board_key(BLACK))

    def test_repeated_positions_never_cross_splits_and_pairs_preserve_every_other_byte(self):
        repeated = bytearray(self.rows[0])
        repeated[28] = 20
        repeated[26] = 1
        self.write_corpus(self.rows + [bytes(repeated)])
        report = self.prepare()
        groups = {}
        for split in ["train", "val"]:
            old = list(paired.records(self.out / f"baseline-{split}.bin"))
            new = list(paired.records(self.out / f"teacher-{split}.bin"))
            self.assertTrue(old)
            self.assertEqual(len(old), len(new))
            groups[split] = {paired.board_key(r) for r in old}
            for a, b in zip(old, new):
                self.assertEqual(a[:24] + a[26:], b[:24] + b[26:])
        self.assertFalse(groups["train"] & groups["val"])
        self.assertEqual(report["counts"]["teacher_train"] + report["counts"]["teacher_val"], 17)
        self.assertEqual(sum(report["distinct_groups"].values()), 16)

    def test_anchor_replay_excludes_teacher_boards_and_uses_identical_bounded_prefixes(self):
        anchor = self.root / "anchors.bin"
        other = bytearray(BLACK)
        occupancy = int.from_bytes(BLACK[:8], "little") & ~(1 << 18)
        other[:8] = (occupancy | (1 << 26)).to_bytes(8, "little")
        anchor.write_bytes(BLACK + bytes(other) + bytes(other) + b"partial")
        report = self.prepare(anchors=[anchor], max_anchors=2)
        self.assertEqual(report["counts"]["anchors_excluded_teacher_board"], 1)
        self.assertEqual(report["counts"]["anchor_train"] + report["counts"]["anchor_val"], 1)
        self.assertEqual(report["anchors"][0]["prefix_bytes"], 64)
        self.assertEqual(report["anchors"][0]["prefix_sha256"], paired.prefix_digest(anchor, 64))
        self.assertEqual(report["anchors"][0]["records_read"], 2)
        for split in ["train", "val"]:
            a = list(paired.records(self.out / f"baseline-{split}.bin"))
            b = list(paired.records(self.out / f"teacher-{split}.bin"))
            for x, y in zip(a, b):
                if paired.board_key(x) == paired.board_key(other):
                    self.assertEqual(x, y)

    def test_hash_mutation_is_rejected_before_study_files_exist(self):
        with (self.corpus / "teacher.bin").open("ab") as f:
            f.write(BLACK)
        with self.assertRaisesRegex(ValueError, "corpus changed"):
            self.prepare()
        self.assertFalse(self.out.exists())

    def test_outcome_mutation_cannot_be_hidden_by_a_valid_file_hash(self):
        changed = bytearray(self.rows[0])
        changed[26] = 0
        teachers = [bytes(changed)] + self.rows[1:]
        self.write_corpus(self.rows, teachers)
        with self.assertRaisesRegex(ValueError, "non-score bytes"):
            self.prepare()
        self.assertEqual(json.loads((self.out / "prepared.json").read_text())["status"], "failed")

    def test_live_data_output_and_unbounded_anchor_budgets_are_rejected(self):
        self.out = self.root / "selfplay" / "output"
        with self.assertRaisesRegex(ValueError, "outside live self-play"):
            self.prepare()
        self.out = self.root / "prepared"
        with self.assertRaisesRegex(ValueError, "bounded positive"):
            self.prepare(anchors=[self.corpus / "baseline.bin"])


if __name__ == "__main__":
    unittest.main()
