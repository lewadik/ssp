import unittest
from pathlib import Path

from ssipng.backends.apache import Apache
from ssipng.backends.caddy import Caddy
from ssipng.backends.nginx import Nginx
from ssipng.config import Config
from ssipng.service import Record
from ssipng.util import RunResult


def make_rec(**kw):
    base = dict(
        vmid=101, node="pve1", name="web-1", proxmox_type="vm", ip="10.0.0.5", port=80,
        domain="web-1-101.203-0-113-5.sslip.io", backend="nginx", tls="none",
        tls_mode="none", vhost_path="/tmp/x", created_at="t", updated_at="t",
    )
    base.update(kw)
    return Record(**base)


class FakeRunner:
    def __init__(self, fail=()):
        self.calls = []
        self.fail = tuple(fail)

    def run(self, cmd):
        self.calls.append(list(cmd))
        joined = " ".join(cmd)
        for f in self.fail:
            if f in joined:
                return RunResult(1, "", "boom: %s" % joined)
        return RunResult(0, "", "")

    def check(self, cmd, what):
        r = self.run(cmd)
        if r.rc != 0:
            from ssipng.util import ServiceError
            raise ServiceError("%s failed (rc=%s): %s" % (what, r.rc, r.combined))
        return r


def make_cfg(tmp_path=None, **sections):
    cfg = Config()
    for section, updates in sections.items():
        for key, value in (updates or {}).items():
            setattr(getattr(cfg, section), key, value)
    return cfg


class TestNginx(unittest.TestCase):
    def render(self, rec, cfg):
        return Nginx(cfg, FakeRunner()).render(rec)

    def test_plain_http(self):
        content = self.render(make_rec(), make_cfg())
        self.assertIn("server_name web-1-101.203-0-113-5.sslip.io;", content)
        self.assertIn("listen 80;", content)
        self.assertIn("proxy_pass http://10.0.0.5:80;", content)
        self.assertIn("map $http_upgrade $ssipng_ws_101", content)
        self.assertNotIn("listen 443", content)

    def test_no_websocket(self):
        content = self.render(make_rec(), make_cfg(web={"websocket": False}))
        self.assertNotIn("$ssipng_ws_101", content)

    def test_selfsigned_tls(self):
        rec = make_rec(tls="selfsigned", tls_mode="selfsigned")
        content = self.render(rec, make_cfg(tls={"mode": "selfsigned",
                                                 "selfsigned_dir": "/etc/ssipng/tls"}))
        self.assertIn("listen 443 ssl http2;", content)
        self.assertIn("ssl_certificate /etc/ssipng/tls/web-1-101.203-0-113-5.sslip.io.crt;", content)
        self.assertIn("return 301 https://$host$request_uri;", content)

    def test_certbot_tls(self):
        rec = make_rec(tls="certbot", tls_mode="certbot")
        content = self.render(rec, make_cfg(tls={"mode": "certbot"}))
        self.assertIn("/etc/letsencrypt/live/web-1-101.203-0-113-5.sslip.io/fullchain.pem", content)
        self.assertIn("location /.well-known/acme-challenge/", content)

    def test_certbot_pending_renders_http_only(self):
        rec = make_rec(tls="certbot", tls_mode="none")
        content = self.render(rec, make_cfg(tls={"mode": "certbot"}))
        self.assertNotIn("listen 443", content)
        self.assertIn("location /.well-known/acme-challenge/", content)

    def test_vhost_path(self):
        cfg = make_cfg(web={"config_dir": "/tmp/confd"})
        p = Nginx(cfg, FakeRunner()).vhost_path("d", 7)
        self.assertEqual(p, Path("/tmp/confd/ssipng-7.conf"))


class TestApache(unittest.TestCase):
    def test_plain_http(self):
        b = Apache(make_cfg(), FakeRunner())
        content = b.render(make_rec())
        self.assertIn("<VirtualHost *:80>", content)
        self.assertIn("ServerName web-1-101.203-0-113-5.sslip.io", content)
        self.assertIn("ProxyPass / http://10.0.0.5:80/", content)
        self.assertIn("ProxyPreserveHost On", content)

    def test_tls_vhosts(self):
        cfg = make_cfg(tls={"mode": "selfsigned"})
        rec = make_rec(tls="selfsigned", tls_mode="selfsigned")
        content = Apache(cfg, FakeRunner()).render(rec)
        self.assertIn("<VirtualHost *:443>", content)
        self.assertIn("SSLEngine on", content)
        self.assertIn("Redirect permanent / https://web-1-101.203-0-113-5.sslip.io/", content)

    def test_prepare_enables_modules_and_site(self):
        cfg = make_cfg(web={"apache_mods_dir": "/nonexistent/mods",
                            "apache_enabled_dir": "/nonexistent/enabled"})
        rec = make_rec(tls="selfsigned", tls_mode="selfsigned")
        cmds = Apache(cfg, FakeRunner()).prepare_cmds(rec, newly_created=True)
        joined = [" ".join(c) for c in cmds]
        self.assertIn("a2enmod proxy proxy_http", joined)
        self.assertIn("a2enmod ssl", joined)
        self.assertIn("a2ensite ssipng-101", joined)

    def test_prepare_skips_existing_symlink(self):
        cfg = make_cfg(web={"apache_mods_dir": "/nonexistent/mods",
                            "apache_enabled_dir": "/nonexistent/enabled"})
        rec = make_rec()
        cmds = Apache(cfg, FakeRunner()).prepare_cmds(rec, newly_created=False)
        self.assertFalse(any("a2enmod" in " ".join(c) and "ssl" in c for c in cmds))


class TestCaddy(unittest.TestCase):
    def test_http_only(self):
        content = Caddy(make_cfg(), FakeRunner()).render(make_rec())
        self.assertIn("http://web-1-101.203-0-113-5.sslip.io {", content)
        self.assertIn("reverse_proxy 10.0.0.5:80", content)

    def test_auto_tls(self):
        rec = make_rec(tls="auto", tls_mode="auto")
        content = Caddy(make_cfg(tls={"mode": "auto"}), FakeRunner()).render(rec)
        self.assertIn("web-1-101.203-0-113-5.sslip.io {", content)
        self.assertNotIn("http://", content)

    def test_manual_tls(self):
        rec = make_rec(tls="selfsigned", tls_mode="selfsigned")
        cfg = make_cfg(tls={"mode": "selfsigned"})
        content = Caddy(cfg, FakeRunner()).render(rec)
        self.assertIn("tls /etc/ssipng/tls/web-1-101.203-0-113-5.sslip.io.crt", content)

    def test_extra_writes_add_import_line(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(web={"caddyfile": str(Path(tmp) / "Caddyfile"),
                                "caddy_import_dir": str(Path(tmp) / "conf.d")})
            b = Caddy(cfg, FakeRunner())
            writes = dict(b.extra_writes(make_rec()))
            self.assertIn(Path(tmp) / "conf.d" / "00-keep.caddy", writes)
            caddyfile = Path(tmp) / "Caddyfile"
            self.assertIn(caddyfile, writes)
            self.assertIn("import %s/*.caddy" % (Path(tmp) / "conf.d"), writes[caddyfile])
            caddyfile.write_text(writes[caddyfile], encoding="utf-8")
            self.assertEqual(dict(b.extra_writes(make_rec())), {
                Path(tmp) / "conf.d" / "00-keep.caddy": writes[Path(tmp) / "conf.d" / "00-keep.caddy"]})


if __name__ == "__main__":
    unittest.main()
