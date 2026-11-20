"""Daemon: polls the Proxmox cluster for new/changed guests and keeps domains in sync."""

from __future__ import annotations

import signal
import threading

from . import __version__


class Daemon:
    def __init__(self, cfg, assigner, log=None):
        self.cfg = cfg
        self.assigner = assigner
        self.log = log or (lambda msg: print(msg, flush=True))
        self._stop = threading.Event()
        self._absent_streak: dict[int, int] = {}

    def stop(self, *args):  # noqa: ARG002 (signal handler signature)
        self._stop.set()

    def install_signal_handlers(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, self.stop)
            except (ValueError, OSError):
                pass  # not the main thread / unsupported platform

    def run(self):
        interval = max(5, int(self.cfg.daemon.interval))
        self.log(f"ssipng daemon {__version__} started "
                 f"(interval {interval}s, backend {self.cfg.web.backend})")
        self.install_signal_handlers()
        first = True
        while not self._stop.is_set():
            try:
                self._tick(first or self.cfg.daemon.cleanup is False)
            except Exception as e:  # noqa: BLE001 — keep the daemon alive
                self.log(f"daemon: sync failed: {e}")
            first = False
            self._stop.wait(interval)
        self.log("ssipng daemon stopped")

    def _tick(self, do_cleanup: bool):
        report = self.assigner.sync(cleanup=False)
        for item in report:
            action = item.get("action")
            if action in ("created", "removed", "error"):
                self.log("daemon: vmid %s -> %s %s" % (
                    item["vmid"], action, item.get("domain") or item.get("message") or ""))
        if do_cleanup and self.cfg.daemon.cleanup:
            self._cleanup_absent()

    def _cleanup_absent(self):
        """Remove domains for guests gone from the cluster after two consecutive polls."""
        try:
            present = {int(r["vmid"]) for r in self.assigner.proxmox.list_resources()}
        except Exception as e:  # noqa: BLE001
            self.log(f"daemon: cleanup skipped ({e})")
            return
        managed = {int(k) for k in self.assigner.state.all() if str(k).isdigit()}
        for vmid in managed - present:
            streak = self._absent_streak.get(vmid, 0) + 1
            self._absent_streak[vmid] = streak
            if streak >= 2:
                self.log(f"daemon: vmid {vmid} gone from cluster — removing domain")
                try:
                    self.assigner.remove(vmid)
                except Exception as e:  # noqa: BLE001
                    self.log(f"daemon: removal of vmid {vmid} failed: {e}")
                self._absent_streak.pop(vmid, None)
        for vmid in list(self._absent_streak):
            if vmid in present:
                self._absent_streak.pop(vmid, None)
