"""nginx backend — writes one drop-in .conf per guest into conf.d (or a custom include dir)."""

from __future__ import annotations

from pathlib import Path

from ..tls import cert_paths
from ..util import ConfigError
from .base import Backend

DEFAULT_CONFIG_DIR = "/etc/nginx/conf.d"


def _server(lines):
    return ["server {"] + ["    " + x for x in lines] + ["}"]


def _proxy_lines(rec, web):
    lines = [
        "location / {",
        f"    proxy_pass http://{rec.ip}:{rec.port};",
        "    proxy_http_version 1.1;",
        "    proxy_set_header Host $host;",
        "    proxy_set_header X-Real-IP $remote_addr;",
        "    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
        "    proxy_set_header X-Forwarded-Proto $scheme;",
    ]
    if web.websocket:
        lines += [
            "    proxy_set_header Upgrade $http_upgrade;",
            f"    proxy_set_header Connection $ssipng_ws_{rec.vmid};",
        ]
    lines += ["    proxy_read_timeout 90s;", "}"]
    return lines


def _plain_server(rec, web, with_acme):
    lines = [f"listen {web.port};", f"server_name {rec.domain};"]
    if with_acme:
        lines.append(f"location /.well-known/acme-challenge/ {{ root {web.acme_webroot}; }}")
    lines += _proxy_lines(rec, web)
    return _server(lines)


def _http_redirect_server(rec, web, with_acme):
    lines = [f"listen {web.port};", f"server_name {rec.domain};"]
    if with_acme:
        lines.append(f"location /.well-known/acme-challenge/ {{ root {web.acme_webroot}; }}")
    lines.append("location / { return 301 https://$host$request_uri; }")
    return _server(lines)


def _https_server(rec, web, crt, key):
    lines = [
        "listen 443 ssl http2;",
        f"server_name {rec.domain};",
        f"ssl_certificate {crt};",
        f"ssl_certificate_key {key};",
        "ssl_protocols TLSv1.2 TLSv1.3;",
    ]
    lines += _proxy_lines(rec, web)
    return _server(lines)


def render_nginx(rec, web, tls_cfg) -> str:
    mode = rec.tls_mode
    header = "# managed by ssipng — guest %s (%s, vmid %s); edits will be overwritten" % (
        rec.domain, rec.name, rec.vmid)
    L = [header]
    if web.websocket:
        L.append("map $http_upgrade $ssipng_ws_%s { default upgrade; '' close; }" % rec.vmid)

    if mode in ("selfsigned", "certbot"):
        crt, key = cert_paths(tls_cfg, rec.domain, mode)
        L += _http_redirect_server(rec, web, with_acme=(mode == "certbot"))
        L += _https_server(rec, web, crt, key)
    elif mode == "none":
        L += _plain_server(rec, web, with_acme=(rec.tls == "certbot"))
    else:
        raise ConfigError("tls mode %r is not supported by the nginx backend" % mode)
    return "\n".join(L) + "\n"


class Nginx(Backend):
    name = "nginx"

    def vhost_path(self, domain: str, vmid) -> Path:
        directory = self.web.config_dir or DEFAULT_CONFIG_DIR
        return Path(directory) / f"ssipng-{vmid}.conf"

    def render(self, rec) -> str:
        return render_nginx(rec, self.web, self.cfg.tls)

    def validate_cmds(self):
        return [["nginx", "-t"]]

    def reload_cmds(self):
        return [["systemctl", "reload", "nginx"]]
