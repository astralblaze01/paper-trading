const $ = id => document.getElementById(id);
const storageNamespace = document.documentElement.dataset.storageNamespace;
let csrf = '', page = 1, weeklyPage = 1, pendingOrder = null;
let maxMode = false;
// One display rule for every amount: USD as "$1,000.00", KRW as "1,000원".
// Formatting only; amounts are never rounded for calculation here.
const usdFormat = new Intl.NumberFormat('en-US', {style: 'currency', currency: 'USD'}), krwFormat = new Intl.NumberFormat('ko-KR', {maximumFractionDigits: 0});
const nativeMoney = (x, currency) => {
  if (x == null) return '—';
  const n = Number(x); if (!Number.isFinite(n)) return '—';
  if (currency === 'KRW') { const won = Math.round(n); return krwFormat.format(won === 0 ? 0 : won) + '원'; }
  return usdFormat.format(Math.abs(n) < 0.005 ? 0 : n);
};
const money = x => nativeMoney(x, 'USD');
// Values that round to zero print as 0.00%, never -0.00%.
const pct = x => x == null ? '—' : `${(Math.abs(Number(x)) < 0.005 ? 0 : Number(x)).toFixed(2)}%`;
const categories = {us: '미국 주식 / ETF', kr: '한국 주식 / ETF', us_bond: '미국 채권 ETF', kr_bond: '한국 채권 ETF', gold: '금 ETF'};
let displayMode='native', viewFx=null, portfolioCache=null, rankingCache=null, historyCache=[], weeklyCache=null;
let rankingBucketSeen='', rankingRequest=null;
try{displayMode=localStorage.getItem(storageNamespace+':currency')||localStorage.getItem('paper-harbor:currency')||'native';}catch{}
if(!['native','KRW','USD'].includes(displayMode))displayMode='native';
function viewCurrency(native){return displayMode==='native'?native:displayMode;}
function viewValue(value,currency,rate=viewFx){
  if(value==null)return null;
  if(viewCurrency(currency)===currency)return Number(value);
  const n=Number(rate?.rate);if(!Number.isFinite(n)||n<=0)return null;
  return Number(value)*(currency==='USD'?n:1/n);
}
function viewMoney(value,currency,rate=viewFx){return nativeMoney(viewValue(value,currency,rate),viewCurrency(currency));}
function signed(value,text){const n=document.createElement('span');n.className=value==null?'':Number(value)>0?'gain':Number(value)<0?'loss':'flat';n.textContent=text;return n;}
function signedPct(value){const shown=value==null?null:Math.abs(Number(value))<0.005?0:Number(value);return signed(shown,value==null?'—':(shown>0?'+':'')+pct(value));}
function userLink(username){const a=document.createElement('a');a.href='#user/'+encodeURIComponent(username);a.textContent=username;a.className='user-link';return a;}
// Cash is always shown per wallet in its own currency, whatever the display currency.
function cashLines(w){const box=document.createElement('span');box.className='cash-lines';for(const c of ['USD','KRW']){const line=document.createElement('span');line.textContent=`${c}: ${nativeMoney(w?.[c],c)}`;box.append(line);}return box;}
function toast(text,kind='info',timeout=4500){
  const region=$('toasts');if(!region)return message(text);
  const item=document.createElement('div');item.className='toast toast-'+kind;item.setAttribute('role',kind==='error'?'alert':'status');
  const body=document.createElement('p');body.textContent=text;
  const close=document.createElement('button');close.type='button';close.className='toast-close';close.setAttribute('aria-label','알림 닫기');close.textContent='×';
  const dismiss=()=>{item.classList.add('leaving');setTimeout(()=>item.remove(),200);};
  close.addEventListener('click',dismiss);item.append(body,close);region.append(item);
  while(region.children.length>4)region.firstElementChild.remove();
  setTimeout(dismiss,timeout);
}
function stockLink(x){const a=document.createElement('a');a.href='#detail/'+encodeURIComponent(x.symbol);a.textContent=x.name+' · '+x.symbol;a.className='text-button portfolio-stock-link';return a;}
function renderPositions(target,p){table(target,['종목','수량','평균가','현재가','평가액','미실현 손익','수익률'],p.positions.map(x=>[stockLink(x),x.quantity,viewMoney(x.average_cost,x.currency),viewMoney(x.quote?.native_price??x.quote?.price,x.currency),viewMoney(x.value,x.currency),signed(x.pnl,viewMoney(x.pnl,x.currency)),signedPct(x.return_pct)]));}
function renderPortfolio(){const p=portfolioCache;if(!p)return;
  $('metrics').replaceChildren();
  renderMetrics($('metrics'),p);
  renderPositions($('positions'),p);
  if(window.renderAllocation)renderAllocation($('allocation'),p);
  if(window.renderMyProfile)renderMyProfile();
  $('realized').textContent='누적 실현손익: '+viewMoney(p.realized_pnl.USD,'USD')+' / '+viewMoney(p.realized_pnl.KRW,'KRW')+' · 초기 달러 원금의 환율 변동 효과: '+viewMoney(p.initial_fx_effect,'KRW')+' · 그 외 손익: '+viewMoney(p.other_pnl,'KRW')+' · '+(p.return_basis||'초기 KRW 평가액 대비 (외부 입출금 반영)');
}
function renderMetrics(target,p){
  target.replaceChildren();
  const fields=[['총 평가금액',viewMoney(p.equity,'KRW')],['현금',cashLines(p.wallets)],['총 손익',viewMoney(p.pnl,'KRW'),p.pnl],['누적 수익률',p.return_pct==null?'—':(Number(p.return_pct)>0?'+':'')+pct(p.return_pct),p.return_pct]];
  for(const [name,value,change] of fields){const box=document.createElement('div');box.className='metric';const label=document.createElement('small');label.textContent=name;let v;if(value instanceof Node){v=document.createElement('span');v.append(value);}else v=signed(change,value);v.classList.add('metric-value');box.append(label,v);target.append(box);}
}
function renderRanking(){if(!rankingCache)return;
  const status=$('rankingStatus');
  if(status){
    const markets=(rankingCache.market_status||[]).map(x=>`${x.market==='KR'?'한국':'미국'} ${x.label}`).join(' · ');
    const stamp=value=>new Date(value).toLocaleString('ko-KR',{timeZone:'Asia/Seoul',hour12:false});
    const asOf=rankingCache.updated_at?stamp(rankingCache.updated_at):'아직 없음';
    const next=rankingCache.next_refresh_at?stamp(rankingCache.next_refresh_at):'다음 경계 시각';
    status.textContent=rankingCache.incomplete
      ? `마지막 정상 갱신: ${asOf} · `+(rankingCache.errors||[]).join(' ')
      : (rankingCache.market_open===false
        ? `장이 닫혀 마지막 랭킹을 유지합니다 · 기준 ${asOf}${markets?' · '+markets:''}`
        : `기준 ${asOf} · 다음 갱신 ${next}${markets?' · '+markets:''} · 10초 단위`);
  }
  const person=x=>{const box=document.createElement('span');box.className='rank-user';if(window.avatar)box.append(avatar(x.username,x.image_version,'small'));box.append(userLink(x.username));return box;};
  // Ranked by USD value; shown in the selected display currency at the snapshot's rate.
  table($('ranking'),['순위','사용자 · 프로필 보기','총 평가금액 ('+viewCurrency('USD')+')','누적 수익률'],rankingCache.rows.map(x=>[x.rank,person(x),viewMoney(x.equity_usd,'USD',x.fx||viewFx),signedPct(x.return_pct)]));
  if(window.renderMyProfile)renderMyProfile();
}
function syncCurrency(){$('displayCurrency').value=displayMode;$('displayRateNote').textContent=viewFx?`${viewFx.date} 기준 · 1 USD = ${Number(viewFx.rate).toLocaleString('ko-KR',{maximumFractionDigits:2})} KRW · 환산 표시만 변경`:'환율 확인 중';}
async function changeDisplayCurrency(value){displayMode=value;try{localStorage.setItem(storageNamespace+':currency',value);}catch{}syncCurrency();renderPortfolio();renderRanking();renderHistory();if(weeklyCache)renderWeekly();window.dispatchEvent(new Event('displaycurrencychange'));}
$('displayCurrency').addEventListener('change',e=>changeDisplayCurrency(e.target.value));
function message(text) { $('status').textContent = text; }
async function api(path, body) {
  const response = await fetch('/api/' + path, {method: body ? 'POST' : 'GET', headers: body ? {'Content-Type': 'application/json', 'X-CSRF-Token': csrf} : {}, body: body ? JSON.stringify(body) : undefined});
  let data;
  try { data = await response.json(); } catch { throw Error(`요청 실패 (${response.status}). 잠시 후 다시 시도하세요.`); }
  if (!response.ok) {
    if (typeof data.detail === 'string') throw Error(data.detail);
    if (Array.isArray(data.detail)) {
      const labels={recipient:'받는 사용자',amount:'금액',currency:'통화',username:'사용자 이름',password:'비밀번호',quantity:'수량',symbol:'종목'};
      const fields = [...new Set(data.detail.map(x => labels[x.loc?.at(-1)]||x.loc?.at(-1)).filter(Boolean))];
      throw Error(fields.length ? `${fields.join(', ')} 값을 확인하세요.` : '요청 값을 확인하세요.');
    }
    throw Error(`요청을 처리하지 못했습니다 (${response.status}).`);
  }
  return data;
}
function table(target, headers, rows) {
  const t = document.createElement('table'), thead = document.createElement('thead'), head = document.createElement('tr'), tbody = document.createElement('tbody');
  headers.forEach(h => { const th = document.createElement('th'); th.scope = 'col'; th.textContent = h; head.append(th); });
  thead.append(head); t.append(thead, tbody);
  rows.forEach(row => { const tr = document.createElement('tr'); row.forEach(value => { const td = document.createElement('td'); if(value instanceof Node)td.append(value);else td.textContent = value; tr.append(td); }); tbody.append(tr); });
  target.replaceChildren(t);
  if (!rows.length) { const p = document.createElement('p'); p.className = 'empty-state'; p.textContent = {positions:'아직 보유한 종목이 없습니다. 첫 주문을 시작해보세요.',publicPositions:'보유한 종목이 없습니다.',history:'아직 거래내역이 없습니다.',transferHistory:'아직 이체내역이 없습니다.',fxHistory:'아직 환전내역이 없습니다.',adminAudit:'관리자 작업 기록이 없습니다.'}[target.id] || '표시할 순위가 없습니다.'; target.append(p); }
}
async function boot() {
  const s = await api('session'); csrf = s.csrf;
  window.sessionUsername=s.username; window.isAdmin=!!s.is_admin; $('auth').hidden = !!s.username; $('dashboard').hidden = !s.username; $('logout').hidden = !s.username; $('adminNav').hidden = !s.is_admin;
  if(!s.username)showAuthView();
  refreshNotice();
  document.querySelectorAll('.app-nav a').forEach(a=>{if(s.is_admin)a.hidden=a.id!=='adminNav';else if(a.id!=='adminNav')a.hidden=false;});
  const settings=document.querySelector('.view-settings');if(settings)settings.hidden=!!s.is_admin;
  const unavailable = [];
  if (!s.providers.us) unavailable.push('미국 시세');
  if (!s.providers.kr) unavailable.push('한국 시세');
  if (unavailable.length) message(unavailable.join(' · ') + ' 서비스 연결이 필요합니다. 계좌 생성과 지원 종목 목록 조회는 이용할 수 있습니다.');
  if (s.username) {
    if(s.is_admin){if(location.hash!=='#admin')location.hash='admin';if(window.routePage)await routePage();}
    else {$('greeting').textContent = s.username + '님의 투자 현황'; await Promise.all([refresh(),refreshMarketSessions()]); if(window.renderRecentStocks)renderRecentStocks(); if(window.routePage) await routePage();}
  }
}
async function refreshMarketSessions(){
  if(!window.sessionUsername||window.isAdmin)return;
  try{const r=await api('market-overview');window.marketOpen=Object.fromEntries(r.markets.map(x=>[x.market,['정규장','장전','장후','프리장','애프터장'].includes(x.label)]));$('marketSessions').textContent=r.markets.map(x=>{const unsupported=x.market==='US'&&['프리장','애프터장'].includes(x.label)&&x.extended_prices===false?' (체결 시세 미지원)':'';return `${x.market==='KR'?'한국':'미국'} ${x.label}${unsupported}`;}).join(' · ');}catch(e){$('marketSessions').textContent='시장 상태 확인 불가';}
}
setInterval(()=>{if(!document.hidden)refreshMarketSessions();},60000);
// Notice posted by an administrator (e.g. the maintenance template): a banner only, nothing is blocked.
function showSiteNotice(notice){
  const box=$('siteNotice');box.hidden=!notice||!!window.isAdmin;if(box.hidden)return;
  box.dataset.kind=notice.kind;box.querySelector('.notice-kind').textContent=notice.label;
  box.querySelector('.notice-title').textContent=notice.title;box.querySelector('.notice-body').textContent=notice.body;
}
async function refreshNotice(){try{showSiteNotice((await api('notice')).notice);}catch{}}
setInterval(()=>{if(!document.hidden)refreshNotice();},30000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refreshNotice();});
// The price collector fetches a symbol only after it is first requested, so a
// holding can briefly have no price. Re-read the valuation until all are priced.
let pricingRetryTimer=null;
function retryMissingPrices(load,attempt=1){
  clearTimeout(pricingRetryTimer);
  if(attempt>4)return;
  pricingRetryTimer=setTimeout(async()=>{try{const p=await load();if(p&&p.positions.some(x=>x.value==null))retryMissingPrices(load,attempt+1);}catch{}},2500*attempt);
}
function applyPortfolio(p){
  window.walletBalances=p.wallets; if(window.updateFxBalance)window.updateFxBalance();
  portfolioCache=p;viewFx=p.fx||viewFx;syncCurrency();renderPortfolio();
}
async function reloadPortfolioPrices(){
  if(!window.sessionUsername||window.isAdmin)return null;
  const p=await api('portfolio');applyPortfolio(p);
  if(!p.errors.length&&$('status').textContent.includes('준비 중'))message('');
  return p;
}
async function refresh() {
  const p = await api('portfolio'); $('metrics').replaceChildren();
  applyPortfolio(p);
  if(p.positions.some(x=>x.value==null))retryMissingPrices(reloadPortfolioPrices);
  if (p.errors.length) message(p.errors.join('\n')); else if (p.stale) message('마지막 제공 시세 기준 평가입니다. 지연 시세 종목은 거래가 제한됩니다.');
  await history(); if(window.loadLimits)await loadLimits(); await weekly(); const r = await api('ranking');
  rankingCache=r;renderRanking(); rankingBucketSeen=seoulTenSecondKey();
  if (r.incomplete) message('시세를 조회할 수 없어 전체 랭킹을 잠시 표시하지 않습니다.');
}
async function refreshRankingOnly(){
  if(rankingRequest)return rankingRequest;
  rankingRequest=(async()=>{try{rankingCache=await api('ranking');}catch(e){if(rankingCache)rankingCache={...rankingCache,incomplete:true,stale:true,errors:[e.message]};else throw e;}renderRanking();})();
  try{await rankingRequest;}finally{rankingRequest=null;}
}
async function history() {
  const rows = await api('transactions?page=' + page);
  historyCache=rows;renderHistory();
  $('page').textContent = page + ' 페이지'; $('previous').disabled = page === 1; $('next').disabled = rows.length < 50;
}
function renderHistory(){const rows=historyCache;
  table($('history'), ['체결 시각', '종목', '매매', '수량', '체결가', '총액', '수수료 / 세금', '정산 금액'], rows.map(t => [new Date(t.created_at).toLocaleString(), t.symbol, t.side === 'buy' ? '매수' : '매도', t.quantity, viewMoney(t.native_price,t.currency),viewMoney(t.gross_amount,t.currency),viewMoney(t.fee,t.currency)+' / '+viewMoney(t.tax,t.currency),viewMoney(t.net_amount,t.currency)]));
  $('page').textContent = page + ' 페이지'; $('previous').disabled = page === 1; $('next').disabled = rows.length < 50;
}
async function search() {
  const category = $('category').value;
  const rows = await api('search?' + new URLSearchParams({q: $('query').value, category}));
  $('searchResults').replaceChildren();
  for (const row of rows) {
    const b = document.createElement('button'); b.type = 'button';
    const title = document.createElement('strong'), detail = document.createElement('span'); title.textContent = row.name; detail.textContent = window.isAdmin ? `${row.symbol} · ${categories[row.category]} · ${row.currency}` : `${categories[row.category]} · ${row.currency}`;
    b.append(title, detail); b.addEventListener('click', () => { if(window.openStock) openStock(row.symbol); else $('symbol').value=row.symbol; }); $('searchResults').append(b);
  }
  if (!rows.length) { const p = document.createElement('p'); p.className = 'field-help'; p.textContent = '검색 결과가 없습니다. 한국 종목은 6자리 코드로도 조회할 수 있습니다.'; $('searchResults').append(p); }
}
function handle(id, event, fn) { $(id).addEventListener(event, async e => { e.preventDefault(); try { await fn(e); } catch (err) { message(err.message); } }); }
// Signed-out pages: the start page logs in; #signup is the separate registration page.
function showAuthView(){const signup=location.hash==='#signup';$('authForm').hidden=signup;$('registerForm').hidden=!signup;$('registerError').textContent='';(signup?$('registerUsername'):$('username')).focus({preventScroll:true});}
window.addEventListener('hashchange',()=>{if(!window.sessionUsername)showAuthView();});
// Note: the global history() below (transaction list) shadows window.history, so only the hash is used.
$('showLogin').addEventListener('click',e=>{e.preventDefault();location.hash='';showAuthView();});
handle('authForm', 'submit', async () => { $('loginSubmit').disabled=true; try { await api('login', {username: $('username').value, password: $('password').value}); } finally { $('loginSubmit').disabled=false; } $('password').value = ''; message(''); if(location.hash==='#signup')location.hash=''; await boot(); window.scrollTo(0,0); });
function registerProblem(){
  const username=$('registerUsername').value.trim(),password=$('registerPassword').value,confirm=$('registerConfirm').value;
  if(!username)return '아이디를 입력하세요.';
  if(!/^[가-힣a-zA-Z0-9_]{3,32}$/.test(username))return '아이디는 한글, 영문, 숫자, 밑줄로 3~32자여야 합니다.';
  if(!password)return '비밀번호를 입력하세요.';
  if(password.length<8)return '비밀번호는 8자 이상이어야 합니다.';
  if(password!==confirm)return '비밀번호가 일치하지 않습니다.';
  return '';
}
$('registerForm').addEventListener('submit',async e=>{
  e.preventDefault();const problem=registerProblem();$('registerError').textContent=problem;if(problem)return;
  $('registerSubmit').disabled=true;
  try{
    const r=await api('register',{username:$('registerUsername').value.trim(),password:$('registerPassword').value,password_confirm:$('registerConfirm').value});
    $('registerForm').reset();$('username').value=r.username;$('password').value='';
    location.hash='';showAuthView();$('password').focus();
    toast('회원가입이 완료되었습니다. 만든 계정으로 로그인하세요.','success');
  }catch(err){$('registerError').textContent=err.message;}finally{$('registerSubmit').disabled=false;}
});
handle('logout', 'click', async () => { await api('logout', {}); pendingOrder = null; message(''); await boot(); });
handle('refresh', 'click', refresh);
handle('searchForm', 'submit', search);
handle('category', 'change', async () => { $('query').value = ''; $('marketHelp').textContent = $('category').value === 'kr' ? '등록된 이름으로 검색하거나, 한국 종목의 6자리 코드를 입력하세요.' : '채권·금 분류는 등록된 ETF 목록입니다. 개별 채권과 금 현물은 지원하지 않습니다.'; await search(); });
handle('quote', 'click', async () => {
  if(window.openStock) { openStock($('symbol').value); return; }
  const q = await api('quote/' + encodeURIComponent($('symbol').value));
  const adminStamp = window.isAdmin ? ` · ${new Date(q.timestamp * 1000).toLocaleString()} · ${q.data_status || q.source || '공급자 시세'}` : '';
  $('quoteInfo').textContent = `${viewMoney(q.native_price, q.currency)} · 실제 주문 통화 ${q.currency}${q.stale ? ' · 오래된 시세: 주문은 새 가격이 올 때까지 대기합니다.' : ' · 주문 시 가격이 달라질 수 있습니다.'}${adminStamp}`;
});
handle('orderForm', 'submit', async () => {
  const data = {symbol: $('symbol').value, side: $('side').value, quantity: Number($('quantity').value), use_max: maxMode}; const signature = JSON.stringify(data);
  if (!pendingOrder || pendingOrder.signature !== signature) {
    const bytes = crypto.getRandomValues(new Uint8Array(16)); bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
    const h = Array.from(bytes, b => b.toString(16).padStart(2, '0')).join(''); pendingOrder = {signature, id: `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`};
  }
  $('submitOrder').disabled = true;
  const sideName = data.side === 'buy' ? '매수' : '매도', label = window.orderSymbolLabel ? orderSymbolLabel() : data.symbol;
  let result;
  try { result = await api('orders', {...data, request_id: pendingOrder.id}); }
  catch (err) { $('submitOrder').disabled = false; toast(`${sideName} 주문을 처리하지 못했습니다.\n${err.message}`, 'error', 7000); return; }
  pendingOrder = null; page = 1; maxMode=false;
  toast(result.replayed ? `이미 처리된 ${sideName} 주문입니다.\n${label} ${result.quantity}주` : `${sideName} 주문이 체결되었습니다.\n${label} ${result.quantity}주`, 'success');
  try { await refresh(); if(window.loadStock) await loadStock(false); } finally { $('submitOrder').disabled = false; }
});
handle('previous', 'click', async () => { page = Math.max(1, page - 1); await history(); });
handle('next', 'click', async () => { page++; await history(); });
const reportDate = value => new Date(value).toLocaleString('ko-KR', {timeZone: 'Asia/Seoul', year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'});
async function weekly() {
  weeklyCache = await api('weekly?page=' + weeklyPage);renderWeekly();
}
function renderWeekly(){
  const data=weeklyCache;
  const days = ['월', '화', '수', '목', '금', '토', '일'];
  $('weeklySchedule').textContent = data.enabled ? `매주 ${days[data.weekday]}요일 ${data.hour}시 · 한국 시간` : '자동 게시 중지';
  $('weeklyStatus').textContent = data.error || (data.baseline_at ? `다음 게시: ${reportDate(data.next_due)} · 직전 집계 자산 대비 기간 수익률 · ${data.reports[0]?.return_basis||'누적 수익률은 초기 KRW 평가액 대비 (외부 입출금 반영)'}` : '첫 주간 기록을 준비하고 있습니다. 계좌의 기준 평가금액이 저장되면 집계가 시작됩니다.');
  $('weeklyReports').replaceChildren();
  for (const report of data.reports) {
    const article = document.createElement('article'); article.className = 'weekly-report';
    const period = document.createElement('p'); period.className = 'weekly-period'; period.textContent = `${reportDate(report.period_start)} ~ ${reportDate(report.period_end)}`;
    const title = document.createElement('h3'); const winners = report.rows.filter(row => row.rank === 1);
    title.textContent = winners.length ? `${winners.length > 1 ? '공동 ' : ''}1위 · ${winners.map(row => row.username).join(', ')} (${pct(winners[0].return_pct)})` : '이번 집계에는 비교 가능한 참여자가 없습니다.';
    const details = document.createElement('details'), summary = document.createElement('summary'), list = document.createElement('div'); list.className = 'scroll'; summary.textContent = `전체 순위 보기 · ${report.rows.length}명`;
    table(list, ['순위', '사용자', '기간 수익률', '기간 손익 ('+viewCurrency(report.base_currency)+')', '누적 수익률'], report.rows.map(row => [row.rank, userLink(row.username), signedPct(row.return_pct), signed(row.pnl,viewMoney(row.pnl,report.base_currency,report.fx_rate?{rate:report.fx_rate}:null)), signedPct(row.total_return_pct)]));
    details.append(summary, list);
    const note = document.createElement('p'); note.className = 'weekly-note';
    note.textContent = `시작 평가액이 없거나 0인 ${report.excluded_new_or_zero}명은 비교에서 제외됩니다.` + (report.oldest_quote ? ` 사용 시세 중 가장 오래된 시각: ${reportDate(report.oldest_quote)}.` : '') + (report.late ? ' 게시가 지연되어 실제 집계 시점까지의 성과입니다.' : '');
    article.append(period, title, details, note); $('weeklyReports').append(article);
  }
  if (!data.reports.length) { const p = document.createElement('p'); p.className = 'empty-state'; p.textContent = '아직 게시된 주간 순위가 없습니다. 첫 결과는 예정된 집계 후 표시됩니다.'; $('weeklyReports').append(p); }
  $('weeklyPage').textContent = weeklyPage + ' 페이지'; $('weeklyPrevious').disabled = weeklyPage === 1; $('weeklyNext').disabled = data.reports.length < 10;
}
handle('weeklyPrevious', 'click', async () => { weeklyPage = Math.max(1, weeklyPage - 1); await weekly(); });
handle('weeklyNext', 'click', async () => { weeklyPage++; await weekly(); });
function seoulTenSecondKey(date=new Date()){
  const parts=Object.fromEntries(new Intl.DateTimeFormat('en-US',{timeZone:'Asia/Seoul',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).formatToParts(date).filter(x=>x.type!=='literal').map(x=>[x.type,x.value]));
  return `${parts.year}-${parts.month}-${parts.day}-${parts.hour}-${parts.minute}-${Math.floor(Number(parts.second)/10)}`;
}
setInterval(async()=>{
  if(document.hidden||!window.sessionUsername||$('dashboard').hidden)return;
  const pageName=(location.hash.replace(/^#/,'').split('/')[0]||'explore');
  if(pageName!=='ranking')return;
  const key=seoulTenSecondKey(); if(key===rankingBucketSeen)return;
  rankingBucketSeen=key;
  try{await refreshRankingOnly();}catch(err){message(err.message);}
},1000);
boot().catch(e => message(e.message));
