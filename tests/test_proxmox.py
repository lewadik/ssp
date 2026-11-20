import io
import json
import unittest
import urllib.error

from ssipng.proxmox import (
    ProxmoxAPI,
    ProxmoxCLI,
    build_request,
    filter_resources,
    normalize_agent_interfaces,
    normalize_ip_j,
    normalize_lxc_interfaces,
    parse_ct_config_ip,
    pick_ip,
)
from ssipng.util import ProxmoxError, RunResult, Runner

SAMPLE_RESOURCES = [
    {"id": "node/pve1", "type": "node", "node": "pve1", "status": "online"},
    {"id": "sdn/vx", "type": "sdn", "status": "ok"},
    {"id": "vm/101", "vmid": 101, "node": "pve1", "name": "web-1", "status": "running", "type": "vm"},
    {"id": "lxc/102", "vmid": 102, "node": "pve1", "name": "db", "status": "running", "type": "lxc"},
]

AGENT_IFACES = [
    {"name": "lo", "ip-addresses": [{"ip-address": "127.0.0.1", "ip-address-type": "ipv4"}]},
    {"name": "eth0", "ip-addresses": [
        {"ip-address": "10.0.0.5", "ip-address-type": "ipv4"},
        {"ip-address": "fe80::1", "ip-address-type": "ipv6"},
    ]},
]


class TestPickIp(unittest.TestCase):
    def test_filters_loopback_and_linklocal(self):
        cand = normalize_agent_interfaces(AGENT_IFACES)
        self.assertEqual(pick_ip(cand), "10.0.0.5")
        self.assertEqual(pick_ip(cand, family="ipv6"), None)  # fe80::1 is link-local

    def test_prefer_interface(self):
        cand = [("eth0", [("10.0.0.5", "ipv4")]), ("eth1", [("10.0.0.6", "ipv4")])]
        self.assertEqual(pick_ip(cand, prefer="eth1"), "10.0.0.6")

    def test_garbage_filtered(self):
        self.assertIsNone(pick_ip([("eth0", [("junk", "ipv4"), ("169.254.9.9", "ipv4")])]))


class TestNormalizers(unittest.TestCase):
    def test_agent(self):
        cand = normalize_agent_interfaces(AGENT_IFACES)
        self.assertEqual(cand[1][0], "eth0")
        self.assertIn(("10.0.0.5", "ipv4"), cand[1][1])

    def test_ip_j(self):
        raw = [{"ifname": "eth0", "addr_info": [
            {"family": "inet", "local": "192.168.1.9"},
            {"family": "inet6", "local": "fd00::9"}]}]
        cand = normalize_ip_j(raw)
        self.assertIn(("192.168.1.9", "ipv4"), cand[0][1])
        self.assertIn(("fd00::9", "ipv6"), cand[0][1])

    def test_lxc_interfaces(self):
        raw = [{"name": "eth0", "ipv4": [{"address": "10.0.0.7"}], "ipv6": []}]
        self.assertEqual(pick_ip(normalize_lxc_interfaces(raw)), "10.0.0.7")

    def test_ct_config_static(self):
        nets = {"net0": "name=eth0,bridge=vmbr0,ip=10.0.0.9/24,gw=10.0.0.1"}
        self.assertEqual(parse_ct_config_ip(nets), "10.0.0.9")

    def test_ct_config_dhcp(self):
        self.assertIsNone(parse_ct_config_ip({"net0": "name=eth0,ip=dhcp"}))

    def test_ct_config_multi(self):
        nets = {"net1": "name=eth1,ip=dhcp", "net0": "name=eth0,ip=192.168.5.20/24"}
        self.assertEqual(parse_ct_config_ip(nets), "192.168.5.20")


class TestFilterResources(unittest.TestCase):
    def test_keeps_vm_and_lxc_only(self):
        out = filter_resources(SAMPLE_RESOURCES)
        self.assertEqual([r["vmid"] for r in out], [101, 102])


class TestBuildRequest(unittest.TestCase):
    def test_get_query(self):
        url, headers, body = build_request("https://pve:8006/", "GET", "/cluster/resources",
                                           {"type": "vm"})
        self.assertEqual(url, "https://pve:8006/api2/json/cluster/resources?type=vm")
        self.assertIsNone(body)
        self.assertEqual(headers["Accept"], "application/json")

    def test_post_body(self):
        url, headers, body = build_request("https://pve:8006", "POST", "/access/ticket",
                                           {"username": "root@pam", "password": "pw"})
        self.assertEqual(url, "https://pve:8006/api2/json/access/ticket")
        self.assertEqual(body, b"username=root%40pam&password=pw")
        self.assertEqual(headers["Content-Type"], "application/x-www-form-urlencoded")


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TestProxmoxAPI(unittest.TestCase):
    def test_token_auth_and_json(self):
        captured = []

        def fake_urlopen(req, timeout=None, context=None):
            captured.append(req)
            return FakeResp(json.dumps({"data": SAMPLE_RESOURCES}).encode())

        api = ProxmoxAPI(url="https://pve:8006", token_id="root@pam!ssipng",
                         token_secret="s3cr3t", urlopen=fake_urlopen)
        out = api.list_resources()
        self.assertEqual([r["vmid"] for r in out], [101, 102])
        auth = captured[0].headers.get("Authorization")
        self.assertEqual(auth, "PVEAPIToken=root@pam!ssipng=s3cr3t")

    def test_http_error_wrapped(self):
        def fake_urlopen(req, timeout=None, context=None):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", None,
                                         io.BytesIO(b"no access"))

        api = ProxmoxAPI(url="https://pve:8006", token_id="a", token_secret="b",
                         urlopen=fake_urlopen)
        with self.assertRaises(ProxmoxError):
            api.list_resources()

    def test_missing_credentials(self):
        api = ProxmoxAPI(url="https://pve:8006", urlopen=None)
        with self.assertRaises(ProxmoxError):
            api.list_resources()

    def test_vm_agent_ip(self):
        def fake_urlopen(req, timeout=None, context=None):
            self.assertTrue(req.full_url.endswith("/nodes/pve1/qemu/101/agent/network-get-interfaces"))
            self.assertEqual(req.get_method(), "GET")
            return FakeResp(json.dumps({"data": {"result": AGENT_IFACES}}).encode())

        api = ProxmoxAPI(url="https://pve:8006", token_id="a", token_secret="b",
                         urlopen=fake_urlopen)
        ip = api.guest_ip({"vmid": 101, "node": "pve1", "type": "vm"})
        self.assertEqual(ip, "10.0.0.5")


class TestProxmoxCLI(unittest.TestCase):
    def test_vm_agent_ip(self):
        class Fake(Runner):
            def run(self, cmd):
                self.last = list(cmd)
                return RunResult(0, json.dumps({"result": AGENT_IFACES}), "")

        fake = Fake()
        cli = ProxmoxCLI(runner=fake)
        ip = cli.guest_ip({"vmid": 101, "type": "vm"})
        self.assertEqual(ip, "10.0.0.5")
        self.assertEqual(fake.last, ["pvesh", "get", "/nodes/localhost/qemu/101/agent/network-get-interfaces", "--output-format", "json"])

    def test_lxc_static_config(self):
        class Fake(Runner):
            def run(self, cmd):
                if cmd[:2] == ["pct", "config"]:
                    return RunResult(0, "net0: name=eth0,bridge=vmbr0,ip=192.168.5.20/24,gw=192.168.5.1\n", "")
                return RunResult(1, "", "should not be called")

        cli = ProxmoxCLI(runner=Fake())
        ip = cli.guest_ip({"vmid": 102, "type": "lxc"})
        self.assertEqual(ip, "192.168.5.20")

    def test_pvesh_failure(self):
        class Fake(Runner):
            def run(self, cmd):
                return RunResult(2, "", "no auth")

        cli = ProxmoxCLI(runner=Fake())
        with self.assertRaises(ProxmoxError):
            cli.list_resources()


if __name__ == "__main__":
    unittest.main()
