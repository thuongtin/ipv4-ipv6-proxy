#!/usr/bin/env python3
"""Redacted operational commands for the Alpine-only proxy installation."""
import argparse
import csv
import fcntl
import getpass
import io
import ipaddress
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

import manage
from config import validate_auth, render_config

ROOT = pathlib.Path('/')
BASE = pathlib.Path('/var/lib/3proxy-setup')


def read_json(name):
    path = ROOT / name
    if not path.is_file():
        raise RuntimeError('Managed installation data is missing; run setup first')
    return json.loads(path.read_text())


def read_pool():
    items = read_json('root/3proxy-pool.json')
    if not isinstance(items, list) or not items:
        raise ValueError('Invalid pool manifest')
    identities, endpoints, addresses = set(), set(), set()
    for item in items:
        validate_auth(item['username'], item['password'])
        ipaddress.IPv4Address(item['host'])
        address = str(ipaddress.IPv6Address(item['egress_ipv6']))
        if type(item['port']) is not int or not 1024 <= item['port'] <= 65535:
            raise ValueError('Invalid manifest port')
        endpoints.add((item['host'], item['port']))
        addresses.add(address)
        identities.add((item['username'], item['password']))
    if len(endpoints) != len(items) or len(addresses) != len(items) or len(identities) != 1:
        raise ValueError('Pool must contain unique bindings and shared credentials')
    if len({p['host'] for p in items}) != 1:
        raise ValueError('Pool must belong to one server')
    return items


def status():
    if not (ROOT / 'root/3proxy-pool.json').exists():
        return {'installed': False, 'healthy': False}
    items = read_pool()
    settings = read_json('var/lib/3proxy-setup/settings.json')
    expected = {p['port'] for p in items}
    owned = manage.owned_ports(items[0]['host'])
    try:
        info = manage.addresses(settings['interface'])
        valid = {str(ipaddress.IPv6Address(a['local'])) for a in info if a['family'] == 'inet6' and not a.get('tentative') and not a.get('dadfailed') and 'tentative' not in a.get('flags', []) and 'dadfailed' not in a.get('flags', [])}
    except (OSError, subprocess.CalledProcessError, KeyError, ValueError):
        valid = set()
    address_count = len(valid & {p['egress_ipv6'] for p in items})
    matches = (ROOT / 'etc/3proxy/3proxy.cfg').read_text() == render_config(items, settings['prefix'])
    running = manage.service_running('3proxy')
    enabled = (ROOT / 'etc/runlevels/default/3proxy').exists()
    return {'installed': True, 'healthy': running and expected == owned and address_count == len(items) and matches and enabled,
            'service_running': running, 'enabled_at_boot': enabled, 'ipv4': items[0]['host'],
            'port_first': min(expected), 'port_last': max(expected), 'expected_listeners': len(items),
            'owned_listeners': len(owned & expected), 'unexpected_listeners': len(owned - expected),
            'ready_ipv6': address_count, 'config_matches_manifest': matches}


def preflight(args):
    required = ('python3', 'apk', 'ip', 'curl', 'rc-service', 'ufw', 'iptables-restore')
    missing = [name for name in required if shutil.which(name) is None]
    result = {'ready': False, 'missing_commands': missing, 'disk_free_mib': shutil.disk_usage('/').free // 1048576, 'ipv6_probe_performed': False}
    if missing:
        return result
    host, primary, prefix, interface = manage.detect(args)
    result.update(ipv4=host, primary_ipv6=primary, ipv6_prefix=prefix, interface=interface, ready=True)
    if args.probe_ipv6:
        manage.canary(prefix, interface)
        result['ipv6_probe_performed'] = True
    return result


def selected_indices(count, sample):
    if count < 1 or sample < 1:
        raise ValueError('Sample count must be positive')
    if sample >= count:
        return list(range(count))
    if sample == 1:
        return [0]
    return sorted({round(i * (count - 1) / (sample - 1)) for i in range(sample)})


def probe(item, timeout, authenticated=True, wrong_credentials=False):
    conf = f'proxy = "http://{item["host"]}:{item["port"]}"\n'
    if authenticated or wrong_credentials:
        password = item['password'] + '-invalid' if wrong_credentials else item['password']
        conf += f'proxy-user = "{item["username"]}:{password}"\n'
    endpoint = 'https://api6.ipify.org' if authenticated else 'http://api6.ipify.org'
    with tempfile.TemporaryDirectory(prefix='3proxy-verify-') as temp:
        body_path = pathlib.Path(temp) / 'response'
        command = ['curl', '--silent', '--show-error', '--noproxy', '', '--connect-timeout', str(min(10, timeout)), '--max-time', str(timeout), '--config', '-', '--output', str(body_path), '--write-out', '%{http_code} %{http_connect}', endpoint]
        response = subprocess.run(command, input=conf, capture_output=True, text=True)
        codes = response.stdout.split()
        status_code = int(codes[0]) if codes and codes[0].isdigit() else 0
        connect_code = int(codes[1]) if len(codes) > 1 and codes[1].isdigit() else 0
        row = {'port': item['port'], 'http_status': status_code, 'connect_status': connect_code,
               'curl_exit': response.returncode, 'authenticated': authenticated, 'pass': False,
               'check': 'ipv6_egress' if authenticated else ('wrong_credentials' if wrong_credentials else 'missing_credentials')}
        if not authenticated:
            row['pass'] = response.returncode == 0 and status_code == 407
            return row
        if response.returncode == 0 and status_code == 200:
            try:
                address = str(ipaddress.IPv6Address(body_path.read_text().strip()))
                row['pass'] = address == str(ipaddress.IPv6Address(item['egress_ipv6']))
                row['actual_ipv6'] = address
            except (ValueError, OSError):
                row['error'] = 'invalid_ipv6_response'
        return row


def save_new(path, text):
    target = pathlib.Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as output:
            output.write(text)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


def verify(args):
    items = read_pool()
    indexes = list(range(len(items))) if args.all else selected_indices(len(items), args.sample)
    if args.output and os.path.lexists(pathlib.Path(args.output).expanduser()):
        raise FileExistsError('Output file already exists')
    results = []
    next_start = 0.0
    def checked(item, authenticated, wrong=False):
        nonlocal next_start
        delay = next_start - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        next_start = time.monotonic() + 0.5
        return probe(item, args.timeout, authenticated, wrong)
    # Refuse to continue egress tests if missing credentials are accepted.
    negative = checked(items[indexes[0]], False)
    results.append(negative)
    stopped = not negative['pass']
    if not stopped:
        wrong = checked(items[indexes[0]], False, True)
        results.append(wrong)
        stopped = not wrong['pass']
    if not stopped:
        for number, index in enumerate(indexes, 1):
            row = checked(items[index], True)
            results.append(row)
            if number % 25 == 0:
                print(f'VERIFY_PROGRESS {number}/{len(indexes)}', file=sys.stderr, flush=True)
            if not row['pass']:
                stopped = True
                break
    report = {'requested_proxies': len(indexes), 'checked_proxies': sum(row['authenticated'] for row in results),
              'passed_checks': sum(row['pass'] for row in results), 'checks': len(results),
              'pass': not stopped and len(results) == len(indexes) + 2, 'results': results}
    if args.output:
        save_new(args.output, json.dumps(report, indent=2) + '\n')
    return report


def export(args):
    items = read_pool()
    if args.format == 'json':
        text = json.dumps(items, indent=2) + '\n'
    elif args.format == 'txt':
        text = ''.join(f'{p["host"]}:{p["port"]}:{p["username"]}:{p["password"]}\n' for p in items)
    else:
        output = io.StringIO(newline='')
        writer = csv.DictWriter(output, fieldnames=['host', 'port', 'username', 'password', 'egress_ipv6'], lineterminator='\n')
        writer.writeheader()
        writer.writerows({k: p[k] for k in writer.fieldnames} for p in items)
        text = output.getvalue()
    path = save_new(args.output, text)
    return {'exported_records': len(items), 'format': args.format, 'output': str(path), 'mode': '0600'}


def restart():
    items = read_pool()
    manage.run('rc-service', '3proxy-ipv6', 'start')
    manage.run('rc-service', '3proxy', 'restart', timeout=120)
    manage.wait_ready(items)
    manage.smoke(items)
    return {'restarted': True, 'listeners': len(items), 'ipv6_smoke': True}


def credentials():
    items = read_pool()
    settings = read_json('var/lib/3proxy-setup/settings.json')
    username = os.environ.get('PROXY_USERNAME', items[0]['username'])
    password = os.environ.get('PROXY_PASSWORD')
    if password is None:
        if not sys.stdin.isatty():
            raise ValueError('Set PROXY_PASSWORD for unattended credential changes')
        username = input(f'New shared username [{username}]: ').strip() or username
        password = getpass.getpass('New shared password: ')
        if password != getpass.getpass('Confirm new shared password: '):
            raise ValueError('Passwords do not match')
    validate_auth(username, password)
    os.environ['PROXY_USERNAME'], os.environ['PROXY_PASSWORD'] = username, password
    args = SimpleNamespace(count=settings['count'], start_port=settings['start_port'],
                           ipv4=settings['ipv4'], ipv6_prefix=settings['prefix'], interface=settings['interface'])
    if settings['count'] != len(items) or [p['port'] for p in items] != list(range(settings['start_port'], settings['start_port'] + len(items))):
        raise ValueError('Stored settings and pool bindings disagree; run setup explicitly')
    host, primary, prefix, interface = manage.detect(args)
    if (host, primary, prefix, interface) != (items[0]['host'], items[0]['egress_ipv6'], settings['prefix'], settings['interface']):
        raise ValueError('Network bindings changed; run setup explicitly before credential rotation')
    marker = BASE / 'latest-snapshot.txt'
    before_marker = marker.read_text() if marker.exists() else None
    manage.configure(args)
    after = read_pool()
    if [(p['host'], p['port'], p['egress_ipv6']) for p in items] != [(p['host'], p['port'], p['egress_ipv6']) for p in after]:
        if marker.exists() and marker.read_text() != before_marker:
            manage.rollback(pathlib.Path(marker.read_text().strip()))
        raise RuntimeError('Pool bindings changed during credential update; inspect the restored state')
    return {'credentials_updated': True, 'bindings_preserved': len(items)}


def rollback(args):
    if args.list:
        folder = BASE / 'snapshots'
        paths = sorted(str(p) for p in folder.iterdir() if p.is_dir() and not p.is_symlink() and (p / 'manifest.json').is_file()) if folder.exists() else []
        return {'snapshots': paths}
    path = pathlib.Path(args.snapshot).expanduser().resolve()
    if not (path / 'manifest.json').is_file() or not (path / 'runtime.json').is_file():
        raise ValueError('Snapshot metadata is missing')
    manage.rollback(path)
    return {'restored_snapshot': str(path)}


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest='operation', required=True)
    p = sub.add_parser('preflight', help='Inspect prerequisites without installing anything')
    p.add_argument('--ipv4'); p.add_argument('--ipv6-prefix'); p.add_argument('--interface')
    p.add_argument('--probe-ipv6', action='store_true', help='Temporarily add/remove one address to verify provider routing')
    p = sub.add_parser('status', help='Read redacted installation health'); p.add_argument('--json', action='store_true')
    p = sub.add_parser('verify', help='Check proxy authentication and IPv6 egress; no retries')
    mode = p.add_mutually_exclusive_group(); mode.add_argument('--all', action='store_true'); mode.add_argument('--sample', type=int, default=5)
    p.add_argument('--timeout', type=int, default=30); p.add_argument('--output')
    sub.add_parser('restart', help='Restart services and run an authenticated smoke check')
    sub.add_parser('credentials', help='Change shared credentials through environment or TTY prompts')
    p = sub.add_parser('export', help='Write a private export without overwriting existing files')
    p.add_argument('--format', choices=('txt', 'csv', 'json'), required=True); p.add_argument('--output', required=True)
    p = sub.add_parser('rollback', help='List snapshots or explicitly restore one')
    mode = p.add_mutually_exclusive_group(required=True); mode.add_argument('--list', action='store_true'); mode.add_argument('--snapshot')
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    if args.operation == 'verify' and (args.sample < 1 or not 1 <= args.timeout <= 120):
        raise ValueError('Sample must be positive and timeout must be between 1 and 120 seconds')
    if os.geteuid() != 0 or not (ROOT / 'etc/alpine-release').is_file():
        raise RuntimeError('Root on Alpine is required')
    if args.operation == 'preflight':
        report = preflight(args)
    elif args.operation == 'status':
        report = status()
    else:
        if not BASE.is_dir():
            raise RuntimeError('Managed installation is missing; run setup first')
        mutation = args.operation in ('restart', 'credentials') or (args.operation == 'rollback' and not args.list)
        # Readers share a lock; mutations serialize with the setup manager.
        with open(BASE / 'setup.lock', 'a' if mutation else 'r') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX if mutation else fcntl.LOCK_SH)
            handlers = {'verify': lambda: verify(args), 'export': lambda: export(args),
                        'restart': restart, 'credentials': credentials, 'rollback': lambda: rollback(args)}
            report = handlers[args.operation]()
    print(json.dumps(report, indent=2))
    return 0 if report.get('healthy', report.get('ready', report.get('pass', True))) else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError, OSError, KeyError, subprocess.CalledProcessError) as error:
        # Exceptions describe stages/paths, never the contents of credential files.
        print('ERROR: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
