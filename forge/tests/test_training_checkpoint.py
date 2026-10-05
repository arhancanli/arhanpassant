import importlib.util
import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
HAS_TRAINING_DEPS = all(importlib.util.find_spec(name) is not None for name in ("numpy", "torch"))


class TrainingCheckpointTests(unittest.TestCase):
    @unittest.skipUnless(HAS_TRAINING_DEPS, "requires the optional NumPy/PyTorch trainer dependencies")
    def test_no_improvement_run_saves_the_best_initial_model_and_checkpoint(self):
        # A zero learning rate keeps every model weight unchanged. This exercises
        # the real CLI, dataset, validation and export without relying on loss noise.
        record = bytearray(32)
        record[:8] = ((2**16 - 1) | ((2**16 - 1) << 48)).to_bytes(8, "little")
        codes = [3, 1, 2, 4, 5, 2, 1, 3] + [0] * 8 + [8] * 8 + [11, 9, 10, 12, 13, 10, 9, 11]
        record[8:24] = bytes(codes[i] | (codes[i + 1] << 4) for i in range(0, 32, 2))
        record[26] = 1
        record[29] = 1
        code = """
from pathlib import Path
import sys
import torch
import train
train.main()
out = Path(sys.argv[sys.argv.index('--out') + 1])
state = torch.load(str(out) + '.pt', map_location='cpu', weights_only=True)
torch.manual_seed(37)
expected = train.Net(8)
assert set(state) == set(expected.state_dict())
assert all(torch.equal(state[k], value) for k, value in expected.state_dict().items())
expected.load_state_dict(state)
reexport = out.with_suffix('.reexport.nnue')
train.export(expected, str(reexport))
assert out.read_bytes() == reexport.read_bytes()
"""
        with tempfile.TemporaryDirectory() as tmp:
            folder = pathlib.Path(tmp)
            data, out = folder / "positions.bin", folder / "model.nnue"
            data.write_bytes(record * 8192)
            result = subprocess.run(
                [sys.executable, "-W", "error::ResourceWarning", "-c", code,
                 "--data", str(data), "--out", str(out), "--hidden", "8",
                 "--input-buckets", "8", "--output-buckets", "8", "--batch", "512",
                 "--val-every", "2", "--epochs", "1", "--lr", "0",
                 "--workers", "1", "--threads", "1", "--seed", "37"],
                cwd=ROOT / "trainer", capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("(best, saved)", result.stdout)
            self.assertIn("records 4,096/4,096 steps 8/8", result.stdout)
            self.assertTrue(out.exists())
            self.assertTrue(pathlib.Path(str(out) + ".pt").exists())
            self.assertEqual(out.read_bytes()[:4], b"APNN")


if __name__ == "__main__":
    unittest.main()
