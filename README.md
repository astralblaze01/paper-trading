# VANTAGE

한국·미국 주식과 채권·금 ETF를 실제 시세로 연습 거래하는 **웹 기반 모의투자 서비스**입니다.
모든 자산은 가상이며, 실제 돈이나 증권은 거래되지 않습니다. 시세 공급자 API는 조회 전용으로만 사용합니다.

- 백엔드: Python 3.12 · FastAPI · SQLAlchemy · PostgreSQL 17 · Redis 7
- 프론트엔드: 빌드 과정 없는 HTML / CSS / JavaScript (`app/static`)
- 배포: Docker Compose + nginx (선택적으로 Let's Encrypt HTTPS)

## 주요 기능

### 거래
- **USD·KRW 이중 지갑**: 가입 시 USD 지갑에 초기 자금(기본 $100,000)이 지급됩니다. 미국 종목은 USD, 국내 종목은 KRW로 결제합니다.
- **모의 시장가 주문**: 최신 시세가 있으면 즉시 체결합니다. 시세가 오래됐거나 장이 닫혀 있으면 대기 주문으로 넘기지 않고 거절합니다.
  - 한국: 정규장에서만 주문 가능
  - 미국: 프리장·정규장·애프터장에서 주문 가능
- **빠른 수량 선택**: 최대 / 50% / 25% / 10% / 5% (수수료·세금 반영 후 서버에서 재계산)
- **가상 환전**: USD ↔ KRW, ECB 일별 기준환율에 설정된 수수료·스프레드를 적용합니다. 실행 전 예상 금액을 확인할 수 있습니다.
- **거래 대상**: 미국·한국 주식/ETF, 한국·미국 채권 ETF, 금 ETF (개별 채권과 금 현물은 지원하지 않습니다)
- 모든 금액 계산은 `Decimal`로 처리하며, 주문·환전은 요청 ID로 중복 실행을 막습니다.

### 시장 정보
- **시장 탐색**: 자산군별(국내주식, 해외주식, 국내/해외 채권 ETF, 금 ETF) 거래량·거래대금·급상승·급하락 순위와 서비스 내 인기 종목(최근 1·24시간)
- **종목 검색**: 종목명(한글 포함) 또는 코드. 국내 종목은 6자리 코드(`KR:005930` 형식)로도 조회됩니다.
- **종목 상세**: 현재가, 장 상태, 1D / 1W / 3M / 1Y / 5Y / ALL 가격 차트, 기간 수익률, 회사 기본 정보, 배당수익률
- **관심종목**: 최대 50개

### 계좌와 커뮤니티
- **포트폴리오**: 총 평가금액, 누적 수익률, 실현 손익, 자산 비중, 보유 종목
- **표시 통화 선택**: 거래 통화 / 원화 / 달러. 금액 표시는 이 설정을 따르고, 현금 지갑은 원래 통화로 표시합니다.
- **랭킹**: 일반 사용자의 총 자산을 USD로 환산해 10초 단위로 갱신합니다(관리자 제외). 사용자를 누르면 공개 프로필과 투자 현황이 열립니다.
- **주간 순위**: 매주 정해진 시각(기본 토요일 09:00, 한국 시간)에 주간 수익률 순위를 사이트에 게시합니다.
- **프로필**: 자기소개(160자), 프로필 사진(JPG/PNG/WEBP, 최대 5MB, 원형 영역 자르기), 가입 일수 표시
- **거래내역·환전내역** 조회
- **사이트 공지**: 새로고침 없이 10초 주기로 표시됩니다.
- **회원 탈퇴**: 비밀번호 확인 후 계정과 모든 기록을 삭제합니다.

### 관리자
- 서비스 상태(DB·Redis·시세 공급자)와 사용자·거래 통계 확인
- 공지 등록/해제 (서버 점검 예고, 업데이트 안내, 일반 공지 템플릿)
- 새 계좌 초기 지급액 변경
- 사용자 검색, 관리자 메모, 계정 정지/활성화
- 지원금 지급(개별 또는 전체 일반 사용자), 수익률 기준 재설정, 가입 직후 상태로 초기화, 계정 영구 삭제
- 모든 관리자 작업은 감사 기록에 남습니다.

## 구성

`compose.yaml`은 다음 서비스를 실행합니다.

| 서비스 | 역할 |
| --- | --- |
| `nginx` | 외부 진입점. 보안 헤더, 요청 속도 제한, `/internal/` 차단 |
| `web` | FastAPI 앱 (`app.main:app`). 시작 시 테이블 생성과 마이그레이션 수행 |
| `market-worker` | 요청된 종목 시세를 수집해 Redis에 캐시, 국내·미국 종목 마스터를 하루 한 번 갱신 |
| `worker` | 1분마다 내부 작업 호출 (이전 주문 처리, 주간 순위 게시) |
| `db` | PostgreSQL 17 (`pgdata` 볼륨) |
| `redis` | Redis 7 시세 캐시 (`redis_data` 볼륨) |

### 외부 데이터 공급자

| 공급자 | 용도 | 설정 |
| --- | --- | --- |
| [Finnhub](https://finnhub.io) | 미국 시세, 검색, 차트, 장 상태, 기업 정보·배당 | `FINNHUB_API_KEY` |
| [한국투자증권 Open API](https://apiportal.koreainvestment.com) | 국내 시세·차트·순위·휴장일·기업 정보, 미국 순위·차트 보조 | `KIS_APP_KEY`, `KIS_APP_SECRET` (계좌번호 불필요) |
| [Alpha Vantage](https://www.alphavantage.co) | KIS를 쓸 수 없을 때 미국 시장 순위 대체 | `ALPHAVANTAGE_API_KEY` (선택) |
| [Frankfurter](https://frankfurter.dev) (ECB) | USD/KRW 일별 기준환율 | 키 불필요 |

키가 없는 공급자에 해당하는 기능은 안내 메시지와 함께 비활성 상태로 표시됩니다.

## 빠른 시작

요구 사항: Docker, Docker Compose v2

```bash
# 1. 환경 파일 만들기
cp .env.example .env

# 2. .env에서 최소한 아래 두 값을 바꾸고, 사용할 시세 API 키를 입력
#    POSTGRES_PASSWORD=<긴 무작위 문자열>
#    SESSION_SECRET=<32자 이상 무작위 문자열>
#    예: python3 -c "import secrets; print(secrets.token_urlsafe(48))"

# 3. 실행
docker compose up -d --build

# 4. 상태 확인
docker compose ps
curl http://127.0.0.1:8080/health
```

브라우저에서 `http://127.0.0.1:8080`에 접속해 회원가입 후 로그인합니다.
같은 네트워크의 다른 기기에서 접속하려면 `.env`의 `BIND_ADDRESS`를 `0.0.0.0` 또는 서버의 LAN IP로 바꿉니다.

### 관리자 지정

관리자는 이미 가입한 계정을 명령으로 승격해서 만듭니다.

```bash
docker compose exec web python -m app.admin_cli <아이디>
```

다시 로그인하면 메뉴에 **관리자** 탭이 나타납니다.

## 환경 설정

모든 값은 `.env`에서 설정합니다. 수수료와 세금은 bp 단위입니다(10bp = 0.10%).

| 변수 | 기본값 | 설명 |
| --- | --- | --- |
| `POSTGRES_PASSWORD` | (필수) | 데이터베이스 비밀번호 |
| `SESSION_SECRET` | (필수) | 세션 서명 키, 32자 이상 |
| `BIND_ADDRESS` / `HTTP_PORT` | `127.0.0.1` / `8080` | nginx가 여는 주소와 포트 |
| `COOKIE_SECURE` | `false` | HTTPS 전용 쿠키 사용 여부 |
| `FINNHUB_API_KEY` | | 미국 시세 |
| `KIS_APP_KEY` / `KIS_APP_SECRET` | | 한국투자증권 시세 |
| `KOREA_MARKET_PROVIDER` | `kis` | `kis` 또는 `disabled` |
| `KOREA_MARKET_API_KEY` / `KOREA_MARKET_API_SECRET` | | 지정하면 KIS 키 대신 사용 |
| `ALPHAVANTAGE_API_KEY` | | 미국 순위 대체 공급자 |
| `INITIAL_USD` | `100000` | 새 계좌 초기 지급액(USD). 관리자 화면에서 바꾼 값이 우선합니다. |
| `FX_FEE_BPS` / `FX_SPREAD_BPS` | `10` / `5` | 환전 수수료 / 스프레드 |
| `US_BUY_FEE_BPS` / `US_SELL_FEE_BPS` | `0` / `0` | 미국 종목 매수·매도 수수료 |
| `KR_BUY_FEE_BPS` / `KR_SELL_FEE_BPS` | `0` / `0` | 국내 종목 매수·매도 수수료 |
| `KR_SELL_TAX_BPS` | `0` | 국내 종목 매도 세금 |
| `QUOTE_TTL` | `15` | 시세 캐시·갱신 주기(초) |
| `MAX_QUOTE_AGE` | `900` | 국내 시세의 주문 허용 최대 나이(초) |
| `US_MAX_QUOTE_AGE` | `1800` | 미국 시세의 주문 허용 최대 나이(초) |
| `MARKET_CALLS_PER_MINUTE` | `50` | Finnhub 분당 호출 한도 |
| `WEEKLY_ENABLED` | `true` | 주간 순위 게시 사용 여부 |
| `WEEKLY_DAY` / `WEEKLY_HOUR` | `5` / `9` | 게시 요일(월=0 … 일=6)과 시각, 한국 시간 |
| `DOMAIN` / `LETSENCRYPT_EMAIL` | | HTTPS 배포용 도메인과 인증서 연락처 |
| `PUBLIC_HTTP_PORT` / `HTTPS_PORT` | `80` / `443` | HTTPS 배포 시 공개 포트 |

설정을 바꾼 뒤에는 `docker compose up -d`로 컨테이너를 다시 만듭니다.

## HTTPS 배포 (선택)

기본 구성은 HTTP만 제공합니다. 공개 도메인이 있고 외부 TCP 80/443이 이 서버로 전달된다면 `scripts/tls.py`로 Let's Encrypt 인증서를 발급할 수 있습니다. `.env`에 `DOMAIN`을 먼저 설정하세요.

```bash
python3 scripts/tls.py check                                   # DNS 확인
python3 scripts/tls.py bootstrap --confirmed-public-routing    # ACME 검증용 임시 nginx
python3 scripts/tls.py issue     --confirmed-public-routing    # 인증서 발급
python3 scripts/tls.py enable    --confirmed-public-routing    # HTTPS 구성으로 전환 (자동 갱신 포함)
python3 scripts/tls.py hsts      --confirmed-public-routing    # HTTPS 동작 확인 후 HSTS 적용
```

HTTPS 구성은 `docker compose -f compose.yaml -f compose.https.yaml ...`로 실행하며, 이때 `COOKIE_SECURE`는 자동으로 `true`가 됩니다.

## 테스트

서비스 테스트는 실행 중인 `db` 컨테이너에 별도 데이터베이스(`paper_test`)를 만들어 pytest로 실행합니다. 운영 데이터는 건드리지 않습니다.

```bash
docker compose exec web python tests/run.py
```

브라우저 스모크 테스트(Playwright, Chromium)는 고정 시세를 쓰는 테스트 전용 앱과 DB(`paper_browser_test`)로 실행되며, 화면 캡처를 `artifacts/`에 저장합니다.

```bash
docker compose -f compose.yaml -f compose.browser.yaml run --rm --build browser
```

공개 전 저장소 파일에 `.env`의 비밀 값이나 개인 파일이 섞였는지 검사할 수 있습니다.

```bash
python3 scripts/check_publication.py
```

## 프로젝트 구조

```
app/
  main.py            앱 생성, 인증·세션, 주문, 포트폴리오, 랭킹
  routes.py          시장 탐색, 환전, 관심종목, 차트, 관리자 API
  accounts.py        프로필·프로필 사진, 회원 탈퇴
  admin_ops.py       관리자 사용자 관리, 감사 기록
  notices.py         사이트 공지
  trading.py         주문 검증과 체결
  fx.py, money.py    환전, 수수료·세금 계산
  portfolio.py       평가금액과 수익률
  weekly.py          주간 순위 게시
  market.py          Finnhub 어댑터
  multi_market.py    시장별 공급자 통합, KIS·환율 어댑터
  providers.py       차트·순위·장 상태
  kr_symbols.py, us_symbols.py   종목 마스터 검색
  instruments.py     기본 종목 목록(채권·금 ETF 포함)
  db.py, migrations.py           테이블 정의와 마이그레이션
  worker.py, market_worker.py    백그라운드 작업
  admin_cli.py       관리자 승격 명령
  static/            웹 화면 (index.html, app.js, portal.js, profile.js, style.css)
nginx/               nginx 설정 (HTTP, HTTPS 템플릿)
scripts/             HTTPS 설정, 공개 전 비밀 값 검사
tests/               pytest 테스트, 브라우저 스모크 테스트
```

## 참고 사항

- 가격 차트와 시세는 공급자가 제공하는 범위와 요금제 권한에 따라 지연되거나 비어 있을 수 있습니다.
- 환율은 실시간이 아닌 ECB 일별 기준환율입니다.
- 배당과 주식 분할은 계좌에 자동 반영되지 않습니다.
- 한국 장 상태는 KIS 휴장일 정보와 표준 시간표로 판단하며, 특별 개장 시간은 반영하지 않습니다.
