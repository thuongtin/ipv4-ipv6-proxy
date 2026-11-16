"""Disposable snapshot validation and rollback regression tests."""
import json
import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import config
import manage
import ops


class RollbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name) / 'root'
        self.backup = pathlib.Path(self.temp.name) / 'backup'
        self.live = self.root / config.MANAGED[0]
        self.live.parent.mkdir(parents=True)
        self.live.write_text('baseline')
        config.snapshot(self.root, self.backup)
        self.state = dict(proxy_running=True, network_running=True, proxy_enabled=True,
                          firewall_active=False, network={}, items=[])
        self.write_state()
        self.live.write_text('live')
        self.patch = mock.patch.object(manage, 'ROOT', self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def write_state(self):
        (self.backup / 'runtime.json').write_text(json.dumps(self.state))

    def assert_rejected_without_effects(self):
        with mock.patch.object(manage, 'stop_service') as stop, mock.patch.object(manage, 'remove_extra') as remove, mock.patch.object(manage, 'run') as run:
            with self.assertRaises((ValueError, KeyError, OSError)):
                ops.rollback(SimpleNamespace(list=False, snapshot=str(self.backup)))
            stop.assert_not_called()
            remove.assert_not_called()
            run.assert_not_called()
        self.assertEqual(self.live.read_text(), 'live')

    def test_bad_checksum_keeps_services_and_files(self):
        (self.backup / 'files' / config.MANAGED[0]).write_text('corrupt')
        self.assert_rejected_without_effects()

    def test_missing_file_keeps_services(self):
        (self.backup / 'files' / config.MANAGED[0]).unlink()
        self.assert_rejected_without_effects()

    def test_invalid_mode_keeps_services(self):
        path = self.backup / 'manifest.json'
        data = json.loads(path.read_text())
        data[config.MANAGED[0]]['mode'] = 'invalid'
        path.write_text(json.dumps(data))
        self.assert_rejected_without_effects()

    def test_missing_runtime_flag_keeps_services(self):
        del self.state['proxy_enabled']
        self.write_state()
        self.assert_rejected_without_effects()

    def test_invalid_runtime_network_keeps_services(self):
        self.state['network'] = {'interface': 'eth0', 'primary': 'invalid', 'addresses': []}
        self.write_state()
        self.assert_rejected_without_effects()

    def test_symlink_target_detected_before_any_restore(self):
        target = self.root / config.MANAGED[-1]
        target.parent.mkdir(parents=True)
        target.symlink_to(self.live)
        self.assert_rejected_without_effects()

    def test_symlink_parent_detected_before_restore(self):
        folder = self.root / 'root'
        elsewhere = pathlib.Path(self.temp.name) / 'elsewhere'
        elsewhere.mkdir()
        folder.symlink_to(elsewhere, target_is_directory=True)
        self.assert_rejected_without_effects()

    def test_valid_rollback_restores_files_and_runtime_order(self):
        calls = mock.Mock()
        with mock.patch.object(manage, 'stop_service', calls.stop), mock.patch.object(manage, 'remove_extra', calls.remove), mock.patch.object(manage, 'run', calls.run), mock.patch.object(manage, 'restore_enable_state', calls.enable), mock.patch.object(config.os, 'chown'):
            result = ops.rollback(SimpleNamespace(list=False, snapshot=str(self.backup)))
        self.assertEqual(self.live.read_text(), 'baseline')
        self.assertEqual(result['restored_snapshot'], str(self.backup.resolve()))
        self.assertEqual(calls.mock_calls, [mock.call.stop('3proxy'), mock.call.remove({}, {}),
            mock.call.stop('3proxy-ipv6'), mock.call.run('rc-service', '3proxy-ipv6', 'start'),
            mock.call.run('rc-service', '3proxy', 'start'), mock.call.enable(True)])

    def test_list_does_not_restore(self):
        base = pathlib.Path(self.temp.name) / 'base'
        snapshots = base / 'snapshots'
        snapshots.mkdir(parents=True)
        (snapshots / 'ignored-link').symlink_to(self.backup, target_is_directory=True)
        good = snapshots / 'good'
        good.mkdir()
        (good / 'manifest.json').write_text('{}')
        with mock.patch.object(ops, 'BASE', base), mock.patch.object(manage, 'rollback') as restore:
            self.assertEqual(ops.rollback(SimpleNamespace(list=True))['snapshots'], [str(good)])
            restore.assert_not_called()

    def test_pool_count_port_and_prefix_boundaries(self):
        for count, port, prefix in [(0, 10000, '2001:db8::/64'), (2001, 10000, '2001:db8::/64'),
                                    (2, 65535, '2001:db8::/64'), (1, 1023, '2001:db8::/64'),
                                    (1, 10000, '2001:db8::/48'), (1, 10000, '2001:db8:1::/64')]:
            with self.subTest(count=count, port=port, prefix=prefix), self.assertRaises(ValueError):
                config.make_pool('192.0.2.1', '2001:db8::1', prefix, count, port, 'fixture', 'fixture-pass')
        self.assertEqual(config.make_pool('192.0.2.1', '2001:db8::1', '2001:db8::/64', 1, 65535, 'fixture', 'fixture-pass')[0]['port'], 65535)


if __name__ == '__main__':
    unittest.main()
