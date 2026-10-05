import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "trainer"))
import initialize

HAS_TRAINING_DEPS = all(importlib.util.find_spec(name) is not None for name in ("numpy", "torch"))


class InitializationPathTests(unittest.TestCase):
    def test_outputs_cannot_alias_either_input_including_hard_links(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            checkpoint, network = root / "source.pt", root / "source.nnue"
            checkpoint.write_bytes(b"checkpoint")
            network.write_bytes(b"network")
            with self.assertRaisesRegex(ValueError, "overwrite"):
                initialize.protect_inputs(checkpoint, network, network)
            output = root / "candidate.nnue"
            os.link(checkpoint, str(output) + ".pt")
            with self.assertRaisesRegex(ValueError, "overwrite"):
                initialize.protect_inputs(checkpoint, network, output)
            Path(str(output) + ".pt").unlink()
            Path(str(output) + ".init.json").symlink_to(network)
            with self.assertRaisesRegex(ValueError, "overwrite"):
                initialize.protect_inputs(checkpoint, network, output)
            self.assertEqual(checkpoint.read_bytes(), b"checkpoint")
            self.assertEqual(network.read_bytes(), b"network")

    def test_source_changes_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "source"
            path.write_bytes(b"frozen")
            proof = {"inputs_sha256": {str(path): initialize.digest(path)}}
            initialize.verify_sources(proof)
            path.write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "input changed"):
                initialize.verify_sources(proof)


@unittest.skipUnless(HAS_TRAINING_DEPS, "requires the optional NumPy/PyTorch trainer dependencies")
class TrainingInitializationTests(unittest.TestCase):
    def setUp(self):
        import torch
        import train
        self.torch, self.train = torch, train
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        train.set_layout(8, 8)
        torch.manual_seed(71)
        self.source = train.Net(8)
        self.checkpoint, self.network = self.folder / "source.pt", self.folder / "source.nnue"
        self.output = self.folder / "candidate.nnue"
        torch.save(self.source.state_dict(), self.checkpoint)
        train.export(self.source, self.network)

    def test_checkpoint_restores_weights_and_reproduces_network(self):
        self.torch.manual_seed(999)
        target = self.train.Net(8)
        proof = initialize.restore(target, self.checkpoint, self.network, self.output, self.train.export)
        self.assertTrue(proof["initial_export_matches_network"])
        self.assertTrue(all(self.torch.equal(value, target.state_dict()[name])
                            for name, value in self.source.state_dict().items()))
        self.assertFalse(self.output.exists())
        self.assertEqual(proof["inputs_sha256"][str(self.network.resolve())], initialize.digest(self.network))

    def test_different_architecture_is_rejected_before_candidate_export(self):
        with self.assertRaisesRegex(ValueError, "layout or dtype mismatch"):
            initialize.restore(self.train.Net(16), self.checkpoint, self.network, self.output, self.train.export)
        self.assertFalse(self.output.exists())

    def test_nonfinite_checkpoint_is_rejected(self):
        state = self.source.state_dict()
        state["ft.weight"][0, 0] = float("nan")
        self.torch.save(state, self.checkpoint)
        with self.assertRaisesRegex(ValueError, "nonfinite"):
            initialize.restore(self.train.Net(8), self.checkpoint, self.network, self.output, self.train.export)

    def test_missing_weights_and_different_dtype_are_rejected(self):
        state = self.source.state_dict()
        del state["out.bias"]
        self.torch.save(state, self.checkpoint)
        with self.assertRaisesRegex(ValueError, "keys do not match"):
            initialize.restore(self.train.Net(8), self.checkpoint, self.network, self.output, self.train.export)
        state = self.source.state_dict()
        state["ft.weight"] = state["ft.weight"].double()
        self.torch.save(state, self.checkpoint)
        with self.assertRaisesRegex(ValueError, "dtype mismatch"):
            initialize.restore(self.train.Net(8), self.checkpoint, self.network, self.output, self.train.export)

    def test_unrelated_network_is_rejected(self):
        changed = bytearray(self.network.read_bytes())
        changed[-1] ^= 1
        self.network.write_bytes(changed)
        with self.assertRaisesRegex(ValueError, "does not reproduce"):
            initialize.restore(self.train.Net(8), self.checkpoint, self.network, self.output, self.train.export)
        self.assertFalse(self.output.exists())

    def run_cli(self, extra):
        record = bytearray(32)
        record[:8] = ((2**16 - 1) | ((2**16 - 1) << 48)).to_bytes(8, "little")
        pieces = [3, 1, 2, 4, 5, 2, 1, 3] + [0] * 8 + [8] * 8 + [11, 9, 10, 12, 13, 10, 9, 11]
        record[8:24] = bytes(pieces[i] | (pieces[i + 1] << 4) for i in range(0, 32, 2))
        record[26], record[29] = 1, 1
        data = self.folder / "positions.bin"
        data.write_bytes(record * 8193)
        return subprocess.run(
            [sys.executable, str(ROOT / "trainer/train.py"), "--data", str(data),
             "--out", str(self.output), "--hidden", "8", "--input-buckets", "8",
             "--output-buckets", "8", "--batch", "512", "--val-every", "2",
             "--epochs", "1", "--workers", "1", "--threads", "1", "--seed", "999", *extra],
            capture_output=True, text=True, timeout=60,
        )

    def test_zero_lr_cli_preserves_verified_initial_weights_and_partial_rows(self):
        result = self.run_cli(["--init-checkpoint", str(self.checkpoint),
                               "--init-network", str(self.network), "--lr", "0"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("records 4,097/4,097 steps 9/9", result.stdout)
        self.assertEqual(self.output.read_bytes(), self.network.read_bytes())
        state = self.torch.load(str(self.output) + ".pt", weights_only=True, map_location="cpu")
        self.assertTrue(all(self.torch.equal(state[name], value)
                            for name, value in self.source.state_dict().items()))
        proof = json.loads(Path(str(self.output) + ".init.json").read_text())
        self.assertEqual(proof["status"], "complete")
        self.assertEqual(proof["best_epoch"], 0)
        self.assertEqual(proof["learning_rate"], 0)
        self.assertEqual(proof["train_records"], 4097)
        self.assertEqual(proof["validation_records"], 4096)

    def test_warm_cli_has_conservative_default_lr(self):
        result = self.run_cli(["--init-checkpoint", str(self.checkpoint), "--init-network", str(self.network)])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        proof = json.loads(Path(str(self.output) + ".init.json").read_text())
        self.assertEqual(proof["learning_rate"], 1e-5)

    def test_cli_requires_both_initialization_inputs_and_finite_lr(self):
        result = self.run_cli(["--init-checkpoint", str(self.checkpoint)])
        self.assertEqual(result.returncode, 2)
        self.assertIn("must be supplied together", result.stderr)
        result = self.run_cli(["--lr", "nan"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("finite and nonnegative", result.stderr)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
