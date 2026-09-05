"""Explicit disk assignments, service endpoints, and file-backed secrets."""

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import tomllib
from urllib.parse import urlsplit


class ConfigError(ValueError):
    pass


def absolute_path(value):
    if not isinstance(value, str) or not value.startswith("/"):
        raise ConfigError("paths must be absolute POSIX paths")
    if ".." in value.split("/") or "\x00" in value:
        raise ConfigError("paths must not contain traversal or NUL characters")
    return str(PurePosixPath(value))


def endpoint(value):
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ConfigError("service URLs must use http or https")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ConfigError("service URLs must not contain credentials, queries, or fragments")
    return value.rstrip("/")


def secret(path):
    value = Path(path).read_text().strip()
    if not value or value.startswith("CHANGE_ME"):
        raise ConfigError("secret files must contain a non-placeholder value")
    return value


@dataclass(frozen=True)
class Disk:
    name: str
    path: str
    downloads: str = "Downloads"
    reserve_bytes: int = 50 * 1024**3


@dataclass(frozen=True)
class Application:
    name: str
    url: str
    api_key_file: str
    label: str
    download_client: str
    library: str


@dataclass(frozen=True)
class Config:
    mode: str
    database: str
    token_file: str
    deluge_url: str
    deluge_password_file: str
    disks: tuple[Disk, ...]
    apps: dict[str, Application]
    host: str = "0.0.0.0"
    port: int = 8080
    poll_seconds: int = 10
    reconcile_seconds: int = 60
    timeout_seconds: int = 10


def load(path):
    with open(path, "rb") as handle:
        raw = tomllib.load(handle)
    server = raw.get("server", {})
    mode = server.get("mode", "observe")
    if mode not in {"observe", "apply"}:
        raise ConfigError("mode must be observe or apply")
    disks = []
    for item in raw.get("disks", []):
        disk = Disk(item["name"], absolute_path(item["path"]),
                    item.get("downloads", "Downloads"), item.get("reserve_bytes", 50 * 1024**3))
        if disk.path == "/" or not disk.name:
            raise ConfigError("each disk needs a name and a dedicated mount path")
        if "/" in disk.downloads or disk.downloads in {"", ".", ".."}:
            raise ConfigError("downloads must be a single directory name")
        if not isinstance(disk.reserve_bytes, int) or disk.reserve_bytes < 0:
            raise ConfigError("reserve_bytes must be a nonnegative integer")
        for previous in disks:
            if disk.name == previous.name or PurePosixPath(disk.path).is_relative_to(previous.path) or PurePosixPath(previous.path).is_relative_to(disk.path):
                raise ConfigError("disk names must be unique and mount paths must not overlap")
        disks.append(disk)
    if not disks:
        raise ConfigError("at least one disk is required")
    apps = {}
    for name, item in raw.get("apps", {}).items():
        if name not in {"radarr", "sonarr"}:
            raise ConfigError("only radarr and sonarr applications are supported")
        library = item.get("library", "Movies" if name == "radarr" else "TV")
        if "/" in library or library in {"", ".", ".."} or any(library == d.downloads for d in disks):
            raise ConfigError("library must be a directory name distinct from Downloads")
        app = Application(name, endpoint(item["url"]), item["api_key_file"],
                          item["label"], item["download_client"], library)
        if not app.label.endswith("-placement") or not app.download_client:
            raise ConfigError("use a dedicated label ending in -placement and a download_client name")
        apps[name] = app
    if not apps or len({a.label for a in apps.values()}) != len(apps):
        raise ConfigError("configure at least one application with a unique placement label")
    deluge = raw["deluge"]
    settings = Config(mode, server.get("database", "/state/jobs.sqlite3"),
                      server["token_file"], endpoint(deluge["url"]), deluge["password_file"],
                      tuple(disks), apps, server.get("host", "0.0.0.0"), server.get("port", 8080),
                      server.get("poll_seconds", 10), server.get("reconcile_seconds", 60),
                      server.get("timeout_seconds", 10))
    if not 1 <= settings.port <= 65535 or min(settings.poll_seconds, settings.reconcile_seconds, settings.timeout_seconds) < 1:
        raise ConfigError("port and intervals must be positive and valid")
    return settings
