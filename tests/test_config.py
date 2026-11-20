import tomllib
import unittest

from ssipng.config import EXAMPLE, Config, from_dict
from ssipng.util import ConfigError


def parse(text=EXAMPLE):
    return tomllib.loads(text)


class TestExample(unittest.TestCase):
    def test_example_parses(self):
        cfg = from_dict(parse())
        self.assertEqual(cfg.domain.mode, "proxy")
        self.assertEqual(cfg.domain.public_ip, "203.0.113.5")
        self.assertEqual(cfg.web.backend, "nginx")
        self.assertEqual(cfg.proxmox.mode, "api")
        self.assertEqual(cfg.daemon.interval, 30)
        self.assertEqual(cfg.targets, {})

    def test_example_values_roundtrip(self):
        data = parse()
        self.assertEqual(data["web"]["default_target_port"], 80)
        self.assertEqual(data["tls"]["mode"], "none")


class TestValidation(unittest.TestCase):
    def test_proxy_requires_public_ip(self):
        with self.assertRaises(ConfigError):
            from_dict({"proxmox": {"password": "x"}, "domain": {"mode": "proxy"}})

    def test_direct_allows_missing_public_ip(self):
        cfg = from_dict({"proxmox": {"password": "x"}, "domain": {"mode": "direct"}})
        self.assertEqual(cfg.domain.mode, "direct")
        self.assertIsNone(cfg.domain.public_ip)

    def test_api_requires_credentials(self):
        with self.assertRaises(ConfigError):
            from_dict({"domain": {"public_ip": "10.0.0.1"}})  # no token, no password
        cfg = from_dict({"proxmox": {"password": "x"}, "domain": {"public_ip": "10.0.0.1"}})
        self.assertEqual(cfg.proxmox.password, "x")

    def test_auto_tls_needs_caddy(self):
        with self.assertRaises(ConfigError):
            from_dict({"tls": {"mode": "auto"}, "web": {"backend": "nginx"},
                       "proxmox": {"password": "x"}, "domain": {"public_ip": "10.0.0.1"}})

    def test_unknown_key_rejected(self):
        with self.assertRaises(ConfigError):
            from_dict({"web": {"bogus_key": 1}})

    def test_bad_target_ip(self):
        with self.assertRaises(ConfigError):
            from_dict({"proxmox": {"password": "x"}, "domain": {"public_ip": "10.0.0.1"},
                       "targets": {"101": {"ip": "999.1.1.1"}}})

    def test_bad_domain_template_placeholder(self):
        with self.assertRaises(ConfigError):
            from_dict({"proxmox": {"password": "x"}, "domain": {"public_ip": "10.0.0.1"},
                       "targets": {"101": {"domain": "bad label"}}})

    def test_non_strict_skips_validation(self):
        cfg = from_dict({"domain": {"mode": "proxy"}}, strict=False)
        self.assertIsNone(cfg.domain.public_ip)

    def test_targets_parsed(self):
        cfg = from_dict({"proxmox": {"password": "x"}, "domain": {"public_ip": "10.0.0.1"},
                         "targets": {"101": {"ip": "10.0.0.5", "port": 8080,
                                             "domain": "app.example.com", "tls": "selfsigned"}}})
        t = cfg.targets["101"]
        self.assertEqual(t.ip, "10.0.0.5")
        self.assertEqual(t.port, 8080)
        self.assertEqual(t.domain, "app.example.com")
        self.assertEqual(t.tls, "selfsigned")


class TestDefaults(unittest.TestCase):
    def test_defaults(self):
        cfg = Config()
        self.assertEqual(cfg.domain.template, "{name}-{vmid}.{public_ip_dashed}.{suffix}")
        self.assertEqual(cfg.web.backend, "nginx")
        self.assertEqual(cfg.tls.mode, "none")
        self.assertEqual(cfg.daemon.state_file, "/var/lib/ssipng/state.json")


if __name__ == "__main__":
    unittest.main()
