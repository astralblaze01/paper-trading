# 현재가 SSE 구현·검증 보고서

작업 기준: `451faae` (`main`, 2026-09-25 작업 시작). 원격 main과 차이 없이 시작했으며,
저장소에 적용할 AGENTS.md나 기존 미커밋 변경은 없었다.
작업 브랜치: `codex/quote-sse`. 운영 배포 및 자동 병합은 수행하지 않는다.

## 구현

- `app/quote_data.py`: 공유 검증·freshness 계산, Decimal 문자열과 공개 필드 allowlist.
- `app/redis_cache.py`: 기존 일반 캐시 API 유지. WATCH/MULTI로 가격·영속 버전·publish를 함께 처리.
  과거 공급자 timestamp 및 잘못된 가격은 저장하지 않으며 동일 timestamp 정정은 허용.
- `app/market_worker.py`: 저장 성공에만 연동되는 이벤트, urgent 요청에도 수집 간격 적용,
  마지막 성공이 오래된 종목 우선 수집. 공급자·15초 목표 간격·50회 공유 예산·backoff 유지.
- `app/market_stream.py`: 프로세스당 비동기 subscriber, 구독 ACK 이후 snapshot,
  최신 값 1개 queue, 재연결 snapshot, 60초 관심 유지, shared Redis 사용자/IP lease와 시도 제한,
  named heartbeat, 세션 만료·계정 정지 확인, 최대 30분 연결, 취소 시 정리.
- `app/main.py`: lifespan과 기존 인증 연결, `/api/session`에 실제 기능 설정 전달.
- `app/routes.py`의 기존 장 상태·candles·preview 경로는 유지했다. preview의 장 상태 조회는 기존 미국 60초/한국 휴장일 1일 provider cache를 재사용하며, heartbeat는 preview를 요청하지 않는다.
- `app/multi_market.py`, `app/trading.py`: worker 모드에서 직접 공급자 fallback 없이 서버 Redis 조회,
  요청 시 freshness 계산 및 계좌 잠금 대기 후 재검증. 주문 가격·Decimal·멱등성·회계 정책 유지.
- `app/static/portal.js`, `app/static/app.js`, HTML/CSS: 현재가 전용 부분 렌더링,
  한 SSE 연결·한 재시도 timer, generation으로 이전 이벤트 제외, 이탈/hidden/로그아웃 정리,
  60초 장 상태 조회, 가격 변경 시 직렬화된 preview. 같은 가격 heartbeat는 preview를 호출하지 않음.
- 과거 candles는 진입·기간 변경·명시적 재시도만 조회. 기존 임시 현재가 봉을 제거해
  과거 OHLCV 및 거래량을 변형하지 않음. 표시 통화와 한국·미국 주문 흐름 유지.
- HTTP·HTTPS nginx에서 buffering/cache/gzip 해제 및 90초 read timeout.
- `.env.example`, `compose.yaml`, README: 기본 flag OFF, 활성화·롤백 절차.
  기존 redis 5.2.1의 asyncio API 사용; 의존성 추가 없음.
- `compose.sse-test.yaml`, 테스트 파일: 운영과 분리된 DB·Redis·브라우저·HTTP/HTTPS 검증 환경.
  Pub/Sub은 Redis DB 번호로 격리되지 않으므로 서비스 테스트와 브라우저는 서로 다른 Redis 서버 사용.

## 검증

실제 Finnhub/KIS API key 및 운영 DB/Redis를 사용하지 않았다.

| 검증 | 결과 |
| --- | --- |
| 격리 전체 pytest | 108개 통과; Starlette/AnyIO deprecated alias 경고 1개 |
| 실제 Redis | 원자적 저장/발행, 오류 시 미발행, timestamp/정정/버전/TTL 이후 유지/epoch 초기화 |
| fan-out | 독립 Python 프로세스 2개 수신, 한 프로세스 100 queues + 다른 hub 수신 |
| 장애/수명 | subscriber 연결 강제 종료 후 복구, queue 상한·정리, 601초 fake clock 관심 갱신 |
| 인증/제한 | 401/403/422, 비활성 flag 503, origin/동시 연결/시도 제한, heartbeat 중 계정 정지 종료 |
| 주문 | 브라우저 A 이후 Redis B로 주문하면 B 체결, 잠금 후 만료 거절, cache miss 직접 공급자 호출 없음 |
| 기존 주문 회귀 | 수량·잔고·시장 상태·수수료·세금·멱등성 등 기존 테스트 포함 |
| 브라우저 | 데스크톱/모바일 전체 흐름 및 SSE 시나리오 통과 |
| 65초 요청 기록 | 상세 `/api/quote` 반복 0회, 장 상태 약 60초, quote 이벤트로 company/candles 증가 없음 |
| 수명 전환 | 빠른 종목 전환, offline/online, hidden/visible 이벤트, 한국 종목 및 로그아웃 정리 |
| web 프로세스 재시작 | 연결 중 테스트 web 실제 재시작 후 새 가격 snapshot 복구, EventSource 1개·REST fallback 없음 |
| flag OFF 롤백 | 데스크톱/모바일 회귀 및 31초 관측에서 REST 최초 1회+30초 1회, EventSource 0개 통과 |
| nginx HTTP/HTTPS | 실제 두 운영 설정으로 snapshot 각각 19.6ms/12.8ms, 후속 quote 및 7개 heartbeat, 95초 이상 idle 연결 통과 |
| 정적 검사 | Python 컴파일, JavaScript 문법, git diff 공백 검사 통과 |
| 공개 검사 | `python3 scripts/check_publication.py`: 비밀 값·개인 산출물 없음 |

실행 명령은 README의 독립 compose 명령을 사용했다. 운영 web에서 테스트를 실행하거나 운영 이미지를
교체하지 않았다. 최초 브라우저 검증에서 발견한 주문 후 보유 수량 preview 갱신 누락을 수정한 뒤 통과했다. flag OFF 검증에서 발견한 최근 본 종목 저장 누락도 공통 quote 적용 경로에서 수정했다.

### 전달 지연

로컬 Docker 네트워크의 테스트 Redis → FastAPI → Chromium DOM,
20회 결정적 시세 변경, 주입 HTTP 요청 왕복을 포함한 보수적 측정:
**p95 29.8ms** (앞선 실행 31.4ms). 1초 목표 이내. 측정 원본은 로컬 `artifacts/sse-latency.json`에 남는다.
공급자 timestamp 지연을 측정한 값이 아니며, 운영 부하나 인터넷 환경의 SLA를 뜻하지 않는다.

## 제약 및 운영 확인

- Finnhub REST/KIS와 기존 수집·공유 limiter를 유지한다. SSE만으로 거래소 실시간 체결을 제공하지 않는다.
- Pub/Sub replay는 없다. snapshot으로 최신 상태에 수렴하며 Redis 전체 초기화는 새 epoch로 복구한다.
- 10분 구독 만료 방지는 601초 fake clock으로 검증했다. 실제 공급자의 장기 429 및 운영 부하 시험은 수행하지 않았다.
- 100개 구독은 서버 queue/fan-out 검증이며 100개 실제 브라우저의 부하 시험은 아니다.
- hidden/visible 검증은 브라우저의 visibility 이벤트를 결정적으로 주입한다.
- default flag는 OFF다. 운영 활성화 전 실제 사용자 수·수집 종목 수·공급자 예산과 중간 CDN을 별도로 확인한다.
- 운영 반영/롤백 시 web 재생성과 페이지 새로고침으로 bootstrap 설정을 반영한다.
