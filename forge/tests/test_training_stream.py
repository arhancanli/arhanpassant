import pathlib
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "trainer"))
import stream


class TrainingStreamTests(unittest.TestCase):
    @staticmethod
    def concatenate(buffers):
        return [row for buffer in buffers for row in buffer]

    def test_buffer_tails_are_carried_and_every_record_is_seen_once(self):
        buffers = [[0, 1, 2], [3], [4, 5], [], [6, 7, 8]]
        batches = list(stream.complete_batches(iter(buffers), 4, self.concatenate))
        self.assertEqual(batches, [[0, 1, 2, 3], [4, 5, 6, 7], [8]])
        self.assertEqual(sum(batches, []), list(range(9)))
        self.assertEqual(len(batches), -(-9 // 4))

    def test_dataset_smaller_than_a_batch_still_delivers_all_validation_rows(self):
        batches = list(stream.complete_batches([["held-out-a"], ["held-out-b", "held-out-c"]],
                                                4, self.concatenate))
        self.assertEqual(batches, [["held-out-a", "held-out-b", "held-out-c"]])

    def test_aligned_buffers_do_not_copy_or_change_existing_batch_order(self):
        def no_copy(_):
            raise AssertionError("aligned buffers must not be concatenated")
        batches = list(stream.complete_batches([[4, 3, 2, 1], [8, 7, 6, 5]], 2, no_copy))
        self.assertEqual(batches, [[4, 3], [2, 1], [8, 7], [6, 5]])

    def test_invalid_batch_budget_and_empty_input(self):
        with self.assertRaisesRegex(ValueError, "batch size"):
            list(stream.complete_batches([[1]], 0, self.concatenate))
        self.assertEqual(list(stream.complete_batches([[], []], 4, self.concatenate)), [])

    def test_frozen_prefix_allows_growth_and_rejects_shortened_input(self):
        self.assertEqual(stream.frozen_record_count(128, 96), 3)
        self.assertEqual(stream.frozen_record_count(103), 3)
        self.assertEqual(stream.frozen_record_count(128, 103), 3)
        with self.assertRaisesRegex(RuntimeError, "frozen input shortened"):
            stream.frozen_record_count(95, 96)
        with self.assertRaises(ValueError):
            stream.frozen_record_count(128, -1)

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
