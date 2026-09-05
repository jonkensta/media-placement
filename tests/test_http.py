from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from media_placement.clients import DelugeClient, RemoteError
from media_placement.server import handler_for
import test_engine as fixtures


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.EngineTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.token = "test-token-" + "x" * 32
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(self.fixture.engine, self.token))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def post(self, payload, token=None, path="/events/radarr"):
        req = urllib.request.Request(self.url + path, data=json.dumps(payload).encode(),
                                     headers={"Authorization": "Bearer " + (token or self.token), "Content-Type": "application/json"})
        return urllib.request.urlopen(req, timeout=2)

    def test_accepts_and_persists_realistic_event(self):
        with self.post(fixtures.event()) as response:
            self.assertEqual(response.status, 202)
            job_id = json.load(response)["id"]
        self.assertIsNotNone(self.fixture.store.get(job_id))

    def test_unauthorized_event_creates_no_job(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.post(fixtures.event(), token="wrong")
        self.assertEqual(error.exception.code, 401)
        error.exception.close()
        self.assertEqual(self.fixture.store.jobs(), [])

    def test_test_button_has_no_side_effect(self):
        with self.post({"eventType": "Test"}) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(self.fixture.store.jobs(), [])

    def test_status_is_authenticated(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(self.url + "/jobs", timeout=2)
        self.assertEqual(error.exception.code, 401)
        error.exception.close()

    def test_health_waits_for_worker(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(self.url + "/healthz", timeout=2)
        self.assertEqual(error.exception.code, 503)
        error.exception.close()
        self.fixture.engine.tick()
        with urllib.request.urlopen(self.url + "/healthz", timeout=2) as response:
            self.assertTrue(json.load(response)["healthy"])


class DelugeAdapterTests(unittest.TestCase):
    def test_json_rpc_authentication_cookie_and_method_shape(self):
        calls = []
        test = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append(body)
                method = body["method"]
                if method == "auth.login":
                    test.assertEqual(body["params"], ["test-password"])
                    result = True
                else:
                    test.assertIn("_session_id=test", self.headers.get("Cookie", ""))
                    result = True if method == "web.connected" else {"state": "Paused"}
                payload = json.dumps({"result": result, "error": None, "id": body["id"]}).encode()
                self.send_response(200)
                if method == "auth.login":
                    self.send_header("Set-Cookie", "_session_id=test; Path=/")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        with tempfile.TemporaryDirectory() as tmp:
            password = Path(tmp) / "password"
            password.write_text("test-password\n")
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                client = DelugeClient(f"http://127.0.0.1:{server.server_port}", str(password))
                self.assertEqual(client.torrent("a" * 40), {"state": "Paused"})
                self.assertEqual([c["method"] for c in calls], ["auth.login", "web.connected", "core.get_torrent_status"])
                self.assertEqual(calls[-1]["params"][0], "a" * 40)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
