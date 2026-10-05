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


class SearchResourceCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        (self.root / 'forge').mkdir()
        for name in ['engine', 'arena', 'net', 'book']:
            (self.root / name).write_bytes(name.encode())
        self.item = {'name': 'cont4', 'opts': {'cont4': '1'}, 'change': 'continuation'}
        self.state = {'champion_net': str(self.root / 'net')}
        self.args = types.SimpleNamespace(data=str(self.root), engine=str(self.root / 'engine'),
            arena=str(self.root / 'arena'), book=str(self.root / 'book'), cpu_budget=4,
            selfplay_threads=4, concurrency=4, tc='8+0.08', max_games=12, batch_games=4)
        loop.save_json(str(self.root / 'forge/search.json'), {'accepted': {}, 'history': []})
        loop.save_json(str(self.root / 'forge/queue.json'), {'pending': [self.item], 'done': []})

    def tearDown(self):
        self.tmp.cleanup()

    def checkpoint(self, concurrency):
        _, _, _, out = loop.search_evidence(self.args, self.state, self.item)
        def batch(cmd, logfile):
            local_gate.save(cmd[cmd.index('--out') + 1], {'seed': int(cmd[cmd.index('--seed') + 1]),
                'games': 4, 'wins': 1, 'losses': 1, 'draws': 2, 'penta': [0, 1, 0, 1, 0],
                'seconds': 1, 'reasons': {'adjudicated': 4}})
            return 0
        with patch.object(local_gate, 'run', batch):
            local_gate.gate(engine=self.args.engine, arena=self.args.arena,
                candidate=self.state['champion_net'], champion=self.state['champion_net'],
                book=self.args.book, tc=self.args.tc, concurrency=concurrency, max_games=12,
                elo0=0, elo1=5, cand_opts=self.item['opts'], champ_opts={},
                out=out, logfile=str(self.root / 'log'), log=lambda _: None,
                batch_games=4, max_batches=1)
        return pathlib.Path(out)

    def test_storage_pause_preserves_the_existing_gate_and_its_concurrency(self):
        out = self.checkpoint(2)
        original = out.read_bytes()
        self.assertTrue(loop.search_has_progress(self.args, self.state, self.item, ready=False))
        with patch.object(local_gate, 'gate', return_value={'decision': 'running'}) as run:
            loop.search_batch(self.args, self.state, str(self.root / 'log'))
        self.assertEqual(run.call_args.kwargs['concurrency'], 2)
        self.assertEqual(out.read_bytes(), original)

    def test_storage_recovery_does_not_oversubscribe_a_full_budget_checkpoint(self):
        self.checkpoint(4)
        self.assertTrue(loop.search_has_progress(self.args, self.state, self.item, ready=True))
        self.assertEqual(loop.selfplay_workers(self.args, self.item, self.state), 0)
        # Extra live generators must not cause a valid checkpoint to be replaced.
        playing = {**self.state, 'selfplay_pid': 1, 'selfplay_threads': 2}
        with patch.object(loop, 'alive', return_value=True):
            with self.assertRaisesRegex(RuntimeError, 'saved search gate'):
                loop.search_batch(self.args, playing, str(self.root / 'log'))

    def test_saved_resources_do_not_allow_changed_settings_or_inputs_to_mix(self):
        self.checkpoint(2)
        changed = {**self.item, 'opts': {'cont4': '0'}}
        self.assertFalse(loop.search_has_progress(self.args, self.state, changed, ready=False))
        (self.root / 'book').write_bytes(b'different opening book')
        self.assertFalse(loop.search_has_progress(self.args, self.state, self.item, ready=False))

    def test_ready_training_remains_deferred_through_a_storage_pause(self):
        out = self.checkpoint(2)
        before = loop.load_json(str(out), {})
        data = self.root / 'selfplay/gen1/data.bin'
        data.parent.mkdir(parents=True)
        data.write_bytes(bytes(4 * 32))
        state = {**self.state, 'generation': 1, 'champion_version': '0.12.0',
                 'trained_on': 0, 'attempts': 0, 'sizes': {}}
        loop.save_json(str(self.root / 'forge/state.json'), state)
        def batch(cmd, logfile):
            self.assertEqual(cmd[cmd.index('--concurrency') + 1], '2')
            local_gate.save(cmd[cmd.index('--out') + 1], {'seed': int(cmd[cmd.index('--seed') + 1]),
                'games': 4, 'wins': 1, 'losses': 1, 'draws': 2, 'penta': [0, 1, 0, 1, 0],
                'seconds': 1, 'reasons': {'adjudicated': 4}})
            return 0
        argv = ['loop.py', '--data', str(self.root), '--engine', self.args.engine,
            '--arena', self.args.arena, '--book', self.args.book, '--cpu-budget', '4',
            '--selfplay-threads', '4', '--concurrency', '4', '--min-new', '1',
            '--max-games', '12', '--batch-games', '4', '--min-free-gb', '0',
            '--local-search', '--once', '--no-publish']
        with patch.object(sys, 'argv', argv), patch.object(loop.signal, 'signal'):
            with patch.object(loop, 'storage_ready', return_value=False), patch.object(loop, 'ensure_selfplay'):
                with patch.object(loop, 'run', side_effect=AssertionError('training preempted the active gate')):
                    with patch.object(local_gate, 'run', batch), contextlib.redirect_stdout(io.StringIO()):
                        loop.main()
        saved = loop.load_json(str(self.root / 'forge/state.json'), {})
        after = loop.load_json(str(out), {})
        self.assertEqual(saved['attempts'], 0)
        self.assertEqual(saved['training_deferred_for'], 'cont4')
        self.assertEqual(after['games'], 8)
        self.assertEqual(after['config'], before['config'])
        self.assertEqual(after['seed'], before['seed'])


if __name__ == '__main__':
    unittest.main()
