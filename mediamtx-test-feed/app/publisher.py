"""Own exactly one FFmpeg process; no shell and no background descendants."""
import json
import logging
import math
from fractions import Fraction
import subprocess
import threading
from pathlib import Path
from urllib.parse import urlsplit

VIDEO_EXTENSIONS = {'.mp4', '.m4v', '.mkv', '.mov', '.avi', '.webm', '.ts', '.mts', '.m2ts', '.mpg', '.mpeg'}
logger = logging.getLogger(__name__)


def probe_video(path, run=subprocess.run, entries='stream=codec_name,pix_fmt,width,height:stream_disposition=attached_pic'):
    result = run(
        ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
         '-show_entries', entries, '-of', 'json', str(path)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, check=True, timeout=10, shell=False)
    return json.loads(result.stdout)


def positive_number(value):
    try:
        number = float(Fraction(str(value)))
        return number if math.isfinite(number) and number > 0 else None
    except (ValueError, ZeroDivisionError, OverflowError):
        return None


def can_copy_video(path, run=subprocess.run):
    """Copy H.264 unless required metadata is invalid or incompatibility is known."""
    try:
        stream = probe_video(path, run)['streams'][0]
        if not isinstance(stream, dict):
            return False
        disposition = stream.get('disposition', {})
        if not isinstance(disposition, dict):
            return False
        # FFprobe may identify H.264 and dimensions without resolving its pixel
        # format. Profile and level are likewise not required for stream copy.
        return (stream['codec_name'] == 'h264'
                and stream.get('pix_fmt') in (None, 'unknown', 'yuv420p')
                and all(type(stream[key]) is int and stream[key] > 0
                        for key in ('width', 'height'))
                and disposition.get('attached_pic', 0) == 0)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError, TypeError):
        logger.warning('Video probe failed or returned incomplete metadata; using transcoding.')
        return False


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
    def __init__(self, directory, target, popen=subprocess.Popen, probe_run=subprocess.run):
        parsed = urlsplit(target)
        if parsed.scheme != 'rtsp' or not parsed.hostname or not parsed.path.strip('/'):
            raise ValueError('MEDIAMTX_URL must be an RTSP URL with a host and fixed path.')
        self.directory = Path(directory).resolve()
        self.target = target
        self._popen = popen
        self._probe_run = probe_run
        self._lock = threading.Lock()
        self._process = None
        self._filename = None
        self._loop = False
        self._error = None
        self._closed = False
        self._mode = None

    def files(self):
        files = []
        for entry in self.directory.iterdir():
            try:
                video_path(self.directory, entry.name)
                files.append(entry.name)
            except ValueError:
                pass
        return sorted(files, key=str.casefold)

    def file_sizes(self, filenames):
        return {name: video_path(self.directory, name).stat().st_size for name in filenames}

    def metadata(self, filename):
        path = video_path(self.directory, filename)
        try:
            data = probe_video(path, self._probe_run,
                               'stream=codec_name,width,height,avg_frame_rate,r_frame_rate,duration:format=duration')
            stream = data['streams'][0]
            width, height = stream.get('width'), stream.get('height')
            codec = stream.get('codec_name')
            return {'width': width if type(width) is int and width > 0 else None,
                    'height': height if type(height) is int and height > 0 else None,
                    'codec': codec if isinstance(codec, str) else None,
                    'fps': positive_number(stream.get('avg_frame_rate'))
                           or positive_number(stream.get('r_frame_rate')),
                    'duration': positive_number(stream.get('duration'))
                                or positive_number(data.get('format', {}).get('duration'))}
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise RuntimeError('Video metadata unavailable. Check the file and FFprobe.') from None

    def delete(self, filename):
        # Serialize the active-file check and deletion with Start/Stop.
        with self._lock:
            path = video_path(self.directory, filename)
            if (self.directory / filename).is_symlink():
                raise ValueError('Deleting symbolic links is not supported.')
            self._refresh()
            if self._process and path.samefile(self.directory / self._filename):
                raise RuntimeError('Cannot delete the video currently being streamed. Stop it first.')
            path.unlink()

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
                'mode': self._mode if self._process else 'Idle',
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
            copy_video = can_copy_video(path, self._probe_run)
            command += ['-i', str(path), '-map', '0:v:0', '-an']
            if copy_video:
                command += ['-c:v', 'copy']
            else:
                command += ['-c:v', 'libx264', '-preset', 'veryfast', '-tune', 'zerolatency',
                            '-pix_fmt', 'yuv420p', '-vf', 'pad=ceil(iw/2)*2:ceil(ih/2)*2']
            command += ['-f', 'rtsp', '-rtsp_transport', 'tcp', self.target]
            logger.info('Launching FFmpeg for %r using %s.', filename,
                        'H.264 stream copy' if copy_video else 'libx264 transcoding')
            try:
                self._process = self._popen(command, stdin=subprocess.DEVNULL,
                                            stdout=subprocess.DEVNULL, shell=False)
            except OSError:
                self._error = 'Could not start FFmpeg. Check that it is installed and executable.'
                raise RuntimeError(self._error) from None
            self._mode = 'Stream copy' if copy_video else 'Transcoding'
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
