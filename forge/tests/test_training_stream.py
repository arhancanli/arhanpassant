import pathlib
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "trainer"))
import stream


class TrainingStreamTests(unittest.TestCase):
    def test_decode_failure_reaches_the_trainer_instead_of_hanging(self):
        def broken():
            yield "decoded batch"
            raise OSError("lost data chunk")
        batches = stream.prefetch(broken(), depth=1)
        self.assertEqual(next(batches), "decoded batch")
        with self.assertRaisesRegex(OSError, "lost data chunk"):
            next(batches)

    def test_abandoned_stream_releases_its_producer(self):
        closed = threading.Event()
        def batches():
            try:
                while True:
                    yield "batch"
            finally:
                closed.set()
        prefetched = stream.prefetch(batches(), depth=1)
        self.assertEqual(next(prefetched), "batch")
        prefetched.close()
        self.assertTrue(closed.wait(1))

    def test_launchd_file_limit_is_raised_for_the_dataset(self):
        import resource
        with patch.object(resource, "getrlimit", return_value=(256, 8192)):
            with patch.object(resource, "setrlimit") as set_limit:
                stream.prepare_file_limit(700)
        set_limit.assert_called_once_with(resource.RLIMIT_NOFILE, (828, 8192))

    def test_insufficient_hard_file_limit_reports_the_required_budget(self):
        import resource
        with patch.object(resource, "getrlimit", return_value=(256, 512)):
            with self.assertRaisesRegex(RuntimeError, "828 open files"):
                stream.prepare_file_limit(700)


if __name__ == "__main__":
    unittest.main()
