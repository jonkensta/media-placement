from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import time
import unittest

from media_placement.clients import RemoteError
from media_placement.config import Application, Config, Disk
from media_placement.engine import Engine
from media_placement.placement import Refused
from media_placement.state import Store


TORRENT = "a" * 40


def event(app="radarr", torrent=TORRENT, entity_id=7):
    return {"eventType": "Grab", "downloadClient": "Deluge Placement", "downloadClientType": "Deluge",
            "downloadId": torrent, "movie" if app == "radarr" else "series": {"id": entity_id},
            "release": {"size": 1000}}


class FakeArr:
    def __init__(self, name="radarr"):
        self.name = name
        self.path = "/storage/disk00/" + ("Movies/Example" if name == "radarr" else "TV/Example")
        self.records = [{"eventType": "grabbed", "downloadId": TORRENT.upper(),
                         "movieId" if name == "radarr" else "seriesId": 7,
                         "date": datetime.fromtimestamp(time.time() + 1, timezone.utc).isoformat(),
                         "data": {"downloadClient": "Deluge Placement", "size": "1000"}}]
        self.fail = False

    def entity(self, entity_id):
        return {"id": entity_id, "path": self.path}

    def history(self, page=1, download_id=None):
        if self.fail:
            raise RemoteError("test API failure")
        return {"records": self.records, "totalRecords": len(self.records)}


class FakeDeluge:
    def __init__(self):
        self.status = {"name": "Example.mkv", "state": "Paused", "paused": True,
                       "save_path": "/staging", "total_size": 1000, "total_done": 0,
                       "is_finished": False, "label": "radarr-placement", "time_added": time.time() + 1,
                       "move_completed": True, "move_completed_path": "/wrong-disk"}
        self.calls = []
        self.delay_move = False
        self.resume_error = False

    def torrent(self, torrent):
        return dict(self.status)

    def configure(self, torrent, target):
        self.calls.append(("configure", torrent, target))
        self.status.update(move_completed=False, move_completed_path=target)

    def move(self, torrent, target):
        self.calls.append(("move", torrent, target))
        if not self.delay_move:
            self.status["save_path"] = target

    def resume(self, torrent):
        self.calls.append(("resume", torrent))
        if self.resume_error:
            raise RemoteError("resume timed out")
        self.status.update(paused=False, state="Downloading")


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "jobs.sqlite3")
        apps = {name: Application(name, "http://unused", "unused", name + "-placement", "Deluge Placement",
                                  "Movies" if name == "radarr" else "TV") for name in ("radarr", "sonarr")}
        self.config = Config("observe", self.store.path, "unused", "http://unused", "unused",
                             (Disk("disk00", "/storage/disk00", reserve_bytes=0),), apps)
        self.arr = {name: FakeArr(name) for name in apps}
        self.deluge = FakeDeluge()
        self.capacity_calls = []

        def capacity(*args):
            self.capacity_calls.append(args)
            return 100000

        self.engine = Engine(self.config, self.store, self.arr, self.deluge, capacity)

    def enqueue(self, app="radarr"):
        return self.engine.enqueue(app, event(app))

    def apply(self):
        self.engine.config = replace(self.config, mode="apply")

    def test_observation_reads_actual_library_and_performs_no_mutations(self):
        payload = event()
        payload["movie"]["folderPath"] = "/attacker-controlled"
        job = self.engine.enqueue("radarr", payload)
        self.engine.process(job)
        result = self.store.get(job["id"])
        self.assertEqual(result["status"], "observed")
        self.assertEqual(json.loads(result["plan"])["library"], self.arr["radarr"].path)
        self.assertEqual(self.deluge.calls, [])

    def test_apply_configures_moves_verifies_and_resumes(self):
        self.apply()
        job = self.enqueue()
        self.engine.process(job)
        self.assertEqual([c[0] for c in self.deluge.calls], ["configure", "move", "resume"])
        self.assertEqual(self.store.get(job["id"])["status"], "done")
        self.assertFalse(self.deluge.status["move_completed"])

    def test_sonarr_uses_existing_series_assignment(self):
        self.deluge.status["label"] = "sonarr-placement"
        job = self.enqueue("sonarr")
        self.engine.process(job)
        plan = json.loads(self.store.get(job["id"])["plan"])
        self.assertEqual(plan["library"], "/storage/disk00/TV/Example")

    def test_duplicate_webhook_survives_restart(self):
        original = self.enqueue()
        store = Store(self.store.path)
        duplicate = store.enqueue("radarr", TORRENT, 7, 1000)
        self.assertEqual(original["id"], duplicate["id"])
        self.assertEqual(len(store.jobs()), 1)

    def test_hash_cannot_be_assigned_to_two_apps(self):
        self.enqueue()
        with self.assertRaises(ValueError):
            self.enqueue("sonarr")

    def test_test_event_creates_no_job(self):
        self.assertIsNone(self.engine.enqueue("radarr", {"eventType": "Test"}))
        self.assertEqual(self.store.jobs(), [])

    def test_invalid_events_are_rejected(self):
        for update in ({"eventType": "Download"}, {"downloadId": "../bad"},
                       {"downloadClient": "Other"}, {"downloadClientType": "SABnzbd"},
                       {"movie": {"id": True}}, {"release": {"size": -1}}):
            with self.subTest(update=update), self.assertRaises(Refused):
                self.engine.enqueue("radarr", event() | update)

    def test_started_finished_old_or_unlabelled_torrents_are_not_touched(self):
        self.apply()
        original = dict(self.deluge.status)
        for update in ({"state": "Downloading", "paused": False}, {"total_done": 1},
                       {"is_finished": True}, {"time_added": 0}, {"label": "radarr"}):
            with self.subTest(update=update):
                self.deluge.status = original | update
                job = self.enqueue()
                self.store.update(job["id"], status="pending", error=None)
                job = self.store.get(job["id"])
                self.engine.process(job)
                self.assertEqual(self.store.get(job["id"])["status"], "blocked")
                self.assertEqual(self.deluge.calls, [])

    def test_insufficient_capacity_does_not_mutate_deluge(self):
        self.apply()
        def no_space(*args):
            raise Refused("disk full")
        self.engine.check_capacity = no_space
        job = self.enqueue()
        self.engine.process(job)
        self.assertEqual(self.store.get(job["id"])["status"], "blocked")
        self.assertFalse(self.deluge.calls)

    def test_unconfirmed_move_is_not_resumed(self):
        self.apply()
        self.deluge.delay_move = True
        job = self.enqueue()
        self.engine.process(job)
        self.assertEqual(self.store.get(job["id"])["status"], "moving")
        self.assertNotIn("resume", [c[0] for c in self.deluge.calls])
        self.deluge.delay_move = False
        self.engine.process(self.store.get(job["id"]))
        self.assertEqual(self.store.get(job["id"])["status"], "done")

    def test_asynchronous_moving_state_waits_without_blocking_or_resuming(self):
        self.apply()
        original_move = self.deluge.move

        def moving(torrent, target):
            original_move(torrent, target)
            self.deluge.status["state"] = "Moving"

        self.deluge.move = moving
        job = self.enqueue()
        self.engine.process(job)
        self.assertEqual(self.store.get(job["id"])["status"], "moving")
        self.engine.process(self.store.get(job["id"]))
        self.assertEqual([c[0] for c in self.deluge.calls].count("move"), 1)
        self.deluge.status["state"] = "Paused"
        self.engine.process(self.store.get(job["id"]))
        self.assertEqual(self.store.get(job["id"])["status"], "done")

    def test_reservations_are_included_in_the_next_plan(self):
        self.apply()
        self.engine.process(self.enqueue())
        second_hash = "b" * 40
        self.arr["radarr"].records = [self.arr["radarr"].records[0] | {"downloadId": second_hash.upper()}]
        self.deluge.status.update(paused=True, state="Paused", save_path="/staging")
        second = self.engine.enqueue("radarr", event(torrent=second_hash))
        self.engine.process(second)
        self.assertEqual(self.capacity_calls[-1][-1], 1000)

    def test_eight_api_failures_block_with_no_external_writes(self):
        self.apply()
        self.arr["radarr"].fail = True
        job = self.enqueue()
        for _ in range(8):
            self.engine.process(self.store.get(job["id"]))
        self.assertEqual(self.store.get(job["id"])["status"], "blocked")
        self.assertFalse(self.deluge.calls)

    def test_resume_timeout_is_not_repeated(self):
        self.apply()
        self.deluge.resume_error = True
        job = self.enqueue()
        self.engine.process(job)
        self.assertEqual(self.store.get(job["id"])["status"], "resuming")
        self.engine.process(self.store.get(job["id"]))
        self.assertEqual(self.store.get(job["id"])["status"], "blocked")
        self.assertEqual([c[0] for c in self.deluge.calls].count("resume"), 1)

    def test_resume_recovery_accepts_running_torrent_without_second_resume(self):
        self.apply()
        job = self.enqueue()
        self.engine.process(job)
        self.store.update(job["id"], status="resuming")
        self.engine.process(self.store.get(job["id"]))
        self.assertEqual(self.store.get(job["id"])["status"], "done")
        self.assertEqual([c[0] for c in self.deluge.calls].count("resume"), 1)

    def test_resume_intent_is_persisted_before_rpc(self):
        self.apply()
        job = self.enqueue()
        def resume(torrent):
            self.assertEqual(self.store.get(job["id"])["status"], "resuming")
        self.deluge.resume = resume
        self.engine.process(job)

    def test_library_change_during_move_blocks_retry(self):
        self.apply()
        self.deluge.delay_move = True
        job = self.enqueue()
        self.engine.process(job)
        self.arr["radarr"].path = "/storage/disk00/Movies/Changed"
        self.engine.process(self.store.get(job["id"]))
        self.assertEqual(self.store.get(job["id"])["status"], "blocked")

    def test_observed_jobs_are_not_replayed_when_apply_enabled(self):
        self.engine.process(self.enqueue())
        self.apply()
        self.engine.tick()
        self.assertFalse(self.deluge.calls)

    def test_api_failure_retries_without_mutation(self):
        self.apply()
        self.arr["radarr"].fail = True
        job = self.enqueue()
        self.engine.process(job)
        result = self.store.get(job["id"])
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["attempts"], 1)
        self.assertGreater(result["next_attempt"], time.time())
        self.assertFalse(self.deluge.calls)

    def test_reconciliation_recovers_missed_event_and_deduplicates(self):
        self.arr["sonarr"].records = []
        self.engine.reconcile()
        self.engine.reconcile()
        self.assertEqual(len(self.store.jobs()), 1)
        self.assertEqual(self.store.jobs()[0]["torrent"], TORRENT)

    def test_reconciliation_ignores_historical_grabs(self):
        for client in self.arr.values():
            client.records[0]["date"] = "2000-01-01T00:00:00Z"
        self.engine.reconcile()
        self.assertEqual(self.store.jobs(), [])

    def test_reconciliation_retains_cursor_on_failure(self):
        self.arr["radarr"].fail = True
        self.engine.reconcile()
        self.assertIsNone(self.store.metadata("history_cursor_radarr"))
        self.assertIn("radarr", self.engine.reconcile_errors)

    def test_reservations_survive_restart_and_release_on_completion(self):
        self.apply()
        job = self.enqueue()
        self.engine.process(job)
        restarted = Store(self.store.path)
        self.assertEqual(restarted.reservations("disk00", -1), 1000)
        self.deluge.status["is_finished"] = True
        self.engine.release_completed()
        self.assertEqual(restarted.reservations("disk00", -1), 0)

    def test_conflicting_history_blocks_placement(self):
        self.arr["radarr"].records.append(self.arr["radarr"].records[0] | {"movieId": 8})
        self.apply()
        job = self.enqueue()
        self.engine.process(job)
        self.assertEqual(self.store.get(job["id"])["status"], "blocked")
        self.assertFalse(self.deluge.calls)
