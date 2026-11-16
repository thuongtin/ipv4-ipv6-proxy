# Chẩn đoán sự cố Alpine

Bắt đầu bằng dữ liệu chỉ đọc, giữ nguyên SSH và firewall ngoài phạm vi quản lý:

```sh
sh setup-alpine.sh --operation status --json
sh setup-alpine.sh --operation preflight
rc-service 3proxy-ipv6 status
rc-service 3proxy status
ip -4 route show
ip -6 route show
ip -6 address show
```

Không dán nguyên cấu hình proxy, file export, environment hoặc snapshot lên ticket: chúng có thể chứa credential. Status che credential, nhưng địa chỉ và topology vẫn là dữ liệu nội bộ.

## Root, Alpine hoặc Python chưa sẵn sàng

- `Root on Alpine is required`: kiểm tra `id -u`, `/etc/alpine-release`, OpenRC và máy đích. Bộ cài chỉ hỗ trợ Alpine Linux.
- Operation thiếu Python/tools: operation không cài dependency hoặc build binary. Dùng luồng setup chuẩn khi chủ động cài mới; với hệ đã cài, xác nhận `/var/lib/3proxy-setup/tools` còn đầy đủ và đúng phiên bản.
- Lỗi parse option: xem `sh setup-alpine.sh --help`; không dùng option cài đặt thay cho option operation. Khi gọi wrapper trong checkout, truyền tham số sau tên wrapper.

## Không phát hiện đúng IPv4/interface/prefix

Kiểm tra địa chỉ và default route; nếu có nhiều interface, chạy preflight với topology rõ:

```sh
sh setup-alpine.sh --operation preflight \
  --interface INTERFACE_NAME --ipv4 PUBLIC_IPV4 \
  --ipv6-prefix ROUTED_IPV6_PREFIX/64
```

Thay placeholder bằng thông tin nhà cung cấp xác nhận. IPv4 public phải thực sự gán trên host. Không ép một địa chỉ public quan sát qua NAT vào tham số `--ipv4` nếu nó không thuộc interface. IPv6 chính phải nằm trong /64 đã route.

## IPv6 chính chạy nhưng IPv6 bổ sung không chạy

Một /128 chạy được không chứng minh toàn /64 được route. Kiểm tra provider bật IPv6, route /64, gateway và chính sách anti-spoofing/NDP. Không thêm hàng nghìn địa chỉ để thử mò.

```sh
sh setup-alpine.sh --operation preflight --probe-ipv6
```

Probe thêm một địa chỉ tạm rồi gỡ; chỉ chạy khi chấp nhận thay đổi mạng tạm thời. Nếu canary thất bại, xử lý routing với nhà cung cấp trước khi áp pool. Không restart liên tục hoặc vô hiệu firewall toàn cục để vượt qua lỗi route.

## Listener có nhưng client ngoài không vào được

- Kiểm tra bind IPv4 và dải cổng trong status, cấu hình client và security group/firewall nhà cung cấp.
- Mặc định dải 2.000 proxy là TCP `10000:11999`. Nếu đổi count/start-port, dùng đúng dải hiện tại.
- Bộ cài không tự bật firewall inactive. Một rule UFW tồn tại không chứng minh cổng đã được provider mở.
- Kiểm tra có xác thực từ VPS và từ máy khác riêng biệt; local smoke không chứng minh Internet ingress.

## HTTP 407, 401/403/429 hoặc sai egress

- HTTP 407 ở request sai credential là kết quả mong đợi của kiểm tra âm tính; 407 ở request hợp lệ cần kiểm tra username/password và client.
- HTTP 401/403/429 từ đích khiến verify dừng, không retry. Giữ báo cáo, phân biệt lỗi upstream với từ chối xác thực proxy và không đổi danh tính để tiếp tục cùng bài thử.
- Egress không khớp pool: kiểm tra listener/cổng đã chọn, IPv6 được gán, prefix và báo cáo verify. HTTP 200 đơn thuần không đủ.
- Timeout có thể đến từ route, DNS, endpoint hoặc giới hạn tài nguyên. Kiểm tra mẫu nhỏ trước, chỉ tăng `--timeout` khi có nguyên nhân cụ thể.

```sh
sh setup-alpine.sh --operation verify --sample 3 --timeout 20 \
  --output /root/verify-diagnostic.json
```

Không chạy `--all` lặp lại không giới hạn. Verify tuần tự tối đa hai request/giây, không retry; báo cáo bị dừng không có nghĩa phần chưa kiểm đã pass.

## Đích bị chặn hoặc lỗi ứng dụng

Proxy chỉ cho phép đích trên cổng 80/443; ACL chặn địa chỉ private, loopback, link-local và mạng pool/server. Truy cập dashboard nội bộ hoặc cổng tùy ý không thuộc behavior được hỗ trợ. Không xóa ACL để chẩn đoán lỗi egress công khai. Xác nhận client dùng HTTP proxy, kể cả khi truy cập HTTPS qua CONNECT.

## Build/checksum/binary managed

- Đọc `/var/lib/3proxy-setup/build.log` khi compile thất bại; file có thể chưa có nếu lỗi xảy ra trước bước build.
- Checksum source không khớp: giữ bằng chứng, kiểm tra nguồn tải hoặc `SOURCE_ARCHIVE` nếu dùng archive local; không bỏ bước SHA-256.
- Binary hiện có không khớp manifest: bộ cài giữ nguyên binary đó. Kiểm tra nguồn gốc và trạng thái đang chạy trước khi quyết định thay; không xóa binary hoặc sửa manifest để ép pass.
- Thiếu RAM/disk/dependency: giải quyết nguyên nhân rồi chạy lại setup chuẩn. Dependency build tạm được dọn sau compile; dependency runtime được giữ.

## Restart hoặc reboot không phục hồi đủ pool

```sh
rc-service 3proxy-ipv6 status
rc-service 3proxy status
rc-update show default
ip -6 address show
sh setup-alpine.sh --operation status
```

Xem dịch vụ mạng IPv6 trước dịch vụ proxy: OpenRC phải phục hồi IPv6 và chờ sẵn sàng rồi mới chạy listener. Khi đã xác định cấu hình mạng hợp lệ, có thể chủ động restart bằng operation để chạy lại trình tự và smoke:

```sh
sh setup-alpine.sh --operation restart
```

Lệnh này ngắt kết nối đang mở. Nếu thay cấu hình gần nhất gây lỗi, liệt kê snapshot và chọn đường dẫn rõ theo [rollback](OPERATIONS.md#6-snapshot-và-rollback-có-lựa-chọn-rõ). Không dùng bản mới nhất một cách mặc định, không chạy rollback production chỉ để thử.

## Export hoặc credential thất bại

- File export đã có: chọn đường dẫn mới; operation không ghi đè hoặc tự upload.
- Credential bị reject: đối chiếu tập ký tự ở [SETUP](SETUP.md), dùng TTY hoặc environment, không password argv.
- Sau đổi credential: xác nhận file xuất mới, credential mới hoạt động và credential cũ bị từ chối riêng biệt. Không chạy lại bộ cài với count mặc định để chữa lỗi đổi mật khẩu nếu pool đang dùng kích thước khác.
- Snapshot có lỗi checksum hoặc manifest: giữ nguyên để điều tra, không chỉnh hash cho qua kiểm tra.

## Gói evidence khi báo lỗi

Ghi phiên bản bundle/commit, thời điểm, lệnh đã chạy đã bỏ secret, exit status, status đã redacted, report verify và phạm vi đã kiểm: local, VPS, client ngoài, reboot. Nêu số listener đã kiểm thực tế và điểm dừng. Chỉ gửi phần log liên quan đã kiểm tra, không gửi file export/snapshot/config chứa mật khẩu. Đếm listener, unit test và smoke mẫu là các boundary riêng, không thay thế nghiệm thu toàn bộ hoặc đo tải đồng thời.
