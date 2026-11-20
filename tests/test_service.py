import os
import unittest
from pathlib import Path

from ssipng.backends import from_config as backend_from_config
from ssipng.config import Config, Target
from ssipng.proxmox import ProxmoxError
from ssipng.service import Assigner
from ssipng.state import State
from ssipng.util import Runner, RunResult, ServiceError

RESOURCES = [
    {"vmid": 101, "node": "pve1", "name": "web-1", "status": "running", "type": "vm", "tags": ""},
    {"vmid": 102, "node": "pve1", "name": "db", "status": "running", "type": "lxc", "tags": "ssipng"},
    {"vmid": 103, "node": "pve1", "name": "noagent", "status": "running", "type": "vm", "tags": ""},
]


class FakeProxmox:
    def __init__(self, resources, ips):
        self.resources = resources
        self.ips = dict(ips)

    def list_resources(self):
        return [dict(r) for r in self.resources]

    def guest_ip(self, res, prefer=None, family=None):
        ip = self.ips.get(res["vmid"])
        if not ip:
            raise ProxmoxError("no agent reply")
        return ip


class FakeRunner(Runner):
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
            raise ServiceError("%s failed (rc=%s): %s" % (what, r.rc, r.combined))
        return r


def make_setup(tmp_path, resources=RESOURCES, ips=None, backend="nginx", targets=None,
               runner_fail=()):
    tmp = Path(tmp_path)
    cfg = Config()
    cfg.domain.public_ip = "203.0.113.5"
    cfg.web.backend = backend
    if backend == "nginx":
        cfg.web.config_dir = str(tmp / "conf.d")
    elif backend == "apache":
        cfg.web.config_dir = str(tmp / "sites-available")
        cfg.web.apache_enabled_dir = str(tmp / "sites-enabled")
        cfg.web.apache_mods_dir = str(tmp / "mods-enabled")
    elif backend == "caddy":
        cfg.web.caddyfile = str(tmp / "Caddyfile")
        cfg.web.caddy_import_dir = str(tmp / "caddy-conf.d")
    cfg.daemon.state_file = str(tmp / "state.json")
    cfg.tls.selfsigned_dir = str(tmp / "tls")
    if targets:
        cfg.targets = {k: Target(**v) for k, v in targets.items()}
    runner = FakeRunner(fail=runner_fail)
    proxmox = FakeProxmox(resources, ips or {})
    backend_obj = backend_from_config(cfg, runner)
    state = State(cfg.daemon.state_file)
    assigner = Assigner(cfg, proxmox, backend_obj, state, runner)
    return cfg, assigner, runner


class TestAssign(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.cfg, self.assigner, self.runner = make_setup(
            self.tmp, ips={101: "10.0.0.5", 102: "10.0.0.6"})

    def tearDown(self):
        self._tmp.cleanup()

    def test_assign_creates_vhost_and_state(self):
        rec, actions = self.assigner.assign(101)
        path = os.path.join(self.tmp, "conf.d", "ssipng-101.conf")
        self.assertTrue(os.path.exists(path))
        content = open(path, encoding="utf-8").read()
        self.assertIn("server_name web-1-101.203-0-113-5.sslip.io;", content)
        self.assertIn("proxy_pass http://10.0.0.5:80;", content)
        self.assertEqual(rec.domain, "web-1-101.203-0-113-5.sslip.io")
        self.assertEqual(self.assigner.state.get(101)["domain"], rec.domain)
        joined = {" ".join(c) for c in self.runner.calls}
        self.assertIn("nginx -t", joined)
        self.assertIn("systemctl reload nginx", joined)
        self.assertIn("wrote vhost", " ".join(actions))

    def test_assign_is_idempotent(self):
        self.assigner.assign(101)
        calls_before = len(self.runner.calls)
        rec, actions = self.assigner.assign(101)
        self.assertIn("vhost unchanged", actions)
        self.assertEqual(len(self.runner.calls), calls_before)  # no validate/reload re-run

    def test_assign_with_ip_override(self):
        rec, _ = self.assigner.assign(101, ip="10.9.9.9", port=8080)
        self.assertEqual(rec.ip, "10.9.9.9")
        self.assertEqual(rec.port, 8080)

    def test_unresolvable_ip_raises(self):
        with self.assertRaises(ServiceError):
            self.assigner.assign(103)
        # no state entry for failed assign
        self.assertIsNone(self.assigner.state.get(103))

    def test_rollback_on_failed_validation(self):
        cfg, assigner, runner = make_setup(self.tmp, ips={101: "10.0.0.5"},
                                           runner_fail=("nginx -t",))
        with self.assertRaises(ServiceError):
            assigner.assign(101)
        path = os.path.join(self.tmp, "conf.d", "ssipng-101.conf")
        self.assertFalse(os.path.exists(path))  # rolled back
        self.assertIsNone(assigner.state.get(101))

    def test_dry_run_writes_nothing(self):
        rec, actions = self.assigner.assign(101, dry_run=True)
        self.assertEqual(rec.domain, "web-1-101.203-0-113-5.sslip.io")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "conf.d", "ssipng-101.conf")))
        self.assertIsNone(self.assigner.state.get(101))
        self.assertEqual(self.runner.calls, [])

    def test_targets_override_pin(self):
        cfg, assigner, runner = make_setup(
            self.tmp, ips={}, targets={"101": {"ip": "10.10.10.10", "port": 8080,
                                               "domain": "app.example.com"}})
        rec, _ = assigner.assign(101)
        self.assertEqual(rec.domain, "app.example.com")
        content = open(rec.vhost_path, encoding="utf-8").read()
        self.assertIn("server_name app.example.com;", content)
        self.assertIn("proxy_pass http://10.10.10.10:8080;", content)


class TestRemove(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.cfg, self.assigner, self.runner = make_setup(self.tmp, ips={101: "10.0.0.5"})

    def tearDown(self):
        self._tmp.cleanup()

    def test_remove_cleans_everything(self):
        rec, _ = self.assigner.assign(101)
        self.assertTrue(os.path.exists(rec.vhost_path))
        actions = self.assigner.remove(101)
        self.assertFalse(os.path.exists(rec.vhost_path))
        self.assertIsNone(self.assigner.state.get(101))
        self.assertTrue(any("reloaded" in a for a in actions))

    def test_remove_by_domain(self):
        rec, _ = self.assigner.assign(101)
        self.assigner.remove(rec.domain)
        self.assertIsNone(self.assigner.state.get(101))

    def test_remove_unknown(self):
        with self.assertRaises(ServiceError):
            self.assigner.remove(999)


class TestSync(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_sync_creates_all(self):
        cfg, assigner, _ = make_setup(self.tmp, ips={101: "10.0.0.5", 102: "10.0.0.6"})
        report = assigner.sync()
        by_action = {i["vmid"]: i["action"] for i in report}
        self.assertEqual(by_action[101], "created")
        self.assertEqual(by_action[102], "created")

    def test_sync_pending_and_managed(self):
        cfg, assigner, _ = make_setup(self.tmp, ips={101: "10.0.0.5"})
        assigner.assign(101)
        report = assigner.sync()
        rows = {i["vmid"]: i["action"] for i in report}
        self.assertEqual(rows[101], "managed")
        self.assertEqual(rows[102], "pending")
        self.assertEqual(rows[103], "pending")

    def test_sync_tag_filter(self):
        cfg, assigner, _ = make_setup(self.tmp, ips={102: "10.0.0.6"})
        cfg.daemon.include_tags = ["ssipng"]
        report = assigner.sync()
        rows = {i["vmid"]: i["action"] for i in report}
        self.assertEqual(rows[102], "created")
        self.assertEqual(rows[101], "skipped")
        self.assertEqual(rows[103], "skipped")
        self.assertIsNotNone(assigner.state.get(102))
        self.assertIsNone(assigner.state.get(101))

    def test_sync_excluded_vmid(self):
        cfg, assigner, _ = make_setup(self.tmp, ips={101: "10.0.0.5"})
        cfg.daemon.exclude_vmids = [101]
        report = assigner.sync()
        self.assertEqual(report[0]["action"], "excluded")
        self.assertIsNone(assigner.state.get(101))

    def test_sync_refreshes_changed_ip(self):
        cfg, assigner, runner = make_setup(self.tmp, ips={101: "10.0.0.5"})
        rec, _ = assigner.assign(101)
        assigner.proxmox.ips[101] = "10.0.0.99"
        report = assigner.sync()
        item = next(i for i in report if i["vmid"] == 101)
        self.assertEqual(item["action"], "updated")
        content = open(rec.vhost_path, encoding="utf-8").read()
        self.assertIn("proxy_pass http://10.0.0.99:80;", content)
        self.assertEqual(assigner.state.get(101)["ip"], "10.0.0.99")

    def test_sync_cleanup_removed_guest(self):
        cfg, assigner, runner = make_setup(self.tmp, ips={101: "10.0.0.5"})
        rec, _ = assigner.assign(101)
        proxmox = assigner.proxmox
        proxmox.resources = [r for r in proxmox.resources if r["vmid"] != 101]
        report = assigner.sync(cleanup=True)
        self.assertTrue(any(i["action"] == "removed" for i in report))
        self.assertFalse(os.path.exists(rec.vhost_path))
        self.assertIsNone(assigner.state.get(101))

    def test_sync_dry_run(self):
        cfg, assigner, _ = make_setup(self.tmp, ips={101: "10.0.0.5"})
        report = assigner.sync(dry_run=True)
        self.assertEqual(report[0]["action"], "dry-run")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "conf.d", "ssipng-101.conf")))
        self.assertIsNone(assigner.state.get(101))

    def test_sync_apache_backend(self):
        cfg, assigner, runner = make_setup(self.tmp, ips={101: "10.0.0.5"}, backend="apache")
        rec, actions = assigner.assign(101)
        self.assertTrue(os.path.exists(rec.vhost_path))
        joined = {" ".join(c) for c in runner.calls}
        self.assertIn("a2ensite ssipng-101", joined)
        self.assertIn("apache2ctl -t", joined)
        self.assertIn("systemctl reload apache2", joined)

    def test_sync_caddy_backend(self):
        cfg, assigner, runner = make_setup(self.tmp, ips={101: "10.0.0.5"}, backend="caddy")
        rec, actions = assigner.assign(101)
        self.assertTrue(os.path.exists(rec.vhost_path))
        caddyfile = open(cfg.web.caddyfile, encoding="utf-8").read()
        self.assertIn("import ", caddyfile)
        keep = os.path.join(cfg.web.caddy_import_dir, "00-keep.caddy")
        self.assertTrue(os.path.exists(keep))
        joined = {" ".join(c) for c in runner.calls}
        self.assertIn("caddy validate --config " + cfg.web.caddyfile, joined)


if __name__ == "__main__":
    unittest.main()
