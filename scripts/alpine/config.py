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
