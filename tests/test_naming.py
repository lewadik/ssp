import unittest

from ssipng.naming import build_domain, ip_dashed, render_template, slugify, validate_hostname
from ssipng.util import ConfigError


class DC:
    """Minimal stand-in for config.DomainConfig."""
    def __init__(self, mode="proxy", public_ip="203.0.113.5", suffix="sslip.io",
                 template="{name}-{vmid}.{public_ip_dashed}.{suffix}",
                 name_max_len=40, include_label=True):
        self.mode = mode
        self.public_ip = public_ip
        self.suffix = suffix
        self.template = template
        self.name_max_len = name_max_len
        self.include_label = include_label


class TestSlugify(unittest.TestCase):
    def test_common(self):
        self.assertEqual(slugify("Web Server 01"), "web-server-01")
        self.assertEqual(slugify("a__b!!c"), "a-b-c")
        self.assertEqual(slugify("x.y.z"), "x-y-z")

    def test_empty_becomes_vm(self):
        self.assertEqual(slugify(""), "vm")
        self.assertEqual(slugify("!!!"), "vm")

    def test_max_len(self):
        self.assertEqual(slugify("a" * 100, max_len=10), "a" * 10)

    def test_trailing_dashes_after_truncate(self):
        self.assertEqual(slugify("abc-" * 20, max_len=6).strip("-").startswith("abc"), True)


class TestIpDashed(unittest.TestCase):
    def test_ipv4(self):
        self.assertEqual(ip_dashed("203.0.113.5"), "203-0-113-5")

    def test_ipv6_compressed(self):
        self.assertEqual(ip_dashed("2001:db8::1"), "2001-db8--1")
        self.assertEqual(ip_dashed("::1"), "--1")

    def test_invalid(self):
        with self.assertRaises(ValueError):
            ip_dashed("not-an-ip")


class TestTemplate(unittest.TestCase):
    def test_unknown_placeholder(self):
        with self.assertRaises(ConfigError):
            render_template("{bogus}", {})

    def test_missing_value(self):
        with self.assertRaises(ConfigError):
            render_template("{name}", {})

    def test_ok(self):
        self.assertEqual(render_template("{name}-{vmid}", {"name": "a", "vmid": 1}), "a-1")


class TestBuildDomain(unittest.TestCase):
    def test_proxy_default_template(self):
        d = build_domain(DC(), 101, "web-1", "10.0.0.5")
        self.assertEqual(d, "web-1-101.203-0-113-5.sslip.io")

    def test_proxy_custom_template(self):
        d = build_domain(DC(template="{ip_dashed}.{suffix}"), 101, "web", "10.0.0.5")
        self.assertEqual(d, "10-0-0-5.sslip.io")

    def test_proxy_requires_embedded_public_ip(self):
        with self.assertRaises(ConfigError):
            build_domain(DC(template="{name}-{vmid}"), 101, "web", "10.0.0.5")

    def test_proxy_missing_public_ip(self):
        with self.assertRaises(ConfigError):
            build_domain(DC(public_ip=None), 101, "web", "10.0.0.5")

    def test_direct_with_label(self):
        d = build_domain(DC(mode="direct"), 101, "Web 1", "10.0.0.5")
        self.assertEqual(d, "web-1-101.10-0-0-5.sslip.io")

    def test_direct_without_label(self):
        d = build_domain(DC(mode="direct", include_label=False), 101, "web", "10.0.0.5")
        self.assertEqual(d, "10-0-0-5.sslip.io")

    def test_custom_suffix(self):
        d = build_domain(DC(suffix="nip.io"), 7, "db", "192.168.3.4")
        self.assertEqual(d, "db-7.203-0-113-5.nip.io")  # proxy mode embeds the HOST public IP

    def test_ipv6_public_ip(self):
        d = build_domain(DC(public_ip="2001:db8::1"), 5, "v6", "10.0.0.1")
        self.assertEqual(d, "v6-5.2001-db8--1.sslip.io")

    def test_unknown_mode(self):
        with self.assertRaises(ConfigError):
            build_domain(DC(mode="weird"), 1, "x", "10.0.0.1")


class TestValidateHostname(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(validate_hostname("web-1-101.203-0-113-5.sslip.io"),
                         "web-1-101.203-0-113-5.sslip.io")

    def test_empty(self):
        with self.assertRaises(ValueError):
            validate_hostname("")

    def test_empty_label(self):
        with self.assertRaises(ValueError):
            validate_hostname("a..b")

    def test_leading_dash(self):
        with self.assertRaises(ValueError):
            validate_hostname("-lead.sslip.io")
        self.assertEqual(validate_hostname("--1.sslip.io", allow_leading_dash=True),
                         "--1.sslip.io")

    def test_long_label(self):
        with self.assertRaises(ValueError):
            validate_hostname("x" * 64 + ".sslip.io")

    def test_bad_chars(self):
        with self.assertRaises(ValueError):
            validate_hostname("bad_host.sslip.io")


if __name__ == "__main__":
    unittest.main()
