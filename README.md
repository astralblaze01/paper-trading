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
  - 미국: 데이마켓·프리장·정규장·애프터장에서 주문 가능. 단, 현재 세션의 체결가가 확인된 경우에만 체결합니다([미국 세션과 시세](#미국-세션과-시세)).
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
| `market-worker` | 요청된 종목 시세를 REST로 수집해 Redis에 캐시, 국내·미국 종목 마스터를 하루 한 번 갱신 |
| `us-trade-stream` | KIS WebSocket 하나로 미국 실시간 체결을 받아 Redis에 저장 (앱키당 세션 1개라 복제 금지) |
| `worker` | 1분마다 내부 작업 호출 (이전 주문 처리, 주간 순위 게시) |
| `db` | PostgreSQL 17 (`pgdata` 볼륨) |
| `redis` | Redis 7 시세 캐시 (`redis_data` 볼륨) |

### 외부 데이터 공급자

| 공급자 | 용도 | 설정 |
| --- | --- | --- |
| [Finnhub](https://finnhub.io) | 미국 시세, 검색, 차트, 장 상태, 기업 정보·배당 | `FINNHUB_API_KEY` |
| [한국투자증권 Open API](https://apiportal.koreainvestment.com) | 국내 시세·차트·순위·휴장일·기업 정보, 미국 시간외·데이마켓 시세와 실시간 체결, 미국 순위·차트 보조 | `KIS_APP_KEY`, `KIS_APP_SECRET` (계좌번호 불필요) |
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
| `US_MAX_QUOTE_AGE` | `1800` | 미국 REST 시세의 주문 허용 최대 나이(초) |
| `US_TRADE_STREAM_ENABLED` | `true` | 미국 실시간 체결 스트림 사용 여부 |
| `US_STREAM_MAX_AGE` | `10` | 스트림 워커 heartbeat가 이 시간(초)보다 오래되면 스트림 가격을 실시간으로 보지 않음 |
| `US_STREAM_MAX_SUBSCRIPTIONS` | `3` | KIS WebSocket 동시 구독 수 (현재 키 실측 한도 3) |
| `US_STREAM_RECONNECT_MAX_SECONDS` | `60` | 재연결 지수 백오프 최대 간격(초) |
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
  us_session.py      미국 세션 판정 (America/New_York 기준)
  us_quotes.py       세션별 미국 REST 시세 소스
  us_trade_stream.py KIS WebSocket 체결 스트림 워커
  quote_policy.py    시세의 세션·실시간·주문 가능 여부 판정
  kr_symbols.py, us_symbols.py   종목 마스터 검색
  instruments.py     기본 종목 목록(채권·금 ETF 포함)
  db.py, migrations.py           테이블 정의와 마이그레이션
  worker.py, market_worker.py    백그라운드 작업
  admin_cli.py       관리자 승격 명령
  static/            웹 화면 (index.html, app.js, portal.js, profile.js, style.css)
nginx/               nginx 설정 (HTTP, HTTPS 템플릿)
scripts/             HTTPS 설정, 공개 전 비밀 값 검사, 미국 세션별 시세 진단(check_us_day_market.py)
tests/               pytest 테스트, 브라우저 스모크 테스트
```

## 참고 사항

- 가격 차트와 시세는 공급자가 제공하는 범위와 요금제 권한에 따라 지연되거나 비어 있을 수 있습니다.
- 환율은 실시간이 아닌 ECB 일별 기준환율입니다.
- 배당과 주식 분할은 계좌에 자동 반영되지 않습니다.
- 한국 장 상태는 KIS 휴장일 정보와 표준 시간표로 판단하며, 특별 개장 시간은 반영하지 않습니다.

## 미국 세션과 시세

미국 주식은 현재 시장 세션과 사용 가능한 시세 source에 따라 실시간 stream 또는 REST fallback을 사용합니다.
데이마켓·프리마켓·애프터마켓에서는 해당 세션의 체결가가 검증된 경우에만 주문이 가능합니다.
정규장 마지막 가격이나 이전 세션 가격을 다른 세션의 체결가로 사용하지 않습니다.

### 세션

세션은 `America/New_York` 시계로 판정하므로 서머타임 전환을 자동으로 따릅니다(한국 시각 숫자는 쓰지 않습니다).
휴장일과 조기 폐장은 Finnhub `market-status`로 반영합니다.

| 세션 | 화면 표시 | 뉴욕 시각 |
| --- | --- | --- |
| `overnight` | 데이마켓 | 20:00–04:00 (일–목요일 밤) |
| `pre_market` | 프리장 | 04:00–09:30 |
| `regular` | 정규장 | 09:30–16:00 |
| `after_hours` | 애프터장 | 16:00–20:00 |
| `closed` | 장마감 / 휴장 | 주말·휴장일 |

시계는 세션의 이름만 정합니다. KIS가 실제로 데이마켓을 서비스하는지는 시계가 아니라 받은 체결 데이터로 판단합니다.
KIS 분봉 응답에는 KIS의 세션 범위(데이마켓 20:00–04:00 ET)도 함께 옵니다.

### 세션별 가격 source

| 세션 | 1순위 | 2순위 | 없으면 |
| --- | --- | --- | --- |
| 데이마켓 | KIS WebSocket `R`+`BAQ/BAY/BAA` (`overnight_stream`) | KIS 데이마켓 1분봉 (`overnight_rest`) | 주문 불가 |
| 프리장·애프터장 | KIS WebSocket `D`+`NAS/NYS/AMS` (`extended_stream`) | KIS 주 거래소 1분봉 (`extended_rest`) | 주문 불가 |
| 정규장 | KIS WebSocket (`trade_stream`) | Finnhub `/quote`, 실패 시 KIS 1분봉 (`rest`) | 주문 불가 |

2026-09-24 실측(`scripts/check_us_day_market.py`) 결과는 다음과 같습니다.
- KIS 현재가 API는 체결 시각이 없습니다. 게다가 데이마켓 시간에 `NAS`로 조회하면 정규장 종가를 돌려줍니다. 그래서 전일 대비 계산의 기준가로만 씁니다.
- Finnhub `/quote`는 장 마감 뒤에도 16:00 종가를 줍니다. 따라서 정규장에서만 체결가로 인정합니다.

모든 시세에는 체결 시각과 source가 검증된 세션(`valid_sessions`)이 붙습니다. 주문할 때 서버는 시세를 다시 조회해서 다음을 모두 확인합니다.
1. 체결 시각이 속한 세션이 현재 세션과 같은지
2. 그 source가 현재 세션용으로 검증됐는지
3. 오래되지 않았는지

하나라도 어긋나면 `현재 미국 데이마켓 체결 시세를 확인할 수 없어 주문할 수 없습니다.`처럼 세션을 밝혀 거절합니다.
데이마켓 시세가 없는 종목은 시장이 열려 있어도 해당 종목만 `session_tradeable=false`가 됩니다.

### 실시간 여부 판정 (freshness)

- **스트림 가격**은 두 조건을 모두 만족하는 동안 `realtime`입니다.
  - 스트림 워커의 heartbeat가 `US_STREAM_MAX_AGE`(10초) 이내입니다.
  - 그 종목이 같은 연결에서 계속 구독 중입니다.
  - 이때 마지막 체결 자체는 오래됐어도 됩니다. 거래가 드문 종목은 체결 간격이 길고, 연결이 살아 있는 한 더 새로운 체결은 없기 때문입니다.
  - 연결이 끊기면 그 가격은 REST와 같은 기준으로 판정합니다.
- **REST 가격**은 체결 시각이 `US_MAX_QUOTE_AGE`(1800초) 이내일 때만 주문에 사용합니다. 화면에는 `보조 시세`로 표시합니다.

### WebSocket 제한

- 현재 키는 세션당 구독 3건만 허용합니다. 4번째부터 `OPSP0008 MAX SUBSCRIBE OVER`가 나옵니다.
- 같은 앱키로 두 번째 연결을 하면 `ALREADY IN USE appkey`로 거절됩니다.
- 그래서 `us-trade-stream` 하나만 연결합니다. Redis 리더 락으로 중복 실행도 막습니다.
- 슬롯은 지금 상세 화면을 보거나 주문하는 종목(`market:stream:interest`, 최근 150초) 순으로 배정합니다. 한 번 배정한 종목은 최소 30초 유지합니다.
- 나머지 종목은 market-worker의 REST 시세를 씁니다.
- 연결이 끊기면 1초부터 시작하는 지수 백오프(최대 `US_STREAM_RECONNECT_MAX_SECONDS`)로 재연결합니다. 그동안 REST로 대체합니다.

Redis 키:

| 키 | 내용 |
| --- | --- |
| `market:price:{symbol}` | 화면·주문이 읽는 최신 시세 스냅샷 (REST 또는 스트림) |
| `market:trade:{symbol}` | 스트림이 받은 마지막 체결 |
| `market:stream:status` | 스트림 연결 상태·구독 종목·heartbeat |
| `market:stream:interest` | 스트림 구독 우선순위 |
| `market:rest-health:{source}` | REST source별 최근 성공/실패 |

### 용어

- **실시간 체결 시세**: WebSocket으로 받은, 현재 세션의 체결가
- **REST 최근 시세 (보조 시세)**: 주기적으로 조회한 최근 체결가. 실시간이 아니며 몇 초에서 1분 가까이 늦을 수 있습니다.
- **시간외 시세**: 프리장·애프터장 체결가 (KIS 주 거래소)
- **데이마켓 시세**: 20:00–04:00 ET 주간거래 체결가 (KIS `BAQ/BAY/BAA`)
- **과거 차트 데이터**: 차트용 분봉·일봉. 주문 가격으로 쓰지 않습니다.

### 진단

```bash
# 스트림 워커와 같은 앱키로 WebSocket을 열 수 없으므로 REST만 확인
docker compose run --rm --no-deps -v ./scripts:/srv/scripts market-worker \
  python scripts/check_us_day_market.py --seconds 0 AAPL TSLA QQQ
docker compose exec redis redis-cli GET market:stream:status
```

WebSocket까지 확인하려면 먼저 `docker compose stop us-trade-stream` 하고 `--seconds 30`으로 실행합니다.
관리자 화면에는 세션, 가격 모드, 스트림 상태, 구독 수/한도, REST source 상태가 표시됩니다.

## 종목 상세 현재가 SSE (선택 활성화)

`QUOTE_SSE_ENABLED=true`인 worker 모드에서 현재가를
`Finnhub REST / KIS → market-worker → Redis 저장·Pub/Sub → FastAPI → EventSource`로 전달합니다.
기본값은 `false`이며 Redis 없는 direct 개발 모드도 기존 30초 REST 표시를 사용합니다.
SSE 모드에서는 현재가 REST 폴링을 하지 않습니다. 장 상태는 진입·복귀와 60초 간격,
과거 차트는 진입·기간 변경·명시적 재시도에만 조회합니다. 차트에 임시 현재가 봉을 추가하지 않습니다.

| 설정 | 기본값 | 의미 |
| --- | --- | --- |
| `QUOTE_SSE_ENABLED` | `false` | web의 `/api/session`을 통해 프론트에도 전달 |
| `QUOTE_SSE_HEARTBEAT` | `15` | named heartbeat 간격(초), 허용 범위 5–30 |
| `QUOTE_SSE_USER_LIMIT` | `5` | 모든 web 프로세스 합산 사용자 동시 연결 상한 |
| `QUOTE_SSE_IP_LIMIT` | `30` | 모든 web 프로세스 합산 IP 동시 연결 상한 |

연결됨은 공급자의 최신 체결 데이터라는 뜻이 아닙니다. `QUOTE_TTL=15`는 수집 목표 간격이며
요청 종목 수·응답 지연·공유 호출 예산(`MARKET_CALLS_PER_MINUTE=50`)에 따라 늦어집니다.
15초마다 실제 quote 호출이 발생하면 13종목만으로 약 52회/분이므로 다른 기능의 예산도 고려해야 합니다.
수집 주기와 주문 허용 나이(국내 900초, 미국 1800초)는 변경하지 않았습니다.

- `GET /api/market-stream/{symbol}`: 기존 세션 인증, named `snapshot`, `quote`, `status`, `heartbeat`.
- 가격·환율은 decimal string. 공급자 `timestamp`, 서버 `cached_at`, heartbeat `time`은 별도 의미입니다.
- 가격 키는 기존 `market:price:{symbol}`이며 주문도 요청 시 이 키를 조회합니다.
- `market:quote-version:{symbol}`은 만료하지 않는 epoch/sequence/공급자 시각 메타데이터입니다.
  가격 TTL 만료·worker 재시작에도 순서가 유지됩니다. Redis 전체 초기화 시 새 epoch의 **snapshot**으로 초기화합니다.
- 구독을 준비한 뒤 snapshot을 읽으며, 연결당 queue는 최신 상태 1개만 보관합니다.
  중복·이전 버전은 제외하고, subscriber 재연결 때 활성 종목을 다시 읽습니다.
  [Redis Pub/Sub은 누락 이벤트를 재전송하지 않으므로](https://redis.io/docs/latest/develop/pubsub/)
  이벤트 이력 또는 exactly-once 전달을 보장하지 않습니다.
- 활성 종목은 프로세스당 60초마다 관심 등록을 유지합니다. 마지막 로컬 연결 해제는 전역 등록을 삭제하지 않습니다.
- 사용자/IP Redis lease로 합산 제한을 적용하며 Redis 장애 때 새 연결·lease 갱신은 실패 처리합니다.
  연결 시도 상한은 사용자 30회/분, IP 120회/분입니다. 세션 서명 만료·계정 활성 여부를 heartbeat마다 확인하며
  한 연결은 최대 30분 후 snapshot 재연결합니다. DB transaction을 연결 내내 유지하지 않습니다.
- 브라우저는 hidden/이탈/로그아웃 시 연결을 닫으며 오류 시 한 개의 재시도 timer만 사용합니다.
  자동 REST fallback은 없습니다. 마지막 가격의 공급자 시각으로 오래된 시세를 표시합니다.

### 격리 검증과 적용 순서

운영 컨테이너/볼륨과 분리된 `compose.sse-test.yaml`은 API key 없이 fake provider와 전용 Redis/DB를 사용합니다.

```bash
docker compose -p paper-sse-test -f compose.sse-test.yaml run --rm --build tests
docker compose -p paper-sse-test -f compose.sse-test.yaml run --rm --build browser
# 별도 환경에서 flag OFF 및 REST 복귀 검증
SSE_TEST_ENABLED=false docker compose -p paper-sse-rollback -f compose.sse-test.yaml run --rm --build browser
# 임시 자체서명 인증서: 테스트 전용, 운영 인증서와 무관
mkdir -p /tmp/paper-sse-certs
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj '/CN=tlsproxy' \
  -keyout /tmp/paper-sse-certs/privkey.pem -out /tmp/paper-sse-certs/fullchain.pem
docker compose -p paper-sse-test -f compose.sse-test.yaml run --rm proxytest
python3 scripts/check_publication.py
docker compose -p paper-sse-test -f compose.sse-test.yaml down
docker compose -p paper-sse-rollback -f compose.sse-test.yaml down
```

먼저 테스트 환경에서 flag를 끈 상태로 worker/web을 갱신한 뒤 SSE를 켜고 요청량·연결 수·quote age를 비교합니다.
검증 후 별도 운영 적용 절차에서 web과 worker 이미지를 반영하고 HTTP/HTTPS nginx 설정을 재로드합니다.
롤백은 `QUOTE_SSE_ENABLED=false`로 web을 재생성하고 페이지를 새로고침합니다.
기존 연결은 web 종료로 닫히고, 새 bootstrap은 30초 REST 경로만 시작합니다. DB/Redis 삭제는 필요 없습니다.
이 구현 작업은 운영 배포나 병합을 실행하지 않습니다.

관측: web의 `market-stream` 로그와 프로세스별 hub counters(active, reconnects, updates, coalesced,
invalid, subscriber_errors), worker의 저장 실패 로그 및 마지막 성공 로그를 사용합니다.
공급자 timestamp 나이와 `cached_at` 이후 전달 지연을 구분해 측정해야 합니다.
web 재시작 스모크는 다음 명령을 한 터미널에서 실행하고, `artifacts/sse-restart-ready` 파일이
이번 실행 시각으로 갱신되면 다른 터미널에서 테스트 web만 재시작합니다.

```bash
docker compose -p paper-sse-test -f compose.sse-test.yaml run --rm --build browser python browser_restart_smoke.py
# 별도 터미널, 위 테스트가 준비된 후 실행
docker compose -p paper-sse-test -f compose.sse-test.yaml restart browserweb
```

실행 결과와 제약은 [SSE 구현 검증 보고서](docs/QUOTE_SSE_RESULT.md)에 기록합니다.
