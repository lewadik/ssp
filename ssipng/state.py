"""JSON state store for managed guests, with a simple lock file for concurrent CLI/daemon use."""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

from .util import ServiceError, atomic_write_text, ensure_dir, now_iso

LOCK_TIMEOUT = 10.0
STALE_SECS = 300


class State:
    def __init__(self, path):
        self.path = Path(path)
        self._lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.data: dict = {}
        self._load()

    # -- persistence --------------------------------------------------------
    def _load(self):
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError:
            return
        try:
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError("not a JSON object")
            self.data = data
        except (json.JSONDecodeError, ValueError):
            # keep a copy of the corrupt file and start clean
            try:
                self.path.rename(self.path.with_name(
                    self.path.name + f".corrupt-{now_iso().replace(':', '')}"))
            except OSError:
                pass
            self.data = {}

    def save(self):
        ensure_dir(self.path.parent)
        atomic_write_text(self.path, json.dumps(self.data, indent=2, sort_keys=True) + "\n")

    # -- locking ------------------------------------------------------------
    @contextmanager
    def lock(self, timeout: float = LOCK_TIMEOUT, stale_secs: float = STALE_SECS):
        ensure_dir(self.path.parent)
        deadline = time.monotonic() + timeout
        fd = None
        while True:
            try:
                fd = os.open(self._lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                try:
                    age = time.time() - self._lock_path.stat().st_mtime
                    if age > stale_secs:
                        self._lock_path.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() > deadline:
                    raise ServiceError(f"state is locked (waited {timeout}s): {self._lock_path}")
                time.sleep(0.1)
        try:
            yield
        finally:
            if fd is not None:
                os.close(fd)
            try:
                self._lock_path.unlink(missing_ok=True)
            except OSError:
                pass

    # -- entries ------------------------------------------------------------
    def get(self, vmid):
        return self.data.get(str(vmid))

    def set(self, vmid, record: dict):
        self.data[str(vmid)] = record
        self.save()

    def remove(self, vmid):
        self.data.pop(str(vmid), None)
        self.save()

    def all(self) -> dict:
        return dict(self.data)

    def find_by_domain(self, domain: str):
        for key, rec in self.data.items():
            if isinstance(rec, dict) and rec.get("domain") == domain:
                return key
        return None
