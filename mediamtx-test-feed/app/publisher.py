"""Own exactly one FFmpeg process; no shell and no background descendants."""
import subprocess
import threading
from pathlib import Path
from urllib.parse import urlsplit

VIDEO_EXTENSIONS = {'.mp4', '.m4v', '.mkv', '.mov', '.avi', '.webm', '.ts', '.mts', '.m2ts', '.mpg', '.mpeg'}


def video_path(directory, filename):
    if (not isinstance(filename, str) or not filename or filename in {'.', '..'}
            or any(c in filename for c in ('/', '\\', ':', '\x00'))):
        raise ValueError('Select a video filename from the list.')
    root = Path(directory).resolve()
    try:
        path = (root / filename).resolve(strict=True)
        path.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        raise ValueError('Video is missing or outside the test-video directory.') from None
    if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
        raise ValueError('Select a supported video file.')
    return path


class Publisher:
    def __init__(self, directory, target, popen=subprocess.Popen):
        parsed = urlsplit(target)
        if parsed.scheme != 'rtsp' or not parsed.hostname or not parsed.path.strip('/'):
            raise ValueError('MEDIAMTX_URL must be an RTSP URL with a host and fixed path.')
        self.directory = Path(directory).resolve()
        self.target = target
        self._popen = popen
        self._lock = threading.Lock()
        self._process = None
        self._filename = None
        self._loop = False
        self._error = None
        self._closed = False

    def files(self):
        files = []
        for entry in self.directory.iterdir():
            try:
                video_path(self.directory, entry.name)
                files.append(entry.name)
            except ValueError:
                pass
        return sorted(files, key=str.casefold)

    def _refresh(self):
        if self._process is not None:
            code = self._process.poll()
            if code is not None:
                self._process.wait()
                self._process = None
                self._filename = None
                self._loop = False
                if code:
                    self._error = f'FFmpeg exited with code {code}. Check container logs, the video and MediaMTX target.'

    def _status(self):
        self._refresh()
        return {'state': 'streaming' if self._process else 'stopped',
                'filename': self._filename, 'loop': self._loop, 'error': self._error}

    def status(self):
        with self._lock:
            return self._status()

    def _stop(self):
        self._refresh()
        if self._process is not None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=2)
            self._process = None
        self._filename = None
        self._loop = False

    def start(self, filename, loop=False):
        if not isinstance(loop, bool):
            raise ValueError('Loop must be true or false.')
        with self._lock:
            if self._closed:
                raise RuntimeError('Publisher is shutting down.')
            path = video_path(self.directory, filename)
            self._stop()
            self._error = None
            command = ['ffmpeg', '-hide_banner', '-loglevel', 'warning', '-nostdin', '-re']
            if loop:
                command += ['-stream_loop', '-1']
            command += ['-i', str(path), '-map', '0:v:0', '-an', '-c:v', 'libx264',
                        '-preset', 'veryfast', '-tune', 'zerolatency', '-pix_fmt', 'yuv420p',
                        '-vf', 'pad=ceil(iw/2)*2:ceil(ih/2)*2',
                        '-f', 'rtsp', '-rtsp_transport', 'tcp', self.target]
            try:
                self._process = self._popen(command, stdin=subprocess.DEVNULL,
                                            stdout=subprocess.DEVNULL, shell=False)
            except OSError:
                self._error = 'Could not start FFmpeg. Check that it is installed and executable.'
                raise RuntimeError(self._error) from None
            self._filename = filename
            self._loop = loop
            return self._status()

    def stop(self):
        with self._lock:
            self._stop()
            self._error = None
            return self._status()

    def close(self):
        with self._lock:
            self._closed = True
            self._stop()
