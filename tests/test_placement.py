from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from media_placement.config import ConfigError, Disk, absolute_path, load
from media_placement.placement import Refused, capacity, select_disk


class PlacementTests(unittest.TestCase):
    def test_example_defaults_to_observation(self):
        config = load(Path(__file__).resolve().parents[1] / "config.example.toml")
        self.assertEqual(config.mode, "observe")

    def test_disk_selection_is_component_aware(self):
        disk = Disk("one", "/storage/disk01")
        self.assertEqual(select_disk([disk], "/storage/disk01/Movies/Example", "Movies"), disk)
        for invalid in ("/storage/disk010/Movies/Example", "/storage/disk01/Movies", "/mnt/merged/Movies/Example",
                        "/storage/disk01/Movies/../TV/Example"):
            with self.subTest(path=invalid), self.assertRaises(Refused):
                select_disk([disk], invalid, "Movies")

    def test_paths_reject_traversal_and_relative_inputs(self):
        for path in ("relative/path", "/storage/../etc", "/storage/\x00bad"):
            with self.subTest(path=path), self.assertRaises(ConfigError):
                absolute_path(path)

    def test_capacity_rejects_missing_mount_and_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Downloads").mkdir()
            (root / "Movies").mkdir()
            disk = Disk("one", tmp, reserve_bytes=0)
            with self.assertRaisesRegex(Refused, "not a mountpoint"):
                capacity(disk, str(root / "Movies/Example"), 1, 0)
            (root / "Movies/Example").symlink_to(root / "Downloads", target_is_directory=True)
            with patch.object(Path, "is_mount", return_value=True), self.assertRaisesRegex(Refused, "symlink"):
                capacity(disk, str(root / "Movies/Example"), 1, 0)

    def test_capacity_accounts_for_pending_reservations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Downloads").mkdir()
            (root / "Movies").mkdir()
            disk = Disk("one", tmp, reserve_bytes=100)
            with patch.object(Path, "is_mount", return_value=True), \
                    patch("media_placement.placement.shutil.disk_usage", return_value=SimpleNamespace(free=1000)):
                self.assertEqual(capacity(disk, str(root / "Movies/Example"), 400, 400), 1000)
                with self.assertRaisesRegex(Refused, "insufficient"):
                    capacity(disk, str(root / "Movies/Example"), 600, 400)
