"""Orchestration: assign/remove sslip.io domains for guests and keep them in sync."""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

from . import tls as tls_mod
from .naming import build_domain
from .state import State
from .util import (
    NoGuestIPError,
    SsipngError,
    ServiceError,
    atomic_write_text,
    now_iso,
)


@dataclasses.dataclass
class Record:
    vmid: int
    node: str
    name: str
    proxmox_type: str
    ip: str
    port: int
    domain: str
    backend: str
    tls: str            # desired TLS mode (config value)
    tls_mode: str       # effective mode used for rendering (certbot without a cert yet -> "none")
    vhost_path: str
    created_at: str
    updated_at: str

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Record":
        fields = {f.name: d.get(f.name) for f in dataclasses.fields(cls)}
        fields["vmid"] = int(fields["vmid"] or 0)
        fields["port"] = int(fields["port"] or 80)
        for k in ("node", "name", "proxmox_type", "ip", "domain", "backend",
                  "tls", "tls_mode", "vhost_path", "created_at", "updated_at"):
            fields[k] = fields[k] or ""
        return cls(**fields)


_TAG_SPLIT = re.compile(r"[,;\s]+")


def _has_tag(res: dict, include_tags: list) -> bool:
    tags = set(t for t in _TAG_SPLIT.split(str(res.get("tags") or "")) if t)
    return any(t in tags for t in include_tags)


class Assigner:
    def __init__(self, cfg, proxmox, backend, state: State, runner, log=None):
        self.cfg = cfg
        self.proxmox = proxmox
        self.backend = backend
        self.state = state
        self.runner = runner
        self.log = log or (lambda msg: None)

    # ------------------------------------------------------------------ #
    def _target(self, vmid: int):
        return self.cfg.targets.get(str(vmid)) or self.cfg.targets.get(vmid)

    def _find_resource(self, vmid: int) -> dict:
        for res in self.proxmox.list_resources():
            if res.get("vmid") == vmid:
                return res
        raise ServiceError(f"vmid {vmid} not found in cluster resources")

    def _resolve_ip(self, res: dict, prefer=None):
        try:
            return self.proxmox.guest_ip(res, prefer=prefer)
        except SsipngError as e:
            vmid = res.get("vmid")
            raise NoGuestIPError(
                f"vm {vmid}: no IP yet ({e}); start the guest, install qemu-guest-agent (VMs), "
                f"set a static ip= on the CT netX device, or pin [targets.{vmid}].ip"
            ) from e

    # ------------------------------------------------------------------ #
    def assign(self, vmid: int, dry_run: bool = False, ip: str | None = None,
               port: int | None = None, domain: str | None = None,
               tls: str | None = None):
        """Assign an sslip.io domain to a guest and wire it into the web server.
        Returns (record, actions). Idempotent: unchanged files are not rewritten."""
        actions: list[str] = []
        res = self._find_resource(vmid)
        target = self._target(vmid)

        name = res.get("name") or f"vm{vmid}"
        ip = ip or (target.ip if target else None) or self._resolve_ip(res)
        domain = domain or (target.domain if target else None) or \
            build_domain(self.cfg.domain, vmid, name, ip)
        port = port or (target.port if target else None) or self.cfg.web.default_target_port
        tls = tls or (target.tls if target else None) or self.cfg.tls.mode
        tls_mod.check_mode(tls, self.cfg.web.backend)

        # effective render mode: a certbot cert that does not exist yet degrades to HTTP-only
        tls_mode = tls
        if tls == "certbot":
            paths = tls_mod.cert_paths(self.cfg.tls, domain, tls)
            if paths and not Path(paths[0]).exists():
                tls_mode = "none"
                actions.append("certbot certificate not issued yet — wrote HTTP-only vhost "
                               f"(run: ssipng certs {vmid})")

        rec = Record(
            vmid=int(vmid), node=res.get("node") or "", name=name,
            proxmox_type=res.get("type") or "", ip=str(ip), port=int(port), domain=domain,
            backend=self.backend.name, tls=tls, tls_mode=tls_mode, vhost_path="",
            created_at=now_iso(), updated_at=now_iso(),
        )

        if tls_mode == "selfsigned" and not dry_run:
            crt, key = tls_mod.generate_selfsigned(domain, self.cfg.tls.selfsigned_dir, self.runner)
            actions.append(f"self-signed certificate ready: {crt}")

        content = self.backend.render(rec)
        path = self.backend.vhost_path(domain, vmid)
        rec.vhost_path = str(path)
        existed = path.exists()

        if dry_run:
            actions.append(f"[dry-run] would write vhost {path}")
            actions.append("[dry-run] would validate and reload the web server")
            return rec, actions

        changed = False
        if atomic_write_text(path, content):
            changed = True
            actions.append(f"wrote vhost {path}")
        else:
            actions.append("vhost unchanged")

        for extra_path, extra_content in self.backend.extra_writes(rec):
            if atomic_write_text(extra_path, extra_content):
                changed = True
                actions.append(f"updated {extra_path}")

        if changed or not existed:
            self._apply(rec, changed, actions, old_content=(path.read_text(encoding="utf-8")
                                                            if existed else None))
        self.state.set(vmid, rec.to_dict())
        return rec, actions

    # ------------------------------------------------------------------ #
    def _apply(self, rec, changed: bool, actions: list, old_content: str | None):
        """prepare -> validate -> reload, with rollback of our vhost on failure."""
        path = Path(rec.vhost_path)
        try:
            for cmd in self.backend.prepare_cmds(rec, newly_created=old_content is None):
                self.runner.check(cmd, "prepare (%s)" % " ".join(cmd))
                actions.append("ran: %s" % " ".join(cmd))
            for cmd in self.backend.validate_cmds():
                self.runner.check(cmd, "validate (%s)" % " ".join(cmd))
                actions.append("ran: %s" % " ".join(cmd))
            if changed and self.cfg.web.reload:
                for cmd in self.backend.reload_cmds():
                    self.runner.check(cmd, "reload (%s)" % " ".join(cmd))
                    actions.append("reloaded web server")
        except ServiceError:
            # restore the previous vhost so the web server keeps its working config
            self._rollback(rec, old_content)
            raise

    def _rollback(self, rec, old_content: str | None):
        path = Path(rec.vhost_path)
        try:
            if old_content is None:
                if path.exists():
                    path.unlink()
                    for cmd in self.backend.disable_cmds(rec):
                        self.runner.run(cmd)
            else:
                atomic_write_text(path, old_content)
            if self.cfg.web.reload:
                for cmd in self.backend.validate_cmds():
                    self.runner.run(cmd)
                for cmd in self.backend.reload_cmds():
                    self.runner.run(cmd)
        except OSError:
            pass

    # ------------------------------------------------------------------ #
    def remove(self, key, dry_run: bool = False):
        """Remove domain + vhost + state entry. `key` is a vmid or a domain name."""
        rec_dict = self.state.get(key) if str(key).isdigit() else None
        if rec_dict is None:
            vmid = self.state.find_by_domain(str(key))
            rec_dict = self.state.get(vmid) if vmid is not None else None
        if rec_dict is None:
            raise ServiceError(f"no managed guest matches {key!r}")
        rec = Record.from_dict(rec_dict)
        actions: list[str] = []
        path = Path(rec.vhost_path) if rec.vhost_path else self.backend.vhost_path(
            rec.domain, rec.vmid)
        existed = path.exists()

        if dry_run:
            actions.append(f"[dry-run] would remove {path} and reload the web server")
            return actions

        for cmd in self.backend.disable_cmds(rec):
            self.runner.run(cmd)  # best-effort (a2dissite)
        if existed:
            path.unlink()
            actions.append(f"removed {path}")
            for cmd in self.backend.validate_cmds():
                self.runner.check(cmd, "validate (%s)" % " ".join(cmd))
            if self.cfg.web.reload:
                for cmd in self.backend.reload_cmds():
                    self.runner.check(cmd, "reload (%s)" % " ".join(cmd))
                actions.append("reloaded web server")

        if rec.tls == "selfsigned":
            paths = tls_mod.cert_paths(self.cfg.tls, rec.domain, rec.tls)
            if paths:
                for p in paths:
                    try:
                        Path(p).unlink(missing_ok=True)
                    except OSError:
                        pass
                actions.append("removed self-signed certificates")
        self.state.remove(rec.vmid)
        return actions

    # ------------------------------------------------------------------ #
    def sync(self, dry_run: bool = False, refresh_ip: bool = True, cleanup=None):
        """Discover guests and converge: create missing domains, refresh changed IPs,
        optionally clean up removed guests. Returns a report of per-VMID actions."""
        if cleanup is None:
            cleanup = self.cfg.daemon.cleanup
        report = []
        resources = self.proxmox.list_resources()
        managed = set(self.state.all())

        for res in resources:
            vmid = int(res.get("vmid"))
            if vmid in self.cfg.daemon.exclude_vmids:
                report.append({"vmid": vmid, "action": "excluded"})
                continue
            include = self.cfg.daemon.include_tags
            if include and not _has_tag(res, include):
                report.append({"vmid": vmid, "action": "skipped",
                               "message": "no matching tag"})
                continue
            entry = self.state.get(vmid)
            if entry:
                if refresh_ip and not (self._target(vmid) and self._target(vmid).ip):
                    self._refresh(vmid, entry, dry_run, report)
                else:
                    report.append({"vmid": vmid, "action": "managed",
                                   "domain": entry.get("domain")})
                continue
            try:
                rec, actions = self.assign(vmid, dry_run=dry_run)
                report.append({"vmid": vmid, "action": "dry-run" if dry_run else "created",
                               "domain": rec.domain, "actions": actions})
            except NoGuestIPError as e:
                report.append({"vmid": vmid, "action": "pending", "message": str(e)})
            except SsipngError as e:
                report.append({"vmid": vmid, "action": "error", "message": str(e)})

        if cleanup and not dry_run:
            present = {int(r.get("vmid")) for r in resources}
            for vmid_str in list(self.state.all()):
                vmid = int(vmid_str)
                if vmid not in present and vmid not in self.cfg.daemon.exclude_vmids:
                    actions = self.remove(vmid)
                    report.append({"vmid": vmid, "action": "removed", "actions": actions})
        return report

    def _refresh(self, vmid: int, entry: dict, dry_run: bool, report: list):
        """Re-resolve the guest IP and update the vhost when it changed."""
        rec = Record.from_dict(entry)
        try:
            res = self._find_resource(vmid)
            new_ip = self._resolve_ip(res)
        except SsipngError as e:
            report.append({"vmid": vmid, "action": "managed", "domain": rec.domain,
                           "message": f"refresh skipped: {e}"})
            return
        if new_ip == rec.ip:
            report.append({"vmid": vmid, "action": "managed", "domain": rec.domain})
            return
        rec.ip = new_ip
        rec.updated_at = now_iso()
        if dry_run:
            report.append({"vmid": vmid, "action": "dry-run",
                           "message": f"ip changed -> {new_ip}"})
            return
        content = self.backend.render(rec)
        path = Path(rec.vhost_path or self.backend.vhost_path(rec.domain, rec.vmid))
        old = path.read_text(encoding="utf-8") if path.exists() else None
        if atomic_write_text(path, content):
            try:
                for cmd in self.backend.validate_cmds():
                    self.runner.check(cmd, "validate (%s)" % " ".join(cmd))
                if self.cfg.web.reload:
                    for cmd in self.backend.reload_cmds():
                        self.runner.check(cmd, "reload (%s)" % " ".join(cmd))
            except ServiceError:
                self._rollback(rec, old)
                raise
            self.state.set(vmid, rec.to_dict())
            report.append({"vmid": vmid, "action": "updated",
                           "domain": rec.domain, "message": f"ip -> {new_ip}"})
        else:
            report.append({"vmid": vmid, "action": "managed", "domain": rec.domain})

    # ------------------------------------------------------------------ #
    def refresh_certs(self, vmid: int):
        """Run certbot for the guest's domain, then re-assign to install the TLS vhost."""
        entry = self.state.get(vmid)
        if not entry:
            raise ServiceError(f"vmid {vmid} is not managed by ssipng")
        rec = Record.from_dict(entry)
        tls_mod.certbot_issue(rec.domain, self.cfg.web.acme_webroot,
                              email=self.cfg.tls.certbot_email,
                              staging=self.cfg.tls.certbot_staging, runner=self.runner)
        return self.assign(vmid)
