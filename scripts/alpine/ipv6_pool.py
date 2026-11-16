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
