# Alpine Linux용 IPv4-to-IPv6 프록시

[Tiếng Việt](README.md) | [简体中文](README.zh-CN.md) | [한국어](README.ko.md) | [日本語](README.ja.md) | [English](README.en.md)

이 설치 프로그램은 **Alpine Linux만 지원**하며 3proxy 1.0.3과 OpenRC를 사용합니다. 하나의 공인 IPv4 주소에서 여러 포트로 HTTP 프록시 연결을 받고, 각 포트는 서로 다른 IPv6 주소를 외부 접속에 사용합니다. 이 주소들은 공급자가 VPS로 라우팅하는 /64 대역에서 할당됩니다. 전체 프록시 풀은 하나의 사용자 이름과 비밀번호를 공유합니다.

## Vultr 가입 및 체험 크레딧 받기

**[Vultr 가입 링크](https://www.vultr.com/?ref=8809561)**로 계정을 만든 뒤, VPS를 생성하기 전에 적용 가능한 프로모션을 활성화하세요.

- **신규 사용자 혜택:** Vultr는 대상 서비스에서 사용할 수 있는 **미화 300달러 체험 크레딧**의 프로모션 코드 **`FLY300VULTR`**를 안내하고 있습니다. 서비스 이용 요금에 사용하는 크레딧이며 현금이 아닙니다.
- **받는 방법:** 유효한 신용카드 또는 PayPal 계정을 연결하고 **Vultr Console → Gift Code**에서 코드를 입력하세요. VPS를 배포하기 전에 코드가 승인되었는지 확인하고, 지급된 크레딧과 유효기간을 확인하세요.
- **조건:** 자격을 갖춘 신규 고객에게만 제공되며, 1인당 하나의 프로모션만 적용됩니다. 지정된 상품에만 사용할 수 있고 다른 혜택과 중복 적용할 수 없습니다. 크레딧에는 유효기간이 있으므로 활성화 시 표시되는 약관을 확인하세요.

위 링크는 프로젝트를 후원하는 제휴 링크입니다. **크레딧 지급은 Vultr의 프로모션 코드 승인 여부에 달려 있으며, 제휴 링크를 클릭하는 것만으로 지급이 보장되지는 않습니다.**

확인일: **2026년 11월 16일**. 출처: [Vultr 공식 프로모션](https://www.vultr.com/coupons/), [프로모션 코드 사용 안내](https://docs.vultr.com/platform/billing/faq). 혜택은 변경될 수 있습니다. 이 설치 프로그램에 사용할 VPS를 생성할 때는 Alpine Linux를 선택하고 IPv6를 활성화하세요.

## 설치

필수 조건: Alpine의 root 권한, VPS에 직접 할당된 공인 IPv4 주소, 정상적으로 작동하는 IPv6 연결, VPS로 라우팅되는 IPv6 /64 대역. 설치 전에 공급자 제어판에서 IPv6를 활성화하세요.

[`setup-alpine.sh`](setup-alpine.sh)를 VPS로 복사하고 스크립트를 검토한 다음 실행하세요.

```sh
sh setup-alpine.sh --count 2000
```

스크립트는 TTY를 통해 사용자 이름과 비밀번호를 입력받습니다. 실제 계정 정보나 서버 IP 주소는 내장되어 있지 않습니다. 기본값은 프록시 2,000개이며 포트 범위는 10000부터 11999까지입니다. 설치 후 운영 명령에도 사용하므로 번들 스크립트를 보관하세요.

아래 상세 문서는 베트남어로 작성되어 있습니다.

- [상세 설치 안내](docs/SETUP.md)
- [운영, 검증, 인증 정보 변경, 풀 내보내기 및 롤백](docs/OPERATIONS.md)
- [문제 해결](docs/TROUBLESHOOTING.md)
- [모듈 구조와 로컬 테스트](scripts/alpine/README.md)

## 자주 사용하는 명령

```sh
sh setup-alpine.sh --operation status
sh setup-alpine.sh --operation verify --sample 3
sh setup-alpine.sh --operation export --format csv --output /root/proxy-export.csv
```

운영 명령은 `apk`를 실행하거나 3proxy를 다시 컴파일하지 않습니다. 내보낸 파일에는 비밀번호가 포함되며, `0600` 권한으로 생성되고 자동으로 업로드되지 않습니다.

## 실제 제약 사항

- 목적지 포트가 80/443인 HTTP/HTTPS 연결만 허용합니다. 사설 주소, 링크 로컬 주소, 프록시 풀 또는 서버 네트워크에 속하는 목적지는 차단합니다.
- `maxconn=65535`는 프로세스의 파일 디스크립터 한도와 같으며, 동시 연결 65,535개를 보장한다는 의미는 아닙니다. `nproc`는 무제한입니다. `connlim` 할당량이나 UFW의 512개 연결 제한은 설정하지 않습니다.
- 로컬 테스트는 각 VPS의 라우팅, 인증, 다른 컴퓨터에서의 연결, 재부팅 후 동작 확인을 대체하지 않습니다. 리스닝 포트 수가 맞는 것만으로 처리량이 입증되지는 않습니다.
