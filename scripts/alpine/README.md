# Module Alpine và quy trình phát triển

Entrypoint chuẩn để cài và vận hành là [`../../setup-alpine.sh`](../../setup-alpine.sh). Bundle nhúng các module Python bằng here-document văn bản; chỉ cần chuyển một file này sang VPS. Không chạy từng module như một bộ cài độc lập.

## Trách nhiệm

| File | Trách nhiệm |
| --- | --- |
| `bootstrap.sh` | Template bundle, kiểm tra môi trường, dependency, checksum source và build binary |
| `build_setup.py` | Sinh bundle xác định từ template và module |
| `config.py` | Validate pool/auth, render cấu hình, snapshot và restore |
| `ipv6_pool.py` | Quản lý địa chỉ IPv6 và phục hồi khi OpenRC khởi động |
| `manage.py` | Phát hiện mạng, canary routing, áp cấu hình, smoke và rollback |
| `ops.py` | Preflight, status, verify, restart, credentials, export và rollback |
| `../../tests/test_*.py` | Kiểm thử local với fixture, không thay cho nghiệm thu VPS |

Các shell wrapper trong `scripts/` phục vụ thao tác từ checkout. `scripts/install.sh` chuyển tiếp sang bundle ở root; `scripts/build.sh` sinh bundle; `scripts/test.sh` chạy bộ kiểm thử local. Hướng dẫn người vận hành ở [SETUP](../../docs/SETUP.md), [OPERATIONS](../../docs/OPERATIONS.md) và [TROUBLESHOOTING](../../docs/TROUBLESHOOTING.md).

## Build và kiểm tra

Chạy từ thư mục root của repo:

```sh
sh scripts/build.sh
sh scripts/test.sh
sh setup-alpine.sh --help
```

Có thể chạy trực tiếp những bước cơ bản để chẩn đoán:

```sh
python3 scripts/alpine/build_setup.py
PYTHONPATH=scripts/alpine python3 -m unittest discover -s tests -p 'test_*.py' -v
sh -n setup-alpine.sh
```

Kiểm tra nội dung bundle mà không cài gói hoặc sửa dịch vụ:

```sh
tools_dir=$(mktemp -d)
sh setup-alpine.sh --extract-only "$tools_dir"
python3 -m py_compile "$tools_dir"/*.py
```

`--help` và `--extract-only` không đòi root hay Alpine. Chọn thư mục tạm mới cho extraction. Source archive 3proxy 1.0.3 được pin SHA-256 trong `bootstrap.sh`; giữ kiểm tra checksum khi sửa luồng build. Sau khi sửa module/template, build lại bundle và xem diff trước bàn giao.

## Boundary kiểm chứng

Tách rõ unit test, cú pháp shell, độ khớp bundle và kiểm thử VPS. Operations được thiết kế dùng module đã cài tại `/var/lib/3proxy-setup/tools`; wrapper trong checkout có thể dùng module repo khi chưa có bộ tools cài đặt. Operation không nâng cấp module/binary hoặc cài package.

Trên VPS mới, nghiệm thu riêng: route IPv6 bổ sung, đúng egress mỗi cổng, từ chối sai mật khẩu, kết nối từ máy khác và phục hồi sau reboot. Không coi fixture local, trạng thái OpenRC hoặc số listener là bằng chứng thay thế. Các lệnh chạm mạng/dịch vụ trong tài liệu chỉ chạy khi người vận hành chủ động gọi, không phải một phần của kiểm thử local repo.
