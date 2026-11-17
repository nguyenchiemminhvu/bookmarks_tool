"""Periodic JSON backups written next to the database, restorable with Import."""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
from pathlib import Path

from .store import Store

BACKUP_INTERVAL = 7 * 24 * 3600
CHECK_EVERY = 3600  # how often the running server re-evaluates whether a backup is due
KEEP = 8
PREFIX = "bookmarks-backup-"


def list_backups(directory: Path) -> list[Path]:
    """Backup files, oldest first (names embed a sortable timestamp)."""
    return sorted(directory.glob(f"{PREFIX}*.json"))


def is_due(directory: Path, now: float, interval: float = BACKUP_INTERVAL) -> bool:
    backups = list_backups(directory)
    return not backups or now - backups[-1].stat().st_mtime >= interval


def write_backup(store: Store, directory: Path, keep: int = KEEP) -> Path:
    """Write a new backup atomically, then drop all but the newest ``keep`` files."""
    path = directory / time.strftime(f"{PREFIX}%Y%m%d-%H%M%S.json")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(store.export_data(), indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    for old in list_backups(directory)[:-keep]:
        old.unlink(missing_ok=True)
    return path


class BackupScheduler:
    """Backs up on start (if the newest backup is older than ``interval``) and then while running."""

    def __init__(self, store: Store, directory: Path, interval: float = BACKUP_INTERVAL,
                 check_every: float = CHECK_EVERY) -> None:
        self.store, self.directory = store, Path(directory)
        self.interval, self.check_every = interval, check_every
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="backup", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def run_once(self) -> Path | None:
        if not is_due(self.directory, time.time(), self.interval):
            return None
        return write_backup(self.store, self.directory)

    def _run(self) -> None:
        while True:
            try:
                path = self.run_once()
                if path:
                    print(f"Backup written: {path}", flush=True)
            except Exception:
                traceback.print_exc()
            if self._stop.wait(self.check_every):
                return
