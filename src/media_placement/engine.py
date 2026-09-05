"""One durable worker; webhook handlers only validate and enqueue."""

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import time

from .clients import ArrClient, DelugeClient, RemoteError
from .placement import Refused, capacity, select_disk, validate_torrent

log = logging.getLogger(__name__)
HASH = re.compile(r"[a-fA-F0-9]{40}\Z")


def timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return result.timestamp()
    except (ValueError, TypeError, AttributeError):
        raise Refused("history event has an invalid date") from None


def event_fields(app, payload):
    if not isinstance(payload, dict):
        raise Refused("event must be an object")
    if payload.get("eventType") == "Test":
        return None
    if payload.get("eventType") != "Grab":
        raise Refused("only Grab and Test events are supported")
    if payload.get("downloadClient") != app.download_client or payload.get("downloadClientType") != "Deluge":
        raise Refused("event is not from the configured Deluge client")
    entity = payload.get("movie" if app.name == "radarr" else "series", {})
    entity_id = entity.get("id") if isinstance(entity, dict) else None
    torrent = payload.get("downloadId")
    if type(entity_id) is not int or entity_id < 1 or not isinstance(torrent, str) or not HASH.fullmatch(torrent):
        raise Refused("event needs a positive entity ID and a 40-character torrent hash")
    release = payload.get("release", {})
    size = release.get("size", 0) if isinstance(release, dict) else 0
    if type(size) is not int or size < 0 or size > 2**60:
        raise Refused("release size must be a nonnegative integer")
    return torrent.lower(), entity_id, size


class Engine:
    def __init__(self, config, store, arr=None, deluge=None, check_capacity=capacity):
        self.config, self.store = config, store
        self.arr = arr or {name: ArrClient(app, config.timeout_seconds) for name, app in config.apps.items()}
        self.deluge = deluge or DelugeClient(config.deluge_url, config.deluge_password_file, config.timeout_seconds)
        self.check_capacity = check_capacity
        self.last_tick = 0.0
        self.reconcile_errors = {}

    def enqueue(self, name, payload):
        fields = event_fields(self.config.apps[name], payload)
        return None if fields is None else self.store.enqueue(name, *fields)

    def verify_history(self, job, app):
        history = self.arr[app.name].history(download_id=job["torrent"])
        records = history["records"]
        if history.get("totalRecords", len(records)) > len(records):
            raise Refused("torrent history exceeds one page; ownership needs manual review")
        field = "movieId" if app.name == "radarr" else "seriesId"
        matches = [r for r in records if str(r.get("downloadId", "")).lower() == job["torrent"]
                   and r.get("eventType") in {"grabbed", 1}
                   and r.get("data", {}).get("downloadClient") == app.download_client
                   and timestamp(r.get("date")) >= self.store.since()]
        if not matches:
            raise RemoteError("matching recent grab is not yet visible in application history")
        if {r.get(field) for r in matches} != {job["entity_id"]}:
            raise Refused("torrent is associated with multiple or unexpected titles")

    def process(self, job):
        try:
            self._process(job)
        except Refused as error:
            self.store.update(job["id"], status="blocked", error=str(error))
            log.warning("job %s blocked: %s", job["id"], error)
        except RemoteError as error:
            attempts = job["attempts"] + 1
            fields = dict(error=str(error), attempts=attempts,
                          next_attempt=time.time() + min(300, 2**attempts * self.config.poll_seconds))
            if attempts >= 8:
                fields["status"] = "blocked"
            self.store.update(job["id"], **fields)
            log.warning("job %s remote failure, attempt %s", job["id"], attempts)
        except Exception:
            # Exception text from upstream libraries can contain credentials.
            self.store.update(job["id"], status="blocked", error="unexpected processing error; review configuration and upstream responses")
            log.error("job %s failed with an unexpected error", job["id"])

    def _process(self, job):
        app = self.config.apps.get(job["app"])
        if app is None:
            raise Refused("application was removed from configuration")
        self.verify_history(job, app)
        entity = self.arr[app.name].entity(job["entity_id"])
        library = entity.get("path")
        disk = select_disk(self.config.disks, library, app.library)
        target = str(Path(disk.path) / disk.downloads)
        torrent = self.deluge.torrent(job["torrent"])
        previous = json.loads(job["plan"]) if job["plan"] else None
        if previous and (previous["library"] != library or previous["target"] != target):
            raise Refused("library assignment changed after placement started")

        if job["status"] == "resuming":
            # Resume is deliberately at-most-once. After an uncertain response,
            # never resume a torrent that a human might have paused meanwhile.
            if (torrent.get("label") == app.label and torrent.get("save_path") == target
                    and torrent.get("paused") is False and torrent.get("move_completed") is False):
                self.store.update(job["id"], status="done", error=None)
                return
            raise Refused("resume outcome is uncertain; inspect Deluge manually (no automatic second resume)")

        if self.config.mode != "apply" and job["status"] != "pending":
            raise Refused("unfinished apply job retained; observation mode will not continue it")
        if job["status"] == "moving" and torrent.get("state") == "Moving":
            raise RemoteError("waiting for Deluge's asynchronous storage move")
        validate_torrent(torrent, app.label, self.store.since())
        size = max(job["size"], int(torrent.get("total_size", 0)))
        if size <= 0:
            raise RemoteError("torrent/release size is not available yet")
        name = torrent.get("name")
        if not isinstance(name, str) or name in {"", ".", ".."} or "/" in name or "\\" in name:
            raise Refused("torrent name must be a single safe path component")
        reserved = self.store.reservations(disk.name, job["id"])
        free = self.check_capacity(disk, library, size, reserved)
        if torrent.get("save_path") != target and os.path.lexists(Path(target) / name):
            raise Refused("destination already contains a file or directory with this torrent name")
        plan = dict(disk=disk.name, target=target, library=library, size=size,
                    free_bytes=free, reserved_bytes=reserved,
                    original_path=previous["original_path"] if previous else torrent.get("save_path"))
        if self.config.mode == "observe":
            self.store.update(job["id"], status="observed", plan=plan, error=None)
            log.info("job %s would place on disk %s", job["id"], disk.name)
            return

        self.store.update(job["id"], status="moving", plan=plan, error=None)
        self.deluge.configure(job["torrent"], target)
        if torrent.get("save_path") != target:
            self.deluge.move(job["torrent"], target)
        current = self.deluge.torrent(job["torrent"])
        if current.get("state") == "Moving":
            raise RemoteError("waiting for Deluge's asynchronous storage move")
        validate_torrent(current, app.label, self.store.since())
        if current.get("save_path") != target or current.get("move_completed") is not False:
            raise RemoteError("waiting for Deluge to confirm storage location and completion settings")
        # The intent is durable BEFORE sending a non-idempotent resume request.
        self.store.update(job["id"], status="resuming", error=None)
        self.deluge.resume(job["torrent"])
        self.store.update(job["id"], status="done", error=None)
        log.info("job %s placed and resumed on disk %s", job["id"], disk.name)

    def release_completed(self):
        for job in self.store.downloading():
            try:
                torrent = self.deluge.torrent(job["torrent"])
                if not torrent or torrent.get("is_finished") is True:
                    self.store.update(job["id"], status="complete", error=None)
            except RemoteError:
                # Retain reservation while status is unknown.
                log.warning("could not refresh reservation for job %s", job["id"])

    def reconcile(self):
        for name, client in self.arr.items():
            try:
                self._reconcile_app(name, client)
                self.reconcile_errors.pop(name, None)
            except Exception:
                self.reconcile_errors[name] = "history reconciliation failed; cursor retained"
                log.warning("history reconciliation failed for %s; cursor retained", name)

    def _reconcile_app(self, name, client):
        cursor_key = "history_cursor_" + name
        since = max(self.store.since(), float(self.store.metadata(cursor_key, self.store.since())) - 120)
        now = time.time()
        app = self.config.apps[name]
        for page in range(1, 11):
            result = client.history(page=page)
            records = result["records"]
            reached_boundary = False
            for record in records:
                when = timestamp(record.get("date"))
                if when < since:
                    reached_boundary = True
                    continue
                data = record.get("data", {})
                if data.get("downloadClient") != app.download_client or record.get("eventType") not in {"grabbed", 1}:
                    continue
                kind = "movie" if name == "radarr" else "series"
                payload = {"eventType": "Grab", "downloadClient": app.download_client,
                           "downloadClientType": "Deluge", "downloadId": record.get("downloadId"),
                           kind: {"id": record.get(kind + "Id")},
                           "release": {"size": int(data.get("size") or 0)}}
                try:
                    self.enqueue(name, payload)
                except (Refused, ValueError):
                    log.warning("ignored invalid or conflicting history record for %s", name)
            if reached_boundary or not records or page * 100 >= result.get("totalRecords", len(records)):
                self.store.set_metadata(cursor_key, now)
                return
        raise RemoteError("history exceeded the 1000-record reconciliation limit")

    def tick(self, reconcile=False):
        if reconcile:
            self.release_completed()
            self.reconcile()
        for job in self.store.ready():
            self.process(job)
        self.last_tick = time.time()
