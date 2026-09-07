import signal

from waitress import create_server

from .web import create_app


def main():
    app = create_app()
    publisher = app.extensions['publisher']

    def shutdown(signum, frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    server = None
    try:
        server = create_server(app, host='0.0.0.0', port=8080, threads=4)
        server.run()
    finally:
        # Mark closed under the same lock used by Start, including in-flight requests.
        publisher.close()
        if server is not None:
            server.close()


if __name__ == '__main__':
    main()
