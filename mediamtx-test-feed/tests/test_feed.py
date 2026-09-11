import json
import io
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

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

        self.stream = {'codec_name': 'h264', 'pix_fmt': 'yuv420p', 'width': 1280,
                       'height': 720, 'disposition': {'attached_pic': 0}}
        self.probe = Mock(return_value=Mock(stdout=json.dumps({'streams': [self.stream]})))
        self.publisher = Publisher(self.root, 'rtsp://mediamtx:8554/test', spawn, self.probe)
        self.addCleanup(self.publisher.close)
        self.client = create_app(self.publisher).test_client()

    def test_discovery(self):
        (self.root / 'folder.mp4').mkdir()
        self.assertEqual(self.publisher.files(), ['bird one.mp4', 'BIRD.MKV'])

    def test_file_sizes(self):
        (self.root / 'bird one.mp4').write_bytes(b'x' * 1234)
        data = self.client.get('/api/videos').json
        self.assertEqual(data['sizes'], {'bird one.mp4': 1234, 'BIRD.MKV': 0})
        self.assertEqual(data['files'], ['bird one.mp4', 'BIRD.MKV'])

    def test_metadata(self):
        self.probe.return_value.stdout = json.dumps({
            'streams': [self.stream | {'avg_frame_rate': '30000/1001'}],
            'format': {'duration': '12.5'}})
        response = self.client.get('/api/videos/bird%20one.mp4')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['width'], 1280)
        self.assertEqual(response.json['height'], 720)
        self.assertEqual(response.json['codec'], 'h264')
        self.assertAlmostEqual(response.json['fps'], 29.97003, places=4)
        self.assertEqual(response.json['duration'], 12.5)
        self.assertEqual(self.commands, [])

    def test_metadata_unknown_and_failure(self):
        self.probe.return_value.stdout = json.dumps({'streams': [
            {'avg_frame_rate': '0/0', 'duration': 'N/A', 'width': -1}]})
        self.assertEqual(self.client.get('/api/videos/BIRD.MKV').json,
                         dict(width=None, height=None, codec=None, fps=None, duration=None))
        for value in ['invalid', '{}', '{"streams": [null]}']:
            self.probe.return_value.stdout = value
            self.assertEqual(self.client.get('/api/videos/BIRD.MKV').status_code, 503)
        self.probe.side_effect = subprocess.TimeoutExpired('ffprobe', 10)
        self.assertEqual(self.client.get('/api/videos/BIRD.MKV').status_code, 503)

    def test_mode_reporting(self):
        self.assertEqual(self.publisher.status()['mode'], 'Idle')
        self.assertEqual(self.publisher.start('BIRD.MKV')['mode'], 'Stream copy')
        self.assertEqual(self.publisher.stop()['mode'], 'Idle')
        self.probe.return_value.stdout = json.dumps({'streams': [self.stream | {'codec_name': 'vp9'}]})
        self.assertEqual(self.publisher.start('BIRD.MKV')['mode'], 'Transcoding')
        self.assertEqual(self.client.get('/api/status').json['mode'], 'Transcoding')
        self.processes[-1].poll.return_value = 1
        self.assertEqual(self.publisher.status()['mode'], 'Idle')
        self.publisher._popen = Mock(side_effect=FileNotFoundError)
        with self.assertRaises(RuntimeError):
            self.publisher.start('BIRD.MKV')
        self.assertEqual(self.publisher.status()['mode'], 'Idle')

    def test_delete_success_and_duplicate(self):
        self.publisher.start('bird one.mp4')
        self.assertEqual(self.client.delete('/api/videos/BIRD.MKV').status_code, 200)
        self.assertFalse((self.root / 'BIRD.MKV').exists())
        self.assertEqual(self.publisher.status()['filename'], 'bird one.mp4')
        self.assertEqual(self.client.delete('/api/videos/BIRD.MKV').status_code, 400)

    def test_delete_active_refused(self):
        self.publisher.start('BIRD.MKV')
        response = self.client.delete('/api/videos/BIRD.MKV')
        self.assertEqual(response.status_code, 409)
        self.assertIn('currently being streamed', response.json['error'])
        self.assertTrue((self.root / 'BIRD.MKV').exists())
        self.processes[-1].poll.return_value = 0
        self.assertEqual(self.client.delete('/api/videos/BIRD.MKV').status_code, 200)

    def test_delete_active_alias_refused(self):
        try:
            (self.root / 'alias.mp4').hardlink_to(self.root / 'BIRD.MKV')
        except OSError:
            self.skipTest('Hard link creation unavailable')
        self.publisher.start('BIRD.MKV')
        self.assertEqual(self.client.delete('/api/videos/alias.mp4').status_code, 409)
        self.assertTrue((self.root / 'alias.mp4').exists())

    def test_delete_permission_failure(self):
        with patch.object(Path, 'unlink', side_effect=PermissionError):
            response = self.client.delete('/api/videos/BIRD.MKV')
        self.assertEqual(response.status_code, 503)
        self.assertIn('permissions', response.json['error'])
        self.assertTrue((self.root / 'BIRD.MKV').exists())

    def test_metadata_and_delete_unsafe_paths(self):
        for name in ['..', '..%2Foutside.mp4', 'sub%5Cclip.mp4', 'C:clip.mp4', 'notes.txt']:
            for method in (self.client.get, self.client.delete):
                with self.subTest(name=name, method=method):
                    self.assertIn(method('/api/videos/' + name).status_code, (400, 404))
        self.assertTrue((self.root / 'notes.txt').exists())

    def test_delete_symlink_refused(self):
        outside = Path(self.temp.name) / 'outside.mp4'
        outside.write_bytes(b'keep')
        try:
            (self.root / 'link.mp4').symlink_to(outside)
        except OSError:
            self.skipTest('Symlink creation unavailable')
        self.assertEqual(self.client.delete('/api/videos/link.mp4').status_code, 400)
        self.assertEqual(outside.read_bytes(), b'keep')

    def upload(self, filename, content=b'test video' * 1024):
        return self.client.post('/api/upload', data={'file': (io.BytesIO(content), filename)})

    def test_upload_success(self):
        self.publisher.start('bird one.mp4', True)
        before = self.publisher.status()
        for filename in ['new bird.mp4', 'clip.mov', 'clip.MKV']:
            with self.subTest(filename=filename):
                response = self.upload(filename)
                self.assertEqual(response.status_code, 201)
                saved = filename.replace(' ', '_')
                self.assertEqual(response.json, {'filename': saved})
                self.assertEqual((self.root / saved).read_bytes(), b'test video' * 1024)
                self.assertIn(saved, self.client.get('/api/videos').json['files'])
        self.assertEqual(self.publisher.status(), before)
        self.assertEqual(len(self.commands), 1)

    def test_upload_unsupported_extension(self):
        for filename in ['notes.txt', 'clip.avi', 'clip.mp4.exe', 'clip']:
            with self.subTest(filename=filename):
                response = self.upload(filename)
                self.assertEqual(response.status_code, 400)
                self.assertIn('.mp4', response.json['error'])
                self.assertFalse((self.root / filename).exists() and filename != 'notes.txt')

    def test_upload_unsafe_filename(self):
        before = sorted(self.root.iterdir())
        for filename in ['../escape.mp4', '/tmp/escape.mp4', 'sub/clip.mp4',
                         'sub\\clip.mp4', 'C:\\clip.mp4', 'clip\x00.mp4']:
            with self.subTest(filename=filename):
                response = self.upload(filename)
                self.assertEqual(response.status_code, 400)
                self.assertIn('path', response.json['error'])
        self.assertEqual(sorted(self.root.iterdir()), before)

    def test_upload_duplicate_filename(self):
        self.assertEqual(self.upload('new bird.mp4', b'original').status_code, 201)
        response = self.upload('new_bird.mp4', b'replacement')
        self.assertEqual(response.status_code, 409)
        self.assertIn('already exists', response.json['error'])
        self.assertEqual((self.root / 'new_bird.mp4').read_bytes(), b'original')

    def test_upload_missing_file(self):
        self.assertEqual(self.client.post('/api/upload', data={}).status_code, 400)
        self.assertEqual(self.upload('').status_code, 400)

    def test_upload_existing_symlink(self):
        outside = Path(self.temp.name) / 'outside.mp4'
        outside.write_bytes(b'original')
        try:
            (self.root / 'link.mp4').symlink_to(outside)
        except OSError:
            self.skipTest('Symlink creation unavailable')
        self.assertEqual(self.upload('link.mp4').status_code, 409)
        self.assertEqual(outside.read_bytes(), b'original')

    def assert_encoding(self, copy):
        with self.assertLogs('app.publisher', level='INFO') as logs:
            self.publisher.start('bird one.mp4', True)
        command = self.commands[-1]
        self.assertEqual(command[command.index('-c:v') + 1], 'copy' if copy else 'libx264')
        self.assertIn('stream copy' if copy else 'transcoding', '\n'.join(logs.output))
        self.assertIn('-an', command)
        self.assertLess(command.index('-re'), command.index('-i'))
        self.assertLess(command.index('-stream_loop'), command.index('-i'))
        self.assertEqual(command[command.index('-stream_loop') + 1], '-1')
        self.assertEqual(command[command.index('-map') + 1], '0:v:0')
        self.assertEqual(command[-5:], ['-f', 'rtsp', '-rtsp_transport', 'tcp',
                                        'rtsp://mediamtx:8554/test'])
        if copy:
            for option in ['-preset', '-tune', '-pix_fmt', '-vf']:
                self.assertNotIn(option, command)
        else:
            for option, value in [('-preset', 'veryfast'), ('-tune', 'zerolatency'),
                                  ('-pix_fmt', 'yuv420p'),
                                  ('-vf', 'pad=ceil(iw/2)*2:ceil(ih/2)*2')]:
                self.assertEqual(command[command.index(option) + 1], value)

    def test_h264_copy(self):
        self.assert_encoding(copy=True)
        args, kwargs = self.probe.call_args
        self.assertEqual(args[0][-1], str((self.root / 'bird one.mp4').resolve()))
        self.assertEqual(args[0][args[0].index('-select_streams') + 1], 'v:0')
        self.assertFalse(kwargs['shell'])
        self.assertEqual(kwargs['timeout'], 10)
        self.assertTrue(kwargs['check'])

    def test_incompatible_video_transcodes(self):
        for changes in [{'codec_name': 'hevc'}, {'codec_name': 'vp9'},
                        {'codec_name': 'mpeg4'}, {'pix_fmt': 'yuv420p10le'},
                        {'pix_fmt': 'yuv422p'}, {'pix_fmt': 'yuv444p'},
                        {'width': 0},
                        {'height': '720'}, {'disposition': {'attached_pic': 1}}]:
            with self.subTest(changes=changes):
                self.probe.return_value.stdout = json.dumps({'streams': [self.stream | changes]})
                self.assert_encoding(copy=False)

    def test_h264_unknown_or_missing_optional_metadata_copies(self):
        for optional in [{'pix_fmt': 'unknown', 'profile': 'unknown', 'level': -99},
                         {}, {'pix_fmt': None}, {'pix_fmt': 'yuv420p'}]:
            with self.subTest(optional=optional):
                stream = {'codec_name': 'h264', 'width': 1280, 'height': 720} | optional
                self.probe.return_value.stdout = json.dumps({'streams': [stream]})
                self.assert_encoding(copy=True)

    def test_h264_positive_odd_dimensions_copy(self):
        self.probe.return_value.stdout = json.dumps(
            {'streams': [self.stream | {'width': 1279, 'height': 719}]})
        self.assert_encoding(copy=True)

    def test_malformed_or_missing_dimensions_transcode(self):
        for key in ('width', 'height'):
            for value in (None, 0, -1, '720', 720.5, True, [], {}):
                with self.subTest(key=key, value=value):
                    self.probe.return_value.stdout = json.dumps({'streams': [self.stream | {key: value}]})
                    self.assert_encoding(copy=False)
            with self.subTest(missing=key):
                stream = {k: v for k, v in self.stream.items() if k != key}
                self.probe.return_value.stdout = json.dumps({'streams': [stream]})
                self.assert_encoding(copy=False)

    def test_probe_failure_transcodes(self):
        for error in [FileNotFoundError(), subprocess.TimeoutExpired('ffprobe', 10),
                      subprocess.CalledProcessError(1, 'ffprobe')]:
            with self.subTest(error=error):
                self.probe.side_effect = error
                self.assert_encoding(copy=False)

    def test_invalid_probe_metadata_transcodes(self):
        for output in ['invalid json', '{}', '{"streams": []}', '{"streams": [null]}',
                       '{"streams": [{}]}', '{"streams": null}']:
            with self.subTest(output=output):
                self.probe.return_value.stdout = output
                self.assert_encoding(copy=False)

    def test_each_start_probes_selected_file(self):
        self.assert_encoding(copy=True)
        self.probe.return_value.stdout = json.dumps({'streams': [self.stream | {'codec_name': 'vp9'}]})
        self.publisher.start('BIRD.MKV')
        self.assertEqual(self.probe.call_count, 2)
        self.assertEqual(self.probe.call_args.args[0][-1], str((self.root / 'BIRD.MKV').resolve()))
        self.assertEqual(self.commands[-1][self.commands[-1].index('-c:v') + 1], 'libx264')

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
