# IPv4-to-IPv6 proxy on Alpine Linux

[Tiếng Việt](README.md) | [简体中文](README.zh-CN.md) | [한국어](README.ko.md) | [日本語](README.ja.md) | [English](README.en.md)

This installer supports **Alpine Linux only** and uses 3proxy 1.0.3 with OpenRC. A public IPv4 address accepts HTTP proxy connections on multiple ports. Each port uses a distinct outbound IPv6 address from a /64 routed to the VPS by the provider. The entire pool shares one username/password pair.

## Sign up for Vultr and redeem trial credit

Create an account using the **[Vultr registration link](https://www.vultr.com/?ref=8809561)**, then activate an eligible promotion before creating your VPS:

- **New-user benefit:** Vultr lists promo code **`FLY300VULTR`** for **US$300 in trial credit** on eligible services. This is service credit, not cash.
- **How to redeem:** link a valid credit card or PayPal account, open **Vultr Console → Gift Code**, and enter the code. Before deploying a VPS, confirm that Vultr has accepted the code and check the awarded credit and its expiration date.
- **Conditions:** eligible new customers only, one promotion per person, selected products only, and no combining with other offers. Credits expire; check the terms shown when activating the promotion.

The link above is an affiliate link that supports this project. **Credit depends on Vultr accepting the promo code; clicking the affiliate link alone does not guarantee credit.**

Checked on **16 November 2026**: [Vultr's official offers](https://www.vultr.com/coupons/) and [promo-code redemption instructions](https://docs.vultr.com/platform/billing/faq). Offers may change. When creating a VPS for this installer, select Alpine Linux and enable IPv6.

## Installation

Requirements: root access on Alpine, a directly assigned public IPv4 address, working IPv6 connectivity, and an IPv6 /64 routed to the VPS. Enable IPv6 in your provider's control panel before installation.

Copy [`setup-alpine.sh`](setup-alpine.sh) to the VPS, review the script, and run:

```sh
sh setup-alpine.sh --count 2000
```

The script prompts for a username and password through a TTY. No actual account credentials or server IP addresses are embedded. By default, it creates 2,000 proxies on ports 10000 through 11999. Keep the bundled script for post-installation operations.

Detailed guides below are in Vietnamese:

- [Detailed setup guide](docs/SETUP.md)
- [Operations, verification, credential changes, pool export, and rollback](docs/OPERATIONS.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Module structure and local testing](scripts/alpine/README.md)

## Common commands

```sh
sh setup-alpine.sh --operation status
sh setup-alpine.sh --operation verify --sample 3
sh setup-alpine.sh --operation export --format csv --output /root/proxy-export.csv
```

Operations do not run `apk` or rebuild 3proxy. Exported files contain passwords, are created with `0600` permissions, and are not uploaded automatically.

## Practical limits

- Only HTTP/HTTPS destinations on ports 80/443 are allowed. Private, link-local, and pool/server network destinations are blocked.
- `maxconn=65535` matches the process file-descriptor limit; it does not guarantee 65,535 simultaneous connections. `nproc` is unlimited. No `connlim` quota or UFW limit of 512 connections is installed.
- Local tests do not replace checks of routing, authentication, connections from another machine, and reboot behavior on each VPS. A complete listener count does not demonstrate throughput.
