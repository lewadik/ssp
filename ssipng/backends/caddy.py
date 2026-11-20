"""Caddy backend — one .caddy file per guest in an import dir; "auto" TLS uses Caddy's ACME,
which works out of the box for sslip.io names via the HTTP-01 challenge."""

from __future__ import annotations

from pathlib import Path

from ..tls import cert_paths
from ..util import ConfigError
from .base import Backend

KEEP_FILE = "00-keep.caddy"


def render_caddy(rec, tls_cfg) -> str:
    mode = rec.tls_mode
    if mode == "auto":
        addr, extra = rec.domain, []
    elif mode == "none":
        addr, extra = "http://" + rec.domain, []
    elif mode in ("selfsigned", "certbot"):
        addr = rec.domain
        crt, key = cert_paths(tls_cfg, rec.domain, mode)
        extra = [f"tls {crt} {key}"]
    else:
        raise ConfigError("tls mode %r is not supported by the caddy backend" % mode)

    body = [addr + " {", f"    reverse_proxy {rec.ip}:{rec.port}"]
    body += ["    " + e for e in extra]
    body += ["}", ""]
    header = "# managed by ssipng — guest %s (%s, vmid %s); edits will be overwritten" % (
        rec.domain, rec.name, rec.vmid)
    return header + "\n" + "\n".join(body)


class Caddy(Backend):
    name = "caddy"

    def vhost_path(self, domain: str, vmid) -> Path:
        directory = self.web.config_dir or self.web.caddy_import_dir
        return Path(directory) / f"ssipng-{vmid}.caddy"

    def render(self, rec) -> str:
        return render_caddy(rec, self.cfg.tls)

    def extra_writes(self, rec) -> list:  # noqa: ARG002
        writes = []
        import_dir = Path(self.web.config_dir or self.web.caddy_import_dir)
        # keep-file so the Caddyfile import glob always matches at least one file
        writes.append((import_dir / KEEP_FILE,
                       "# keeps the ssipng import glob non-empty (Caddy errors on empty globs)\n"))
        caddyfile = Path(self.web.caddyfile)
        marker = f"import {import_dir}/*.caddy"
        try:
            text = caddyfile.read_text(encoding="utf-8")
        except OSError:
            text = ""
        if marker in text:
            return writes
        footer = "\n# added by ssipng — managed vhosts live in %s\n%s\n" % (import_dir, marker)
        new_text = (text.rstrip("\n") + footer) if text.strip() else footer.lstrip("\n")
        writes.append((caddyfile, new_text))
        return writes

    def validate_cmds(self):
        return [["caddy", "validate", "--config", str(self.web.caddyfile)]]

    def reload_cmds(self):
        return [["systemctl", "reload", "caddy"]]
