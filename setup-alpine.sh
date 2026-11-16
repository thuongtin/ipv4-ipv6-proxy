#!/bin/sh
# Standalone Alpine installer template. Build with build_setup.py.
set -eu
umask 077

usage() {
    cat <<'HELP'
Usage: sh setup-alpine.sh [manager options]
       sh setup-alpine.sh --operation COMMAND [options]
       sh setup-alpine.sh --extract-only DIRECTORY

Install 3proxy 1.0.3 on Alpine and configure an authenticated IPv4-to-IPv6 pool.
Default pool size: 2000. Credentials: PROXY_USERNAME and PROXY_PASSWORD, or TTY prompts.
Manager options: --count N --start-port N --ipv4 ADDRESS
                 --ipv6-prefix PREFIX --interface NAME
                 --rollback SNAPSHOT_DIRECTORY
Operations (no package installation or build):
  preflight [--ipv4 ADDRESS --ipv6-prefix PREFIX --interface NAME] [--probe-ipv6]
  status [--json]
  verify [--sample N | --all] [--timeout SECONDS] [--output NEW_FILE]
  restart
  credentials          Read PROXY_USERNAME/PROXY_PASSWORD or prompt on a TTY.
  export --format txt|csv|json --output NEW_FILE
  rollback --list | --snapshot SNAPSHOT_DIRECTORY
--help                Show this help without changing the system.
--extract-only DIR    Extract Python tools without root, Alpine, or package changes.
HELP
}

extract_modules() {
    mkdir -p "$TOOLS"
    cat > "$TOOLS/manage.py" <<'THREEPROXY_449FDA50A4C98E2FF0B63699D2A9B8D81E23709691E5738390AC9CF2C94B8303'
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
THREEPROXY_449FDA50A4C98E2FF0B63699D2A9B8D81E23709691E5738390AC9CF2C94B8303
    cat > "$TOOLS/ipv6_pool.py" <<'THREEPROXY_6E7BA4F9787920DE8928EDB44C351BFF8BE4B22276B17E68BB2E5017E7B119B4'
#!/usr/bin/env python3
"""Manage only explicitly recorded extra IPv6 addresses for OpenRC."""
import ipaddress
import json
import pathlib
import subprocess
import sys
import time


def current(interface):
    data = json.loads(subprocess.check_output(['ip', '-j', '-6', 'addr', 'show', 'dev', interface]))
    return {str(ipaddress.ip_address(a['local'])): a for dev in data for a in dev['addr_info']}


def main():
    plan = json.loads(pathlib.Path('/etc/3proxy/ipv6-pool.json').read_text())
    interface = plan['interface']
    addresses = {str(ipaddress.IPv6Address(a)) for a in plan['addresses']}
    action = sys.argv[1]
    if action == 'apply' and plan.get('primary'):
        primary = str(ipaddress.IPv6Address(plan['primary']))
        for _ in range(90):
            info = current(interface).get(primary, {})
            if info and not info.get('tentative', False) and 'tentative' not in info.get('flags', []):
                break
            time.sleep(1)
        else:
            raise RuntimeError('Primary IPv6 address is not ready')
    present = current(interface)
    if action == 'apply':
        selected = addresses - set(present)
        verb = 'add'
    elif action == 'remove':
        selected = addresses & set(present)
        verb = 'del'
    else:
        raise SystemExit('Expected apply or remove')
    if selected:
        batch = ''.join(f'addr {verb} {a}/128 dev {interface}\n' for a in sorted(selected))
        subprocess.run(['ip', '-6', '-batch', '-'], input=batch, text=True, check=True, timeout=120)
    if action == 'apply':
        for _ in range(120):
            present = current(interface)
            failed = [a for a in addresses if a in present and (present[a].get('dadfailed', False) or 'dadfailed' in present[a].get('flags', []))]
            if failed:
                raise RuntimeError('IPv6 duplicate-address detection failed')
            ready = {a for a in addresses if a in present and not present[a].get('tentative', False) and 'tentative' not in present[a].get('flags', [])}
            if ready == addresses:
                print(f'IPV6_POOL_READY={len(ready)}')
                return
            time.sleep(1)
        raise RuntimeError('Timed out waiting for IPv6 addresses')
    print(f'IPV6_POOL_REMOVED={len(selected)}')


if __name__ == '__main__':
    main()
THREEPROXY_6E7BA4F9787920DE8928EDB44C351BFF8BE4B22276B17E68BB2E5017E7B119B4
    cat > "$TOOLS/config.py" <<'THREEPROXY_C7F03268AA5A451A1D9EE9E51AD1C2B229AF70F54178204609830D3375DFDD2C'
"""Pure configuration and snapshot helpers for the Alpine installer."""
import hashlib
import ipaddress
import json
import os
import pathlib
import re
import secrets
import shutil

FD_LIMIT = 65535
# 3proxy has no unlimited sentinel. Matching the process FD ceiling avoids
# an additional, lower application connection quota.
MAX_CONNECTIONS = FD_LIMIT
MANAGED = [
    'etc/3proxy/3proxy.cfg', 'etc/3proxy/ipv6-pool.json',
    'etc/init.d/3proxy', 'etc/init.d/3proxy-ipv6',
    'etc/ufw/before.rules', 'etc/ufw/user.rules', 'etc/ufw/user6.rules',
    'usr/local/libexec/3proxy-ipv6.py',
    'root/3proxy-pool.json', 'root/3proxy-pool.csv', 'root/3proxy-pool.txt',
    'root/3proxy-access.json', 'root/3proxy.curl',
    'var/lib/3proxy-setup/settings.json',
]


def validate_auth(username, password):
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', username):
        raise ValueError('Username must contain 1-64 letters, digits, underscore, dot or hyphen')
    if not re.fullmatch(r'[A-Za-z0-9!@%_+=.,~?/-]{1,128}', password):
        raise ValueError('Password contains characters unsupported by this installer')


def make_pool(host, primary, prefix, count, start_port, username, password, previous=()):
    validate_auth(username, password)
    ipaddress.IPv4Address(host)
    network = ipaddress.IPv6Network(prefix)
    if network.prefixlen != 64 or ipaddress.IPv6Address(primary) not in network:
        raise ValueError('A routed /64 containing the primary IPv6 address is required')
    if not 1 <= count <= 2000 or not 1024 <= start_port <= 65535 or start_port + count > 65536:
        raise ValueError('Invalid proxy count or port range')
    old = {p['port']: p for p in previous}
    used = {str(ipaddress.IPv6Address(primary))}
    items = []
    for index in range(count):
        port = start_port + index
        candidate = old.get(port, {}).get('egress_ipv6')
        if index == 0:
            address = str(ipaddress.IPv6Address(primary))
        elif candidate and ipaddress.IPv6Address(candidate) in network and str(ipaddress.IPv6Address(candidate)) not in used:
            address = str(ipaddress.IPv6Address(candidate))
        else:
            while True:
                address = str(ipaddress.IPv6Address(int(network.network_address) | secrets.randbits(64)))
                if address not in used and address != str(network.network_address):
                    break
        used.add(address)
        items.append({'host': host, 'port': port, 'username': username, 'password': password,
                      'egress_ipv6': address, 'protocol': 'http', 'allowed_destination_ports': [80, 443]})
    return items


def render_config(items, prefix):
    if not items or len({(p['username'], p['password']) for p in items}) != 1:
        raise ValueError('All listeners must use one shared credential pair')
    first = items[0]
    denied = f'0.0.0.0/8,10.0.0.0/8,100.64.0.0/10,127.0.0.0/8,169.254.0.0/16,172.16.0.0/12,192.168.0.0/16,224.0.0.0/4,::/128,::1/128,::ffff:0:0/96,fc00::/7,fe80::/10,ff00::/8,{prefix},{first["host"]}/32'
    lines = [f'maxconn {MAX_CONNECTIONS}', 'backlog 1024',
             'nserver 1.1.1.1', 'nserver 8.8.8.8', 'nscache 65536', 'nscache6 65536',
             'timeouts 1 5 30 60 180 1800 15 60', 'log @3proxy',
             f'users {first["username"]}:CL:{first["password"]}', 'auth strong',
             f'deny * * {denied}', f'allow {first["username"]} * * 80,443', 'deny *']
    # Each service copies the current ACL. Do not flush it between listeners.
    lines.extend(f'proxy -6 -n -a -p{p["port"]} -i{p["host"]} -e{p["egress_ipv6"]}' for p in items)
    return '\n'.join(lines) + '\n'


def remove_managed_cap(text):
    lines = text.splitlines(keepends=True)
    result = []
    i = 0
    while i < len(lines):
        if lines[i].strip() == '# 3proxy pool: global pre-authentication connection cap':
            if i + 1 < len(lines) and lines[i + 1].startswith('-A ufw-before-input ') and '--connlimit-above' in lines[i + 1]:
                i += 2
                continue
        result.append(lines[i])
        i += 1
    return ''.join(result)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(root, destination):
    destination.mkdir(parents=True, mode=0o700)
    records = {}
    for name in MANAGED:
        src = root / name
        if src.is_symlink():
            raise ValueError(f'Managed path must not be a symlink: {name}')
        if src.exists():
            target = destination / 'files' / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
            st = src.stat()
            records[name] = {'sha256': sha(src), 'uid': st.st_uid, 'gid': st.st_gid, 'mode': st.st_mode & 0o777}
        else:
            records[name] = None
    (destination / 'manifest.json').write_text(json.dumps(records, indent=2) + '\n')
    return records


def validate_snapshot(root, backup):
    """Validate every file and destination before any restore side effects."""
    records = json.loads((backup / 'manifest.json').read_text())
    if not isinstance(records, dict) or set(records) != set(MANAGED):
        raise ValueError('Snapshot manifest does not match managed paths')
    for name, entry in records.items():
        target = root / name
        if any(p.is_symlink() for p in (target, *target.parents) if root in p.parents):
            raise ValueError('Refusing to restore through a symlink')
        if target.exists() and not target.is_file():
            raise ValueError('Managed restore target must be a regular file')
        if entry is not None:
            if not isinstance(entry, dict) or set(entry) != {'sha256', 'uid', 'gid', 'mode'}:
                raise ValueError('Invalid snapshot file metadata')
            if any(type(entry[k]) is not int or entry[k] < 0 for k in ('uid', 'gid', 'mode')) or entry['mode'] > 0o777:
                raise ValueError('Invalid snapshot ownership or mode')
            source = backup / 'files' / name
            if not source.is_file() or any(p.is_symlink() for p in (source, *source.parents) if backup in p.parents):
                raise ValueError('Snapshot file must be a regular file')
            if sha(source) != entry['sha256']:
                raise ValueError('Snapshot checksum mismatch')
    return records


def restore(root, backup, ownership=True):
    records = validate_snapshot(root, backup)
    for name, entry in records.items():
        target = root / name
        if target.is_symlink():
            raise ValueError('Refusing to restore through a symlink')
        if entry is None:
            target.unlink(missing_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup / 'files' / name, target)
            target.chmod(entry['mode'])
            if ownership:
                os.chown(target, entry['uid'], entry['gid'])
    return records
THREEPROXY_C7F03268AA5A451A1D9EE9E51AD1C2B229AF70F54178204609830D3375DFDD2C
    cat > "$TOOLS/ops.py" <<'THREEPROXY_EF9B4112C609166F38340CEFCD107DFC91BA7C34DE54AC3CED5B01E91BEAF239'
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
THREEPROXY_EF9B4112C609166F38340CEFCD107DFC91BA7C34DE54AC3CED5B01E91BEAF239
}

# Help is side-effect free even when combined with other options.
for argument in "$@"; do
    case "$argument" in
        --help|-h) usage; exit 0 ;;
    esac
done
if [ "${1:-}" = '--extract-only' ]; then
    [ "$#" -eq 2 ] && [ -n "$2" ] || { echo 'Usage: --extract-only DIRECTORY' >&2; exit 2; }
    TOOLS=$2
    extract_modules
    printf 'Extracted Python tools to %s\n' "$TOOLS"
    exit 0
fi

[ "$(id -u)" -eq 0 ] && [ -f /etc/alpine-release ] || {
    echo 'Root on Alpine is required' >&2
    exit 1
}
BASE=/var/lib/3proxy-setup
BINARY=/usr/local/bin/3proxy
VERSION=1.0.3
SOURCE_SHA256=71acfaf75cdabe6c486aa182eb2ecb8ec731bdb05d52d3f4efe68ecdc1898492

# Operations never install packages or persist replacement tools. An older
# installation without ops.py can use temporary embedded modules instead.
if [ "${1:-}" = '--operation' ]; then
    [ "$#" -ge 2 ] || { echo 'Specify an operation; use --help for the list' >&2; exit 2; }
    shift
    OPERATION=$1
    shift
    case "$OPERATION" in
        preflight|status|verify|restart|credentials|export|rollback) ;;
        *) echo 'Unknown operation; use --help for the list' >&2; exit 2 ;;
    esac
    command -v python3 >/dev/null || { echo 'Operations require Python 3; the setup command installs dependencies' >&2; exit 1; }
    if [ -f "$BASE/tools/ops.py" ]; then
        exec python3 "$BASE/tools/ops.py" "$OPERATION" "$@"
    fi
    TOOLS=$(mktemp -d /tmp/3proxy-operation.XXXXXX)
    trap 'rm -rf "$TOOLS"' EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    extract_modules
    python3 "$TOOLS/ops.py" "$OPERATION" "$@"
    exit $?
fi

# Rollback uses the installed manager without package, binary, or tool upgrades.
for argument in "$@"; do
    case "$argument" in
        --rollback|--rollback=*)
            command -v python3 >/dev/null || {
                echo 'Rollback requires the Python interpreter from the managed installation' >&2
                exit 1
            }
            for module in manage.py config.py ipv6_pool.py; do
                [ -f "$BASE/tools/$module" ] || {
                    echo 'Rollback requires the installed managed Python tools' >&2
                    exit 1
                }
            done
            exec python3 "$BASE/tools/manage.py" "$@"
            ;;
    esac
done

# Reuse only the exact binary recorded by this installer or the prior managed installer.
if [ -e "$BINARY" ] || [ -L "$BINARY" ]; then
    MATCH=0
    for manifest in "$BASE/binary.sha256" "$BASE/installed.sha256"; do
        if [ -f "$manifest" ]; then
            expected=$(awk -v binary="$BINARY" '$2 == binary {print $1; exit}' "$manifest")
            actual=$(sha256sum "$BINARY" | awk '{print $1}')
            if [ -n "$expected" ] && [ "$expected" = "$actual" ]; then MATCH=1; break; fi
        fi
    done
    [ "$MATCH" -eq 1 ] && [ -x "$BINARY" ] || {
        echo 'Existing 3proxy binary is not a verified managed installation; leaving it unchanged' >&2
        exit 1
    }
fi

apk add --no-cache python3 curl ca-certificates iproute2 openrc ufw
mkdir -p "$BASE" /usr/local/bin
if [ ! -e "$BINARY" ]; then
    BUILD=$(mktemp -d /tmp/3proxy-build.XXXXXX)
    BUILD_PACKAGE=".3proxy-setup-build-$$"
    BUILD_DEPS=0
    cleanup() {
        rm -rf "$BUILD"
        if [ "$BUILD_DEPS" -eq 1 ]; then apk del "$BUILD_PACKAGE" >/dev/null; fi
    }
    trap cleanup EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    apk add --no-cache --virtual "$BUILD_PACKAGE" build-base linux-headers
    BUILD_DEPS=1
    ARCHIVE="$BUILD/3proxy-$VERSION.tar.gz"
    if [ -n "${SOURCE_ARCHIVE:-}" ]; then
        cp "$SOURCE_ARCHIVE" "$ARCHIVE"
    else
        curl --fail --location --retry 2 --proto '=https' --tlsv1.2 \
            -o "$ARCHIVE" "https://github.com/3proxy/3proxy/releases/download/$VERSION/3proxy-$VERSION.tar.gz"
    fi
    printf '%s  %s\n' "$SOURCE_SHA256" "$ARCHIVE" | sha256sum -c -
    tar -xzf "$ARCHIVE" -C "$BUILD"
    make -C "$BUILD/3proxy-$VERSION" -f Makefile.Linux -j1 HTTPSRV=false PLUGINS= \
        PAM_CHECK=false PCRE_CHECK=false OPENSSL_CHECK=false WOLFSSL_CHECK=false > "$BASE/build.log" 2>&1
    BUILT_BINARY="$BUILD/3proxy-$VERSION/bin/3proxy"
    [ -s "$BUILT_BINARY" ] && [ -x "$BUILT_BINARY" ] || {
        echo 'Build did not produce an executable 3proxy binary' >&2
        exit 1
    }
    # strip rejects invalid object files before anything is installed.
    strip "$BUILT_BINARY"
    install -m 755 "$BUILT_BINARY" "$BINARY"
    sha256sum "$BINARY" > "$BASE/binary.sha256"
    cleanup
    trap - EXIT INT TERM
fi
TOOLS="$BASE/tools"
extract_modules
exec python3 "$TOOLS/manage.py" "$@"
