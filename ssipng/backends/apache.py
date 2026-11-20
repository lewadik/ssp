"""Apache backend — one site config per guest in sites-available, enabled via a2ensite."""

from __future__ import annotations

from pathlib import Path

from ..tls import cert_paths
from ..util import ConfigError
from .base import Backend

DEFAULT_CONFIG_DIR = "/etc/apache2/sites-available"
DEFAULT_ENABLED_DIR = "/etc/apache2/sites-enabled"
DEFAULT_MODS_DIR = "/etc/apache2/mods-enabled"


def _vhost(rec, web, port, ssl_paths=None, redirect=None, acme=False):
    lines = [f"<VirtualHost *:{port}>", f"    ServerName {rec.domain}"]
    if acme:
        lines += [
            f"    Alias /.well-known/acme-challenge/ {web.acme_webroot}/.well-known/acme-challenge/",
            f"    <Directory {web.acme_webroot}>",
            "        Require all granted",
            "    </Directory>",
        ]
    if redirect:
        lines.append(f"    Redirect permanent / {redirect}")
    if ssl_paths or not redirect:
        crt, key = ssl_paths or (None, None)
        if ssl_paths:
            lines += ["    SSLEngine on", f"    SSLCertificateFile {crt}",
                      f"    SSLCertificateKeyFile {key}"]
        lines += [
            "    ProxyPreserveHost On",
            f"    ProxyPass / http://{rec.ip}:{rec.port}/",
            f"    ProxyPassReverse / http://{rec.ip}:{rec.port}/",
        ]
    lines += ["</VirtualHost>", ""]
    return lines


def render_apache(rec, web, tls_cfg) -> str:
    mode = rec.tls_mode
    header = "# managed by ssipng — guest %s (%s, vmid %s); edits will be overwritten" % (
        rec.domain, rec.name, rec.vmid)
    L = [header]
    if mode in ("selfsigned", "certbot"):
        crt, key = cert_paths(tls_cfg, rec.domain, mode)
        L += _vhost(rec, web, 80, redirect=f"https://{rec.domain}/", acme=(mode == "certbot"))
        L += _vhost(rec, web, 443, ssl_paths=(crt, key))
    elif mode == "none":
        L += _vhost(rec, web, 80, acme=(rec.tls == "certbot"))
    else:
        raise ConfigError("tls mode %r is not supported by the apache backend" % mode)
    return "\n".join(L) + "\n"


class Apache(Backend):
    name = "apache"

    def vhost_path(self, domain: str, vmid) -> Path:
        directory = self.web.config_dir or DEFAULT_CONFIG_DIR
        return Path(directory) / f"ssipng-{vmid}.conf"

    def render(self, rec) -> str:
        return render_apache(rec, self.web, self.cfg.tls)

    def prepare_cmds(self, rec, newly_created: bool) -> list:
        cmds = []
        mods = Path(self.web.apache_mods_dir or DEFAULT_MODS_DIR)
        if not (mods / "proxy_http.load").exists():
            cmds.append(["a2enmod", "proxy", "proxy_http"])
        if (rec.tls_mode or "none") != "none" and not (mods / "ssl.load").exists():
            cmds.append(["a2enmod", "ssl"])
        enabled = Path(self.web.apache_enabled_dir or DEFAULT_ENABLED_DIR)
        stem = f"ssipng-{rec.vmid}"
        if not (enabled / f"{stem}.conf").exists():
            cmds.append(["a2ensite", stem])
        return cmds

    def validate_cmds(self):
        return [["apache2ctl", "-t"]]

    def reload_cmds(self):
        return [["systemctl", "reload", "apache2"]]

    def disable_cmds(self, rec):
        return [["a2dissite", f"ssipng-{rec.vmid}"]]
