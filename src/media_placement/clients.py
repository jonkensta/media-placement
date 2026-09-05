"""Bounded HTTP adapters. Never put remote bodies or credentials in errors."""

from http.cookiejar import CookieJar
import json
import urllib.error
import urllib.parse
import urllib.request

from .config import secret


class RemoteError(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(opener, url, timeout, headers=None, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with opener.open(req, timeout=timeout) as response:
            payload = response.read(8 * 1024 * 1024 + 1)
        if len(payload) > 8 * 1024 * 1024:
            raise RemoteError("remote response exceeded size limit")
        return json.loads(payload)
    except urllib.error.HTTPError as error:
        raise RemoteError(f"remote HTTP status {error.code}") from None
    except (OSError, ValueError, urllib.error.URLError):
        raise RemoteError("remote request failed or returned invalid JSON") from None


class ArrClient:
    def __init__(self, app, timeout=10):
        self.app, self.timeout = app, timeout
        self.opener = urllib.request.build_opener(NoRedirect())

    def get(self, path, **query):
        suffix = "?" + urllib.parse.urlencode(query) if query else ""
        return request(self.opener, f"{self.app.url}/api/v3/{path}{suffix}", self.timeout,
                       {"X-Api-Key": secret(self.app.api_key_file)})

    def entity(self, entity_id):
        kind = "movie" if self.app.name == "radarr" else "series"
        value = self.get(f"{kind}/{entity_id}")
        if not isinstance(value, dict) or value.get("id") != entity_id:
            raise RemoteError("application returned an unexpected entity")
        return value

    def history(self, page=1, download_id=None):
        query = dict(page=page, pageSize=100, sortKey="date", sortDirection="descending", eventType=1)
        if download_id:
            query["downloadId"] = download_id.upper()
        value = self.get("history", **query)
        if not isinstance(value, dict) or not isinstance(value.get("records"), list):
            raise RemoteError("application returned invalid history")
        return value


TORRENT_KEYS = ["name", "state", "paused", "save_path", "total_size", "total_done",
                "is_finished", "move_completed", "move_completed_path", "label", "time_added"]


class DelugeClient:
    def __init__(self, url, password_file, timeout=10):
        self.url, self.password_file, self.timeout = url, password_file, timeout
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()), NoRedirect())
        self.serial = 0

    def _call(self, method, params):
        self.serial += 1
        value = request(self.opener, self.url + "/json", self.timeout,
                        {"Content-Type": "application/json"},
                        {"method": method, "params": params, "id": self.serial})
        if not isinstance(value, dict) or value.get("error") or "result" not in value:
            raise RemoteError(f"Deluge RPC failed: {method}")
        return value["result"]

    def call(self, method, *params):
        if self._call("auth.login", [secret(self.password_file)]) is not True:
            raise RemoteError("Deluge Web authentication failed")
        if self._call("web.connected", []) is not True:
            raise RemoteError("Deluge Web is not connected to its daemon")
        return self._call(method, list(params))

    def torrent(self, torrent_hash):
        value = self.call("core.get_torrent_status", torrent_hash, TORRENT_KEYS)
        if not isinstance(value, dict):
            raise RemoteError("Deluge returned invalid torrent status")
        return value

    def move(self, torrent_hash, target):
        self.call("core.move_storage", [torrent_hash], target)

    def configure(self, torrent_hash, target):
        self.call("core.set_torrent_options", [torrent_hash],
                  {"move_completed": False, "move_completed_path": target})

    def resume(self, torrent_hash):
        self.call("core.resume_torrent", torrent_hash)
