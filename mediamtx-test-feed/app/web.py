import os
import subprocess
from pathlib import Path

from flask import Flask, jsonify, render_template, request
from werkzeug.utils import secure_filename

from .publisher import Publisher


def create_app(publisher=None):
    app = Flask(__name__)
    app.config['MAX_CONTENT_LENGTH'] = 4096
    publisher = publisher or Publisher(os.getenv('VIDEO_DIR', '/videos'),
                                      os.getenv('MEDIAMTX_URL', 'rtsp://mediamtx:8554/test'))
    app.extensions['publisher'] = publisher

    @app.get('/')
    def index():
        return render_template('index.html')

    @app.get('/health')
    def health():
        return jsonify(status='ok')

    @app.get('/api/videos')
    def videos():
        try:
            files = publisher.files()
            return jsonify(files=files, sizes=publisher.file_sizes(files))
        except (OSError, ValueError):
            return jsonify(error='Cannot read the test-video directory. Check the mount and permissions.'), 503

    @app.get('/api/videos/<filename>')
    def video_metadata(filename):
        try:
            return jsonify(publisher.metadata(filename))
        except ValueError as error:
            return jsonify(error=str(error)), 400
        except RuntimeError as error:
            return jsonify(error=str(error)), 503

    @app.delete('/api/videos/<filename>')
    def delete_video(filename):
        try:
            publisher.delete(filename)
            return jsonify(filename=filename)
        except ValueError as error:
            return jsonify(error=str(error)), 400
        except RuntimeError as error:
            return jsonify(error=str(error)), 409
        except OSError:
            return jsonify(error='Cannot delete the video. Check the mount and permissions.'), 503

    @app.get('/api/status')
    def status():
        return jsonify(publisher.status())

    @app.post('/api/upload')
    def upload():
        request.max_content_length = 2 * 1024 ** 3
        uploaded = request.files.get('file')
        if uploaded is None or not uploaded.filename:
            return jsonify(error='Choose a video file to upload.'), 400
        original = uploaded.filename
        if any(c in original for c in ('/', '\\', ':', '\x00')) or original in {'.', '..'}:
            return jsonify(error='Use a filename without a directory or path.'), 400
        filename = secure_filename(original)
        if Path(filename).suffix.lower() not in {'.mp4', '.mov', '.mkv'}:
            return jsonify(error='Upload an .mp4, .mov or .mkv video.'), 400
        destination = publisher.directory / filename
        try:
            # Exclusive creation also rejects existing symlinks and concurrent duplicates.
            output = destination.open('xb')
            try:
                with output:
                    uploaded.save(output)
            except OSError:
                destination.unlink()
                raise
        except FileExistsError:
            return jsonify(error=f'A file named "{filename}" already exists.'), 409
        except OSError:
            return jsonify(error='Cannot save the video. Check directory permissions and free space.'), 503
        return jsonify(filename=filename), 201

    @app.errorhandler(413)
    def too_large(error):
        return jsonify(error='Upload exceeds the 2 GiB limit.' if request.endpoint == 'upload'
                       else 'Request is too large.'), 413

    @app.post('/api/start')
    def start():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(error='Expected a JSON object.'), 400
        try:
            return jsonify(publisher.start(data.get('filename'), data.get('loop', False)))
        except ValueError as error:
            return jsonify(error=str(error)), 400
        except RuntimeError as error:
            return jsonify(error=str(error)), 503
        except (OSError, subprocess.TimeoutExpired):
            return jsonify(error='Could not stop the previous publisher. Check container logs.'), 503

    @app.post('/api/stop')
    def stop():
        # Requiring JSON also prevents ordinary cross-origin form submissions.
        if not request.is_json:
            return jsonify(error='Expected JSON.'), 400
        try:
            return jsonify(publisher.stop())
        except (OSError, subprocess.TimeoutExpired):
            return jsonify(error='Could not stop FFmpeg. Check container logs.'), 503

    return app
