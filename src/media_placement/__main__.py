import argparse
import fcntl
import json
import logging
from pathlib import Path
import signal
import sys
import threading

from .config import load, secret
from .engine import Engine
from .server import serve
from .state import Store


def main():
    parser = argparse.ArgumentParser(description="Coordinate downloads with existing library disk assignments")
    parser.add_argument("--config", default="/config/config.toml")
    parser.add_argument("command", choices=["serve", "check", "jobs"], nargs="?", default="serve")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = load(args.config)
        if args.command == "check":
            print(f"Configuration valid: mode={config.mode}, disks={len(config.disks)}, apps={','.join(config.apps)}")
            return
        if args.command == "jobs":
            if not Path(config.database).is_file():
                parser.error("state database does not exist")
            print(json.dumps(Store(config.database).jobs(), indent=2))
            return
        token = secret(config.token_file)
        if len(token) < 32:
            parser.error("webhook token must be at least 32 characters")
        secret(config.deluge_password_file)
        for app in config.apps.values():
            secret(app.api_key_file)
        Path(config.database).parent.mkdir(parents=True, exist_ok=True)
        with open(config.database + ".lock", "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                parser.error("another coordinator is using this state database")
            store = Store(config.database)
            stop = threading.Event()
            for signum in (signal.SIGTERM, signal.SIGINT):
                signal.signal(signum, lambda *_: stop.set())
            serve(Engine(config, store), token, stop)
    except (OSError, ValueError, KeyError, TypeError):
        print("Startup failed: check configuration, secret files, and state-directory permissions.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
