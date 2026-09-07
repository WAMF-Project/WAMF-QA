import os
import subprocess

from flask import Flask, jsonify, render_template, request

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
            return jsonify(files=publisher.files())
        except OSError:
            return jsonify(error='Cannot read the test-video directory. Check the mount and permissions.'), 503

    @app.get('/api/status')
    def status():
        return jsonify(publisher.status())

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
