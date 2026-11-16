import ipaddress
import json
import pathlib
import tempfile
import unittest
from unittest import mock
import config
import manage


class SharedTests(unittest.TestCase):
    def pool(self, previous=()):
        return config.make_pool('192.0.2.1', '2001:db8:1:1::1', '2001:db8:1:1::/64', 2000, 10000, 'fixture', 'fixture-pass', previous)

    def test_compact_shared_config(self):
        items = self.pool()
        text = config.render_config(items, '2001:db8:1:1::/64')
        for prefix in ['users ', 'auth ', 'allow ']:
            self.assertEqual(sum(line.startswith(prefix) for line in text.splitlines()), 1)
        self.assertEqual(sum(line.startswith('proxy ') for line in text.splitlines()), 2000)
        self.assertNotIn('\nflush', text)
        self.assertNotIn('\nconnlim', text)
        self.assertEqual(config.MAX_CONNECTIONS, config.FD_LIMIT)
        self.assertLess(len(text.splitlines()), 2020)

    def test_repeat_preserves_addresses_and_uses_one_identity(self):
        first = self.pool()
        second = self.pool(first)
        self.assertEqual(first, second)
        self.assertEqual(len({p['egress_ipv6'] for p in first}), 2000)
        self.assertEqual(len({(p['username'], p['password']) for p in first}), 1)

    def test_unsafe_credential_tokens_rejected(self):
        for password in ['a b', 'a\nb', '$file', 'a:b', 'quote"']:
            with self.assertRaises(ValueError):
                config.validate_auth('fixture', password)

    def test_remove_only_owned_firewall_cap(self):
        unrelated = '-A OTHER -m connlimit --connlimit-above 7 -j REJECT\n'
        owned = '# 3proxy pool: global pre-authentication connection cap\n-A ufw-before-input -m connlimit --connlimit-above 512 -j REJECT\n'
        self.assertEqual(config.remove_managed_cap(unrelated + owned + 'COMMIT\n'), unrelated + 'COMMIT\n')

    def test_snapshot_and_restore_preserve_unrelated_files(self):
        with tempfile.TemporaryDirectory() as d:
            root, backup = pathlib.Path(d) / 'root', pathlib.Path(d) / 'backup'
            file = root / config.MANAGED[0]
            file.parent.mkdir(parents=True)
            file.write_text('baseline')
            file.chmod(0o640)
            records = config.snapshot(root, backup)
            file.write_text('modified')
            new = root / 'root/3proxy-pool.txt'
            new.parent.mkdir(parents=True)
            new.write_text('new')
            (root / 'unrelated').write_text('keep')
            config.restore(root, backup, ownership=False)
            self.assertEqual(file.read_text(), 'baseline')
            self.assertEqual(file.stat().st_mode & 0o777, 0o640)
            self.assertFalse(new.exists())
            self.assertEqual((root / 'unrelated').read_text(), 'keep')
            self.assertEqual(config.sha(file), records[config.MANAGED[0]]['sha256'])

    def test_corrupted_snapshot_does_not_modify_target(self):
        with tempfile.TemporaryDirectory() as d:
            root, backup = pathlib.Path(d) / 'root', pathlib.Path(d) / 'backup'
            p = root / config.MANAGED[0]
            p.parent.mkdir(parents=True)
            p.write_text('baseline')
            config.snapshot(root, backup)
            p.write_text('live')
            (backup / 'files' / config.MANAGED[0]).write_text('corrupt')
            with self.assertRaises(ValueError):
                config.restore(root, backup, ownership=False)
            self.assertEqual(p.read_text(), 'live')

    def test_interface_change_removes_source_addresses(self):
        with mock.patch.object(manage.subprocess, 'run') as run:
            manage.remove_extra({'interface': 'eth0', 'addresses': ['2001:db8::1']}, {'interface': 'eth1', 'addresses': ['2001:db8::1']})
            self.assertEqual(run.call_args.args[0], ['ip', '-6', 'addr', 'del', '2001:db8::1/128', 'dev', 'eth0'])

    def test_same_interface_keeps_reused_addresses(self):
        with mock.patch.object(manage.subprocess, 'run') as run:
            manage.remove_extra({'interface': 'eth0', 'addresses': ['2001:db8::1']}, {'interface': 'eth0', 'addresses': ['2001:db8::1']})
            run.assert_not_called()

    def test_disabled_runlevel_restored_independently_of_running_state(self):
        with tempfile.TemporaryDirectory() as d:
            link = pathlib.Path(d) / '3proxy'
            link.symlink_to('/etc/init.d/3proxy')
            manage.restore_enable_state(False, link)
            self.assertFalse(link.is_symlink())
        with mock.patch.object(manage, 'run') as run:
            manage.restore_enable_state(True)
            run.assert_called_once_with('rc-update', 'add', '3proxy', 'default')

    def test_inactive_firewall_does_not_validate_against_missing_kernel_chains(self):
        with mock.patch.object(manage.subprocess, 'check_output', return_value='Status: inactive'), mock.patch.object(manage, 'run') as run:
            self.assertFalse(manage.validate_firewall('fragment'))
            run.assert_not_called()

    def test_active_firewall_fragment_is_validated(self):
        with mock.patch.object(manage.subprocess, 'check_output', return_value='Status: active'), mock.patch.object(manage, 'run') as run:
            self.assertTrue(manage.validate_firewall('fragment'))
            run.assert_called_once_with('iptables-restore', '--test', input='fragment')

    def test_detect_new_vps_without_existing_state(self):
        route = [{'dev': 'ens3', 'prefsrc': '45.76.199.20'}]
        info = [{'addr_info': [{'family': 'inet', 'local': '45.76.199.20'}, {'family': 'inet6', 'local': '2001:19f0:7001:365::1', 'prefixlen': 64}]}]
        args = type('Args', (), {'interface': None, 'ipv4': None, 'ipv6_prefix': None})()
        with mock.patch.object(manage.subprocess, 'check_output', side_effect=[json.dumps(route).encode(), json.dumps(info).encode()]), mock.patch.object(manage, 'load', return_value={}):
            self.assertEqual(manage.detect(args), ('45.76.199.20', '2001:19f0:7001:365::1', '2001:19f0:7001:365::/64', 'ens3'))

    def test_128_address_requires_explicit_provider_prefix(self):
        route = [{'dev': 'eth0', 'prefsrc': '45.76.199.20'}]
        info = [{'addr_info': [{'family': 'inet', 'local': '45.76.199.20'}, {'family': 'inet6', 'local': '2001:19f0:7001:365::1', 'prefixlen': 128}]}]
        args = type('Args', (), {'interface': None, 'ipv4': None, 'ipv6_prefix': None})()
        with mock.patch.object(manage.subprocess, 'check_output', side_effect=[json.dumps(route).encode(), json.dumps(info).encode()]), mock.patch.object(manage, 'load', return_value={}):
            with self.assertRaises(ValueError):manage.detect(args)


if __name__ == '__main__':
    unittest.main()
