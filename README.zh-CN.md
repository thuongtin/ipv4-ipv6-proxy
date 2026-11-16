# Alpine Linux 上的 IPv4 转 IPv6 代理

[Tiếng Việt](README.md) | [简体中文](README.zh-CN.md) | [한국어](README.ko.md) | [日本語](README.ja.md) | [English](README.en.md)

本安装程序**仅支持 Alpine Linux**，使用 3proxy 1.0.3 和 OpenRC。通过同一个公网 IPv4 地址上的多个端口接收 HTTP 代理连接，每个端口使用独立的 IPv6 地址作为出口。这些 IPv6 地址来自服务商路由至 VPS 的 /64 网段。整个代理池共用一组用户名和密码。

## 注册 Vultr 并领取试用额度

通过 **[Vultr 注册链接](https://www.vultr.com/?ref=8809561)** 创建账户，然后在创建 VPS 前激活符合条件的优惠：

- **新用户权益：** Vultr 公布的优惠码 **`FLY300VULTR`** 可为符合条件的服务提供 **300 美元试用额度**。该额度用于支付服务费用，并非现金。
- **领取方式：** 绑定有效的信用卡或 PayPal 账户，进入 **Vultr Console → Gift Code** 并输入优惠码。部署 VPS 前，请确认 Vultr 已接受该优惠码，并核对到账额度及有效期。
- **适用条件：** 仅限符合条件的新用户，每人限用一次优惠，仅适用于指定产品，且不可与其他优惠叠加。试用额度有有效期，请查看激活时显示的条款。

以上链接为用于支持本项目的推广链接。**是否获得额度取决于 Vultr 是否接受优惠码，仅点击推广链接并不保证获得额度。**

信息核对日期：**2026 年 11 月 16 日**。来源：[Vultr 官方优惠](https://www.vultr.com/coupons/)及[优惠码兑换说明](https://docs.vultr.com/platform/billing/faq)。优惠可能发生变化。为本安装程序创建 VPS 时，请选择 Alpine Linux 并启用 IPv6。

## 安装

要求：Alpine 系统的 root 权限、直接分配给 VPS 的公网 IPv4 地址、可用的 IPv6 连接，以及路由至 VPS 的 IPv6 /64 网段。安装前请在服务商控制面板中启用 IPv6。

将 [`setup-alpine.sh`](setup-alpine.sh) 复制到 VPS，阅读脚本后运行：

```sh
sh setup-alpine.sh --count 2000
```

脚本通过 TTY 提示输入用户名和密码，不内置实际账户凭据或服务器 IP 地址。默认创建 2,000 个代理，端口范围为 10000 至 11999。请保留此打包脚本，以便安装后执行运维操作。

以下详细文档为越南语：

- [详细安装指南](docs/SETUP.md)
- [运维、验证、修改凭据、导出代理池与回滚](docs/OPERATIONS.md)
- [故障排查](docs/TROUBLESHOOTING.md)
- [模块结构与本地测试](scripts/alpine/README.md)

## 常用命令

```sh
sh setup-alpine.sh --operation status
sh setup-alpine.sh --operation verify --sample 3
sh setup-alpine.sh --operation export --format csv --output /root/proxy-export.csv
```

这些运维操作不会运行 `apk` 或重新编译 3proxy。导出文件包含密码，以 `0600` 权限创建，且不会自动上传。

## 实际限制

- 仅允许访问 80/443 端口上的 HTTP/HTTPS 目标。私有地址、链路本地地址以及代理池或服务器网段中的目标均被阻止。
- `maxconn=65535` 与进程的文件描述符上限一致，不代表保证支持 65,535 个并发连接。`nproc` 无限制，不设置 `connlim` 配额或 UFW 的 512 连接限制。
- 本地测试不能替代对每台 VPS 的路由、身份验证、其他机器发起的连接及重启后行为的检查。监听端口数量齐全并不能证明实际吞吐量。
