"""Plan placements using existing application paths, never title guessing."""

import os
from pathlib import Path, PurePosixPath
import shutil

from .config import absolute_path


class Refused(ValueError):
    """An operation needs human attention rather than an automatic retry."""


def select_disk(disks, library_path, library):
    try:
        path = PurePosixPath(absolute_path(library_path))
    except (ValueError, TypeError):
        raise Refused("application library path is invalid") from None
    for disk in disks:
        root = PurePosixPath(disk.path) / library
        if path != root and path.is_relative_to(root):
            return disk
    raise Refused("library path is not assigned to a configured physical disk")


def capacity(disk, library_path, size, reserved):
    root = Path(disk.path)
    downloads = root / disk.downloads
    library = Path(library_path)
    if not root.is_mount() or root.resolve() != root:
        raise Refused("disk is absent, is not a mountpoint, or uses a symlink")
    if not downloads.is_dir() or downloads.resolve() != downloads or library.resolve() != library:
        raise Refused("download directory is absent or a media path contains a symlink")
    ancestor = library
    while not ancestor.exists() and ancestor != root:
        ancestor = ancestor.parent
    device = root.stat().st_dev
    if downloads.stat().st_dev != device or ancestor.stat().st_dev != device:
        raise Refused("downloads and library are not on the same filesystem")
    free = shutil.disk_usage(root).free
    if free - reserved - size < disk.reserve_bytes:
        raise Refused("assigned disk has insufficient unreserved space")
    return free


def validate_torrent(torrent, label, managed_since):
    if not torrent:
        raise Refused("torrent does not exist")
    if torrent.get("label") != label:
        raise Refused("torrent does not have the dedicated placement label")
    if torrent.get("state") != "Paused" or torrent.get("paused") is not True:
        raise Refused("torrent must be paused before placement")
    if torrent.get("total_done") != 0 or torrent.get("is_finished") is not False:
        raise Refused("prototype only places new torrents with no downloaded payload")
    if float(torrent.get("time_added", 0)) < managed_since:
        raise Refused("torrent predates coordinator initialization")
