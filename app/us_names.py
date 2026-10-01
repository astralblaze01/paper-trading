"""Korean names for US listings.

The KIS overseas master names most stocks in Korean (애플, 테슬라) but most ETFs
only in upper-case English ("VANGUARD S&P 500"). Popular ETFs get a hand-written
name; the rest are rendered word by word: issuers are transliterated the way
Korean brokers write them (뱅가드, 아이셰어즈), common fund terms are translated
(TREASURY → 국채, HIGH YIELD → 하이일드), and index names, tickers and other
acronyms (S&P 500, MSCI, QQQ) stay as they are.
"""
import re

# Hand-written names for widely held ETFs, as Korean brokers list them.
POPULAR = {
    'SPY': 'SPDR S&P 500 ETF', 'VOO': '뱅가드 S&P 500 ETF', 'IVV': '아이셰어즈 코어 S&P 500 ETF',
    'SPLG': 'SPDR 포트폴리오 S&P 500 ETF', 'QQQ': '인베스코 QQQ ETF', 'QQQM': '인베스코 나스닥 100 ETF',
    'DIA': 'SPDR 다우존스 산업평균 ETF', 'IWM': '아이셰어즈 러셀 2000 ETF', 'VTI': '뱅가드 토탈 주식시장 ETF',
    'SCHD': '슈왑 미국 배당주 ETF', 'JEPI': 'JP모건 에쿼티 프리미엄 인컴 ETF', 'JEPQ': 'JP모건 나스닥 에쿼티 프리미엄 인컴 ETF',
    'TQQQ': '프로셰어즈 울트라프로 QQQ ETF', 'SQQQ': '프로셰어즈 울트라프로 숏 QQQ ETF', 'QLD': '프로셰어즈 울트라 QQQ ETF',
    'UPRO': '프로셰어즈 울트라프로 S&P 500 ETF', 'SSO': '프로셰어즈 울트라 S&P 500 ETF', 'SH': '프로셰어즈 숏 S&P 500 ETF',
    'SOXL': '디렉시온 반도체 3배 ETF', 'SOXS': '디렉시온 반도체 -3배 ETF', 'TMF': '디렉시온 미국 국채 20년 이상 3배 ETF',
    'SOXX': '아이셰어즈 반도체 ETF', 'SMH': '반에크 반도체 ETF', 'ARKK': 'ARK 이노베이션 ETF',
    'VEA': '뱅가드 선진국 ETF', 'VWO': '뱅가드 신흥국 ETF', 'VIG': '뱅가드 배당성장 ETF', 'VYM': '뱅가드 고배당 ETF',
    'VGT': '뱅가드 정보기술 ETF', 'VNQ': '뱅가드 리츠 ETF', 'VUG': '뱅가드 성장주 ETF', 'VTV': '뱅가드 가치주 ETF',
    'VXUS': '뱅가드 토탈 국제주식 ETF', 'VT': '뱅가드 토탈 월드 주식 ETF', 'BND': '뱅가드 토탈 채권시장 ETF',
    'AGG': '아이셰어즈 코어 미국 종합채권 ETF', 'LQD': '아이셰어즈 투자등급 회사채 ETF', 'HYG': '아이셰어즈 하이일드 회사채 ETF',
    'SCHG': '슈왑 미국 대형 성장주 ETF', 'SCHX': '슈왑 미국 대형주 ETF', 'RSP': '인베스코 S&P 500 동일가중 ETF',
    'XLK': '기술 셀렉트 섹터 SPDR ETF', 'XLF': '금융 셀렉트 섹터 SPDR ETF', 'XLE': '에너지 셀렉트 섹터 SPDR ETF',
    'XLV': '헬스케어 셀렉트 섹터 SPDR ETF', 'XLY': '임의소비재 셀렉트 섹터 SPDR ETF', 'XLP': '필수소비재 셀렉트 섹터 SPDR ETF',
    'XLI': '산업재 셀렉트 섹터 SPDR ETF', 'XLU': '유틸리티 셀렉트 섹터 SPDR ETF', 'XLC': '커뮤니케이션 셀렉트 섹터 SPDR ETF',
    'SLV': '아이셰어즈 은 트러스트 ETF', 'IBIT': '아이셰어즈 비트코인 트러스트 ETF', 'EFA': '아이셰어즈 MSCI EAFE ETF',
    'EEM': '아이셰어즈 MSCI 신흥국 ETF', 'TIP': '아이셰어즈 물가연동 국채 ETF', 'USO': '미국 원유 펀드 ETF',
    'TLT': '아이셰어즈 미국 국채 20년 이상 ETF', 'IEF': '아이셰어즈 미국 국채 7–10년 ETF', 'SHY': '아이셰어즈 미국 국채 1–3년 ETF',
    'SGOV': '아이셰어즈 미국 단기 국채 ETF', 'BIL': 'SPDR 미국 초단기 국채 ETF', 'GOVT': '아이셰어즈 미국 국채 ETF',
    'VGSH': '뱅가드 미국 단기 국채 ETF', 'VGIT': '뱅가드 미국 중기 국채 ETF', 'VGLT': '뱅가드 미국 장기 국채 ETF',
    'GLD': 'SPDR 금 ETF', 'IAU': '아이셰어즈 금 트러스트 ETF', 'GLDM': 'SPDR 금 미니 ETF',
}

# Multi-word names and terms, matched before single words.
PHRASES = [
    ('T. ROWE PRICE', 'T.로우프라이스'), ('GOLDMAN SACHS', '골드만삭스'), ('JP MORGAN', 'JP모건'), ('J.P. MORGAN', 'JP모건'),
    ('JOHN HANCOCK', '존핸콕'), ('EATON VANCE', '이튼밴스'), ('BNY MELLON', 'BNY멜론'), ('JANUS HENDERSON', '재너스헨더슨'),
    ('NEUBERGER BERMAN', '뉴버거버먼'), ('COHEN & STEERS', '코헨앤스티어스'), ('FIRST TRUST', '퍼스트트러스트'), ('FT VEST', 'FT베스트'),
    ('GLOBAL X', '글로벌X'), ('STATE STREET', '스테이트스트리트'), ('DORSEY WRIGHT', '도시라이트'), ('NORTHERN TRUST', '노던트러스트'),
    ('LEVERAGE SHARES', '레버리지셰어즈'), ('DOW JONES', '다우존스'), ('NEW YORK', '뉴욕'),
    ('SHORT TERM', '단기'), ('SHORT-TERM', '단기'), ('INTERMEDIATE TERM', '중기'), ('INTERMEDIATE-TERM', '중기'),
    ('LONG TERM', '장기'), ('LONG-TERM', '장기'), ('ULTRA SHORT', '초단기'), ('ULTRA-SHORT', '초단기'),
    ('HIGH YIELD', '하이일드'), ('HIGH DIVIDEND', '고배당'), ('REAL ESTATE', '부동산'), ('EMERGING MARKETS', '신흥국'),
    ('EMERGING MARKET', '신흥국'), ('DEVELOPED MARKETS', '선진국'), ('COVERED CALL', '커버드콜'), ('EQUAL WEIGHT', '동일가중'),
    ('INVESTMENT GRADE', '투자등급'), ('ARTIFICIAL INTELLIGENCE', '인공지능'), ('HEALTH CARE', '헬스케어'),
    ('CONSUMER STAPLES', '필수소비재'), ('CONSUMER DISCRETIONARY', '임의소비재'), ('NATURAL RESOURCES', '천연자원'),
    ('NATURAL GAS', '천연가스'), ('SMALL CAP', '소형주'), ('SMALL-CAP', '소형주'), ('MID CAP', '중형주'), ('MID-CAP', '중형주'),
    ('LARGE CAP', '대형주'), ('LARGE-CAP', '대형주'), ('TOTAL STOCK MARKET', '토탈 주식시장'), ('TOTAL BOND MARKET', '토탈 채권시장'),
    ('MUNICIPAL BOND', '지방채'), ('CORPORATE BOND', '회사채'), ('RARE EARTH', '희토류'), ('MINIMUM VOLATILITY', '최소변동성'),
    ('MIN VOL', '최소변동성'), ('WELLS FARGO', '웰스파고'), ('MORGAN STANLEY', '모건스탠리'), ('MOTLEY FOOL', '모틀리풀'),
    ('BAILLIE GIFFORD', '베일리기퍼드'), ('ROUND HILL', '라운드힐'), ('TREASURY BOND', '국채'), ('TREASURY BD', '국채'), ('TAX-EXEMPT', '비과세'), ('EX-US', '미국 제외'), ('EX-CHINA', '중국 제외'),
]

WORDS = {
    # Issuers and fund families
    'ISHARES': '아이셰어즈', 'INVESCO': '인베스코', 'INNOVATOR': '이노베이터', 'PROSHARES': '프로셰어즈', 'VANGUARD': '뱅가드',
    'WISDOMTREE': '위즈덤트리', 'VANECK': '반에크', 'DIREXION': '디렉시온', 'FIDELITY': '피델리티', 'PACER': '페이서',
    'NUVEEN': '누빈', 'JPMORGAN': 'JP모건', 'FRANKLIN': '프랭클린', 'YIELDMAX': '일드맥스', 'ALLIANZIM': '알리안츠IM',
    'ROUNDHILL': '라운드힐', 'BLACKROCK': '블랙록', 'CALAMOS': '캘러모스', 'NORTHERN': '노던', 'GRANITESHARES': '그래닛셰어즈',
    'AMPLIFY': '앰플리파이', 'XTRACKERS': 'X트래커스', 'DIMENSIONAL': '디멘셔널', 'SIMPLIFY': '심플리파이', 'HARBOR': '하버',
    'PIMCO': '핌코', 'MICROSECTORS': '마이크로섹터스', 'DEFIANCE': '디파이언스', 'VIRTUS': '버투스', 'AVANTIS': '아반티스',
    'KRANESHARES': '크레인셰어즈', 'SCHWAB': '슈왑', 'VICTORYSHARES': '빅토리셰어즈', 'COLUMBIA': '컬럼비아', 'ABERDEEN': '애버딘',
    'MORNINGSTAR': '모닝스타', 'TRUESHARES': '트루셰어즈', 'HARTFORD': '하트퍼드', 'CAMBRIA': '캠브리아', 'BONDBLOXX': '본드블록스',
    'SPROTT': '스프랏', 'GRAYSCALE': '그레이스케일', 'NEOS': '네오스', 'PRINCIPAL': '프린시펄', 'APTUS': '앱터스',
    'WESTERN': '웨스턴', 'ADVISORSHARES': '어드바이저셰어즈', 'KURV': '커브', 'DOUBLELINE': '더블라인', 'GABELLI': '가벨리',
    'BETABUILDERS': '베타빌더스', 'BITWISE': '비트와이즈', 'ALPS': '알프스', 'FEDERATED': '페더레이티드', 'MATTHEWS': '매튜스',
    'VISTASHARES': '비스타셰어즈', 'STRIVE': '스트라이브', 'TOUCHSTONE': '터치스톤', 'MACKAY': '맥케이', 'RIVERNORTH': '리버노스',
    'PUTNAM': '퍼트넘', 'BULLETSHARES': '불렛셰어즈', 'YIELDBOOST': '일드부스트', 'ALPHADEX': '알파덱스', 'WEEKLYPAY': '위클리페이',
    'TRUSECTOR': '트루섹터', 'BLOOMBERG': '블룸버그', 'RUSSELL': '러셀', 'NASDAQ': '나스닥', 'FTSE': 'FTSE', 'SS': '',
    # Asset classes, styles and themes
    'INCOME': '인컴', 'US': '미국', 'U.S.': '미국', 'USA': '미국', 'AMERICAN': '아메리칸', 'AMERICA': '아메리카',
    'EQUITY': '주식', 'EQUITIES': '주식', 'STOCK': '주식', 'STOCKS': '주식', 'CAP': '캡', 'BOND': '채권', 'BONDS': '채권',
    'TRUST': '트러스트', 'TR': '트러스트', 'GLOBAL': '글로벌', 'INTERNATIONAL': '인터내셔널', 'GROWTH': '성장',
    'HIGH': '하이', 'SMALL': '소형', 'LARGE': '대형', 'MID': '중형', 'DIVIDEND': '배당', 'DIVIDENDS': '배당', 'CORE': '코어',
    'FIRST': '퍼스트', 'MUNICIPAL': '지방채', 'MUNI': '지방채', 'MARKETS': '마켓', 'MARKET': '마켓', 'VALUE': '가치',
    'YIELD': '일드', 'EMERGING': '신흥', 'CORPORATE': '회사채', 'STRATEGY': '전략', 'STRATEGIC': '전략', 'VEST': '베스트',
    'SHORT': '숏', 'LONG': '롱', 'ACTIVE': '액티브', 'DAILY': '데일리', 'TERM': '만기', 'AND': '&', 'TREASURY': '국채',
    'TREASURIES': '국채', 'ENHANCED': '인핸스드', 'OPPORTUNITIES': '기회', 'OPPORTUNITY': '기회', 'QUALITY': '퀄리티',
    'STRUCTURED': '구조화', 'SELECT': '셀렉트', 'CAPITAL': '캐피털', 'TARGET': '타깃', 'DURATION': '듀레이션',
    'DYNAMIC': '다이나믹', 'PLUS': '플러스', 'PREMIUM': '프리미엄', 'BITCOIN': '비트코인', 'FACTOR': '팩터',
    'TECHNOLOGY': '기술', 'TECH': '테크', 'POWER': '파워', 'HEDGED': '헤지', 'HEDGE': '헤지', 'ENERGY': '에너지',
    'OPTION': '옵션', 'PROTECTION': '프로텍션', 'GOLD': '금', 'SILVER': '은', 'COPPER': '구리', 'OIL': '원유', 'GAS': '가스',
    'REAL': '리얼', 'ESTATE': '부동산', 'INFRASTRUCTURE': '인프라', 'SECTOR': '섹터', 'SHARES': '셰어즈',
    'VOLATILITY': '변동성', 'MODERATE': '중립형', 'MANAGED': '매니지드', 'INVESTMENT': '투자', 'DEVELOPED': '선진',
    'MOMENTUM': '모멘텀', 'ULTRA': '울트라', 'ULTRAPRO': '울트라프로', 'PORTFOLIO': '포트폴리오', 'CREDIT': '크레딧',
    'GRADE': '등급', 'CHINA': '중국', 'LEADERS': '리더스', 'PREFERRED': '우선주', 'MULTI': '멀티', 'ASSET': '자산',
    'INTERMEDIATE': '중기', 'CONSUMER': '소비재', 'ALPHA': '알파', 'MINERS': '채굴기업', 'RATE': '금리', 'RATES': '금리',
    'ALLOCATION': '배분', 'MAX': '맥스', 'LOW': '로우', 'FREE': '프리', 'TAX': '세금', 'EQUAL': '동일', 'WEIGHT': '가중',
    'WEIGHTED': '가중', 'FIXED': '고정', 'TACTICAL': '전술', 'COVERED': '커버드', 'CALL': '콜', 'DUAL': '듀얼',
    'DIRECTIONAL': '디렉셔널', 'LADDERED': '래더', 'LADDER': '래더', 'AUTOCALLABLE': '오토콜러블', 'FOCUSED': '포커스',
    'FOCUS': '포커스', 'FUTURES': '선물', 'CASH': '현금', 'CAPPED': '캡드', 'UNCAPPED': '언캡드', 'DIVERSIFIED': '분산',
    'SECURITIES': '증권', 'INFLATION': '인플레이션', 'WORLD': '월드', 'RETURN': '리턴', 'EDGE': '엣지', 'TOP': '탑',
    'RISK': '리스크', 'TIPS': '물가연동국채', 'SERIES': '시리즈', 'INNOVATION': '이노베이션', 'NEW': '뉴', 'EUROPE': '유럽',
    'COMMODITY': '원자재', 'COMMODITIES': '원자재', 'HORIZON': '호라이즌', 'QUARTERLY': '분기', 'GROUP': '그룹', 'ALL': '올',
    'AGGREGATE': '종합', 'RESEARCH': '리서치', 'TOTAL': '토탈', 'INDIA': '인도', 'DEFENSE': '방산', 'ROTATION': '로테이션',
    'CALIFORNIA': '캘리포니아', 'JAPAN': '일본', 'FLOATING': '변동금리', 'SEMICONDUCTOR': '반도체', 'SEMICONDUCTORS': '반도체',
    'REIT': '리츠', 'REITS': '리츠', 'MULTIFACTOR': '멀티팩터', 'DEEP': '딥', 'ALTERNATIVE': '대체', 'THE': '',
    'MATERIALS': '소재', 'HEALTH': '헬스', 'HEALTHCARE': '헬스케어', 'CARE': '케어', 'METALS': '금속', 'CENTURY': '센추리',
    'CRYPTO': '크립토', 'DIGITAL': '디지털', 'DATA': '데이터', 'INDUSTRIALS': '산업재', 'INDUSTRIAL': '산업', 'INDUSTRY': '산업',
    'MATURITY': '만기', 'SERVICES': '서비스', 'RESOURCES': '자원', 'ETHEREUM': '이더리움', 'ETHER': '이더', 'STAPLES': '필수소비재',
    'UTILITIES': '유틸리티', 'ASIA': '아시아', 'PACIFIC': '퍼시픽', 'INTELLIGENCE': '인텔리전스', 'FUTURE': '미래',
    'CURRENCY': '통화', 'MORTGAGE': '모기지', 'ARTIFICIAL': '인공', 'FLEXIBLE': '플렉서블', 'FINANCIAL': '금융',
    'FINANCIALS': '금융', 'CONSERVATIVE': '안정형', 'CLIMATE': '기후', 'ADVANTAGE': '어드밴티지', 'INTERNET': '인터넷',
    'DISCRETIONARY': '임의소비재', 'PHYSICAL': '실물', 'GOVERNMENT': '국채', 'DISTRIBUTING': '분배', 'ACCELERATED': '가속',
    'CONVERTIBLE': '전환사채', 'PRIVATE': '사모', 'NEXT': '넥스트', 'OUTCOME': '아웃컴', 'THEMES': '테마', 'INTEREST': '이자',
    'SECURITIZED': '증권화', 'DEBT': '채권', 'BROAD': '브로드', 'SPACE': '우주', 'SOLANA': '솔라나', 'NORTH': '노스',
    'SENIOR': '선순위', 'SMART': '스마트', 'PURE': '퓨어', 'EFFICIENT': '효율', 'ROBOTICS': '로보틱스', 'MEMORY': '메모리',
    'NATIONAL': '내셔널', 'SOCIAL': '소셜', 'BIOTECH': '바이오테크', 'BIOTECHNOLOGY': '바이오테크', 'CARBON': '탄소',
    'BETA': '베타', 'BRAZIL': '브라질', 'LOAN': '대출', 'LOANS': '대출', 'CLEAN': '클린', 'CYBERSECURITY': '사이버보안',
    'ARISTOCRATS': '귀족', 'KOREA': '한국', 'COMMUNICATION': '커뮤니케이션', 'FRONTIER': '프런티어', 'BUFFER': '버퍼',
    'BULL': '불', 'BEAR': '베어', 'INVERSE': '인버스', 'LEVERAGED': '레버리지', 'INDEX': '지수', 'FUND': '펀드',
    'MONTH': '개월', 'YEAR': '년', 'WEEKLY': '위클리', 'MONTHLY': '월배당', 'CANADA': '캐나다', 'GERMANY': '독일',
    'TAIWAN': '대만', 'MEXICO': '멕시코', 'URANIUM': '우라늄', 'LITHIUM': '리튬', 'BATTERY': '배터리', 'SOLAR': '태양광',
    'WATER': '물', 'GENOMICS': '유전체', 'CLOUD': '클라우드', 'SOFTWARE': '소프트웨어', 'GAMING': '게임',
    # Further terms and issuers, by how often they appear
    'CORGI': '코기', 'DEFINED': '디파인드', 'ALT': '대체', 'SWAN': '스완', 'LIBERTY': '리버티', 'FLOW': '플로우',
    'COMPANY': '컴퍼니', 'EAGLE': '이글', 'ARCHITECT': '아키텍트', 'AWARE': '어웨어', 'IMPACT': '임팩트', 'OAK': '오크',
    'SUSTAINABLE': '지속가능', 'COWS': '카우즈', 'ADAPTIVE': '어댑티브', 'BARRIER': '배리어', 'FLOOR': '플로어',
    'HERMES': '헤르메스', 'OPPORTUNISTIC': '기회추구', 'UNITED': '유나이티드', 'OVERLAY': '오버레이', 'ENHANCE': '인핸스',
    'BUYWRITE': '바이라이트', 'ACHIEVERS': '어치버스', 'TEUCRIUM': '튜크리엄', 'LIFEPATH': '라이프패스',
    'MAGNIFICENT': '매그니피센트', 'DISCIPLINED': '디서플린드', 'CONCENTRATED': '집중', 'COMPUTING': '컴퓨팅',
    'ECOSYSTEM': '생태계', 'RISING': '라이징', 'TREND': '트렌드', 'SYSTEMATIC': '시스테마틱', 'ACCESS': '액세스',
    'BANKS': '은행', 'BANK': '은행', 'GENERATION': '제너레이션', 'VOYA': '보야', 'BIG': '빅', 'LIMITED': '리미티드',
    'MONEY': '머니', 'AEROSPACE': '항공우주', 'RATED': '등급', 'INSPIRE': '인스파이어', 'DOW': '다우', 'ZERO': '제로',
    'DATE': '데이트', 'LONGEVITY': '장수', 'GREEN': '그린', 'QUANTUM': '양자', 'DEFENSIVE': '방어', 'MINIMUM': '최소',
    'DISRUPTIVE': '혁신', 'TECHNOLOGIES': '테크놀로지', 'OPTIONS': '옵션', 'LAZARD': '라자드', 'STRATEGIES': '전략',
    'STANDARD': '스탠더드', 'MORGAN': '모건', 'STANLEY': '스탠리', 'GROWERS': '그로워스', 'PAY': '페이',
    'EARNINGS': '이익', 'STACKED': '스택드', 'FLEX': '플렉스', 'BLUE': '블루', 'MOAT': '모트', 'ANGEL': '엔젤',
    'JUNIOR': '주니어', 'GALAXY': '갤럭시', 'FOR': '포', 'ABSOLUTE': '절대', 'STRENGTH': '스트렝스', 'ECONOMY': '경제',
    'THEMATIC': '테마', 'SCIENCE': '사이언스', 'SCIENCES': '사이언스', 'BACKED': '담보', 'BUILDING': '빌딩',
    'EXPLORATION': '탐사', 'ASSETS': '자산', 'SHAREHOLDER': '주주', 'RETAIL': '소매', 'TEXAS': '텍사스',
    'APPRECIATION': '어프리시에이션', 'INVESTORS': '인베스터스', 'GUGGENHEIM': '구겐하임', 'STATES': '스테이츠',
    'PHOTONICS': '포토닉스', 'NUCLEAR': '원자력', 'PLAN': '플랜', 'COUPON': '쿠폰', 'CHIP': '칩', 'TAIL': '테일',
    'SOLUTIONS': '솔루션', 'ONE': '원', 'FINANCE': '파이낸스', 'TRANSPORTATION': '운송', 'BUFFERED': '버퍼드',
    'FUNDAMENTAL': '펀더멘털', 'INNOVATIVE': '혁신', 'INNOVATORS': '이노베이터스', 'BLOCK': '블록', 'EUROPEAN': '유럽',
    'ULTRASHORT': '울트라숏', 'TRANSITION': '전환', 'NOTE': '노트', 'NOTES': '노트', 'PRECIOUS': '귀', 'ADVANTAGED': '어드밴티지드',
    'MICRO': '마이크로', 'AGGRESSIVE': '공격형', 'DOLLAR': '달러', 'NEUTRAL': '뉴트럴', 'RESPONSIBLE': '책임투자',
    'LISTED': '상장', 'REVENUE': '매출', 'EURO': '유로', 'BOOST': '부스트', 'AUTOMATION': '자동화', 'VEHICLES': '차량',
    'ADVANCED': '어드밴스드', 'LOCAL': '로컬', 'EUROZONE': '유로존', 'STAKING': '스테이킹', 'MINING': '채굴',
    'QUANTITATIVE': '퀀트', 'ISRAEL': '이스라엘', 'MEDICAL': '의료', 'SENTIMENT': '심리', 'SECURITY': '보안',
    'CORPORATION': '코퍼레이션', 'PRICE': '프라이스', 'REGIONAL': '지역', 'MEGA': '메가', 'PREMIER': '프리미어',
    'TAXABLE': '과세', 'UTILITY': '유틸리티', 'TEMPLETON': '템플턴', 'BUY': '바이', 'MACRO': '매크로',
    'ARBITRAGE': '차익거래', 'COMMERCIAL': '상업', 'SELECTION': '셀렉션', 'SEVEN': '세븐', 'TARGETED': '타깃',
    'DAY': '데이', 'DOGS': '독스', 'UNCONSTRAINED': '언컨스트레인드', 'SOUTH': '사우스', 'PRODUCTION': '생산',
    'BLOCKCHAIN': '블록체인', 'ELECTRIC': '전기', 'ELECTRIFICATION': '전기화', 'VIDEO': '비디오', 'LATIN': '라틴',
    'AGRICULTURE': '농업', 'PHARMACEUTICALS': '제약', 'PHARMACEUTICAL': '제약', 'INTELLIGENT': '인텔리전트',
    'BUYBACK': '자사주매입', 'APPLIED': '어플라이드', 'BASIC': '기초', 'INFORMATION': '정보', 'CRUDE': '원유', 'CHAIN': '체인',
    'BUILDER': '빌더', 'DOGECOIN': '도지코인', 'ANALYST': '애널리스트', 'EXEMPT': '면세', 'PROTECTED': '보호',
    'INFLATION-LINKED': '물가연동', 'T-BILL': '단기국채', 'T-BILLS': '단기국채', 'BILL': '단기국채', 'BILLS': '단기국채',
    'THORNBURG': '손버그', 'TORTOISE': '토터스', 'ALLSPRING': '올스프링', 'ALERIAN': '알레리안', 'KENSHO': '켄쇼',
    'ZACKS': '잭스', 'BARON': '배런', 'MACQUARIE': '맥쿼리', 'KINETICS': '키네틱스', 'THRIVENT': '스리벤트',
    'JENNISON': '제니슨', 'BLACKSTONE': '블랙스톤', 'RIVERFRONT': '리버프런트', 'ROCKEFELLER': '록펠러', 'CALVERT': '캘버트',
    'DAVIS': '데이비스', 'TUTTLE': '터틀', 'SOFI': '소파이', 'VEGASHARES': '베가셰어즈', 'CURRENCYSHARES': '커런시셰어즈',
    'TRADR': '트레이더', 'GROWTH-ORIENTED': '성장형', 'EXCHANGE': '익스체인지', 'TRADED': '트레이디드', 'INTL': '인터내셔널',
    'EMERG': '신흥', 'MKTS': '마켓', 'MKT': '마켓', 'GOVT': '국채', 'CORP': '회사채', 'MUN': '지방채', 'HI': '하이',
    'YLD': '일드', 'DIV': '배당', 'EQ': '주식', 'TECHNOL': '기술', 'FINL': '금융', 'BD': '채권', 'TREAS': '국채',
    'JAN': '1월', 'FEB': '2월', 'MAR': '3월', 'APR': '4월', 'MAY': '5월', 'JUN': '6월', 'JUL': '7월', 'JULY': '7월',
    'AUG': '8월', 'SEP': '9월', 'SEPT': '9월', 'OCT': '10월', 'NOV': '11월', 'DEC': '12월',
}

HANGUL = re.compile('[가-힣]')
_PHRASE = [(re.compile(r'(?<![A-Z0-9])' + re.escape(src) + r'(?![A-Z0-9])'), dst) for src, dst in PHRASES]


def etf_name(name):
    """A Korean rendering of an upper-case English ETF name from the KIS master."""
    text = ' ' + name.upper() + ' '
    for pattern, dst in _PHRASE:
        text = pattern.sub(dst, text)
    # Words may sit beside '/', '(' or ',' ("LONG/SHORT"); hyphenated words are tried whole first.
    text = re.sub(r"[A-Z][A-Z.'-]*[A-Z.]|[A-Z]", lambda m: WORDS.get(m[0], m[0]), text)
    text = re.sub(r'(?<![0-9A-Z])(-?[0-9.]+)X(?![A-Z])', r'\1배', text)            # 2X → 2배
    text = re.sub(r'(국채|지방채|회사채) 채권', r'\1', text)
    out = re.sub(r'\s+', ' ', text).strip()
    if not re.search(r'\b(ETF|ETN)$', out): out += ' ETF'
    return out


def korean_name(row):
    """The display name of one KIS master row: hand-written, the master's Korean name,
    an ETF rendering, or the master's name as given (a few small listings have none)."""
    symbol, name = row['symbol'], row.get('name') or ''
    if symbol in POPULAR: return POPULAR[symbol]
    if HANGUL.search(name): return name
    if row.get('etf'): return etf_name(name or row.get('english') or symbol)
    return name or row.get('english') or symbol
