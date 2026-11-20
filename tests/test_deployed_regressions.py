import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from ssipng.proxmox import filter_resources, normalize_lxc_interfaces, pick_ip
from ssipng.daemon import Daemon
from ssipng.config import Config
from ssipng.util import ServiceError
from tests.test_service import make_setup

class DeployedRegressions(unittest.TestCase):
    def test_qemu_and_templates(self):
        rows = [{'vmid': 1, 'type': 'qemu'}, {'vmid': 2, 'type': 'qemu', 'template': 1}]
        self.assertEqual([r['vmid'] for r in filter_resources(rows)], [1])

    def test_lxc_formats(self):
        for row in ({'name': 'eth0', 'inet': '10.3.0.22/24'},
                    {'name': 'eth0', 'ip-addresses': [{'ip-address': '10.3.0.22', 'ip-address-type': 'inet'}]}):
            self.assertEqual(pick_ip(normalize_lxc_interfaces([row])), '10.3.0.22')

    def test_existing_caddy_route_restored(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, app, runner = make_setup(tmp, ips={101: '10.0.0.5'}, backend='caddy')
            rec, _ = app.assign(101)
            old = Path(rec.vhost_path).read_bytes()
            app.proxmox.ips[101] = '10.0.0.99'
            runner.fail = ('caddy validate',)
            with self.assertRaises(ServiceError):
                app.assign(101)
            self.assertEqual(Path(rec.vhost_path).read_bytes(), old)
            self.assertEqual(app.state.get(101)['ip'], '10.0.0.5')

    def test_stopped_guest_not_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, app, _ = make_setup(tmp, resources=[{'vmid': 101, 'type': 'qemu', 'status': 'stopped'}], ips={101:'10.0.0.5'})
            self.assertEqual(app.sync()[0]['action'], 'pending')
            self.assertIsNone(app.state.get(101))

    def test_daemon_cleanup_requested_each_cycle(self):
        cfg = Config()
        cfg.daemon.cleanup = True
        daemon = Daemon(cfg, Mock(), log=lambda _: None)
        daemon.install_signal_handlers = Mock()
        daemon._stop = Mock()
        daemon._stop.is_set.side_effect = [False, False, True]
        daemon._tick = Mock()
        daemon.run()
        self.assertEqual([c.args for c in daemon._tick.call_args_list], [(True,), (True,)])
