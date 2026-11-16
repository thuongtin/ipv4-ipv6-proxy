#!/usr/bin/env python3
"""Configure or reconfigure an Alpine IPv4-to-IPv6 proxy pool transactionally."""
import argparse
import csv
import datetime
import fcntl
import getpass
import grp
import io
import ipaddress
import json
import os
import pathlib
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import time

from config import FD_LIMIT, MANAGED, make_pool, remove_managed_cap, render_config, restore, snapshot, validate_snapshot

BASE = pathlib.Path('/var/lib/3proxy-setup')
ROOT = pathlib.Path('/')


def run(*args, **kwargs):
    return subprocess.run(args, text=True, check=True, **kwargs)


def load(path, default=None):
    p = pathlib.Path(path)
    return json.loads(p.read_text()) if p.exists() else default


def write(path, text, mode=0o600, gid=0):
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.is_symlink():
        raise RuntimeError('Refusing to replace a symlink at a managed path')
    temp = p.with_name(p.name + '.setup-new')
    temp.write_text(text)
    temp.chmod(mode)
    os.chown(temp, 0, gid)
    temp.replace(p)


def addresses(interface):
    return json.loads(subprocess.check_output(['ip', '-j', 'addr', 'show', 'dev', interface]))[0]['addr_info']


def detect(args):
    routes = json.loads(subprocess.check_output(['ip', '-j', '-4', 'route', 'get', '1.1.1.1']))
    interface = args.interface or routes[0]['dev']
    if not interface or '/' in interface or any(c.isspace() for c in interface):
        raise ValueError('Invalid network interface')
    info = addresses(interface)
    v4 = args.ipv4 or routes[0].get('prefsrc') or routes[0].get('src')
    if not v4 or not ipaddress.IPv4Address(v4).is_global:
        raise ValueError('A directly assigned public IPv4 address is required; use --ipv4 to select it')
    if v4 not in {a['local'] for a in info if a['family'] == 'inet'}:
        raise ValueError('Selected IPv4 address is not assigned to the selected interface')
    global_v6 = [a for a in info if a['family'] == 'inet6' and ipaddress.IPv6Address(a['local']).is_global and not a.get('tentative') and not a.get('dadfailed')]
    if not global_v6:
        raise ValueError('No usable global IPv6 address exists on the selected interface')
    old = load('/etc/3proxy/ipv6-pool.json', {})
    candidates = {str(ipaddress.IPv6Address(a['local'])): a for a in global_v6}
    preferred = old.get('primary')
    if preferred not in candidates:
        # Prefer the provider-assigned /64 over managed /128 pool addresses.
        preferred = next((str(ipaddress.IPv6Address(a['local'])) for a in global_v6 if a['prefixlen'] == 64), next(iter(candidates)))
    address = candidates[preferred]
    if args.ipv6_prefix:
        prefix = str(ipaddress.IPv6Network(args.ipv6_prefix))
    elif address['prefixlen'] == 64:
        prefix = str(ipaddress.IPv6Interface(f'{preferred}/64').network)
    else:
        raise ValueError('Cannot infer a routed /64; supply --ipv6-prefix from your provider')
    network = ipaddress.IPv6Network(prefix)
    if network.prefixlen != 64 or ipaddress.IPv6Address(preferred) not in network:
        raise ValueError('The selected primary IPv6 must belong to the routed /64')
    return v4, preferred, prefix, interface


def credentials():
    previous = load('/root/3proxy-access.json', {})
    username = os.environ.get('PROXY_USERNAME') or previous.get('username')
    password = os.environ.get('PROXY_PASSWORD') or previous.get('password')
    if not username:
        if not sys.stdin.isatty():
            raise ValueError('Set PROXY_USERNAME and PROXY_PASSWORD for unattended setup')
        username = input('Shared proxy username: ').strip()
    if not password:
        if not sys.stdin.isatty():
            raise ValueError('Set PROXY_PASSWORD for unattended setup')
        password = getpass.getpass('Shared proxy password: ')
    return username, password


def canary(prefix, interface):
    network = ipaddress.IPv6Network(prefix)
    existing = {a['local'] for a in addresses(interface)}
    while True:
        candidate = str(ipaddress.IPv6Address(int(network.network_address) | secrets.randbits(64)))
        if candidate not in existing:
            break
    run('ip', '-6', 'addr', 'add', candidate + '/128', 'dev', interface)
    try:
        for _ in range(15):
            entry = next((a for a in addresses(interface) if a['local'] == candidate), {})
            if entry.get('dadfailed'):
                raise RuntimeError('IPv6 canary duplicate-address detection failed')
            if entry and not entry.get('tentative') and 'tentative' not in entry.get('flags', []):
                break
            time.sleep(1)
        else:
            raise RuntimeError('IPv6 canary did not become ready')
        result = subprocess.check_output(['curl', '-6', '--interface', candidate, '--fail', '--silent', '--show-error', '--connect-timeout', '10', '--max-time', '25', 'https://api6.ipify.org'], text=True).strip()
        if ipaddress.ip_address(result) != ipaddress.ip_address(candidate):
            raise RuntimeError('Provider did not route the additional IPv6 address')
        print('IPV6_ROUTING_CANARY_PASS', flush=True)
    finally:
        run('ip', '-6', 'addr', 'del', candidate + '/128', 'dev', interface)


def owned_ports(host):
    pidfile = pathlib.Path('/run/3proxy.pid')
    if not pidfile.exists():
        return set()
    pid = pidfile.read_text().strip()
    fdroot = pathlib.Path(f'/proc/{pid}/fd')
    if not fdroot.exists():
        return set()
    inodes = set()
    for fd in fdroot.iterdir():
        try:
            target = os.readlink(fd)
        except FileNotFoundError:
            continue
        if target.startswith('socket:['):
            inodes.add(target[8:-1])
    ports = set()
    for line in pathlib.Path('/proc/net/tcp').read_text().splitlines()[1:]:
        columns = line.split()
        address, port = columns[1].split(':')
        if columns[3] == '0A' and columns[9] in inodes and socket.inet_ntoa(struct.pack('<I', int(address, 16))) == host:
            ports.add(int(port, 16))
    return ports


def wait_ready(items):
    expected = {p['port'] for p in items}
    for _ in range(90):
        if owned_ports(items[0]['host']) == expected:
            return
        time.sleep(1)
    raise RuntimeError('3proxy did not own every expected IPv4 listener')


def smoke(items):
    first = items[0]
    config = f'proxy = "http://{first["host"]}:{first["port"]}"\nproxy-user = "{first["username"]}:{first["password"]}"\n'
    result = subprocess.check_output(['curl', '--fail', '--silent', '--show-error', '--noproxy', '', '--connect-timeout', '10', '--max-time', '30', '--config', '-', 'https://api6.ipify.org'], input=config, text=True).strip()
    if ipaddress.ip_address(result) != ipaddress.ip_address(first['egress_ipv6']):
        raise RuntimeError('Authenticated IPv6 egress smoke failed')


def service_running(name):
    return subprocess.run(['rc-service', name, 'status'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def stop_service(name):
    if service_running(name):
        run('rc-service', name, 'stop', timeout=120)


def remove_extra(current, previous):
    old = set(previous.get('addresses', [])) if current.get('interface') == previous.get('interface') else set()
    for address in set(current.get('addresses', [])) - old:
        subprocess.run(['ip', '-6', 'addr', 'del', address + '/128', 'dev', current['interface']], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def restore_enable_state(enabled, link=pathlib.Path('/etc/runlevels/default/3proxy')):
    if enabled:
        run('rc-update', 'add', '3proxy', 'default')
    else:
        link.unlink(missing_ok=True)


def validate_runtime_snapshot(state):
    """Reject incomplete runtime data before stopping services or removing IPs."""
    booleans = ('proxy_running', 'network_running', 'proxy_enabled', 'firewall_active')
    if not isinstance(state, dict) or any(type(state.get(k)) is not bool for k in booleans):
        raise ValueError('Invalid snapshot runtime flags')
    network, items = state.get('network'), state.get('items')
    if not isinstance(network, dict) or not isinstance(items, list):
        raise ValueError('Invalid snapshot runtime network or pool')
    if network:
        if not isinstance(network.get('interface'), str) or not network['interface'] or not isinstance(network.get('addresses'), list):
            raise ValueError('Invalid snapshot network metadata')
        ipaddress.IPv6Address(network['primary'])
        for address in network['addresses']:
            ipaddress.IPv6Address(address)
    for item in items:
        if not isinstance(item, dict) or type(item.get('port')) is not int or not 1024 <= item['port'] <= 65535:
            raise ValueError('Invalid snapshot pool metadata')
        ipaddress.IPv4Address(item['host'])
        ipaddress.IPv6Address(item['egress_ipv6'])


def rollback(backup):
    state = load(backup / 'runtime.json')
    validate_runtime_snapshot(state)
    validate_snapshot(ROOT, backup)
    stop_service('3proxy')
    now = load('/etc/3proxy/ipv6-pool.json', {})
    remove_extra(now, state['network'])
    stop_service('3proxy-ipv6')
    restore(ROOT, backup)
    # Preserve the pre-existing firewall activation state, not only its rules.
    if state['firewall_active']:
        run('ufw', 'reload')
    if state['network_running']:
        run('rc-service', '3proxy-ipv6', 'start')
    if state['proxy_running']:
        run('rc-service', '3proxy', 'start')
        if state['items']:
            wait_ready(state['items'])
    restore_enable_state(state['proxy_enabled'])
    print('ROLLBACK_RUNTIME_RESTORED', flush=True)


def validate_firewall(before):
    active = 'Status: active' in subprocess.check_output(['ufw', 'status'], text=True)
    # before.rules is a fragment referencing chains UFW creates at activation.
    # An inactive fresh host has no such chains; do not activate its firewall
    # just to validate an unchanged fragment.
    if active:
        run('iptables-restore', '--test', input=before)
    return active


def configure(args):
    host, primary, prefix, interface = detect(args)
    username, password = credentials()
    previous = load('/root/3proxy-pool.json', [])
    items = make_pool(host, primary, prefix, args.count, args.start_port, username, password, previous)
    network = {'interface': interface, 'primary': primary, 'addresses': [p['egress_ipv6'] for p in items[1:]]}
    if pathlib.Path('/etc/3proxy').is_symlink():
        raise ValueError('The managed configuration directory must not be a symlink')
    gid_lookup = subprocess.run(['getent', 'group', 'threeproxy'], stdout=subprocess.DEVNULL).returncode
    if gid_lookup:
        run('addgroup', '-S', 'threeproxy')
    if subprocess.run(['id', 'threeproxy'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        run('adduser', '-S', '-D', '-H', '-G', 'threeproxy', '-s', '/sbin/nologin', 'threeproxy')
    gid = grp.getgrnam('threeproxy').gr_gid
    service = f'''#!/sbin/openrc-run
name="3proxy"
description="Shared-authentication IPv4-to-IPv6 proxy pool"
command="/usr/local/bin/3proxy"
command_args="/etc/3proxy/3proxy.cfg"
command_user="threeproxy:threeproxy"
command_background="yes"
pidfile="/run/3proxy.pid"
rc_ulimit="-n {FD_LIMIT} -u unlimited"
depend() {{ need net 3proxy-ipv6; after firewall ufw; }}
'''
    network_service = '''#!/sbin/openrc-run
name="3proxy-ipv6"
description="Restore managed IPv6 addresses before 3proxy"
depend() { need net; before 3proxy; }
start() {
    ebegin "Restoring managed IPv6 addresses"
    /usr/bin/python3 /usr/local/libexec/3proxy-ipv6.py apply
    eend $?
}
stop() { return 0; }
'''
    before = pathlib.Path('/etc/ufw/before.rules').read_text()
    cleaned = remove_managed_cap(before)
    csv_file = io.StringIO(newline='')
    writer = csv.DictWriter(csv_file, fieldnames=['host', 'port', 'username', 'password', 'egress_ipv6'], lineterminator='\n')
    writer.writeheader()
    writer.writerows({k: p[k] for k in writer.fieldnames} for p in items)
    settings = {'count': args.count, 'start_port': args.start_port, 'ipv4': host, 'primary_ipv6': primary, 'prefix': prefix, 'interface': interface, 'username': username, 'shared_auth': True, 'connection_quota': None, 'fd_limit': FD_LIMIT}
    desired = {
        'etc/3proxy/3proxy.cfg': (render_config(items, prefix), 0o640, gid),
        'etc/3proxy/ipv6-pool.json': (json.dumps(network, indent=2) + '\n', 0o600, 0),
        'etc/init.d/3proxy': (service, 0o755, 0),
        'etc/init.d/3proxy-ipv6': (network_service, 0o755, 0),
        'usr/local/libexec/3proxy-ipv6.py': (pathlib.Path(__file__).with_name('ipv6_pool.py').read_text(), 0o700, 0),
        'etc/ufw/before.rules': (cleaned, 0o640, 0),
        'root/3proxy-pool.json': (json.dumps(items, indent=2) + '\n', 0o600, 0),
        'root/3proxy-pool.csv': (csv_file.getvalue(), 0o600, 0),
        'root/3proxy-pool.txt': (''.join(f'{p["host"]}:{p["port"]}:{p["username"]}:{p["password"]}\n' for p in items), 0o600, 0),
        'root/3proxy-access.json': (json.dumps(items[0], indent=2) + '\n', 0o600, 0),
        'root/3proxy.curl': (f'proxy = "http://{host}:{args.start_port}"\nproxy-user = "{username}:{password}"\nconnect-timeout = 15\nmax-time = 30\n', 0o600, 0),
        'var/lib/3proxy-setup/settings.json': (json.dumps(settings, indent=2) + '\n', 0o600, 0),
    }
    unchanged = all((ROOT / n).exists() and (ROOT / n).read_text() == content and ((ROOT / n).stat().st_mode & 0o777) == mode and (ROOT / n).stat().st_gid == group for n, (content, mode, group) in desired.items())
    if unchanged and owned_ports(host) == {p['port'] for p in items}:
        smoke(items)
        print(f'UNCHANGED: {len(items)} proxies; shared authentication; no connection quota', flush=True)
        return
    # Prove provider routing before changing any running service or firewall.
    canary(prefix, interface)
    firewall_active = validate_firewall(cleaned)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + secrets.token_hex(3)
    backup = BASE / 'snapshots' / stamp
    snapshot(ROOT, backup)
    state = {'proxy_running': service_running('3proxy'), 'network_running': service_running('3proxy-ipv6'),
             'proxy_enabled': pathlib.Path('/etc/runlevels/default/3proxy').exists(), 'firewall_active': firewall_active,
             'network': load('/etc/3proxy/ipv6-pool.json', {}), 'items': previous}
    (backup / 'runtime.json').write_text(json.dumps(state) + '\n')
    write(BASE / 'latest-snapshot.txt', str(backup) + '\n')
    try:
        stop_service('3proxy')
        stop_service('3proxy-ipv6')
        directory = pathlib.Path('/etc/3proxy')
        directory.mkdir(exist_ok=True)
        directory.chmod(0o750)
        os.chown(directory, 0, gid)
        for name, (content, mode, group) in desired.items():
            write(ROOT / name, content, mode, group)
        if firewall_active:
            run('ufw', 'reload')
        run('ufw', 'allow', 'in', 'proto', 'tcp', 'to', host, 'port', f'{args.start_port}:{args.start_port + args.count - 1}', 'comment', '3proxy-pool')
        if previous:
            old_rule = (previous[0]['host'], previous[0]['port'], previous[-1]['port'])
            if old_rule != (host, args.start_port, args.start_port + args.count - 1):
                subprocess.run(['ufw', 'delete', 'allow', 'in', 'proto', 'tcp', 'to', old_rule[0], 'port', f'{old_rule[1]}:{old_rule[2]}'], check=False)
        run('rc-update', 'add', '3proxy', 'default')
        run('rc-service', '3proxy-ipv6', 'start')
        run('rc-service', '3proxy', 'start')
        wait_ready(items)
        smoke(items)
        remove_extra(state['network'], network)
        print(f'APPLIED: {len(items)} proxies; shared authentication; no connection quota; ports={args.start_port}-{args.start_port + args.count - 1}', flush=True)
        print(f'SNAPSHOT={backup}', flush=True)
    except BaseException:
        print('Configuration failed; restoring the previous managed state', file=sys.stderr, flush=True)
        rollback(backup)
        raise


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--count', type=int, default=int(os.environ.get('PROXY_COUNT', '2000')))
    parser.add_argument('--start-port', type=int, default=int(os.environ.get('PROXY_START_PORT', '10000')))
    parser.add_argument('--ipv4')
    parser.add_argument('--ipv6-prefix')
    parser.add_argument('--interface')
    parser.add_argument('--rollback', type=pathlib.Path)
    args = parser.parse_args()
    if os.geteuid() != 0 or not pathlib.Path('/etc/alpine-release').exists():
        parser.error('Root on Alpine is required')
    BASE.mkdir(parents=True, exist_ok=True)
    with open(BASE / 'setup.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.rollback:
            rollback(args.rollback)
        else:
            configure(args)


if __name__ == '__main__':
    main()
