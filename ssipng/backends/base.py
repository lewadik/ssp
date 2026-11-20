"""Web server backends: render a vhost for a guest's sslip.io domain and reload safely."""

from __future__ import annotations

from pathlib import Path

from ..util import ConfigError


class Backend:
    """Base class. `render` receives a service.Record (duck-typed: domain, ip, port, tls, tls_mode,
    vmid, name). All commands are lists of argv executed via the injected Runner."""

    name = "base"

    def __init__(self, cfg, runner):
        self.cfg = cfg
        self.web = cfg.web
        self.runner = runner

    # -- required -----------------------------------------------------------
    def vhost_path(self, domain: str, vmid) -> Path:  # noqa: ARG002
        raise NotImplementedError

    def render(self, rec) -> str:
        raise NotImplementedError

    # -- optional hooks -----------------------------------------------------
    def prepare_cmds(self, rec, newly_created: bool) -> list:  # noqa: ARG002
        return []

    def extra_writes(self, rec) -> list:  # noqa: ARG002
        """[(path, content)] written after the vhost (e.g. Caddyfile import line)."""
        return []

    def validate_cmds(self) -> list:
        return []

    def reload_cmds(self) -> list:
        return []

    def disable_cmds(self, rec) -> list:  # noqa: ARG002
        return []

    def header(self, rec) -> str:
        return "# managed by ssipng — guest %s (%s, vmid %s); edits will be overwritten" % (
            rec.domain, rec.name, rec.vmid)


def from_config(cfg, runner):
    from .nginx import Nginx
    from .apache import Apache
    from .caddy import Caddy

    registry = {"nginx": Nginx, "apache": Apache, "caddy": Caddy}
    cls = registry.get(cfg.web.backend)
    if not cls:
        raise ConfigError(
            "unknown web.backend %r; supported: %s" % (cfg.web.backend, ", ".join(sorted(registry)))
        )
    return cls(cfg, runner)
