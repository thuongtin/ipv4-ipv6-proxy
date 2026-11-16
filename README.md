# IPv4-to-IPv6 proxy trên Alpine Linux

Bộ cài **chỉ dành cho Alpine Linux**, dùng 3proxy 1.0.3 và OpenRC. Một IPv4 công khai nhận kết nối HTTP proxy tại nhiều cổng; mỗi cổng dùng một IPv6 ra ngoài riêng trong /64 được nhà cung cấp route tới VPS. Toàn bộ pool dùng chung một cặp thông tin xác thực.

## Đăng ký VPS

[Đăng ký Vultr qua link affiliate của tôi](https://www.vultr.com/?ref=8809561). Khi tạo VPS, chọn Alpine Linux và bật IPv6.

## Cài đặt

Yêu cầu: quyền root trên Alpine, IPv4 công khai gán trực tiếp, IPv6 hoạt động và một routed IPv6 /64. Bật IPv6 trong bảng điều khiển nhà cung cấp trước khi cài.

Sao chép [`setup-alpine.sh`](setup-alpine.sh) tới VPS, đọc script rồi chạy:

```sh
sh setup-alpine.sh --count 2000
```

Script hỏi username/password qua TTY; không có tài khoản hoặc IP máy thật được nhúng sẵn. Mặc định tạo 2.000 proxy, cổng 10000 đến 11999. Giữ file bundle để chạy các thao tác vận hành sau cài đặt.

- [Hướng dẫn cài đặt chi tiết](docs/SETUP.md)
- [Vận hành, kiểm tra, đổi mật khẩu, xuất pool và rollback](docs/OPERATIONS.md)
- [Chẩn đoán sự cố](docs/TROUBLESHOOTING.md)
- [Cấu trúc module và kiểm thử local](scripts/alpine/README.md)

## Lệnh thường dùng

```sh
sh setup-alpine.sh --operation status
sh setup-alpine.sh --operation verify --sample 3
sh setup-alpine.sh --operation export --format csv --output /root/proxy-export.csv
```

Các operation không chạy `apk` hoặc biên dịch lại 3proxy. File xuất chứa mật khẩu, được tạo với quyền `0600`, không tự upload.

## Giới hạn thực tế

- Chỉ cho phép đích HTTP/HTTPS trên cổng 80/443; chặn đích private, link-local và mạng pool/server.
- `maxconn=65535` bằng giới hạn file descriptor của process, không phải cam kết 65.535 kết nối đồng thời. `nproc` unlimited; không cài quota `connlim` hay giới hạn UFW 512 kết nối.
- Kiểm tra local không thay thế kiểm tra route, xác thực, kết nối từ máy khác và reboot trên từng VPS. Đếm đủ listener không chứng minh throughput.
