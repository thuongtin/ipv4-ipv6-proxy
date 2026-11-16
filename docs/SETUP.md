# Cài đặt trên Alpine Linux

## 1. Chuẩn bị VPS

- Alpine Linux dùng OpenRC, quyền root, đủ RAM và tài nguyên socket/file descriptor cho số proxy dự kiến.
- Một IPv4 công khai gán trực tiếp lên interface. Host chỉ có IPv4 private sau NAT không được coi là VPS public trực tiếp.
- IPv6 chính hoạt động và một IPv6 /64 được nhà cung cấp route tới VPS, chứa IPv6 chính. Việc có một địa chỉ IPv6 /128 chưa chứng minh nhà cung cấp đã route cả /64.
- Truy cập HTTPS để tải package, source 3proxy và gọi dịch vụ kiểm tra egress.
- Cho phép inbound TCP tới dải cổng proxy trong firewall/security group của nhà cung cấp. Mặc định là `10000:11999`. Giữ đường truy cập SSH hiện có.

Kiểm tra nền tảng bằng lệnh chỉ đọc:

```sh
cat /etc/alpine-release
id -u
ip -4 address show
ip -6 address show
ip -4 route show
ip -6 route show
```

Lấy prefix và cách route từ bảng điều khiển/tài liệu của nhà cung cấp, không đoán /64 chỉ từ một địa chỉ /128. Nếu có nhiều interface/default route, chuẩn bị chỉ định rõ interface, IPv4 và prefix.

## 2. Chuyển bundle và kiểm tra trước cài

Chép file `setup-alpine.sh` từ checkout đã kiểm tra tới VPS. Đây là bundle độc lập, không cần clone repo trên máy đích. Từ thư mục chứa file:

```sh
sh -n setup-alpine.sh
sh setup-alpine.sh --help
```

`--help` không sửa hệ thống. Preflight operation cần Python 3 và các công cụ kiểm tra mạng sẵn có; operation không tự cài dependency. Khi môi trường đã đủ công cụ:

```sh
sh setup-alpine.sh --operation preflight
```

Preflight mặc định chỉ đọc. Để kiểm tra route của một IPv6 bổ sung, chủ động bật probe:

```sh
sh setup-alpine.sh --operation preflight --probe-ipv6
```

Probe thêm tạm một IPv6 rồi gỡ địa chỉ đó; đây không còn là thao tác chỉ đọc. Xem [chẩn đoán route](TROUBLESHOOTING.md) nếu probe thất bại. Bản thân luồng cài cũng kiểm tra canary IPv6 trước khi thay cấu hình proxy đang chạy.

## 3. Cài tương tác

```sh
sh setup-alpine.sh --count 2000
```

Nhập một username và password chung cho pool tại TTY. Không đặt mật khẩu trong command line. Username cho phép 1 đến 64 ký tự chữ, số, `_`, `.`, `-`. Password cho phép 1 đến 128 ký tự thuộc tập sau:

```text
A-Z a-z 0-9 ! @ % _ + = . , ~ ? / -
```

Không dùng khoảng trắng, dấu hai chấm hoặc dấu nháy trong credential. Khi tự động hóa, cung cấp `PROXY_USERNAME` và `PROXY_PASSWORD` qua environment của hệ thống quản lý secret, không ghi literal vào lịch sử shell, repo hoặc log CI. Tắt shell tracing khi xử lý secret.

## 4. Tham số mạng và dải cổng

Mặc định `--count 2000`, `--start-port 10000`. Số proxy hợp lệ từ 1 đến 2000; cổng bắt đầu từ 1024 đến 65535 và toàn bộ dải phải nằm trong giới hạn TCP.

Ví dụ chỉ định topology, thay các placeholder trước khi chạy:

```sh
sh setup-alpine.sh --count 2000 --start-port 10000 \
  --interface INTERFACE_NAME --ipv4 PUBLIC_IPV4 \
  --ipv6-prefix ROUTED_IPV6_PREFIX/64
```

`INTERFACE_NAME`, `PUBLIC_IPV4`, `ROUTED_IPV6_PREFIX` là placeholder, không phải giá trị có thể chạy nguyên văn. Auto-detect không thay thế thông tin route do nhà cung cấp xác nhận.

## 5. Những thay đổi của bộ cài

1. Cài dependency qua `apk`; tải và build source 3proxy **1.0.3** đã pin SHA-256 nếu chưa có binary managed hợp lệ. Gỡ dependency build tạm sau compile.
2. Kiểm tra hash binary đã quản lý trước khi tái sử dụng; không tự ghi đè binary lạ.
3. Sinh pool, một khai báo `users`, một `auth strong`, ACL chung và một listener cho mỗi cổng. Mỗi cổng gắn một egress IPv6 riêng; listener đầu dùng IPv6 chính.
4. Snapshot cấu hình liên quan trước khi thay đổi, cấu hình OpenRC và dịch vụ chạy bằng tài khoản không đặc quyền.
5. Giữ policy firewall ngoài phạm vi quản lý, không sửa SSH và không tự bật firewall đang inactive. Firewall của nhà cung cấp vẫn cần cấu hình riêng.
6. Kiểm tra listener và smoke có xác thực. Lỗi khi áp cấu hình kích hoạt rollback về snapshot trước thay đổi.

`maxconn=65535` bằng FD ceiling, `nproc` unlimited. Các giới hạn kernel, RAM, thread, socket vẫn áp dụng. Đích chỉ được phép ở cổng 80/443 và không thuộc dải private/link-local/server/pool bị chặn.

Chạy lại cùng tham số sẽ tái sử dụng credential và địa chỉ pool hiện có nếu không cung cấp thay thế. Rerun không đổi cấu hình thực hiện smoke, không restart service/PID và không tạo snapshot mới. Khi đổi `count` hoặc `start-port`, truyền rõ giá trị mong muốn; không mặc định xem lệnh cài là lệnh chỉ đọc.

## 6. Lấy danh sách và nghiệm thu

Các file có credential dưới `/root` chỉ cho root đọc:

| File | Nội dung |
| --- | --- |
| `/root/3proxy-pool.txt` | `host:port:username:password` |
| `/root/3proxy-pool.csv` | Host, cổng, credential và egress IPv6 kỳ vọng |
| `/root/3proxy-pool.json` | Pool có cấu trúc |
| `/root/3proxy.curl` | Cấu hình curl để kiểm tra listener đầu, không lộ credential trong argv |

```sh
sh setup-alpine.sh --operation status
sh setup-alpine.sh --operation verify --sample 3
curl --config /root/3proxy.curl https://api6.ipify.org
```

Tiếp theo kiểm tra có kiểm soát từ máy client ngoài VPS, sai mật khẩu bị từ chối, toàn bộ pool khi cần và reboot theo [OPERATIONS](OPERATIONS.md). Chỉ công bố VPS sẵn sàng sau khi các boundary đó đã được kiểm chứng trên chính máy đích. Lượt sửa repo/local không tự xác nhận các operation mới đã chạy live.
