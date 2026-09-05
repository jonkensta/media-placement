"""Internal HTTP endpoints and a single background worker."""

import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import threading
import time

log = logging.getLogger(__name__)
MAX_BODY = 256 * 1024


def handler_for(engine, token):
    class Handler(BaseHTTPRequestHandler):
        server_version = "media-placement"

        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, format, *args):
            # Default HTTP logging echoes untrusted URLs and headers.
            pass

        def respond(self, code, value):
            body = json.dumps(value).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self.wfile.write(body)

        def authorized(self):
            supplied = self.headers.get("Authorization", "").encode()
            return hmac.compare_digest(supplied, ("Bearer " + token).encode())

        def do_GET(self):
            if self.path == "/healthz":
                healthy = engine.last_tick > 0 and time.time() - engine.last_tick < 600
                self.respond(200 if healthy else 503, {"healthy": healthy, "mode": engine.config.mode})
                return
            if not self.authorized():
                self.respond(401, {"error": "authentication required"})
                return
            if self.path == "/jobs":
                self.respond(200, {"jobs": engine.store.jobs(), "reconciliation": engine.reconcile_errors})
            else:
                self.respond(404, {"error": "not found"})

        def do_POST(self):
            if not self.authorized():
                self.respond(401, {"error": "authentication required"})
                return
            paths = {f"/events/{name}": name for name in engine.config.apps}
            if self.path not in paths:
                self.respond(404, {"error": "not found"})
                return
            if self.headers.get("Transfer-Encoding"):
                self.respond(400, {"error": "chunked requests are not supported"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_BODY:
                    self.respond(413, {"error": "body must be between 1 and 262144 bytes"})
                    return
                payload = self.rfile.read(length)
                if len(payload) != length:
                    raise ValueError("incomplete request")
                job = engine.enqueue(paths[self.path], json.loads(payload))
            except (ValueError, UnicodeError):
                self.respond(400, {"error": "invalid, unsupported, or conflicting event"})
                return
            except OSError:
                self.respond(408, {"error": "request timed out"})
                return
            except Exception:
                log.error("could not persist incoming event")
                self.respond(503, {"error": "event was not accepted; retry later"})
                return
            self.respond(200 if job is None else 202,
                         {"status": "test accepted"} if job is None else {"id": job["id"], "status": job["status"]})

    return Handler


def serve(engine, token, stop):
    server = ThreadingHTTPServer((engine.config.host, engine.config.port), handler_for(engine, token))
    server.timeout = 0.5

    def work():
        next_reconcile = 0
        while not stop.is_set():
            try:
                reconcile = time.monotonic() >= next_reconcile
                engine.tick(reconcile=reconcile)
                if reconcile:
                    next_reconcile = time.monotonic() + engine.config.reconcile_seconds
            except Exception:
                log.error("worker cycle failed; it will retry")
            stop.wait(engine.config.poll_seconds)

    worker = threading.Thread(target=work, name="placement-worker", daemon=True)
    worker.start()
    log.info("listening on port %s in %s mode", engine.config.port, engine.config.mode)
    try:
        while not stop.is_set():
            server.handle_request()
    finally:
        server.server_close()
        # Do not exit while the worker may be issuing a Deluge request. Docker's
        # grace period bounds shutdown; persisted intent handles forced exits.
        worker.join()
