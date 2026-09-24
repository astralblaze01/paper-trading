# VANTAGE

Raspberry Pi 5 / Ubuntu ARM64용 다중 사용자 모의투자 웹 서비스입니다. FastAPI, PostgreSQL 17, Redis 7, Nginx, Docker Compose를 사용합니다. **실제 자금 이체·증권 주문 기능은 없습니다.** KIS 연결도 인증과 시세 조회에만 사용하며 계좌번호를 받지 않습니다.

기본 로컬 접속 주소는 **http://127.0.0.1:8080**입니다. LAN 접속은 `.env`의 `BIND_ADDRESS`에 서버의 LAN IP를 설정합니다. 외부 HTTPS는 아래 인증서 발급 절차로 활성화하며 운영 중에는 도메인 주소를 사용하세요.

## 실행과 사용

기존 `.env`와 PostgreSQL 볼륨을 유지하세요. 새 설치에만 `.env.example`을 복사하고 `POSTGRES_PASSWORD`, 32자 이상 `SESSION_SECRET`을 설정합니다.

```bash
docker compose up -d --build
docker compose ps
curl --fail http://192.168.100.13:8080/health
```

1. 한글·영문·숫자·밑줄 3~32자 이름, **8~128자 비밀번호**로 회원가입합니다. 기본 지급액은 가상 USD 100,000, KRW 0입니다.
2. 시장 탐색에서 국내주식·해외주식·국내채권 ETF·해외채권 ETF·금 ETF를 고르고 거래대금·급상승·급하락·VANTAGE 인기를 선택합니다. 버튼을 누르면 바로 갱신되며 화면을 보고 있는 동안 공급자 캐시 주기에 맞춰 다시 조회합니다. 회사명 또는 코드로도 검색할 수 있습니다. 표시 통화는 거래 통화·KRW·USD 중 선택할 수 있지만 주문은 종목의 원래 통화로 처리됩니다.
3. 종목을 누르면 차트와 시장가 주문 패널이 함께 열립니다. `최대 / 50% / 25% / 10% / 5%`는 서버가 수수료를 포함한 매수 가능 수량 또는 보유 매도 수량에서 정수 주로 내림합니다. 1주 미만이면 0주로 표시하고 주문을 막습니다. `최대` 주문은 **체결 직전 잠금 안에서 다시 계산**하며, 비율 선택 주문은 화면의 선택 수량으로 잔액·보유량을 재검증합니다.
4. 국내 종목은 **KRW 지갑**, 미국 종목은 **USD 지갑**에서 결제합니다. KRW가 없으면 환전 화면에서 USD→KRW를 먼저 실행합니다. 송금 통화를 바꾸면 현재 보유 달러/원화가 즉시 표시되고 전액 입력 버튼을 사용할 수 있습니다.
5. 포트폴리오에서 각 지갑, 보유 종목, 수수료 포함 평균가, 미실현 손익, 통화별 누적 실현 손익을 확인합니다. 거래내역·환전내역·관심종목·랭킹은 별도 메뉴입니다.
6. 새로운 지정가 주문 UI와 생성 API는 제거했습니다. 기존 지정가 주문 데이터는 삭제하지 않고 원래 조건으로 처리합니다. 새 시장가 주문은 최신 공급자 가격이 있을 때만 즉시 체결하며 오래된 시세를 대기 주문으로 바꾸지 않습니다.

한국 종목은 `KR:005930` 형식입니다. market-worker가 KIS 공식 코스피·코스닥 종목 마스터를 매일 Redis에 갱신하므로 종목명 일부와 6자리 코드 모두 검색할 수 있습니다. 마스터 갱신 장애 시에는 마지막 Redis 자료와 등록 카탈로그를 사용합니다. 채권·금은 미국/한국 상장 ETF로 지원합니다(TLT, IEF, SHY, KR:114260, GLD 등). **개별 채권, 금 현물, 채권 만기·이표 지급 모델은 지원하지 않습니다.**

## 구조와 변경 파일

| 파일 | 역할 |
|---|---|
| `app/db.py`, `app/migrations.py` | ORM, 버전 기록을 갖춘 추가형 migration |
| `app/money.py`, `app/fx.py`, `app/trading.py`, `app/portfolio.py` | 지갑·환전·수수료·최대수량·체결·KRW 평가 |
| `app/market.py`, `app/multi_market.py`, `app/providers.py`, `app/redis_cache.py` | Finnhub/KIS/FX 어댑터, Redis 시세 cache·중복 방지·호출 예산 |
| `app/market_worker.py` | 활성 구독 종목을 중앙에서 수집하는 시세 worker |
| `app/main.py`, `app/routes.py`, `app/security.py` | 인증, API, CSRF, 권한, 요청 제한 |
| `app/limits.py`, `app/worker.py`, `app/weekly.py` | 지정가 처리와 주간 게시 작업 |
| `app/admin_cli.py` | 기존 계정의 명시적 관리자 지정 |
| `app/static/` | 의존성 없는 HTML/CSS/JS, 모바일 화면, Canvas 차트 |
| `compose.yaml`, `Dockerfile` | web/db/redis/nginx/scheduler/market-worker, healthcheck, restart, ARM64 |
| `compose.https.yaml`, `compose.bootstrap.yaml`, `nginx/tls/`, `scripts/tls.py` | 선택적 Let's Encrypt 배포 |
| `tests/`, `compose.browser.yaml` | 격리된 PostgreSQL 통합·migration·브라우저 검증 |

## DB migration과 회계

앱 시작 시 기존 테이블을 삭제하지 않고 스키마를 확장합니다. migration은 PostgreSQL transaction과 advisory lock으로 보호하며 `schema_migrations`에 적용 버전(현재 6)을 기록합니다. 재시작 시 같은 변환을 반복하지 않습니다.

- 신규 테이블: `wallets`, `fx_transactions`, `watchlists`, `popularity_events`, `settings`, `season_archives`, `limit_orders` (지정가·대기 시장가).
- 사용자: 관리자/활성 상태, 초기 USD/KRW 평가액과 환율 기준일을 추가합니다.
- 기존 `users.cash`를 그대로 USD wallet로 복사하고 KRW wallet을 0으로 생성합니다. `cash`는 호환성용 USD 잔액 사본으로 유지합니다.
- 포지션에 현지통화 평균가를 추가합니다. 기존 한국 보유 종목은 과거 native_price 거래기록으로 평균가를 재구성합니다. 수량이 맞지 않으면 migration을 실패시켜 변환을 중단합니다.
- 거래에 gross/fee/tax/net, 적용 bps, 통화, native_price, 환율/일자, quote_time, realized_pnl, accounting_version을 보관합니다. 정책을 변경해도 과거 수수료는 바뀌지 않습니다.
- 이전 거래는 accounting_version=1로 보존합니다. 누적 실현손익 표시는 새 회계 방식으로 기록된 거래(version=2)부터 계산합니다. 이전 실현손익을 임의 복원하지 않습니다.
- 주간 집계에는 KRW 기준 통화와 native price를 추가합니다. 기존 USD 보고서는 원래 통화로 유지하고, 전환 후 첫 유효 평가를 새 주간 기준으로 설정합니다.

매수·매도·환전은 같은 사용자 행에 `SELECT FOR UPDATE`를 사용합니다. 잔액/수량 검사와 갱신, 거래 저장은 단일 transaction입니다. 요청 UUID 유일 제약으로 재시도 중복 체결을 막습니다. 금액은 `Decimal`/`NUMERIC`; KRW 결제 최소단위 1원, USD 0.0001달러, 수수료는 올림, 수령액은 내림입니다. 소수점 주식은 없습니다.

### 평가 기준

`KRW 현금 + USD 현금 × USDKRW + 국내 주식 평가액 + 미국 주식 평가액 × USDKRW`가 총자산입니다. 포트폴리오·공개 포트폴리오·누적 랭킹의 수익률은 모두 `(현재 KRW 총자산 - 외부 입출금 누계) / 고정된 초기 KRW 평가액 - 1`을 사용합니다. 따라서 환율 변동도 수익에 포함되며 관리자 지원금·사용자 간 이체 원금은 수익에서 제외됩니다.

신규 사용자는 가입 시 유효한 ECB 일별 환율로 초기 USD 지급액의 KRW 기준액을 고정합니다. 기존 사용자는 **migration 이후 처음 확보한 유효 환율**로 초기 지급액 USD 100,000을 환산합니다. 과거 가입일 환율을 추정하지 않습니다. 환율 장애 시 초기 기준 확정을 미루며, 조회/주간 집계 시 최초 확보한 환율과 일자를 DB에 고정합니다. 기준액이나 필요한 시세가 없으면 총 평가/랭킹을 보류하고 현금·보유내역은 보여줍니다.

평균 매입가는 매수 수수료를 포함합니다. 미실현 손익은 현지통화 현재 평가액−취득원가, 실현 손익은 매도 순수령액−매도분 취득원가입니다. 실현 손익은 USD/KRW를 분리하며 기존 시즌까지 누적한 값입니다.

## 설정과 API 키

`.env`는 Git과 Docker build context에서 제외됩니다. 기존 DB 비밀번호와 세션 키는 유지합니다. API 키는 서버 환경으로만 전달합니다.

| 변수 | 기본값 / 의미 |
|---|---|
| `FINNHUB_API_KEY` | 미국 시세·검색·차트·장 상태. 계정별 데이터 권한 필요 |
| `KOREA_MARKET_PROVIDER` | `kis`; `disabled`로 국내 공급자 비활성화 |
| `KOREA_MARKET_API_KEY`, `KOREA_MARKET_API_SECRET` | KIS 앱 키/시크릿. 기존 `KIS_APP_KEY/SECRET`도 호환 |
| `ALPHAVANTAGE_API_KEY` | 미국 전체시장 거래량·상승·하락 순위용 추가 키 |
| `INITIAL_USD` | `100000`; 관리자 DB 설정이 있으면 그 값 우선 |
| `FX_FEE_BPS`, `FX_SPREAD_BPS` | `10`, `5`: 환전 수수료 0.10%, 스프레드 0.05% |
| `US_BUY_FEE_BPS`, `US_SELL_FEE_BPS` | `0`, `0` |
| `KR_BUY_FEE_BPS`, `KR_SELL_FEE_BPS`, `KR_SELL_TAX_BPS` | 모두 `0`; 실제 증권사/법정 세율을 재현하는 기본값이 아님 |
| `QUOTE_TTL`, `MAX_QUOTE_AGE`, `US_MAX_QUOTE_AGE` | `15`초 수집 주기, 국내 `900`초·미국 `1800`초까지 즉시 체결; 더 오래된 시세는 주문 거절 |
| `MARKET_CALLS_PER_MINUTE` | Finnhub 앱 내부 예산 `50`회/분; 공급자 한도를 확대하지 않음 |
| `WEEKLY_ENABLED`, `WEEKLY_DAY`, `WEEKLY_HOUR` | `true`, `5`, `9`: 한국시간 토요일 09시 |
| `BIND_ADDRESS`, `HTTP_PORT` | 예제는 `127.0.0.1`, `8080`; 현 설치는 LAN 주소 |
| `COOKIE_SECURE` | LAN HTTP `false`; HTTPS overlay가 `true`로 강제 |
| `DOMAIN`, `LETSENCRYPT_EMAIL` | 실제 도메인, 인증서 등록 이메일; TLS 전환에 필요 |

**10 bps = 10 / 10,000 = 0.10%**입니다. 환전은 보낸 금액에서 원통화 수수료를 뺀 뒤 `기준환율 × (1−spread/10000)`을 적용합니다. USDKRW=1000, USD1000 환전 예: 수수료 USD1, 적용 환율999.5, 수령 KRW998,500입니다.

설정 변경 후 `docker compose up -d`로 적용합니다(`restart`만으로 환경변수는 갱신되지 않음). HTTPS 운영에서는 항상 아래 overlay 명령을 사용해야 합니다.

### 한국 시세 연결 방법

1. [한국투자증권 공식 Open API 안내](https://github.com/koreainvestment/open-trading-api#34-kis-open-api-%EC%8B%A0%EC%B2%AD-%EB%B0%8F-%EC%84%A4%EC%A0%95)에 따라 계좌와 ID를 연결하고 Open API 서비스를 신청합니다.
2. **실전 서버용** App Key와 App Secret을 발급받습니다. VANTAGE는 실전 KIS 시세 서버에 읽기 전용으로 접속하지만 증권사 주문·잔고 API와는 연결하지 않습니다. KIS 자체 모의투자 서버용 키는 이 어댑터의 서버 주소와 다릅니다.
3. Pi의 `.env`에 `KOREA_MARKET_PROVIDER=kis`, `KOREA_MARKET_API_KEY=발급받은키`, `KOREA_MARKET_API_SECRET=발급받은시크릿`을 직접 입력합니다. 채팅·Git에 키를 붙여넣지 마세요. 기존 `KIS_APP_KEY/SECRET` 변수도 호환됩니다.
4. 현재 인증서 준비 단계에서는 `docker compose -f compose.yaml -f compose.https.yaml -f compose.bootstrap.yaml up -d web nginx`로 적용합니다. HTTPS 전환 후에는 `-f compose.bootstrap.yaml`을 빼고 실행합니다. 국내 탭에서 `KR:005930`을 조회해 확인합니다.

KIS 권한/쿼터 또는 계정 계약상 시세를 웹 사용자에게 재배포할 수 있는지 운영자가 확인해야 합니다. 키가 없다면 국내 시세·순위·주문은 제공되지 않고 계좌/미국 기능은 계속 동작합니다.

### 데이터 공급자와 제한

2026-09-23 공식 문서 기준으로 선택한 경로입니다. 실제 API 키가 제공되지 않아 인증된 운영 시세 호출은 검증하지 못했습니다. 키가 없으면 해당 기능에 설정 필요를 표시하고 로그인·다른 시장 기능은 계속 동작합니다.

- **Finnhub**: [Quote](https://finnhub.io/docs/api/quote)는 미국 시세를 제공하지만 이 앱은 계약별 거래소 커버리지/지연을 판별할 수 없어 무조건 `실시간`으로 표시하지 않습니다. 공급자 timestamp와 지연 미확인 상태를 표시합니다. [Candles](https://finnhub.io/docs/api/stock-candles)는 1/5/15/30/60/D/W/M resolution API를 사용하며, 무료 키에서 역사 데이터 접근이 거절될 수 있습니다. 이 설치의 실제 Finnhub 키로 2026-09-23 확인한 결과 `/stock/candle`은 403 권한 오류였습니다. 현재 운영 차트에는 권한 오류가 표시되며, 과거 시세 권한이 있는 공급자 계정/키가 필요합니다. 401/403은 키·요금제 권한 안내로 표시합니다. 5년/ALL은 키의 보유 이력 범위까지만 제공합니다. [WebSocket trades](https://finnhub.io/docs/api/websocket-trades) API가 있으나 이번 구현은 무료 한도/권한을 고려해 backend polling을 사용합니다. [Rate limit](https://finnhub.io/docs/api/rate-limit)과 [Pricing](https://finnhub.io/pricing-stock-api)의 실제 계정 한도가 우선입니다. 이 앱은 분당50회 예산, 429 후60초 대기를 적용합니다. Quote API에 없는 거래량은 미제공으로 표시합니다. Finnhub 키만 설정된 경우 미국 주식의 전체 시장 거래량/급등락 순위는 제공할 수 없어 등록 종목의 실제 시세를 표시하고 전체 시장 순위가 아님을 명시합니다.
- **KIS 공식 Open API**: [한국투자증권 공식 예제](https://github.com/koreainvestment/open-trading-api)의 코스피·코스닥 종목 마스터와 당일분봉/기간별시세/거래량순위/등락률순위/휴장일 API를 사용합니다. 실전 시세 서버용 앱 키를 사용하되 주문 API는 구현하지 않았습니다. 시세는 분봉 종가와 실제 영업일·체결시각을 사용하며 초단위 실시간 스트림이 아닙니다. 국내 장 상태는 휴장일 응답+Asia/Seoul 표준시간표로 추정하고 특별 개장시간 미반영을 표시합니다.
- **Alpha Vantage**: [TOP_GAINERS_LOSERS](https://www.alphavantage.co/documentation/#top-gainers-losers)로 미국 시장 순위를 읽습니다. 실시간 순위라고 표시하지 않으며 응답의 갱신시각을 보여줍니다. [무료 API 안내](https://www.alphavantage.co/support/#api-key)의 일반 한도는 하루25회입니다. 전체 순위 응답을1시간 공유 캐시하여 UI 필터마다 따로 호출하지 않습니다. 재시작하면 캐시가 초기화되므로 반복 재시작 시 한도에 유의합니다.
- **환율**: [Frankfurter](https://frankfurter.dev/)의 ECB 일별 KRW→USD 기준환율을 역산합니다. 키가 필요 없고 실시간 환전 호가가 아닙니다. 1시간 캐시하며 기준일7일 초과/미래 환율을 거부합니다. API 오류 시 영구적으로 오래된 값을 최신 환율처럼 사용하지 않습니다.

공개 멀티유저 재배포 권한은 각 공급자 계약에 따릅니다. API를 기술적으로 조회할 수 있다는 사실만으로 시세 재배포 권한이 생기지는 않으므로 인터넷 공개 전 발급 계정의 이용범위를 확인해야 합니다.

### 차트·갱신·캐시

| 기간 | 미국 요청 resolution | 국내 요청 |
|---|---|---|
| 1D | 5분 | 당일1분봉 |
| 1W | 30분 | 일봉 fallback |
| 3M / 1Y | 일봉 | 일봉 |
| 5Y | 주봉 | 주봉 |
| ALL | 최대40년 범위 월봉 | 최대40년 범위 월봉 |

ALL은 상장 이후 완전한 이력을 보장하지 않습니다. 국내는 최대10페이지의 기간별 데이터를 읽습니다. 수정주가 차트와 실제 보유 수량 조정은 별개입니다. 차트는 Canvas 종가선, 마우스/터치/키보드 tooltip과 OHLC·거래량을 제공하며 브라우저가 공급자 API에 직접 연결하지 않습니다.

종목 상세와 탐색 화면에서30초마다 backend quote/순위를 다시 확인합니다. 누적 수익률 랭킹 화면은 10초마다 API를 확인합니다. 활성 종목은 market-worker가 중앙에서 수집하고 Redis TTL cache를 모든 요청이 공유합니다. Finnhub 호출 예산도 Redis에서 공유하며 provider 원본 응답은 분산 lock으로 중복 요청을 막습니다. 가격은 공급자 지연·시장 시간에 좌우되며 WebSocket 스트리밍은 다음 단계입니다.

VANTAGE 인기는 최근24시간( API는1시간도 지원)의 상세조회·단일결과검색·주문·관심등록 이벤트 합계입니다. 사용자/종목/행동/시간당1회만 기록하고 요청 제한을 적용합니다. 외부 시장의 인기 순위가 아닙니다. 대량 허위 계정까지 막는 강한 부정행위 방지는 별도 과제입니다.

미국 시세는 공급자 지연을 고려해 최대 30분(설정 가능)까지 즉시 체결에 사용합니다. 그보다 오래된 가격이면 주문을 명확히 거절하며 새 시장가 주문을 대기열로 바꾸지 않습니다. 실제 거래소 매칭/호가·유동성·슬리피지·미국 주간거래 전용 가격은 재현하지 않습니다. 프리장·정규장·애프터장도 공급자가 최신 가격을 제공할 때만 체결합니다.

## 주간 게시와 관리자

별도 scheduler **worker 컨테이너**가1분마다 내부 인증된 작업 endpoint를 호출하고, 별도 **market-worker**가 활성 종목 시세를 수집합니다. 시세 cache와 공급자 호출 예산은 Redis에서 공유합니다. Nginx는 `/internal/`을 외부에 노출하지 않습니다.

- 기본 매주 토요일09시(Asia/Seoul), 사이트의 랭킹 메뉴에 보고서를 저장합니다. 외부 SNS/메신저 전송은 없습니다.
- 주간 수익률은 직전 기준 KRW 자산 대비 변화입니다. 새 계좌/초기화 계좌는 비교 가능한 시작값이 생긴 다음 보고서부터 포함합니다.
- 보고서 평가에는 최대7일 이내의 마지막 실제 가격을 허용하고 가장 오래된 시세와 실제 집계기간을 표시합니다. 주문용 캐시와 구분합니다.
- 평가 불가 시 게시를 보류하고5분 후 재시도합니다. 정지 후 복구하면 복구시점 결과1개를 게시하며 과거 가격을 만들어내지 않습니다.
- 중복 게시 방지는 DB advisory lock+unique 예정시각+transaction으로 처리합니다.

기본 관리자 계정이나 공용 비밀번호는 없습니다. 운영자가 지정한 **기존 사용자**만 다음 명령으로 승격합니다(이번 배포에서는 아무도 자동 승격하지 않음).

```bash
docker compose exec web python -m app.admin_cli 실제사용자이름
```

관리자 메뉴: 사용자/활성상태, 신규 초기 지급액 변경, 수수료 설정 확인, DB·공급자 설정 상태, 사용자별 시즌 초기화. 기존 시즌 초기화 API는 거래기록을 유지합니다. 현재 운영 화면의 전체 초기화는 `CLEAR 사용자이름`을 확인하고 복구 자료를 보관한 뒤 거래기록까지 정리합니다. 범위는 아래 운영 도구 설명을 참고하세요. 보관내역은 `/api/admin/archives`에서 조회합니다. **전체 대회 참가/시작/종료/시즌별 순위 기능은 아직 제공하지 않습니다.**

지정가 worker는 한 번에 최대100개 미체결 주문을 검사하고, 조건 충족 시 같은 거래 엔진에서 체결/상태변경을 한 transaction으로 저장합니다. 사용자당 미체결20개 제한, FIFO 조회이며 자금 예약·부분 체결·실거래 호가 우선순위는 없습니다.

## HTTPS: 도메인과 외부 연결을 준비한 뒤 활성화

기본 Compose는 기존 LAN8080을 유지합니다. **실제 도메인 이름이 먼저 필요합니다.** `.env`의 `DOMAIN=trade.본인도메인`을 채우고 (인증서 만료 연락을 받으려면 `LETSENCRYPT_EMAIL`도 채우고) DNS A가 공인 IPv4를 가리키게 합니다. AAAA를 등록했다면 IPv6도 Pi까지 정상 연결되어야 합니다. 공유기/방화벽은 이 프로젝트가 자동 변경하지 않습니다.

단일 공유기에서 운영자가 설정할 포트포워딩:

```text
외부 TCP 80  -> Raspberry Pi (192.168.100.13) TCP 80
외부 TCP 443 -> Raspberry Pi (192.168.100.13) TCP 443
```

이중 공유기/NAT라면 앞 공유기→뒤 공유기→Pi 두 구간 모두 전달해야 합니다. CGNAT나 ISP 인바운드 차단은 단순 포트포워딩으로 해결되지 않습니다. 공인IP 제공 또는 별도 터널/프록시 대안이 필요합니다. 외부 모바일망에서 연결을 확인하세요. 내부 NAT loopback 지원 여부는 외부 접근 가능 여부와 다릅니다.

현재 공유기 사진의 연결은 앞 공유기 외부80→뒤 공유기 WAN `192.168.0.31:7076`, 외부443→`192.168.0.31:7077`입니다. 이 구조를 유지한다면 **뒤 공유기 규칙을 `7076/TCP→192.168.100.13:80`, `7077/TCP→192.168.100.13:443`으로 바꿔야 합니다.** 현재처럼 두 규칙이 모두 Pi `:8080`을 가리키면 인증서 검증은 앱의404를 받고 HTTPS는 평문 HTTP에 연결됩니다. 공유기 관리 포트 `47839`는 웹서비스와 무관하며 외부에 공개하지 않도록 원격 관리와 해당 전달 규칙을 해제하는 것이 좋습니다.

[Let's Encrypt HTTP-01](https://letsencrypt.org/docs/challenge-types/)은 외부80번으로 검증합니다. DNS와80/443 접근, 호스트 포트 사용 여부를 확인하고 **운영자의 포트 bind·인증서 발급 승인 후에만** 실행합니다. `--confirmed-public-routing`은 운영자가 확인했다는 명시적 표시이며 스크립트가 공유기 상태를 자동 증명하는 옵션은 아닙니다.

```bash
python3 scripts/tls.py check
python3 scripts/tls.py bootstrap --confirmed-public-routing
python3 scripts/tls.py issue --confirmed-public-routing
python3 scripts/tls.py enable --confirmed-public-routing
curl -I http://실제도메인/
curl --fail https://실제도메인/health
# 외부 HTTPS 정상 검증 후에만 HSTS 활성화:
python3 scripts/tls.py hsts --confirmed-public-routing
```

bootstrap 동안 외부80은 ACME 검증과503 안내만 제공하고 기존 LAN8080 서비스는 유지합니다. 인증서 발급 후 nginx는 **80:80,443:443**을 publish하고 일반HTTP를308 HTTPS로 전환합니다. 접속은 `https://실제도메인`이며 포트를 입력하지 않습니다. LAN8080 publish는 overlay에서 제거됩니다. self-signed 인증서는 운영 기본값으로 사용하지 않습니다.

TLS overlay는 Secure/HttpOnly/SameSite=Strict 세션을 사용합니다. HSTS는 기본 꺼짐이며 `hsts` 명령이 유효한 HTTPS를 확인한 후 켭니다. 인증서는 `certificates` volume에 보관하고 갱신 컨테이너는12시간마다 갱신을 시도합니다. Nginx는6시간마다 갱신 인증서를 다시 읽습니다. 갱신 실패는 `logs renew`로 확인하세요.

HTTPS 운영 이후의 모든 compose 명령:

```bash
docker compose -f compose.yaml -f compose.https.yaml up -d --build
docker compose -f compose.yaml -f compose.https.yaml ps
docker compose -f compose.yaml -f compose.https.yaml logs --tail=100 nginx renew
```

기본 compose만 실행하면 LAN 구성으로 돌아갈 수 있으므로 운영 명령을 혼용하지 마세요. `!override`를 지원하는 Compose 2.24.4 이상이 필요합니다.

## 보안과 장애

Argon2id 비밀번호 해시, 12시간 서명 세션, HttpOnly/SameSite=Strict, 변경 요청 CSRF, ORM 바인딩 쿼리를 사용합니다. 계좌 ID는 세션에서 가져오며 사용자가 다른 ID를 지정해 주문할 수 없습니다. 관리자 권한은 매 요청 DB에서 검사합니다. 일반 사용자의 관리자 접근, 타인 주문 취소는 차단합니다.

Nginx 로그인/가입 제한과 앱의 IP별1분 요청제한(인증20, 주문30, 시장120, 인기60)을 적용합니다. Nginx가 `X-Real-IP`를 덮어씁니다. web/db 포트는 호스트에 publish하지 않습니다. 운영 HTTPS 전 LAN HTTP 로그인은 신뢰할 수 있는 네트워크에서만 사용하세요.

시장 API 오류/한도/오래된 가격이면 주문을 거절합니다. **테스트 대역 가격은 격리된 테스트 DB에서만 사용하고 운영 기능에 fallback하지 않습니다.** 평가를 못하면 가짜0원이나 매입가로 대신하지 않습니다. 시세 장애와 로그인/보유내역 조회는 분리됩니다.

### Corporate actions 대응 계획

현재 split/reverse split/dividend/ticker change/delisting 자동 반영은 없습니다. 장기 성과는 기업행사를 반영한 총수익률과 다를 수 있습니다. 조정차트만 보고 수량이 자동 조정됐다고 간주하면 안 됩니다.

운영 대응: 공급자의 공식 기업행사 이력 확보 → 해당 종목 거래/랭킹 집계 일시 중지 → 백업 → 유일한 event ID를 갖춘 감사 가능한 migration으로 split 비율만큼 수량 조정·평균가 역조정 → 현금배당은 별도 wallet ledger → ticker 변경은 참조와 pending order 함께 변환 → 상장폐지는 유효 평가 정책 결정 전 거래 중단 → 수량·원가 불변 검증 후 재개. 이를 자동화하기 전에는 장기 대회에서 해당 기업행사 발생 종목을 점검해야 합니다. 현재 관리자 UI에는 종목별 거래 정지 버튼이 없으므로 이벤트 발견 시 운영자가 관련 거래 서비스/계정을 중단하고 정합성을 먼저 복구해야 합니다.

## 운영·백업·복원

```bash
docker compose ps
docker compose logs --tail=100 web worker nginx db
docker compose up -d --build
docker compose restart
docker volume inspect paper-trading_pgdata
```

데이터는 `paper-trading_pgdata` volume의 컨테이너 `/var/lib/postgresql/data`에 있습니다. 실제 호스트 경로는 `docker volume inspect`의 Mountpoint로 확인하세요. **`docker compose down -v`는 데이터 삭제이므로 사용하지 마세요.**

백업(내용에 계정/암호 해시 포함):

```bash
mkdir -p backups
umask 077
docker compose exec -T db pg_dump -U paper -d paper -Fc > backups/paper.dump
```

안전한 복원 검증은 기존 DB를 덮어쓰지 않고 새 DB에서 합니다:

```bash
docker compose exec -T db createdb -U paper paper_restore_check
docker compose exec -T db pg_restore -U paper -d paper_restore_check < backups/paper.dump
docker compose exec -T db psql -U paper -d paper_restore_check -c 'SELECT count(*) FROM users;'
```

운영 DB 교체 복원은 유지보수 시간과 운영자 승인이 필요합니다. 먼저 신규 백업을 남기고 web/worker를 중지한 뒤 DBA가 복원본을 검증·전환해야 합니다. 위 예제는 운영 DB를 삭제하거나 자동 전환하지 않습니다. 백업은 Pi 밖에도 보관하고 `.env`와 TLS 인증서 volume도 접근 제한된 별도 백업에 포함하세요.

## 검증

```bash
docker compose run --rm web python tests/run.py
# 별도 Chromium 컨테이너 + 격리된 paper_browser_test DB:
docker compose -f compose.yaml -f compose.browser.yaml build browser
docker compose -f compose.yaml -f compose.browser.yaml up -d browserweb
docker compose -f compose.yaml -f compose.browser.yaml run --rm browser
docker compose -f compose.yaml -f compose.browser.yaml stop browserweb
```

통합 테스트는 `paper_test`만 초기화하고 운영 DB를 거부합니다. 브라우저 테스트는 `paper_browser_test`의 대역 시세로 화면 흐름을 검증합니다. 실제 API 권한·현재 시세 정확성 검증과 구분하세요. 실행 결과와 운영 기록은 로컬에서 별도로 관리하며 공개 저장소에는 포함하지 않습니다.

### 탐색 화면의 가격과 순위 범위

종목 이름과 ETF 카탈로그는 코드에 등록되어 있지만, **현재가·등락률·시세 시각은 등록값이 아닙니다.** 공급자 API 응답을 서버가 캐시해 표시합니다. 실제 미국 주식·미국 채권 ETF·금 ETF 가격은 Finnhub, 국내 종목 가격은 KIS 키가 설정되면 KIS에서 읽습니다. 공급자 미설정/장애 시 숫자를 만들지 않고 `—`와 이유를 표시합니다.

미국 시장의 거래량 상위·급상승·급하락 후보는 Alpha Vantage 키가 있어야 하며 응답은 시간별 스냅샷입니다. 미국 **거래대금은 종가/스냅샷 가격×누적 거래량 추정치**로, 공급자가 반환한 거래량 상위 후보 안에서만 정렬합니다. 미국 전체 시장의 정확한 거래대금 순위라고 표시하지 않습니다. 국내 거래대금 순위는 KIS [거래량순위 API의 거래금액순(`FID_BLNG_CLS_CODE=3`)](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/volume_rank/chk_volume_rank.py)를 사용하고 120초 캐시합니다. 공급자가 반환한 개수만 표시합니다. 채권·금 ETF 탭의 정렬은 등록된 종목 범위에 한정됩니다. Finnhub quote에 누적 거래대금이 없으면 거래대금순으로 정렬하지 않습니다. 무료 API 가격의 지연·갱신은 계정 권한/장 상태에 따르므로 실시간이라고 단정하지 않습니다.
목록은 공급자가 제공한 경우 **최대 100개**를 표시하지만, 공급자 응답이 그보다 작거나 키/권한이 없으면 실제 제공 개수만 표시합니다. 현재 KIS 거래금액순 실측 응답은 30개이며 연속조회 헤더가 비어 있어 추가 페이지가 없었습니다. Alpha Vantage의 [TOP_GAINERS_LOSERS](https://www.alphavantage.co/documentation/#top-gainers-losers)는 카테고리당 상위 20개를 제공하며 별도 키가 필요합니다. 100개를 채우려고 가짜 종목·가격을 만들지 않습니다. 국내 채권 ETF와 한·미 금 ETF를 카탈로그에 추가했습니다. [KODEX 운용사 종목 안내](https://m.samsungfund.com/etf/lounge/notice-view.do?no=67815), [ACE KRX금현물 안내](https://www.aceetf.co.kr/cs/notice/5124)를 참고했습니다. 환전 화면에는 USD/KRW 양방향 환산과 기준일이 표시됩니다. 환율은 ECB **일별 기준환율**로 실시간 환율이 아닙니다.

### 종목 상세와 수익률 표시
종목 상세에서 현재가 아래에 전일 대비 금액·등락률을 따로 표시합니다. 상승 빨강, 하락 파랑, 보합 중립색을 전역에서 사용합니다. 모든 기간 차트는 표시 구간 첫 종가 대비 마지막 종가 방향으로 색을 지정합니다. 주문 패널은 최근 본 8개 종목을 로그인 아이디별 브라우저 저장소에 저장하며 기존 저장소 키도 이어 읽습니다. 다른 기기에는 동기화되지 않습니다.
총 수익률은 기존 KRW 기준을 유지합니다. 초기 USD 원금에 대한 환율 변동 효과(`initial_usd × 현재 USDKRW − initial_krw`)와 나머지 손익을 나눠 표시합니다. 이는 실제 보유 달러의 환차익을 개별적으로 계산한 값이 아니라 초기 원금 비교 기준의 분해입니다. 동일 환율에서 환전만 하면 수수료/스프레드 때문에 총자산이 감소합니다.

공개 포트 설정은 `.env`의 `PUBLIC_HTTP_PORT=80`, `HTTPS_PORT=443`으로 지정합니다. 기존 `HTTP_PORT=8080`은 인증서 준비 중 LAN 접속용입니다. 이 환경변수는 HTTPS/인증서 준비 overlay에서 사용하며, `.env`에 값만 넣어도 기본 Compose가 TLS로 전환되는 것은 아닙니다. 인증서 발급 후 `python3 scripts/tls.py enable --confirmed-public-routing`으로 HTTPS 구성을 실행해야 합니다.

HTTPS 운영 전환 완료 후 `.env`에 `COMPOSE_FILE=compose.yaml:compose.https.yaml`을 지정했습니다. 이제 기본 `docker compose up -d --build`도 HTTPS 구성을 유지합니다. 8080 대신 https://mockinvest.duckdns.org/ 로 접속하세요.

## 화면 · 회원 공개 범위 · 운영 도구

- VANTAGE는 기존 Paper Harbor/ASTER의 새 서비스 이름입니다. `app/branding.py`에서 이름과 저장소 이름을 관리하고 HTML에 주입합니다. DB/세션/계정과 프로젝트 경로는 유지합니다.
- 일반 사용자의 탐색에는 코드·데이터 상태·시세시각 열을 표시하지 않습니다. 공급자 진단 필드는 서버에서 관리자 권한을 확인한 뒤에만 탐색 API 응답에 포함합니다. 종목 선택·주문을 위한 공개 식별자 `symbol`은 유지합니다. 관리자는 모바일에서 접힌 `시세 진단`을 열 수 있습니다. 모바일 목록은 종목명·현재가·등락률 아래 거래대금·거래량을 나누고 별 버튼으로 관심등록할 수 있습니다.
- 전체 표시 통화 선택은 차트·포트폴리오·거래내역·관심종목·랭킹에 공유됩니다. 주문·환전 입력과 결제액은 항상 실제 지갑 통화로 명시합니다. 차트는 현재 기준환율로 구간 전체를 환산하며 과거 환율 수익률을 의미하지 않습니다.
- 각 차트 기간은 실제 표시된 첫 종가→마지막 종가의 금액/퍼센트 변동과 시작/종료시각을 표시합니다. 전일 대비는 별도로 표시됩니다. 짧은 공급자 데이터 범위를 전체 5년 데이터로 표시하지 않습니다.
- 기업 기본정보는 [Finnhub profile2](https://finnhub.io/docs/api/company-profile2), [KIS 주식기본조회](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/search_stock_info/search_stock_info.py)를 사용합니다. 제공되는 회사명·업종·거래소·국가·상장일·공식 홈페이지만 표시합니다. 사업 소개문을 임의 생성하지 않으며 ETF/권한 제한은 미제공으로 안내합니다. UX는 [토스증권 공개 화면](https://www.tossinvest.com/)의 종목→차트→주문 흐름을 참고했으며 브랜드나 화면을 복제하지 않습니다.
- 로그인한 회원은 랭킹의 사용자 이름에서 다른 일반 회원의 가상 현금·보유 종목·수익률을 읽기 전용으로 볼 수 있습니다. 거래/환전 내역과 주문 식별자, 자격증명은 공개하지 않습니다. 관리자·정지 계정은 공개 목록에서 제외합니다.
- 관리자는 누적·주간 랭킹(기존 주간 보고서 화면 포함)에서 제외됩니다. 순위는 KRW 평가 기준으로 고정하며 통화 선택은 금액 표시만 변경합니다. 오래된 주간 보고서에 환율이 없으면 다른 통화의 손익을 임의 환산하지 않고 `—`로 표시합니다.
- 관리자 운영 화면에서 사용자 잔액/상태 조회, 정지/활성화, 초기자금 설정, 지원금 지급, 수익률 기준 재설정, 기록 전체 초기화, 감사 기록 조회를 제공합니다. 지원금은 지급시점 KRW 가치로 외부 입금에 기록하고 누적·주간 손익에서 차감합니다. 지급 후의 자산/환율 변동은 수익에 포함됩니다.
- 수익률 기준 재설정은 자산과 거래기록을 유지하고 현재 총자산을 0% 기준으로 설정합니다. 전체 초기화는 대상 이름을 포함한 `CLEAR 사용자이름` 확인이 필요하며 자산·거래·환전·주문·관심종목·인기 활동·기존 주간 참여 기록을 제거하고 초기 USD를 지급합니다. 로그인 계정·비밀번호는 유지하며 관리자 감사/복구 자료는 보관합니다. 즉 개인정보 영구 삭제 도구가 아닙니다.
- migration v4는 `users.net_contributions_krw`, `users.performance_since`와 `admin_audits`를 추가합니다. 운영 배포 시 기존 사용자의 지원금 누계는 0이며 기존 거래를 삭제하거나 자동 초기화하지 않습니다.

## GitHub 공개 전 검사

```bash
python3 scripts/check_publication.py
```

`.env`, DB 백업/덤프, 인증서·개인키, 로그, 에디터 복구 파일, 테스트 스크린샷과 로컬 운영 문서는 제외합니다. `.env.example`, Compose/Nginx 템플릿은 비밀 값이 없는 실행 문서이므로 포함합니다. 실제 DB 비밀번호·세션키·API 키는 서버에서만 관리하세요.

### 사용자 간 가상 이체
이체 메뉴에서 수신자 아이디·통화·받을 금액을 입력하고 견적을 확인한 뒤 실행합니다. `TRANSFER_FEE_BPS=10`은 **0.10%**이며 보내는 사람의 같은 통화 잔액에서 금액+수수료를 차감합니다. 수신자는 입력 금액 전액을 받습니다. 수수료는 통화 최소단위로 올림합니다. 지원금과 마찬가지로 이체 원금은 수익에서 제외되고 수수료만 손실입니다. 환율 변동은 기존 KRW 기준 정책에 따라 반영합니다. 동일 요청 UUID 재시도는 중복 이체하지 않습니다. DB 트랜잭션·순서 있는 계좌 잠금·주간 집계 잠금으로 동시 이체/주문 잔액을 보호합니다.
Migration v5는 `wallet_transfers`와 사용자 기록 표시 시작시각을 추가합니다. 전체 초기화 시 과거 이체는 해당 사용자의 화면에서 숨기며 상대방의 이체내역과 회계 원장은 보존합니다. 실제 돈을 보내는 기능은 없습니다.
