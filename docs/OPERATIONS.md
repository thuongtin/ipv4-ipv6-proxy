# Vận hành pool Alpine

Các ví dụ dưới chạy với quyền root trên VPS đã cài. Giữ `setup-alpine.sh` làm entrypoint chuẩn:

```sh
sh setup-alpine.sh --operation status
```

Khi dùng checkout repo, có thể gọi wrapper tương ứng: `sh scripts/status.sh`, `sh scripts/preflight.sh`, `sh scripts/verify.sh`, `sh scripts/restart.sh`, `sh scripts/credentials.sh`, `sh scripts/export.sh`, `sh scripts/rollback.sh`. Wrapper ưu tiên tools đã cài tại `/var/lib/3proxy-setup/tools`, fallback sang module trong repo. Không trộn bản bundle/module khác phiên bản trong cùng một đợt vận hành.

Operation không chạy `apk`, không build binary và không tự nâng cấp tools. Nếu chưa có tools đã cài, bundle giải nén module vào thư mục tạm và dọn sau khi chạy, không cập nhật tools thường trú. Nếu thiếu Python hoặc dependency runtime, xử lý nguyên nhân trước, không coi operation là lệnh bootstrap.

## 1. Preflight và status

```sh
sh setup-alpine.sh --operation preflight
sh setup-alpine.sh --operation status
sh setup-alpine.sh --operation status --json
```

Preflight mặc định kiểm tra chỉ đọc. Có thể chỉ định topology để tránh chọn nhầm mạng:

```sh
sh setup-alpine.sh --operation preflight \
  --ipv4 PUBLIC_IPV4 --ipv6-prefix ROUTED_IPV6_PREFIX/64 \
  --interface INTERFACE_NAME
```

Thêm `--probe-ipv6` chỉ khi muốn chủ động thêm tạm một IPv6, kiểm tra egress và gỡ nó sau kiểm tra. Preflight không thay cấu hình pool.

Status luôn xuất JSON và che credential trong đầu ra; `--json` được chấp nhận để câu lệnh thể hiện rõ mục đích. Dùng JSON cho thu thập trạng thái, nhưng vẫn xem host, port và topology là dữ liệu vận hành nội bộ. Status không thay thế verify egress.

## 2. Verify egress và xác thực

```sh
sh setup-alpine.sh --operation verify --sample 3
sh setup-alpine.sh --operation verify --sample 10 --timeout 20 \
  --output /root/verify-sample.json
sh setup-alpine.sh --operation verify --all --timeout 20 \
  --output /root/verify-all.json
```

Chọn `--sample N` hoặc `--all`, không dùng cả hai. Lệnh gọi HTTPS IPv6 echo tuần tự, tối đa hai request mỗi giây, không retry; đồng thời thực hiện kiểm tra âm tính xác thực. Phân biệt HTTP 407 từ proxy trong bài kiểm tra credential sai với HTTP 401/403/429 từ đích: các mã đích đó khiến phép kiểm tra dừng, không retry hay đổi danh tính để tiếp tục.

So sánh egress thực tế với IPv6 của từng listener trong pool, không chỉ kiểm tra status 200. Verify toàn bộ 2.000 listener mất thời gian và phát sinh request thật; timeout hoặc lỗi đầu tiên có thể làm kết quả không bao phủ hết pool. Đọc kết quả và exit status trước khi coi là pass; không suy ra các listener chưa kiểm đều hoạt động.

Verify chạy trên VPS chưa chứng minh inbound từ Internet. Dùng máy client khác để kiểm tra một số cổng đầu/giữa/cuối qua firewall của nhà cung cấp. Cấu hình client bằng file credential riêng, tránh nhúng mật khẩu vào command line hoặc ảnh chụp.

## 3. Restart có kiểm tra

```sh
sh setup-alpine.sh --operation restart
sh setup-alpine.sh --operation status
sh setup-alpine.sh --operation verify --sample 3
```

Restart phục hồi lớp địa chỉ mạng/dịch vụ IPv6 trước, sau đó restart proxy và smoke. Thao tác gây gián đoạn kết nối hiện tại, không phải lệnh health check chỉ đọc. Nếu chỉ cần quan sát, dùng status/preflight. Không chạy restart lặp lại khi chưa chẩn đoán lần thất bại trước.

## 4. Đổi credential chung

```sh
sh setup-alpine.sh --operation credentials
```

Nhập credential mới qua TTY, hoặc cung cấp `PROXY_USERNAME` và `PROXY_PASSWORD` từ environment của secret manager. Không có tùy chọn password argv. Bộ ký tự hợp lệ theo [SETUP](SETUP.md).

Thao tác giữ nguyên số proxy, cổng, interface và địa chỉ IP; cập nhật cấu hình cùng các file xuất có credential. Nó có thể restart dịch vụ. Sau thay đổi:

1. Kiểm tra credential mới qua verify.
2. Kiểm tra credential cũ bị từ chối từ client bằng file cấu hình riêng.
3. Cập nhật client cần dùng và bảo quản bản xuất mới.

Bài kiểm tra âm tính tự động không thay thế bằng chứng credential cũ cụ thể đã bị thu hồi, nếu chưa có phép thử đó. Không gửi mật khẩu qua log, ticket hoặc chat.

## 5. Xuất pool

Chọn tên file đích mới, đường dẫn tuyệt đối:

```sh
sh setup-alpine.sh --operation export --format txt --output /root/proxy-export.txt
sh setup-alpine.sh --operation export --format csv --output /root/proxy-export.csv
sh setup-alpine.sh --operation export --format json --output /root/proxy-export.json
```

Export tạo file `0600`, không ghi đè file đã tồn tại và không upload ra dịch vụ ngoài. TXT có dạng `host:port:username:password`; CSV/JSON dùng để xử lý có cấu trúc, gồm IPv6 egress kỳ vọng. File chứa secret dạng rõ: chọn nơi lưu chỉ root truy cập, chuyển qua kênh riêng và không commit vào repo. Khi file đích đã có, chọn một tên mới thay vì xóa tự động.

## 6. Snapshot và rollback có lựa chọn rõ

```sh
sh setup-alpine.sh --operation rollback --list
```

Đọc danh sách, xác nhận snapshot ứng với trạng thái muốn phục hồi, rồi truyền đường dẫn cụ thể:

```sh
sh setup-alpine.sh --operation rollback \
  --snapshot /var/lib/3proxy-setup/snapshots/SNAPSHOT_NAME
```

Thay `SNAPSHOT_NAME` bằng tên thật vừa liệt kê. Không có rollback ngầm sang `latest`. `/var/lib/3proxy-setup/latest-snapshot.txt` chỉ là tham chiếu để xem xét, không phải quyết định phục hồi tự động. Bundle còn hỗ trợ dạng manager trực tiếp:

```sh
sh setup-alpine.sh --rollback /var/lib/3proxy-setup/snapshots/SNAPSHOT_NAME
```

Snapshot được bảo vệ bằng thư mục `0700`, chứa hash, mode, ownership và trạng thái dịch vụ/runlevel. Rollback xác thực toàn bộ manifest, checksum và runtime metadata trước khi dừng dịch vụ; snapshot hỏng bị từ chối trước bước thay đổi. Rollback phục hồi file managed, exports, rules firewall và trạng thái dịch vụ đã ghi, chỉ gỡ IPv6 mới thuộc phạm vi managed. Nó giữ IPv6 chính, không xóa VPS, không gỡ runtime dependency hoặc tài khoản dịch vụ.

Snapshot có credential: bảo quản như secret. Rollback cũng có thể khôi phục credential cũ và gây gián đoạn kết nối. Sau rollback chạy status, verify và kiểm tra từ client; đọc exit status, không chỉ dựa vào việc lệnh đã kết thúc.

## 7. Nghiệm thu reboot và tài nguyên

Sau khi smoke từ VPS và client đều pass, chủ động sắp xếp reboot khi được phép. Bộ cài không yêu cầu reboot chỉ để cài xong. Sau khi máy trở lại:

```sh
rc-service 3proxy-ipv6 status
rc-service 3proxy status
sh setup-alpine.sh --operation status --json
sh setup-alpine.sh --operation verify --all --output /root/verify-after-reboot.json
```

Xác nhận địa chỉ IPv6 managed được khôi phục, đủ listener, credential đúng/sai và egress khớp từng listener. Giữ thời điểm, exit status và báo cáo làm evidence. Kiểm tra 2.000 listener hoạt động không phải benchmark 2.000 kết nối đồng thời.

`maxconn=65535` khớp FD ceiling của process và `nproc` unlimited; RAM, thread, kernel, socket và giới hạn nhà cung cấp vẫn quyết định khả năng tải. Theo dõi tài nguyên thực trước khi tăng tải, không nới destination ACL để chữa lỗi thiếu tài nguyên.
