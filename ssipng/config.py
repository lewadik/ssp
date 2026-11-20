"""Configuration (TOML), validation, and the example config text used by `ssipng init`."""

from __future__ import annotations

import dataclasses
import ipaddress
import tomllib
from pathlib import Path

from .util import ConfigError, ensure_dir

WEB_BACKENDS = ("nginx", "apache", "caddy")
TLS_MODES = ("none", "selfsigned", "certbot", "auto")
DOMAIN_MODES = ("proxy", "direct")
PROXMOX_MODES = ("api", "cli")


@dataclasses.dataclass
class ProxmoxConfig:
    mode: str = "api"
    url: str = "https://127.0.0.1:8006"
    user: str = "root@pam"
    token_id: str | None = None
    token_secret: str | None = None
    password: str | None = None
    verify_ssl: bool = False
    node: str | None = None
    timeout: int = 10


@dataclasses.dataclass
class DomainConfig:
    mode: str = "proxy"
    public_ip: str | None = None
    suffix: str = "sslip.io"
    template: str = "{name}-{vmid}.{public_ip_dashed}.{suffix}"
    name_max_len: int = 40
    include_label: bool = True


@dataclasses.dataclass
class WebConfig:
    backend: str = "nginx"
    port: int = 80
    default_target_port: int = 80
    config_dir: str | None = None
    apache_enabled_dir: str | None = None
    apache_mods_dir: str | None = None
    caddyfile: str = "/etc/caddy/Caddyfile"
    caddy_import_dir: str = "/etc/caddy/conf.d"
    manage: bool = True
    reload: bool = True
    websocket: bool = True
    acme_webroot: str = "/var/www/letsencrypt"


@dataclasses.dataclass
class TLSConfig:
    mode: str = "none"
    selfsigned_dir: str = "/etc/ssipng/tls"
    certbot_email: str | None = None
    certbot_staging: bool = False


@dataclasses.dataclass
class DaemonConfig:
    interval: int = 30
    cleanup: bool = False
    include_tags: list = dataclasses.field(default_factory=list)
    exclude_vmids: list = dataclasses.field(default_factory=list)
    state_file: str = "/var/lib/ssipng/state.json"


@dataclasses.dataclass
class Target:
    ip: str | None = None
    port: int | None = None
    domain: str | None = None
    tls: str | None = None


@dataclasses.dataclass
class Config:
    proxmox: ProxmoxConfig = dataclasses.field(default_factory=ProxmoxConfig)
    domain: DomainConfig = dataclasses.field(default_factory=DomainConfig)
    web: WebConfig = dataclasses.field(default_factory=WebConfig)
    tls: TLSConfig = dataclasses.field(default_factory=TLSConfig)
    daemon: DaemonConfig = dataclasses.field(default_factory=DaemonConfig)
    targets: dict = dataclasses.field(default_factory=dict)  # str(vmid) -> Target
    config_path: str | None = None


def _section(data: dict, name: str) -> dict:
    value = data.get(name) or {}
    if not isinstance(value, dict):
        raise ConfigError("[%s] must be a table" % name)
    return value


def _build(cls, data: dict, label: str):
    known = {f.name for f in dataclasses.fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ConfigError("unknown key(s) in [%s]: %s" % (label, ", ".join(sorted(unknown))))
    return cls(**data)


def _check_port(value, label: str):
    if value is not None and not (isinstance(value, int) and 1 <= value <= 65535):
        raise ConfigError("%s must be a TCP port (1-65535), got %r" % (label, value))


def validate(cfg: Config):
    errors: list[str] = []
    px, dm, web, tls, daemon = cfg.proxmox, cfg.domain, cfg.web, cfg.tls, cfg.daemon

    if px.mode not in PROXMOX_MODES:
        errors.append("[proxmox].mode must be one of %s" % ", ".join(PROXMOX_MODES))
    if not str(px.url).startswith(("http://", "https://")):
        errors.append("[proxmox].url must start with http:// or https://")
    if px.mode == "api" and not ((px.token_id and px.token_secret) or px.password):
        errors.append("[proxmox]: set token_id + token_secret (recommended) or password")

    if dm.mode not in DOMAIN_MODES:
        errors.append("[domain].mode must be one of %s" % ", ".join(DOMAIN_MODES))
    if dm.mode == "proxy":
        if not dm.public_ip:
            errors.append("[domain].public_ip is required in proxy mode (public IP of this host)")
        else:
            try:
                ipaddress.ip_address(dm.public_ip)
            except ValueError:
                errors.append("[domain].public_ip %r is not a valid IP address" % dm.public_ip)

    if web.backend not in WEB_BACKENDS:
        errors.append("[web].backend must be one of %s" % ", ".join(WEB_BACKENDS))
    _check_port(web.port, "[web].port")
    _check_port(web.default_target_port, "[web].default_target_port")

    if tls.mode not in TLS_MODES:
        errors.append("[tls].mode must be one of %s" % ", ".join(TLS_MODES))
    if tls.mode == "auto" and web.backend != "caddy":
        errors.append("[tls].mode = auto (built-in ACME) only works with the caddy backend; "
                      "use 'certbot' or 'selfsigned' for nginx/apache")
    if tls.mode == "certbot" and web.backend == "caddy":
        errors.append("[tls].mode = certbot is not used with caddy (set 'auto' or provide manual certs)")

    if daemon.interval < 5:
        errors.append("[daemon].interval must be >= 5 seconds")
    if not isinstance(daemon.include_tags, list):
        errors.append("[daemon].include_tags must be a list")
    if not isinstance(daemon.exclude_vmids, list):
        errors.append("[daemon].exclude_vmids must be a list")

    for key, raw in cfg.targets.items():
        if not str(key).isdigit():
            errors.append("[targets] keys must be vmids, got %r" % key)
        if raw.ip:
            try:
                ipaddress.ip_address(raw.ip)
            except ValueError:
                errors.append("[targets.%s].ip %r is not a valid IP" % (key, raw.ip))
        _check_port(raw.port, "[targets.%s].port" % key)
        if raw.domain:
            from .naming import validate_hostname
            try:
                validate_hostname(raw.domain)
            except ValueError as e:
                errors.append("[targets.%s].domain: %s" % (key, e))
        if raw.tls is not None and raw.tls not in TLS_MODES:
            errors.append("[targets.%s].tls must be one of %s" % (key, ", ".join(TLS_MODES)))

    if errors:
        raise ConfigError("\n".join("- " + e for e in errors))


def from_dict(data: dict, path: str | None = None, strict: bool = True) -> Config:
    cfg = Config(
        proxmox=_build(ProxmoxConfig, _section(data, "proxmox"), "proxmox"),
        domain=_build(DomainConfig, _section(data, "domain"), "domain"),
        web=_build(WebConfig, _section(data, "web"), "web"),
        tls=_build(TLSConfig, _section(data, "tls"), "tls"),
        daemon=_build(DaemonConfig, _section(data, "daemon"), "daemon"),
        config_path=path,
    )
    targets_raw = _section(data, "targets")
    cfg.targets = {k: _build(Target, v or {}, "targets.%s" % k) for k, v in targets_raw.items()}
    if strict:
        validate(cfg)
    return cfg


def load(path, strict: bool = True) -> Config:
    p = Path(path)
    if not p.exists():
        raise ConfigError("config file not found: %s (run `ssipng init` first)" % p)
    try:
        with p.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError("invalid TOML in %s: %s" % (p, e)) from e
    return from_dict(data, path=str(p), strict=strict)


EXAMPLE = """\
# ssipng configuration — place at /etc/ssipng/config.toml (or pass --config)

[proxmox]
mode = "api"                      # "api" (HTTPS, token or password) or "cli" (pvesh/qm/pct, run as root)
url = "https://127.0.0.1:8006"
user = "root@pam"
token_id = "root@pam!ssipng"      # create with:  pveum user token add root@pam ssipng --privsep 0
token_secret = "REPLACE-ME-UUID"
# password = "…"                  # alternative to a token
verify_ssl = false                # PVE ships a self-signed certificate by default
# node = "pve1"                   # pin to one node (optional)

[domain]
mode = "proxy"                    # "proxy": hostname embeds the host's public IP, the host web server
                                  #          reverse-proxies to the guest
                                  # "direct": hostname embeds the guest IP itself (guest must be routable)
public_ip = "203.0.113.5"         # public IPv4/IPv6 of this Proxmox host — required in proxy mode
suffix = "sslip.io"               # sslip.io / nip.io
template = "{name}-{vmid}.{public_ip_dashed}.{suffix}"
name_max_len = 40
include_label = true              # direct mode: prefix "name-vmid." before the guest IP labels

[web]
backend = "nginx"                 # nginx | apache | caddy (the web server already running on this host)
port = 80                         # listen port of the generated HTTP vhost
default_target_port = 80          # guest port to proxy to when nothing else is set
# config_dir = "/etc/nginx/conf.d"          # override the vhost drop-in directory
# apache_enabled_dir = "/etc/apache2/sites-enabled"
# apache_mods_dir = "/etc/apache2/mods-enabled"
caddyfile = "/etc/caddy/Caddyfile"
caddy_import_dir = "/etc/caddy/conf.d"
manage = true
reload = true
websocket = true                  # nginx: add Upgrade/Connection proxy headers
acme_webroot = "/var/www/letsencrypt"

[tls]
mode = "none"                     # none | selfsigned | certbot | auto (auto = Caddy's built-in ACME)
selfsigned_dir = "/etc/ssipng/tls"
# certbot_email = "you@example.com"
# certbot_staging = false         # true while testing (Let's Encrypt staging CA)

[daemon]
interval = 30                     # seconds between polls for new/changed guests
cleanup = false                   # remove vhost + state entry when the guest disappears from the cluster
include_tags = []                 # e.g. ["ssipng"] — only manage guests with at least one of these PVE tags
exclude_vmids = []                # never touch these vmids, e.g. [101, 102]
state_file = "/var/lib/ssipng/state.json"

# Per-guest overrides (optional); key = vmid. Static IP here skips guest-agent discovery.
[targets]
# [targets.101]
# ip = "10.10.10.10"
# port = 8080
# domain = "app.example.com"      # fully custom domain instead of the sslip.io template
# tls = "selfsigned"
"""


def example_text() -> str:
    return EXAMPLE


def write_example(path, force: bool = False, public_ip: str | None = None) -> Path:
    p = Path(path)
    if p.exists() and not force:
        raise ConfigError("refusing to overwrite existing %s (use --force)" % p)
    ensure_dir(p.parent)
    text = EXAMPLE
    if public_ip:
        text = text.replace('public_ip = "203.0.113.5"', 'public_ip = "%s"' % public_ip)
    atomic = p
    atomic.write_text(text, encoding="utf-8", newline="\n")
    try:
        p.chmod(0o600)
    except OSError:
        pass  # not POSIX
    return p
