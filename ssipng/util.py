"""Shared helpers: errors, subprocess runner, atomic file writes, public IP detection."""

from __future__ import annotations

import ipaddress
import os
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path


class SsipngError(Exception):
    """Base error for ssipng."""


class ConfigError(SsipngError):
    """Invalid or incomplete configuration."""


class ProxmoxError(SsipngError):
    """Proxmox API / CLI failure."""


class ServiceError(SsipngError):
    """Domain assignment/removal failure."""


class NoGuestIPError(ServiceError):
    """The guest has no usable IP address yet."""


@dataclass
class RunResult:
    rc: int
    stdout: str
    stderr: str

    @property
    def combined(self) -> str:
        return (self.stderr or self.stdout or "").strip()


class Runner:
    """Thin subprocess wrapper; replaceable in tests."""

    def __init__(self, timeout: int = 30):
        self.timeout = timeout

    def run(self, cmd: list[str]) -> RunResult:
        try:
            p = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self.timeout, check=False
            )
            return RunResult(p.returncode, p.stdout or "", p.stderr or "")
        except FileNotFoundError as e:
            return RunResult(127, "", f"{cmd[0]}: not found ({e})")
        except subprocess.TimeoutExpired:
            return RunResult(124, "", f"{cmd[0]}: timed out after {self.timeout}s")

    def check(self, cmd: list[str], what: str) -> RunResult:
        r = self.run(cmd)
        if r.rc != 0:
            raise ServiceError(f"{what} failed (rc={r.rc}): {r.combined}")
        return r


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def ensure_dir(path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def atomic_write_text(path, content: str) -> bool:
    """Write UTF-8 text atomically. Returns True when the file content changed."""
    path = Path(path)
    ensure_dir(path.parent)
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == content:
                return False
        except OSError:
            pass
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    tmp.write_text(content, encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    return True


def detect_public_ip(timeout: float = 6.0) -> str:
    """Best-effort detection of this machine's public IP (for `ssipng init`)."""
    urls = ("https://api.ipify.org", "https://checkip.amazonaws.com")
    last_err: Exception | None = None
    for url in urls:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                ip = resp.read().decode().strip()
            ipaddress.ip_address(ip)  # validate
            return ip
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise ServiceError(f"could not detect public IP: {last_err}")
