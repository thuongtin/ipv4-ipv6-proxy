"""Offline operational tests with disposable manifests and mocked system calls."""
import copy
import csv
import io
import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import ops


class OpsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.patch(ops, 'ROOT', self.root)
        self.patch(ops, 'BASE', self.root / 'var/lib/3proxy-setup')
        self.items = [dict(host='192.0.2.1', port=10000 + i,
                           username='fixture-user', password='fixture-secret',
                           egress_ipv6=f'2001:db8::{i + 1}') for i in range(4)]
        self.settings = dict(count=4, start_port=10000, ipv4='192.0.2.1',
                             prefix='2001:db8::/64', interface='eth0')
        self.write('root/3proxy-pool.json', json.dumps(self.items))
        self.write('var/lib/3proxy-setup/settings.json', json.dumps(self.settings))
        # Fail closed if any test forgets to mock an operating-system command.
        self.system_run = self.patch(ops.subprocess, 'run', side_effect=AssertionError('Unexpected subprocess'))

    def patch(self, target, name, *args, **kwargs):
        patcher = mock.patch.object(target, name, *args, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def verify_args(self, **kwargs):
        return SimpleNamespace(**dict(dict(all=False, sample=3, timeout=12, output=None), **kwargs))

    def row(self, passed=True, authenticated=True, code=200):
        return dict(port=10000, http_status=code, connect_status=0, curl_exit=0,
                    authenticated=authenticated, **{'pass': passed})

    def mock_verify(self, rows):
        self.patch(ops.time, 'monotonic', return_value=100)
        self.patch(ops.time, 'sleep')
        return self.patch(ops, 'probe', side_effect=rows)

    def test_manifest_valid_shared_credentials(self):
        self.assertEqual(ops.read_pool(), self.items)

    def test_manifest_rejects_invalid_fields(self):
        for field, values in {'host': ['invalid', '2001:db8::1'],
                              'egress_ipv6': ['invalid', '192.0.2.1'],
                              'port': [True, '10000', 1023, 65536],
                              'username': ['bad user'], 'password': ['bad:secret']}.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    items = copy.deepcopy(self.items)
                    items[0][field] = value
                    self.write('root/3proxy-pool.json', json.dumps(items))
                    with self.assertRaises(ValueError):
                        ops.read_pool()

    def test_manifest_rejects_empty_and_non_list(self):
        for value in [[], {}, None, 'invalid']:
            with self.subTest(value=value):
                self.write('root/3proxy-pool.json', json.dumps(value))
                with self.assertRaises(ValueError):
                    ops.read_pool()

    def test_manifest_rejects_duplicates_and_distinct_identities(self):
        for field, value in [('port', 10000), ('egress_ipv6', '2001:0db8::1'),
                             ('username', 'another'), ('password', 'another'),
                             ('host', '192.0.2.2')]:
            with self.subTest(field=field):
                items = copy.deepcopy(self.items)
                items[1][field] = value
                self.write('root/3proxy-pool.json', json.dumps(items))
                with self.assertRaises(ValueError):
                    ops.read_pool()

    def test_missing_manifest_has_stage_error(self):
        (self.root / 'root/3proxy-pool.json').unlink()
        with self.assertRaisesRegex(RuntimeError, 'run setup first'):
            ops.read_pool()
        self.assertEqual(ops.status(), dict(installed=False, healthy=False))

    def healthy_status(self):
        self.write('etc/3proxy/3proxy.cfg', ops.render_config(self.items, self.settings['prefix']))
        self.write('etc/runlevels/default/3proxy', '')
        self.patch(ops.manage, 'owned_ports', return_value={p['port'] for p in self.items})
        self.patch(ops.manage, 'addresses', return_value=[dict(family='inet6', local=p['egress_ipv6']) for p in self.items])
        self.patch(ops.manage, 'service_running', return_value=True)

    def test_status_healthy_and_redacted(self):
        self.healthy_status()
        report = ops.status()
        self.assertTrue(report['healthy'])
        self.assertEqual(report['ready_ipv6'], 4)
        for secret in ['fixture-user', 'fixture-secret', 'username', 'password']:
            self.assertNotIn(secret, json.dumps(report))

    def test_status_checks_each_health_boundary(self):
        self.healthy_status()
        for change in ['extra_port', 'missing_port', 'service', 'boot', 'config', 'addresses']:
            with self.subTest(change=change):
                with mock.patch.object(ops.manage, 'owned_ports', return_value={10000, 10001, 10002, 10003}), \
                     mock.patch.object(ops.manage, 'service_running', return_value=True):
                    if change == 'extra_port':
                        ops.manage.owned_ports.return_value.add(11000)
                    elif change == 'missing_port':
                        ops.manage.owned_ports.return_value.remove(10000)
                    elif change == 'service':
                        ops.manage.service_running.return_value = False
                    elif change == 'boot':
                        (self.root / 'etc/runlevels/default/3proxy').unlink()
                    elif change == 'config':
                        self.write('etc/3proxy/3proxy.cfg', 'drift')
                    else:
                        ops.manage.addresses.side_effect = OSError('address lookup failed')
                    self.assertFalse(ops.status()['healthy'])
                    self.write('etc/runlevels/default/3proxy', '')
                    self.write('etc/3proxy/3proxy.cfg', ops.render_config(self.items, self.settings['prefix']))

    def test_status_excludes_unready_addresses(self):
        self.healthy_status()
        for attributes in [dict(tentative=True), dict(dadfailed=True),
                           dict(flags=['tentative']), dict(flags=['dadfailed'])]:
            with self.subTest(attributes=attributes):
                ops.manage.addresses.return_value = [dict(family='inet6', local=p['egress_ipv6'], **attributes) for p in self.items]
                self.assertEqual(ops.status()['ready_ipv6'], 0)

    def test_preflight_missing_commands_does_not_detect_or_probe(self):
        self.patch(ops.shutil, 'which', return_value=None)
        detect = self.patch(ops.manage, 'detect')
        canary = self.patch(ops.manage, 'canary')
        report = ops.preflight(SimpleNamespace(probe_ipv6=True))
        self.assertFalse(report['ready'])
        self.assertIn('apk', report['missing_commands'])
        detect.assert_not_called()
        canary.assert_not_called()

    def test_preflight_only_mutates_with_explicit_probe(self):
        self.patch(ops.shutil, 'which', return_value='/fixture/command')
        self.patch(ops.manage, 'detect', return_value=('192.0.2.1', '2001:db8::1', '2001:db8::/64', 'eth0'))
        canary = self.patch(ops.manage, 'canary')
        configure = self.patch(ops.manage, 'configure')
        for enabled in [False, True]:
            report = ops.preflight(SimpleNamespace(probe_ipv6=enabled))
            self.assertTrue(report['ready'])
            self.assertEqual(report['ipv6_probe_performed'], enabled)
            self.assertEqual(canary.call_count, int(enabled))
        canary.assert_called_once_with('2001:db8::/64', 'eth0')
        configure.assert_not_called()

    def test_sampling_covers_boundaries_without_duplicates(self):
        self.assertEqual(ops.selected_indices(10, 3), [0, 4, 9])
        self.assertEqual(ops.selected_indices(10, 1), [0])
        self.assertEqual(ops.selected_indices(3, 10), [0, 1, 2])
        for count in range(1, 31):
            for sample in range(1, 35):
                values = ops.selected_indices(count, sample)
                self.assertEqual(len(values), min(count, sample))
                self.assertEqual(values, sorted(set(values)))
                self.assertTrue(all(0 <= i < count for i in values))
                if sample > 1:
                    self.assertEqual((values[0], values[-1]), (0, count - 1))
        for count, sample in [(0, 1), (1, 0), (-1, 1)]:
            with self.assertRaises(ValueError):
                ops.selected_indices(count, sample)

    def test_probe_uses_stdin_and_checks_exact_ipv6(self):
        def curl(command, **kwargs):
            self.assertEqual(command[command.index('--config') + 1], '-')
            self.assertEqual(command[command.index('--noproxy') + 1], '')
            self.assertNotIn('fixture-user', ' '.join(command))
            self.assertNotIn('fixture-secret', ' '.join(command))
            self.assertIn('proxy-user = "fixture-user:fixture-secret"', kwargs['input'])
            self.assertEqual(command[-1], 'https://api6.ipify.org')
            self.assertEqual(command[command.index('--connect-timeout') + 1], '10')
            pathlib.Path(command[command.index('--output') + 1]).write_text('2001:0db8::1\n')
            return subprocess.CompletedProcess(command, 0, '200 200', '')
        self.system_run.side_effect = curl
        self.assertTrue(ops.probe(self.items[0], 30)['pass'])

    def test_probe_rejects_bad_egress_and_failed_curl(self):
        for body, output, code in [('2001:db8::9', '200 200', 0), ('invalid', '200 200', 0),
                                   ('2001:db8::1', '200 200', 7), ('', 'bad output', 0),
                                   ('', '403 403', 0), ('', '429 429', 0)]:
            with self.subTest(body=body, output=output, code=code):
                def curl(command, **kwargs):
                    pathlib.Path(command[command.index('--output') + 1]).write_text(body)
                    return subprocess.CompletedProcess(command, code, output, '')
                self.system_run.side_effect = curl
                self.assertFalse(ops.probe(self.items[0], 5)['pass'])

    def test_probe_authentication_negatives_require_407(self):
        for wrong in [False, True]:
            for code, exit_code in [(407, 0), (200, 0), (403, 0), (429, 0), (407, 7)]:
                with self.subTest(wrong=wrong, code=code, exit_code=exit_code):
                    self.system_run.side_effect = None
                    self.system_run.return_value = subprocess.CompletedProcess([], exit_code, f'{code} 0', '')
                    row = ops.probe(self.items[0], 5, False, wrong)
                    self.assertEqual(row['pass'], code == 407 and exit_code == 0)
                    self.assertEqual(row['check'], 'wrong_credentials' if wrong else 'missing_credentials')
                    config = self.system_run.call_args.kwargs['input']
                    self.assertEqual('proxy-user' in config, wrong)
                    if wrong:
                        self.assertIn('fixture-secret-invalid', config)

    def test_verify_success_samples_and_throttles(self):
        probe = self.mock_verify([self.row(authenticated=False), self.row(authenticated=False)] + [self.row()] * 3)
        report = ops.verify(self.verify_args())
        self.assertTrue(report['pass'])
        self.assertEqual((report['checked_proxies'], report['checks']), (3, 5))
        self.assertEqual([call.args[0]['port'] for call in probe.call_args_list[2:]], [10000, 10002, 10003])
        self.assertEqual(probe.call_args_list[0].args[2:], (False, False))
        self.assertEqual(probe.call_args_list[1].args[2:], (False, True))
        self.assertEqual(ops.time.sleep.call_count, 4)
        ops.time.sleep.assert_called_with(0.5)

    def test_verify_stops_immediately_on_negative_failure(self):
        for prefix in [[], [self.row(authenticated=False)]]:
            with self.subTest(stage=len(prefix)):
                probe = self.mock_verify(prefix + [self.row(False, False, 403)])
                report = ops.verify(self.verify_args())
                self.assertFalse(report['pass'])
                self.assertEqual(report['checked_proxies'], 0)
                self.assertEqual(probe.call_count, len(prefix) + 1)

    def test_verify_fail_fast_403_429_and_other_egress_failures(self):
        for code in [403, 429, 500, 0]:
            with self.subTest(code=code):
                probe = self.mock_verify([self.row(authenticated=False)] * 2 + [self.row(False, True, code)])
                report = ops.verify(self.verify_args(all=True))
                self.assertFalse(report['pass'])
                self.assertEqual(report['requested_proxies'], 4)
                self.assertEqual(report['checked_proxies'], 1)
                self.assertEqual(probe.call_count, 3)

    def test_verify_existing_output_stops_before_probing(self):
        output = self.write('report.json', 'preserve')
        probe = self.patch(ops, 'probe')
        with self.assertRaises(FileExistsError):
            ops.verify(self.verify_args(output=str(output)))
        probe.assert_not_called()
        self.assertEqual(output.read_text(), 'preserve')

    def test_verify_writes_private_report(self):
        self.mock_verify([self.row(authenticated=False)] * 2 + [self.row()] * 4)
        output = self.root / 'report.json'
        report = ops.verify(self.verify_args(all=True, output=str(output)))
        self.assertEqual(json.loads(output.read_text()), report)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('fixture-secret', output.read_text())

    def test_export_formats_are_private_and_exclusive(self):
        for format_name in ['json', 'csv', 'txt']:
            with self.subTest(format=format_name):
                output = self.root / format_name / 'export'
                args = SimpleNamespace(format=format_name, output=str(output))
                report = ops.export(args)
                self.assertEqual(report['exported_records'], 4)
                self.assertEqual(output.stat().st_mode & 0o777, 0o600)
                self.assertEqual(output.parent.stat().st_mode & 0o777, 0o700)
                text = output.read_text()
                if format_name == 'json':
                    self.assertEqual(json.loads(text), self.items)
                elif format_name == 'csv':
                    rows = list(csv.DictReader(io.StringIO(text)))
                    self.assertEqual(len(rows), 4)
                    self.assertEqual(rows[0]['password'], 'fixture-secret')
                else:
                    self.assertEqual(text.splitlines()[0], '192.0.2.1:10000:fixture-user:fixture-secret')
                with self.assertRaises(FileExistsError):
                    ops.export(args)
                self.assertEqual(output.read_text(), text)

    def test_save_new_rejects_symlink_without_touching_target(self):
        target = self.write('original', 'keep')
        link = self.root / 'link'
        link.symlink_to(target)
        with self.assertRaises(FileExistsError):
            ops.save_new(link, 'replace')
        self.assertEqual(target.read_text(), 'keep')

    def credential_mocks(self):
        environment = mock.patch.dict(os.environ, {'PROXY_USERNAME': 'updated-user', 'PROXY_PASSWORD': 'updated-secret'})
        environment.start()
        self.addCleanup(environment.stop)
        detect = self.patch(ops.manage, 'detect', return_value=('192.0.2.1', '2001:db8::1', '2001:db8::/64', 'eth0'))
        configure = self.patch(ops.manage, 'configure')
        return detect, configure

    def test_credentials_preserve_stored_settings_and_bindings(self):
        detect, configure = self.credential_mocks()
        report = ops.credentials()
        self.assertEqual(report, dict(credentials_updated=True, bindings_preserved=4))
        args = configure.call_args.args[0]
        self.assertEqual(vars(args), dict(count=4, start_port=10000, ipv4='192.0.2.1', ipv6_prefix='2001:db8::/64', interface='eth0'))
        detect.assert_called_once_with(args)
        self.assertNotIn('updated-secret', json.dumps(report))

    def test_credentials_reject_settings_drift_before_configure(self):
        detect, configure = self.credential_mocks()
        for field, value in [('count', 5), ('start_port', 10001)]:
            with self.subTest(field=field):
                settings = dict(self.settings, **{field: value})
                self.write('var/lib/3proxy-setup/settings.json', json.dumps(settings))
                with self.assertRaisesRegex(ValueError, 'settings and pool bindings disagree'):
                    ops.credentials()
        detect.assert_not_called()
        configure.assert_not_called()

    def test_credentials_reject_each_network_drift(self):
        detect, configure = self.credential_mocks()
        baseline = list(detect.return_value)
        for index, value in enumerate(['192.0.2.2', '2001:db8::2', '2001:db9::/64', 'eth1']):
            with self.subTest(index=index):
                changed = baseline.copy()
                changed[index] = value
                detect.return_value = tuple(changed)
                with self.assertRaisesRegex(ValueError, 'Network bindings changed'):
                    ops.credentials()
        configure.assert_not_called()

    def test_credentials_roll_back_post_configure_binding_drift(self):
        _, configure = self.credential_mocks()
        marker = self.write('var/lib/3proxy-setup/latest-snapshot.txt', '/old')
        def drift(args):
            changed = copy.deepcopy(self.items)
            changed[1]['egress_ipv6'] = '2001:db8::99'
            self.write('root/3proxy-pool.json', json.dumps(changed))
            marker.write_text('/new')
        configure.side_effect = drift
        rollback = self.patch(ops.manage, 'rollback')
        with self.assertRaisesRegex(RuntimeError, 'Pool bindings changed'):
            ops.credentials()
        rollback.assert_called_once_with(pathlib.Path('/new'))

    def test_credentials_require_unattended_password(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(ops.sys.stdin, 'isatty', return_value=False):
            configure = self.patch(ops.manage, 'configure')
            with self.assertRaisesRegex(ValueError, 'PROXY_PASSWORD'):
                ops.credentials()
            configure.assert_not_called()

    def test_restart_orders_network_service_readiness_and_smoke(self):
        calls = mock.Mock()
        for name in ['run', 'wait_ready', 'smoke']:
            self.patch(ops.manage, name, getattr(calls, name))
        self.assertEqual(ops.restart(), dict(restarted=True, listeners=4, ipv6_smoke=True))
        self.assertEqual(calls.mock_calls, [mock.call.run('rc-service', '3proxy-ipv6', 'start'),
                                           mock.call.run('rc-service', '3proxy', 'restart', timeout=120),
                                           mock.call.wait_ready(self.items), mock.call.smoke(self.items)])

    def test_restart_propagates_errors_without_later_steps(self):
        for stage in range(4):
            with self.subTest(stage=stage):
                events = []
                def run(*args, **kwargs):
                    events.append('network' if args[1] == '3proxy-ipv6' else 'service')
                    if len(events) == stage + 1:
                        raise subprocess.CalledProcessError(1, 'fixture')
                def wait(items):
                    events.append('ready')
                    if stage == 2:
                        raise RuntimeError('not ready')
                def smoke(items):
                    events.append('smoke')
                    raise RuntimeError('smoke failed')
                with mock.patch.object(ops.manage, 'run', side_effect=run), \
                     mock.patch.object(ops.manage, 'wait_ready', side_effect=wait), \
                     mock.patch.object(ops.manage, 'smoke', side_effect=smoke):
                    with self.assertRaises((RuntimeError, subprocess.CalledProcessError)):
                        ops.restart()
                self.assertEqual(events, ['network', 'service', 'ready', 'smoke'][:stage + 1])


if __name__ == '__main__':
    unittest.main()
