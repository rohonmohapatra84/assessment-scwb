"""Threaded WSGI CLI with graceful request draining and analytics shutdown."""

import argparse
import os
import signal
from socketserver import ThreadingMixIn
import threading
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from .app import create_app


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = False
    block_on_close = True
    request_queue_size = 128


class PrivateRequestHandler(WSGIRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        # Default access logs expose raw client IPs and potentially sensitive URLs.
        pass


def main(argv: list | None = None) -> None:
    parser = argparse.ArgumentParser(description="Standard-library URL shortener")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be from 1 to 65535")
    app = create_app(os.environ.get("SHORTENER_DB", "shortener.db"),
                     base_url=os.environ.get("SHORTENER_BASE_URL", "http://localhost:" + str(args.port)),
                     api_keys=tuple(key.strip() for key in os.environ.get("SHORTENER_API_KEYS", "").split(",")
                                    if key.strip()))
    server = None
    previous: dict[int, object] = {}
    stop_requested = threading.Event()
    shutdown_thread: threading.Thread | None = None
    try:
        server = make_server(args.host, args.port, app, server_class=ThreadingWSGIServer,
                             handler_class=PrivateRequestHandler)
        server.timeout = 1.0
        def request_shutdown(signum: int, frame: object) -> None:
            nonlocal shutdown_thread
            if not stop_requested.is_set():
                stop_requested.set()
                # BaseServer.shutdown must not run in the serve_forever thread.
                shutdown_thread = threading.Thread(target=server.shutdown, name="shortener-shutdown")
                shutdown_thread.start()
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous[signum] = signal.signal(signum, request_shutdown)
        server.serve_forever(poll_interval=0.2)
    finally:
        if shutdown_thread is not None:
            shutdown_thread.join()
        if server is not None:
            server.server_close()
        try:
            app.close()
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)


if __name__ == "__main__":
    main()
