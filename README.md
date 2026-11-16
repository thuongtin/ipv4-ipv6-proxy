# IPv4-to-IPv6 proxy trên Alpine Linux

Bộ cài **chỉ dành cho Alpine Linux**, dùng 3proxy 1.0.3 và OpenRC. Một IPv4 công khai nhận kết nối HTTP proxy tại nhiều cổng; mỗi cổng dùng một IPv6 ra ngoài riêng trong /64 được nhà cung cấp route tới VPS. Toàn bộ pool dùng chung một cặp thông tin xác thực.

## Đăng ký Vultr và nhận credit dùng thử

Tạo tài khoản qua **[link đăng ký Vultr](https://www.vultr.com/?ref=8809561)**, sau đó kích hoạt ưu đãi phù hợp trước khi tạo VPS:

- **Quyền lợi cho người dùng mới:** Vultr đang công bố mã **`FLY300VULTR`** để nhận **300 USD credit dùng thử** cho các dịch vụ thuộc chương trình. Đây là số dư dùng dịch vụ, không phải tiền mặt.
- **Cách nhận:** liên kết thẻ tín dụng hoặc PayPal hợp lệ, vào **Vultr Console → Gift Code** và nhập mã. Kiểm tra Vultr đã chấp nhận mã, số credit được cấp và hạn sử dụng trước khi triển khai VPS.
- **Điều kiện:** chỉ dành cho khách hàng mới đủ điều kiện, một ưu đãi mỗi người, áp dụng cho sản phẩm được chọn và không cộng dồn với ưu đãi khác. Credit có hạn sử dụng; xem điều khoản hiển thị khi kích hoạt.

Link trên là link affiliate giúp hỗ trợ dự án. **Credit phụ thuộc vào mã ưu đãi được Vultr chấp nhận, không được bảo đảm chỉ bằng việc nhấp link affiliate.**

Đối chiếu ngày **16/11/2026**: [ưu đãi chính thức của Vultr](https://www.vultr.com/coupons/) và [hướng dẫn nhập promo code](https://docs.vultr.com/platform/billing/faq). Ưu đãi có thể thay đổi. Khi tạo VPS cho bộ cài này, chọn Alpine Linux và bật IPv6.

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
