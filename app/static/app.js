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
// Account returns come in two bases.  Dollar display shows the USD-basis
// return (dollar principal vs dollar value); everything else the KRW basis,
// which also carries the USD/KRW move.  Holdings keep their own currency.
function returnBasis(){return displayMode==='USD'?'USD':'KRW';}
function basisLabel(basis=returnBasis()){return basis==='USD'?'달러 기준':'원화 기준';}
function accountReturn(p,basis=returnBasis()){return basis==='USD'?p.return_pct_usd:p.return_pct;}
// Total value colored by the account's result: red up, blue down, default ink when even.
// `value` is rounded to what is displayed first, so "0원"/"0.00%" never shows a color.
function equityTone(text,value,unit){const n=value==null?0:Math.round(Number(value)/unit),box=document.createElement('span');box.className=n>0?'gain':n<0?'loss':'';box.textContent=text;return box;}
const signedPctText=x=>x==null?'—':(Number(x)>0?'+':'')+pct(x);
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
// 거래 통화: each holding in its own currency, no FX. 원화/달러: cost at each fill's rate
// against today's value, so a US stock in KRW also carries the USD/KRW move.
function renderPositions(target,p){
  const mode=displayMode==='native'?null:displayMode;
  const suffix=mode?`(${basisLabel(mode)})`:'(거래 통화)';
  table(target,['종목','수량','평균가','현재가','평가액','평가손익 '+suffix,'수익률 '+suffix],p.positions.map(x=>{
    const now=x.quote?.native_price??x.quote?.price;
    if(!mode)return [stockLink(x),x.quantity,nativeMoney(x.average_cost,x.currency),nativeMoney(now,x.currency),nativeMoney(x.value,x.currency),signed(x.pnl,nativeMoney(x.pnl,x.currency)),signedPct(x.return_pct)];
    const b=x.basis?.[mode]||{};
    return [stockLink(x),x.quantity,nativeMoney(b.average_cost,mode),viewMoney(now,x.currency),viewMoney(x.value,x.currency),signed(b.pnl,nativeMoney(b.pnl,mode)),signedPct(b.return_pct)];
  }));
}
function renderPortfolio(){const p=portfolioCache;if(!p)return;
  $('metrics').replaceChildren();
  renderMetrics($('metrics'),p);
  renderPositions($('positions'),p);
  if(window.renderAllocation)renderAllocation($('allocation'),p);
  if(window.renderMyProfile)renderMyProfile();
}
function renderMetrics(target,p){
  target.replaceChildren();
  const basis=returnBasis(),other=basis==='USD'?'KRW':'USD';
  const pnl=basis==='USD'?p.pnl_usd:p.pnl,ret=accountReturn(p,basis),otherRet=accountReturn(p,other);
  const fields=[['총 평가금액',equityTone(viewMoney(p.equity,'KRW'),pnl,basis==='USD'?.01:1)],['현금',cashLines(p.wallets)],
    ['평가손익',nativeMoney(pnl,basis),pnl,basisLabel(basis)+' · 확정 손익 포함'],
    ['평가 수익률',signedPctText(ret),ret,`${basisLabel(other)} ${signedPctText(otherRet)}`]];
  for(const [name,value,change,sub] of fields){const box=document.createElement('div');box.className='metric';const label=document.createElement('small');label.textContent=name;let v;if(value instanceof Node){v=document.createElement('span');v.append(value);}else v=signed(change,value);v.classList.add('metric-value');box.append(label,v);if(sub)box.append(node('small',sub,'metric-sub'));target.append(box);}
}
// Ranks 1-3 get a medal: ring, laurel wings, a star and a ribbon carrying 3/2/1 stars.
const MEDALS={1:{rim:'#f2a31b',face:'#ffdc8e',ink:'#d98511',leaf:'#f7b638',ribbon:'#ea4a4f'},
              2:{rim:'#aeb6bf',face:'#e8ecf0',ink:'#8e98a3',leaf:'#c3cad2',ribbon:'#8b5cf6'},
              3:{rim:'#c8691c',face:'#f7c393',ink:'#b25714',leaf:'#e38b3c',ribbon:'#1f3a8a'}};
function starPath(cx,cy,r){let d='';for(let i=0;i<10;i++){const a=-Math.PI/2+i*Math.PI/5,k=i%2?r*.45:r;d+=(i?'L':'M')+(cx+k*Math.cos(a)).toFixed(2)+' '+(cy+k*Math.sin(a)).toFixed(2);}return d+'Z';}
function medalSvg(rank){
  // Feathers start behind the ring and sweep outward and up, like the wings of a trophy medal.
  const c=MEDALS[rank],leaves=[],u=deg=>[Math.cos(deg*Math.PI/180),Math.sin(deg*Math.PI/180)],f=n=>n.toFixed(2);
  for(const deg of rank===3?[128,158,188]:[118,144,170,196]){
    const [bx,by]=u(deg),[dx,dy]=u(deg+62),base=[24+12*bx,21+12*by],len=rank===3?13:14.5;
    const tip=[base[0]+len*dx,base[1]+len*dy],mid=[(base[0]+tip[0])/2,(base[1]+tip[1])/2],w=3.8;
    leaves.push(`<path d="M${f(base[0])} ${f(base[1])}Q${f(mid[0]-dy*w)} ${f(mid[1]+dx*w)} ${f(tip[0])} ${f(tip[1])}Q${f(mid[0]+dy*w)} ${f(mid[1]-dx*w)} ${f(base[0])} ${f(base[1])}Z"/>`);
  }
  const ribbonStars=[20,24,28].slice(0,4-rank).map((x,i,all)=>`<path d="${starPath(x+(3-all.length)*2,43.5,1.9)}"/>`).join('');
  return `<svg viewBox="0 0 48 56" aria-hidden="true" focusable="false">
    <path d="M16.5 30h15v25l-7.5-5-7.5 5z" fill="${c.ribbon}"/><g fill="#fff">${ribbonStars}</g>
    <g fill="${c.leaf}">${leaves.join('')}</g><g fill="${c.leaf}" transform="matrix(-1 0 0 1 48 0)">${leaves.join('')}</g>
    <circle cx="24" cy="21" r="15" fill="${c.rim}"/><circle cx="24" cy="21" r="11.5" fill="${c.face}"/>
    <text x="24" y="26.6" text-anchor="middle" font-size="16" font-weight="800" font-family="Arial, sans-serif" fill="${c.ink}">${rank}</text>
    <path d="${starPath(31.5,11.5,2.2)}" fill="#fff" opacity=".9"/>
    <path d="${starPath(24,35.5,4.6)}" fill="${c.leaf}" stroke="${c.rim}" stroke-width=".8"/></svg>`;
}
function rankBadge(rank){const n=document.createElement('span');n.className='rank-badge';if(rank<=3){n.classList.add('rank-medal');n.innerHTML=medalSvg(rank);n.setAttribute('role','img');n.setAttribute('aria-label',rank+'위');}else n.textContent=rank;return n;}
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
  const person=x=>{const box=document.createElement('span');box.className='rank-user';if(x.tier)box.dataset.tier=x.tier;if(window.avatar)box.append(avatar(x.username,x.image_version,'small'));if(x.tier&&window.tierIcon)box.append(tierIcon(x.tier));const link=userLink(x.username);if(x.tier)link.classList.add('tier-text-'+x.tier);box.append(link);if(window.rankChange)box.append(rankChange(x.rank,x.previous_rank));return box;};
  // Ranked by USD value; shown in the selected display currency at the snapshot's rate.
  table($('ranking'),['순위','사용자 · 프로필 보기','총 평가금액 ('+viewCurrency('USD')+')','평가 수익률 ('+basisLabel()+')'],rankingCache.rows.map(x=>[rankBadge(x.rank),person(x),equityTone(viewMoney(x.equity_usd,'USD',x.fx||viewFx),accountReturn(x),.01),signedPct(accountReturn(x))]));
  $('ranking').querySelectorAll('tbody tr').forEach((tr,i)=>{const rank=rankingCache.rows[i].rank;if(rank<=3)tr.classList.add('top-rank','top-rank-'+rank);});
  if(window.renderMyProfile)renderMyProfile();
  window.renderHeaderUser?.();
}
function syncCurrency(){$('displayCurrency').value=displayMode;$('displayRateNote').textContent=viewFx?`${viewFx.date} 기준 · 1 USD = ${Number(viewFx.rate).toLocaleString('ko-KR',{maximumFractionDigits:2})} KRW`:'환율 확인 중';}
async function changeDisplayCurrency(value){displayMode=value;try{localStorage.setItem(storageNamespace+':currency',value);}catch{}syncCurrency();renderPortfolio();renderRanking();renderHistory();if(weeklyCache)renderWeekly();window.dispatchEvent(new Event('displaycurrencychange'));}
$('displayCurrency').addEventListener('change',e=>changeDisplayCurrency(e.target.value));
// Theme: A 딥 틸 (light, default) or B 다크 아레나 (dark). Saved per browser; charts redraw.
function themeColor(name){return getComputedStyle(document.documentElement).getPropertyValue(name).trim();}
const THEME_ICONS={moon:'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/></svg>',
  sun:'<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M4.6 4.6l1.4 1.4M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4 6 18M18 6l1.4-1.4"/></svg>'};
function syncThemeToggle(){const dark=document.documentElement.dataset.theme==='dark',b=$('themeToggle');b.innerHTML=dark?THEME_ICONS.sun:THEME_ICONS.moon;b.setAttribute('aria-label',dark?'라이트 모드로 전환':'다크 모드로 전환');b.title=dark?'라이트 모드':'다크 모드';b.setAttribute('aria-pressed',String(dark));}
$('themeToggle').addEventListener('click',()=>{
  const dark=document.documentElement.dataset.theme!=='dark';
  if(dark)document.documentElement.dataset.theme='dark';else delete document.documentElement.dataset.theme;
  try{localStorage.setItem(storageNamespace+':theme',dark?'dark':'light');}catch{}
  syncThemeToggle();window.dispatchEvent(new Event('displaycurrencychange'));
});
syncThemeToggle();
function message(text) { $('status').textContent = text; }
async function api(path, body, retried=false) {
  const response = await fetch('/api/' + path, {method: body ? 'POST' : 'GET', headers: body ? {'Content-Type': 'application/json', 'X-CSRF-Token': csrf} : {}, body: body ? JSON.stringify(body) : undefined});
  // A POST refused only for a stale CSRF token changed nothing; take the session's token and retry once.
  if (body && !retried && response.status === 403 && response.headers.get('X-CSRF-Stale')) {
    try { csrf = (await (await fetch('/api/session')).json()).csrf || csrf; } catch {}
    return api(path, body, true);
  }
  let data;
  try { data = await response.json(); } catch { throw Error(`요청 실패 (${response.status}). 잠시 후 다시 시도하세요.`); }
  if (!response.ok) {
    if(response.status===401)window.disconnectQuoteStream?.();
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
  if (!rows.length) { const p = document.createElement('p'); p.className = 'empty-state'; p.textContent = {positions:'아직 보유한 종목이 없습니다. 첫 주문을 시작해보세요.',publicPositions:'보유한 종목이 없습니다.',history:'아직 거래내역이 없습니다.',fxHistory:'아직 환전내역이 없습니다.',adminAudit:'관리자 작업 기록이 없습니다.'}[target.id] || '표시할 순위가 없습니다.'; target.append(p); }
}
async function boot() {
  window.disconnectQuoteStream?.();
  const s = await api('session'); csrf = s.csrf;
  window.quoteSseEnabled=!!s.quote_sse_enabled;window.quoteMaxAge=s.quote_max_age;
  window.sessionUsername=s.username; window.isAdmin=!!s.is_admin; $('auth').hidden = !!s.username; $('dashboard').hidden = !s.username; $('logout').hidden = !s.username; $('adminNav').hidden = !s.is_admin;
  if(!s.username)showAuthView();
  // The header shows who is signed in; the photo version comes with the profile.
  window.renderHeaderUser?.();
  if(s.username&&!s.is_admin&&window.loadMyProfile)loadMyProfile().catch(()=>{});
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
// Market state comes from the server (open, tradable, price_mode); labels
// are for display only. '열림' and '주문 가능' are separate facts.
window.marketIsOpen=function(x){return x.open??['정규장','장전','장후','데이마켓','프리장','애프터장'].includes(x.label);};
window.marketPriceText=function(x){
  if(!x.session||['closed','unknown'].includes(x.session))return '';
  if(x.open&&x.tradable===false)return '시장 열림 · 주문 시세 확인 불가';
  const mode=x.price_mode||'';
  if(mode.endsWith('_stream'))return '실시간';
  if(mode==='rest'||mode.endsWith('_rest'))return '보조 시세';
  return '체결 시세 미지원';
};
// One entry per market: a green dot when orders can fill now, red when closed or not tradable.
function renderMarketSessions(markets){
  $('marketSessions').replaceChildren(...markets.map(x=>{
    const state=marketPriceText(x),open=marketIsOpen(x)&&x.tradable!==false,item=document.createElement('span');
    item.className='market-session '+(open?'is-open':'is-closed');
    const dot=document.createElement('i');dot.className='session-dot';dot.setAttribute('aria-hidden','true');
    item.append(dot,`${x.market==='KR'?'한국':'미국'} ${x.label}${state?' · '+state:''}`);
    item.title=open?'지금 주문할 수 있습니다':'지금은 주문할 수 없습니다';
    return item;
  }));
}
async function refreshMarketSessions(){
  if(!window.sessionUsername||window.isAdmin)return;
  try{const r=await api('market-overview');window.marketOpen=Object.fromEntries(r.markets.map(x=>[x.market,marketIsOpen(x)]));renderMarketSessions(r.markets);}catch(e){$('marketSessions').textContent='시장 상태 확인 불가';}
}
setInterval(()=>{if(!document.hidden)refreshMarketSessions();},60000);
// Notice posted by an administrator (e.g. the maintenance template): a banner only, nothing is blocked.
// A notice that arrives while the page is open is marked "새 공지" with a toast;
// notices already seen in this browser are not marked again after a reload.
// Several notices can be up; each is marked new once per browser.
function seenNotices(){try{const list=JSON.parse(localStorage.getItem(storageNamespace+':notices-seen')||'[]');const old=localStorage.getItem(storageNamespace+':notice-seen');return new Set(old?[...list,Number(old)]:list);}catch{return new Set();}}
function markNoticesSeen(ids){try{localStorage.setItem(storageNamespace+':notices-seen',JSON.stringify([...ids].slice(-50)));}catch{}}
function showSiteNotices(notices){
  const box=$('siteNotices');notices=window.isAdmin?[]:notices||[];
  box.hidden=!notices.length;
  // Elements already on screen are kept, so a "새 공지" badge survives the 10-second refresh until clicked.
  const seen=seenNotices(),existing=new Map([...box.children].map(n=>[Number(n.dataset.id),n])),arrived=[];
  box.replaceChildren(...notices.map(notice=>{
    let item=existing.get(notice.id);
    if(!item){
      item=document.createElement('div');item.className='site-notice';item.dataset.id=notice.id;
      item.append(node('span','새 공지','notice-new'),node('span','','notice-kind'),node('strong','','notice-title'),node('p','','notice-body'));
      if(!seen.has(notice.id)){item.classList.add('is-new');arrived.push(notice);}
      item.addEventListener('click',()=>item.classList.remove('is-new'));
    }
    item.dataset.kind=notice.kind;item.querySelector('.notice-kind').textContent=notice.label;
    item.querySelector('.notice-title').textContent=notice.title;item.querySelector('.notice-body').textContent=notice.body;
    return item;
  }));
  for(const notice of arrived)toast(`새 공지가 등록되었습니다.\n${notice.label} · ${notice.title}`,'info',7000);
  if(arrived.length)markNoticesSeen(new Set([...seen,...arrived.map(n=>n.id)]));
}
async function refreshNotice(){try{const r=await api('notice');showSiteNotices(r.notices||(r.notice?[r.notice]:[]));}catch{}}
setInterval(()=>{if(!document.hidden)refreshNotice();},10000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refreshNotice();});
window.addEventListener('focus',()=>refreshNotice());
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
// History filters: side (all/buy/sell) and a Korea-time month; both are applied by the server.
let historySide='',historyMonth='';
async function history() {
  const query=new URLSearchParams({page});if(historySide)query.set('side',historySide);if(historyMonth)query.set('month',historyMonth);
  const [rows]=await Promise.all([api('transactions?'+query),loadHistoryMonths()]);
  historyCache=rows;renderHistory();
}
async function loadHistoryMonths(){
  const months=await api('transactions/months'),select=$('historyMonth');
  const options=[new Option('전체 기간','')];
  for(const {month,count} of months){const [y,m]=month.split('-');options.push(new Option(`${y}년 ${Number(m)}월 (${count}건)`,month));}
  select.replaceChildren(...options);select.value=months.some(x=>x.month===historyMonth)?historyMonth:'';
}
function sideLabel(side){const n=document.createElement('span');n.className='trade-side '+(side==='buy'?'gain':'loss');n.textContent=side==='buy'?'매수':'매도';return n;}
function renderHistory(){const rows=historyCache;
  table($('history'), ['체결 시각', '종목', '매매', '수량', '체결가', '총액', '수수료 / 세금', '정산 금액'], rows.map(t => [new Date(t.created_at).toLocaleString(), t.symbol, sideLabel(t.side), t.quantity, viewMoney(t.native_price,t.currency),viewMoney(t.gross_amount,t.currency),viewMoney(t.fee,t.currency)+' / '+viewMoney(t.tax,t.currency),viewMoney(t.net_amount,t.currency)]));
  $('page').textContent = page + ' 페이지'; $('previous').disabled = page === 1; $('next').disabled = rows.length < 50;
}
async function search() {
  const category = 'all';
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
handle('authForm', 'submit', async () => { $('loginSubmit').disabled=true; try { await api('login', {username: $('username').value, password: $('password').value}); } catch (err) { toast(`로그인하지 못했습니다.\n${err.message}`, 'error', 7000); return; } finally { $('loginSubmit').disabled=false; } $('password').value = ''; message(''); if(location.hash==='#signup')location.hash=''; await boot(); window.scrollTo(0,0); });
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
handle('logout', 'click', async () => { window.disconnectQuoteStream?.();await api('logout', {}); pendingOrder = null; message(''); await boot(); });
handle('refresh', 'click', refresh);
handle('searchForm', 'submit', search);
handle('quote', 'click', async () => { if(window.openStock) openStock($('symbol').value); });
handle('orderForm', 'submit', async () => {
  if (window.reserveMode) return submitReservation();
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
let pendingReserve = null;
async function submitReservation() {
  const price = $('reservePrice').value.trim();
  if (!price || !(Number(price) > 0)) { toast('예약 조건 가격을 입력하세요.', 'error'); $('reservePrice').focus(); return; }
  const data = {symbol: $('symbol').value, side: $('side').value, quantity: Number($('quantity').value), limit_price: price, trigger: $('reserveTrigger').value};
  pendingReserve = reuseRequestId(pendingReserve, JSON.stringify(data));
  $('submitOrder').disabled = true;
  const sideName = data.side === 'buy' ? '매수' : '매도', label = window.orderSymbolLabel ? orderSymbolLabel() : data.symbol;
  try {
    await api('limit-orders', {...data, request_id: pendingReserve.id});
    pendingReserve = null;
    toast(`예약 ${sideName}를 등록했습니다.\n${label} ${data.quantity}주 · ${$('reserveTrigger').selectedOptions[0].textContent}`, 'success', 6000);
    await window.loadLimits?.();
  } catch (err) { toast(`예약 ${sideName}를 등록하지 못했습니다.\n${err.message}`, 'error', 7000); }
  finally { $('submitOrder').disabled = false; }
}
handle('previous', 'click', async () => { page = Math.max(1, page - 1); await history(); });
handle('next', 'click', async () => { page++; await history(); });
document.querySelectorAll('#historySide button').forEach(b=>b.addEventListener('click',async()=>{
  historySide=b.dataset.side;page=1;
  document.querySelectorAll('#historySide button').forEach(x=>x.setAttribute('aria-pressed',String(x===b)));
  try{await history();}catch(e){message(e.message);}
}));
$('historyMonth').addEventListener('change',async e=>{historyMonth=e.target.value;page=1;try{await history();}catch(err){message(err.message);}});
const reportDate = value => new Date(value).toLocaleString('ko-KR', {timeZone: 'Asia/Seoul', year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'});
async function weekly() {
  weeklyCache = await api('weekly?page=' + weeklyPage);renderWeekly();
}
function renderWeekly(){
  const data=weeklyCache;
  const days = ['월', '화', '수', '목', '금', '토', '일'];
  $('weeklySchedule').textContent = data.enabled ? `매주 ${days[data.weekday]}요일 ${data.hour}시 · 한국 시간` : '자동 게시 중지';
  $('weeklyStatus').textContent = data.error || (data.baseline_at ? `다음 게시: ${reportDate(data.next_due)} · 직전 집계 자산 대비 기간 수익률 · ${'평가 수익률은 '+(data.reports[0]?.return_basis||'초기 KRW 평가액 대비 (외부 입출금 반영)')}` : '첫 주간 기록을 준비하고 있습니다. 계좌의 기준 평가금액이 저장되면 집계가 시작됩니다.');
  $('weeklyReports').replaceChildren();
  for (const report of data.reports) {
    const article = document.createElement('article'); article.className = 'weekly-report';
    const period = document.createElement('p'); period.className = 'weekly-period'; period.textContent = `${reportDate(report.period_start)} ~ ${reportDate(report.period_end)}`;
    // Ranked by the period's own return (not by assets); ranks 1-4 are shown up front, ties sharing a rank.
    const title = document.createElement('h3'); const leaders = report.rows.filter(row => row.rank <= 4);
    title.textContent = leaders.length ? '이번 주 수익률 상위' : '이번 집계에는 비교 가능한 참여자가 없습니다.';
    const podium = document.createElement('ol'); podium.className = 'weekly-leaders';
    for (const row of leaders) {
      const item = document.createElement('li'); item.className = 'weekly-leader rank-' + row.rank;
      const who = document.createElement('span'); who.className = 'weekly-leader-name'; who.append(userLink(row.username));
      item.append(rankBadge(row.rank), who, signedPct(row.return_pct)); podium.append(item);
    }
    const details = document.createElement('details'), summary = document.createElement('summary'), list = document.createElement('div'); list.className = 'scroll'; summary.textContent = `전체 순위 보기 · ${report.rows.length}명`;
    table(list, ['순위', '사용자', '기간 수익률', '기간 손익 ('+viewCurrency(report.base_currency)+')', '평가 수익률 (원화 기준)'], report.rows.map(row => [row.rank, userLink(row.username), signedPct(row.return_pct), signed(row.pnl,viewMoney(row.pnl,report.base_currency,report.fx_rate?{rate:report.fx_rate}:null)), signedPct(row.total_return_pct)]));
    details.append(summary, list);
    const note = document.createElement('p'); note.className = 'weekly-note';
    note.textContent = `시작 평가액이 없거나 0인 ${report.excluded_new_or_zero}명은 비교에서 제외됩니다.` + (report.oldest_quote ? ` 사용 시세 중 가장 오래된 시각: ${reportDate(report.oldest_quote)}.` : '') + (report.late ? ' 게시가 지연되어 실제 집계 시점까지의 성과입니다.' : '');
    article.append(period, title, podium, details, note); $('weeklyReports').append(article);
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
