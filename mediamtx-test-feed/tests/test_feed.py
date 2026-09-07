import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

from app.publisher import Publisher, video_path
from app.web import create_app


class FeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'videos'
        self.root.mkdir()
        for name in ['bird one.mp4', 'BIRD.MKV', 'notes.txt']:
            (self.root / name).touch()
        self.processes = []
        self.commands = []

        def spawn(command, **kwargs):
            self.assertFalse(kwargs['shell'])
            # A new process must never overlap an existing publisher.
            self.assertTrue(all(p.poll() is not None for p in self.processes))
            p = Mock()
            p.poll.return_value = None
            p.terminate.side_effect = lambda: setattr(p.poll, 'return_value', 0)
            p.kill.side_effect = lambda: setattr(p.poll, 'return_value', -9)
            self.processes.append(p)
            self.commands.append(command)
            return p

        self.publisher = Publisher(self.root, 'rtsp://mediamtx:8554/test', spawn)
        self.addCleanup(self.publisher.close)
        self.client = create_app(self.publisher).test_client()

    def test_discovery(self):
        (self.root / 'folder.mp4').mkdir()
        self.assertEqual(self.publisher.files(), ['bird one.mp4', 'BIRD.MKV'])

    def test_reject_invalid_paths(self):
        for name in [None, 42, '', '..', '../outside.mp4', '/tmp/a.mp4',
                     'C:\\x.mp4', 'sub/a.mp4', 'sub\\a.mp4', 'x\x00.mp4',
                     'bird one.mp4:stream', 'notes.txt', 'missing.mp4']:
            with self.subTest(name=name), self.assertRaises(ValueError):
                video_path(self.root, name)

    def test_symlink_escape(self):
        outside = Path(self.temp.name) / 'outside.mp4'
        outside.touch()
        link = self.root / 'escape.mp4'
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest('Creating symlinks requires Windows Developer Mode or privilege')
        with self.assertRaises(ValueError):
            video_path(self.root, link.name)
        self.assertNotIn(link.name, self.publisher.files())

    def test_filename_is_one_argument(self):
        name = 'bird;echo hello.mp4'
        (self.root / name).touch()
        state = self.publisher.start(name)
        self.assertEqual(state['filename'], name)
        command = self.commands[0]
        self.assertEqual(command[command.index('-i') + 1], str((self.root / name).resolve()))
        self.assertEqual(command[-1], 'rtsp://mediamtx:8554/test')
        self.assertNotIn('-stream_loop', command)

    def test_start_loop_replace_stop(self):
        self.assertEqual(self.publisher.status()['state'], 'stopped')
        state = self.publisher.start('bird one.mp4', True)
        self.assertEqual(state['state'], 'streaming')
        self.assertTrue(state['loop'])
        command = self.commands[0]
        self.assertEqual(command[command.index('-stream_loop') + 1], '-1')
        self.assertLess(command.index('-stream_loop'), command.index('-i'))
        self.publisher.start('BIRD.MKV')
        self.processes[0].terminate.assert_called_once()
        self.processes[0].wait.assert_called_once()
        self.assertEqual(self.publisher.stop()['state'], 'stopped')
        self.publisher.stop()
        self.processes[1].terminate.assert_called_once()

    def test_bad_selection_preserves_feed(self):
        self.publisher.start('bird one.mp4')
        with self.assertRaises(ValueError):
            self.publisher.start('../missing.mp4')
        self.assertEqual(self.publisher.status()['filename'], 'bird one.mp4')
        self.processes[0].terminate.assert_not_called()

    def test_exit_and_error_state(self):
        for code in [0, 1]:
            self.publisher.start('bird one.mp4')
            self.processes[-1].poll.return_value = code
            state = self.publisher.status()
            self.assertEqual(state['state'], 'stopped')
            self.assertIsNone(state['filename'])
            self.assertEqual(bool(state['error']), bool(code))

    def test_kill_fallback_reaps_process(self):
        self.publisher.start('bird one.mp4')
        p = self.processes[0]
        p.wait.side_effect = [subprocess.TimeoutExpired('ffmpeg', 5), 0]
        self.publisher.stop()
        p.kill.assert_called_once()
        self.assertEqual(p.wait.call_count, 2)

    def test_failed_stop_never_starts_replacement(self):
        self.publisher.start('bird one.mp4')
        p = self.processes[0]
        p.terminate.side_effect = None
        p.kill.side_effect = None
        p.wait.side_effect = subprocess.TimeoutExpired('ffmpeg', 2)
        response = self.client.post('/api/start', json={'filename': 'BIRD.MKV'})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(len(self.processes), 1)
        p.wait.side_effect = None
        p.poll.return_value = 0

    def test_concurrent_starts_are_serialized(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: self.publisher.start('bird one.mp4'), range(8)))
        self.assertEqual(sum(p.poll() is None for p in self.processes), 1)

    def test_shutdown_stops_and_prevents_restart(self):
        self.publisher.start('bird one.mp4')
        self.publisher.close()
        self.processes[0].terminate.assert_called_once()
        with self.assertRaises(RuntimeError):
            self.publisher.start('bird one.mp4')
        self.publisher.close()

    def test_spawn_failure(self):
        self.publisher._popen = Mock(side_effect=FileNotFoundError)
        response = self.client.post('/api/start', json={'filename': 'bird one.mp4'})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.publisher.status()['state'], 'stopped')
        self.assertIn('FFmpeg', self.publisher.status()['error'])

    def test_http_routes(self):
        self.assertEqual(self.client.get('/health').json, {'status': 'ok'})
        self.assertIn(b'Test feed', self.client.get('/').data)
        self.assertEqual(len(self.client.get('/api/videos').json['files']), 2)
        response = self.client.post('/api/start', json={'filename': 'bird one.mp4'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get('/api/status').json['state'], 'streaming')
        self.assertEqual(self.client.post('/api/stop', json={}).json['state'], 'stopped')

    def test_bad_requests(self):
        for payload in [[], {}, {'filename': '../a.mp4'}, {'filename': 'BIRD.MKV', 'loop': 'false'}]:
            self.assertEqual(self.client.post('/api/start', json=payload).status_code, 400)
        self.assertEqual(self.client.post('/api/start', data='bad').status_code, 400)
        self.assertEqual(self.client.post('/api/stop', data='').status_code, 400)
        self.assertEqual(self.client.get('/api/start').status_code, 405)

    def test_missing_mount(self):
        self.publisher.directory = self.root / 'missing'
        self.assertEqual(self.client.get('/api/videos').status_code, 503)
        self.assertEqual(self.client.get('/health').status_code, 200)

    def test_invalid_target(self):
        for target in ['http://host/test', 'rtsp://host', 'rtsp:///test']:
            with self.assertRaises(ValueError):
                Publisher(self.root, target)


if __name__ == '__main__':
    unittest.main()
