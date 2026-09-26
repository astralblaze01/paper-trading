/* Page navigation and chart rendering. Prices used for settlement stay on the server. */
let currentSymbol = '', currentRange = '1D', chartRows = [], chartIndex = null, detailMarketOpen = false;
let exchangePending = null;
let detailChange = null, detailQuote=null, publicCache=null, detailCompany=null, watchCache=[];
let orderPreview=null, previewVersion=0, previewTimer=null;
function recentKey(){return storageNamespace+':recent:'+window.sessionUsername;}
// Lists saved before the storage namespace existed are under the fixed 'paper-harbor' key.
function readRecentStocks(){return JSON.parse(localStorage.getItem(recentKey())||localStorage.getItem('paper-harbor:recent:'+window.sessionUsername)||'[]');}
window.renderRecentStocks=function(){
  $('searchResults').replaceChildren();
  let rows=[];try{rows=readRecentStocks();}catch{}
  $('searchResults').append(node('p','최근 본 종목','field-help'));
  for(const r of rows.slice(0,8)){const b=node('button',r.name+' · '+r.symbol,'text-button');b.type='button';b.addEventListener('click',()=>openStock(r.symbol));$('searchResults').append(b);}
  if(!rows.length)$('searchResults').append(node('p','종목을 조회하면 여기에 표시됩니다.','field-help'));
};
function rememberStock(symbol,name){if(!window.sessionUsername)return;try{let rows=readRecentStocks();rows=[{symbol,name:name||symbol},...rows.filter(r=>r.symbol!==symbol)].slice(0,8);localStorage.setItem(recentKey(),JSON.stringify(rows));}catch{}renderRecentStocks();}

// Keep in step with the '30초' in #exploreNotice and the explore page copy.
const EXPLORE_REFRESH_MS=30000;
let exploreVersion = 0;
let exploreMode = 'ranking';
let exploreRowsCache = [], explorePopular = false, displayFx = null;
function node(tag, text, cls) { const n = document.createElement(tag); if(text!=null)n.textContent=text; if(cls)n.className=cls; return n; }
function uuid() { const b=crypto.getRandomValues(new Uint8Array(16)); b[6]=(b[6]&15)|64;b[8]=(b[8]&63)|128;const h=Array.from(b,x=>x.toString(16).padStart(2,'0')).join('');return `${h.slice(0,8)}-${h.slice(8,12)}-${h.slice(12,16)}-${h.slice(16,20)}-${h.slice(20)}`; }
// A retry of the same payload keeps its request id, so the server applies it only once.
function reuseRequestId(pending,sig){return pending&&pending.sig===sig?pending:{sig,id:uuid()};}
window.routePage = async function() {
  disconnectQuoteStream();
  let [pageName, segment] = location.hash.slice(1).split('/');
  // #user/<id> is the public profile; #public/<id> is kept for old links.
  if(pageName==='user')pageName='public';
  if(pageName==='public'&&segment&&decodeURIComponent(segment).toLowerCase()===window.sessionUsername){location.replace('#portfolio');return;}
  let selected=['explore','portfolio','history','fx','watchlist','ranking','admin','detail','public'].includes(pageName)?pageName:'explore';
  if(window.isAdmin)selected='admin';
  document.querySelectorAll('[data-page]').forEach(el=>el.hidden=el.dataset.page!==selected);
  document.querySelectorAll('.app-nav a').forEach(a=>a.setAttribute('aria-current',a.hash==='#'+selected?'page':'false'));
  if($('dashboard').hidden)return;
  message('');
  try {
    if(selected==='explore')await explore();
    if(selected==='public' && segment)await openPublicPage(segment);
    if(selected==='detail' && segment)await openDetailPage(segment);
    if(selected==='fx'){await Promise.all([fxHistory(),loadFxRate()]);}
    if(selected==='watchlist')await watchlist();
    if(selected==='ranking')await refreshRankingOnly();
    if(selected==='admin')await admin();
    if(selected==='portfolio'&&window.loadMyProfile)await loadMyProfile();
  } catch(e){message(e.message);}
};
// segment is still URI-encoded: the missing-price retry compares it with the live hash.
async function openPublicPage(segment){
  publicCache=null;
  const name=decodeURIComponent(segment);
  loadPerformance(name);
  $('publicTitle').textContent='투자 현황 조회 중…';
  for(const id of ['publicPositions','publicMetrics','publicProfile','publicAllocation'])$(id).replaceChildren();
  const [p]=await Promise.all([api('portfolios/'+encodeURIComponent(name)),rankingCache?null:refreshRankingOnly().catch(()=>null)]);
  publicCache=p;renderPublic();
  if(p.positions.some(x=>x.value==null))retryMissingPrices(async()=>{if(location.hash.split('/')[1]!==segment)return null;const next=await api('portfolios/'+encodeURIComponent(name));publicCache=next;renderPublic();return next;});
}
// The previous symbol's quote, company, preview and chart are cleared before anything loads.
async function openDetailPage(segment){
  currentSymbol=decodeURIComponent(segment);chartRows=[];drawChart();
  detailCompany=null;detailQuote=null;orderPreview=null;
  $('symbol').value=currentSymbol;maxMode=false;
  loadCompany(currentSymbol);
  await loadStock(true);
  await api('popularity',{symbol:currentSymbol,kind:'view'});
}
window.openStock = function(symbol) { if(location.hash==='#detail/'+encodeURIComponent(symbol))loadStock(true).catch(e=>message(e.message));else location.hash='detail/'+encodeURIComponent(symbol); };
window.addEventListener('hashchange',routePage);
function displayedPrice(r) {
  const currency=displayMode;
  if(r.price==null)return '—';
  if(currency==='native'||currency===r.currency)return nativeMoney(r.price,r.currency);
  if(!displayFx)return '환율 확인 대기';
  const rate=Number(displayFx.rate);
  return nativeMoney(Number(r.price)*(r.currency==='USD'?rate:1/rate),currency);
}
function displayedTurnover(r){
  if(r.turnover==null || !Number.isFinite(Number(r.turnover)) || Number(r.turnover)<0)return '—';
  const selected=displayMode,currency=selected==='native'?r.currency:selected;
  if(currency!==r.currency&&!displayFx)return '환율 확인 대기';
  const rate=Number(displayFx?.rate||1),value=Number(r.turnover)*(currency===r.currency?1:r.currency==='USD'?rate:1/rate);
  // Abbreviated turnover follows the same rule: "$12.3백만", "4.5억원".
  const scaled=(value/(currency==='KRW'?1e8:1e6)).toLocaleString('ko-KR',{maximumFractionDigits:1});
  return `${currency==='KRW'?scaled+'억원':'$'+scaled+'백만'}${r.turnover_estimated?' (추정)':''}`;
}
function stockTable(target,rows,popular=false){
 target.replaceChildren();const t=node('table',null,'market-table'),head=node('tr');
 [['#','rank-number'],['종목','stock-name'],['현재가','price-cell'],['등락률','change-cell'],['거래대금','turnover-cell'],[popular?'인기 점수':'거래량','volume-cell'],['시장','market-cell']].forEach(([label,cls])=>head.append(node('th',label,cls)));const thead=node('thead');thead.append(head);t.append(thead);const body=node('tbody');
 if(window.isAdmin)[['코드','admin-diagnostic'],['시세 시각','admin-diagnostic'],['데이터 상태','admin-diagnostic']].forEach(([label,cls])=>head.append(node('th',label,cls)));
 rows.forEach((r,i)=>{const tr=node('tr'),first=node('td',null,'stock-name'),b=node('button',r.name||r.symbol,'text-button stock-link');b.addEventListener('click',()=>openStock(r.symbol));
   const tools=node('div',null,'stock-tools'),watch=node('button',r.watchlisted?'★':'☆','watch-toggle');watch.type='button';watch.setAttribute('aria-label',(r.name||r.symbol)+' 관심종목');watch.setAttribute('aria-pressed',String(!!r.watchlisted));
   watch.addEventListener('click',async()=>{watch.disabled=true;try{if(r.watchlisted)await removeWatch(r.symbol);else await api('watchlist',{symbol:r.symbol});r.watchlisted=!r.watchlisted;watch.textContent=r.watchlisted?'★':'☆';watch.setAttribute('aria-pressed',String(r.watchlisted));}catch(e){message(e.message);}finally{watch.disabled=false;}});
   tools.append(watch,node('span',i+1,'mobile-rank'));first.append(tools,b);
   if(window.isAdmin){const meta=node('details',null,'admin-meta-mobile');meta.append(node('summary','시세 진단'),node('span',r.symbol),node('span',r.data_time?new Date(r.data_time).toLocaleString():'시각 없음'),node('span',r.data_status||'상태 없음'));first.append(meta);}
   tr.append(node('td',i+1,'rank-number'),first,node('td',displayedPrice(r),'price-cell'));const change=Number(r.change_pct);tr.append(node('td',r.change_pct==null?'—':`${change>0?'+':''}${pct(change)}`,'change-cell '+(change>0?'gain':change<0?'loss':'flat')),node('td',displayedTurnover(r),'turnover-cell'),node('td',popular?r.score:r.volume==null?'—':Number(r.volume).toLocaleString(),'volume-cell'),node('td',r.market==='KR'||r.currency==='KRW'?'한국':'미국','market-cell'));
   if(window.isAdmin){tr.append(node('td',r.symbol,'admin-diagnostic'),node('td',r.data_time?new Date(r.data_time).toLocaleString():'—','admin-diagnostic'),node('td',r.data_status||'—','admin-diagnostic status-cell'));}
   tr.querySelector('.volume-cell').dataset.label=popular?'인기 점수':'거래량';
   body.append(tr);});t.append(body);target.append(t);if(!rows.length)target.append(node('p','표시할 종목이 없습니다.','empty-state'));
}
async function loadDisplayFx(){try{displayFx=await api('fx');viewFx=displayFx;syncCurrency();$('exploreFxNote').textContent=`환산 표시는 ${displayFx.date} ECB 일별 기준환율 (1 USD = ${Number(displayFx.rate).toLocaleString('ko-KR',{maximumFractionDigits:2})} KRW) 기준입니다. 실제 거래 통화와 주문 가격은 바뀌지 않습니다.`;}catch(e){displayFx=null;viewFx=null;syncCurrency();$('exploreFxNote').textContent='환율을 확인하지 못해 다른 통화로 환산한 가격을 표시할 수 없습니다.';}}
async function explore(silent=false) {
  exploreMode='ranking';
  const version=++exploreVersion,asset=$('exploreMarket').value,kind=$('exploreKind').value;
  document.querySelectorAll('[data-asset]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.asset===asset)));
  document.querySelectorAll('[data-kind]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.kind===kind)));
  if(!silent){$('exploreNotice').textContent='목록을 불러오는 중입니다.';$('exploreRows').replaceChildren();}
  try{
    const [r]=await Promise.all([api('explore?'+new URLSearchParams({asset,kind})),loadDisplayFx()]);if(version!==exploreVersion)return;
    const next=new Date(Date.now()+EXPLORE_REFRESH_MS).toLocaleTimeString('ko-KR',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
    $('exploreNotice').textContent=[r.scope,r.notice,`30초 자동 갱신 · 다음 확인 ${next}`].filter(Boolean).join(' · ');
    if($('status').textContent==='입력값을 확인하세요.')$('status').textContent='';
    exploreRowsCache=r.rows;explorePopular=kind==='popular';stockTable($('exploreRows'),r.rows,explorePopular);
  }catch(e){if(version===exploreVersion){$('exploreNotice').textContent=e.message;stockTable($('exploreRows'),[]);}}
}
$('exploreMarkets').addEventListener('click',e=>{const b=e.target.closest('[data-asset]');if(!b)return;$('exploreMarket').value=b.dataset.asset;explore();});
$('exploreKinds').addEventListener('click',e=>{const b=e.target.closest('[data-kind]');if(!b)return;$('exploreKind').value=b.dataset.kind;explore();});
handle('exploreMarket','change',()=>explore());handle('exploreKind','change',()=>explore());
window.addEventListener('displaycurrencychange',()=>{displayFx=viewFx;stockTable($('exploreRows'),exploreRowsCache,explorePopular);renderDetailQuote();renderOrderPreview();drawChart();renderPublic();renderWatchlist();renderValuation();});
handle('discoverySearch','submit',async()=>{exploreMode='search';++exploreVersion;const query=$('discoveryQuery').value;const rows=await api('search?'+new URLSearchParams({q:query,category:$('exploreMarket').value}));exploreRowsCache=rows;explorePopular=false;stockTable($('exploreRows'),rows);$('exploreNotice').textContent='검색 결과 · 등록 종목 목록이며 가격은 종목 상세에서 확인합니다.';if(rows.length===1)await api('popularity',{symbol:rows[0].symbol,kind:'search'});});
setInterval(()=>{if(document.hidden||(location.hash&&location.hash!=='#explore')||$('dashboard').hidden||exploreMode!=='ranking')return;const asset=$('exploreMarket').value,markets=['kr','kr_bond'].includes(asset)?['KR']:['us','us_bond'].includes(asset)?['US']:['KR','US'];if(markets.every(m=>window.marketOpen?.[m]===false))return;explore(true);},EXPLORE_REFRESH_MS);
// Keep REST_QUOTE_MS in step with the REST fallback's '30초 간격' status line.
const MARKET_STATUS_MS=60000, REST_QUOTE_MS=30000;
let quoteSource=null, quoteRetry=null, quoteWatch=null, marketTimer=null, restTimer=null;
let quoteGeneration=0, quoteVersion=null, quoteBackoff=0, quoteReceived=0, marketRequest=null;
function detailVisible(){return !document.hidden&&!$('dashboard').hidden&&location.hash==='#detail/'+encodeURIComponent(currentSymbol);}
function streamState(text){$('quoteConnection').textContent=text;}
window.disconnectQuoteStream=function(){
  ++quoteGeneration;++previewVersion;
  quoteSource?.close();quoteSource=null;quoteVersion=null;
  clearTimeout(quoteRetry);clearInterval(quoteWatch);clearInterval(marketTimer);clearInterval(restTimer);clearTimeout(previewTimer);
  quoteRetry=quoteWatch=marketTimer=restTimer=null;previewQueued=null;
};
function updateQuoteAge(){
  if(!detailQuote)return;
  const age=Date.now()/1000-Number(detailQuote.timestamp),limit=window.quoteMaxAge?.[currentSymbol.startsWith('KR:')?'KR':'US']??(currentSymbol.startsWith('KR:')?900:1800);
  // A live stream's last trade stays current however quiet the symbol is.
  if(age>limit&&!detailQuote.stale&&!detailQuote.realtime){detailQuote.stale=true;renderDetailQuote();}
}
function applyQuote(q){
  if(!detailQuote)rememberStock(currentSymbol,detailCompany?.name||q.name||currentSymbol);
  const priceChanged=!detailQuote||String(detailQuote.native_price)!==String(q.native_price);
  const changed=JSON.stringify(q)!==JSON.stringify(detailQuote);
  detailQuote=q;detailChange=q.change_pct==null?null:Number(q.change_pct);
  if(changed)renderDetailQuote();
  updateQuoteAge();
  if(priceChanged){clearTimeout(previewTimer);previewTimer=setTimeout(()=>estimate(),200);}
}
async function refreshMarketStatus(){
  if(!detailVisible()||marketRequest)return;
  const symbol=currentSymbol,generation=quoteGeneration;
  marketRequest=api('market-status/'+encodeURIComponent(symbol));
  try{
    const r=await marketRequest;if(generation!==quoteGeneration)return;
    detailMarketOpen=marketIsOpen(r);
    const state=marketPriceText(r);
    const admin=window.isAdmin&&r.session?` · session ${r.session} · ${r.venue||''} · ${r.price_mode} · stream ${r.stream_connected?'connected':'down'} · ${r.source}`:'';
    $('marketState').textContent=`${symbol.startsWith('KR:')?'한국':'미국'} ${r.label}${state?' · '+state:''} · ${r.timezone}${r.verified?'':' · 확정 상태 아님'}${detailMarketOpen?'':' · 마지막 데이터 유지'}${admin}`;
  }catch{if(generation===quoteGeneration)$('marketState').textContent='장 상태를 확인할 수 없습니다.';}
  finally{marketRequest=null;if(generation!==quoteGeneration&&detailVisible())refreshMarketStatus();}
}
window.connectQuoteStream=function(symbol){
  disconnectQuoteStream();
  if(!detailVisible())return;
  const generation=quoteGeneration;
  refreshMarketStatus();marketTimer=setInterval(refreshMarketStatus,MARKET_STATUS_MS);
  if(!window.quoteSseEnabled){
    streamState('서버 시세 · 30초 간격 확인');
    let restLoading=false;
    const refresh=async()=>{if(restLoading||!detailVisible())return;restLoading=true;try{const q=await api('quote/'+encodeURIComponent(symbol));if(generation===quoteGeneration)applyQuote(q);}catch(e){if(generation===quoteGeneration)streamState(e.message);}finally{restLoading=false;}};
    refresh();restTimer=setInterval(refresh,REST_QUOTE_MS);return;
  }
  const valid=()=>generation===quoteGeneration&&detailVisible();
  const retry=()=>{
    quoteSource?.close();quoteSource=null;
    if(!valid()||quoteRetry)return;
    streamState('재연결 중 · 마지막 시세');
    const delay=Math.min(30000,1000*2**Math.min(quoteBackoff++,5))*(.8+Math.random()*.4);
    quoteRetry=setTimeout(async()=>{
      quoteRetry=null;if(!valid())return;
      try{const s=await api('session');if(!valid())return;if(!s.username||!s.active){disconnectQuoteStream();streamState('로그인이 필요합니다.');return;}}
      catch{if(valid())retry();return;}
      if(valid())open();
    },delay);
  };
  const open=()=>{
    if(!valid())return;
    quoteSource?.close();quoteVersion=null;quoteReceived=Date.now();
    streamState('연결 중 · 새 시세 대기');
    const source=new EventSource('/api/market-stream/'+encodeURIComponent(symbol));quoteSource=source;
    const receive=(name,callback)=>source.addEventListener(name,e=>{
      if(!valid()||source!==quoteSource)return;
      try{const data=JSON.parse(e.data);quoteReceived=Date.now();callback(data);}catch{/* Ignore malformed messages without interrupting the page. */}
    });
    for(const name of ['snapshot','quote'])receive(name,data=>{
      if(data.schema_version!==1||data.symbol!==symbol||!data.quote||!/^[a-f0-9]{32}:[1-9][0-9]*$/.test(data.version))return;
      if(name==='quote'&&quoteVersion){const [epoch,seq]=data.version.split(':'),[oldEpoch,oldSeq]=quoteVersion.split(':');if(epoch!==oldEpoch||BigInt(seq)<=BigInt(oldSeq))return;}
      if(!Number.isFinite(Number(data.quote.native_price))||Number(data.quote.native_price)<=0)return;
      quoteVersion=data.version;quoteBackoff=0;applyQuote(data.quote);streamState('연결됨 · 공급자 시세 갱신 시 반영');
    });
    receive('heartbeat',data=>{updateQuoteAge();if(!data.connected)streamState('재연결 중 · 마지막 시세');});
    receive('status',data=>{
      if(data.state==='auth_required'){disconnectQuoteStream();streamState('로그인이 필요합니다.');}
      else if(data.state==='restart'||data.state==='unavailable')retry();
      else if(data.state==='waiting')streamState('새 시세 대기');
    });
    source.onerror=()=>{if(source===quoteSource)retry();};
  };
  quoteWatch=setInterval(()=>{if(!valid())return;updateQuoteAge();if(Date.now()-quoteReceived>45000)retry();},5000);
  open();
};
document.addEventListener('visibilitychange',()=>{if(document.hidden)disconnectQuoteStream();else if(detailVisible())connectQuoteStream(currentSymbol);});
window.addEventListener('pagehide',disconnectQuoteStream);
window.addEventListener('pageshow',e=>{if(e.persisted&&detailVisible())connectQuoteStream(currentSymbol);});
window.loadStock=async function(withChart=true){
  if(!currentSymbol||!detailVisible())return;
  if(!detailQuote){$('detailTitle').textContent=detailCompany?.name||currentSymbol;$('detailPrice').textContent='시세 조회 중…';delete $('detailPrice').dataset.quoteKey;}
  if(!quoteSource&&!restTimer&&!quoteRetry)connectQuoteStream(currentSymbol);
  if(withChart)await loadChart();
  else await estimate();
};
// Per-symbol price state. 실시간 only for a live stream print; a REST price
// is 보조 시세 even when it is recent enough to trade.
function quoteBadge(q){
  if(!q.session)return '';
  if(window.isAdmin)return `${q.source||''} ${q.origin==='stream'?'websocket':'REST'} · ${q.trade_session||'-'} · ${q.price_mode} · ${new Date(q.timestamp*1000).toLocaleTimeString()}`;
  if(['closed','unknown'].includes(q.session))return '최근 체결가';
  if(q.realtime)return '실시간';
  return q.session_tradeable?'보조 시세':'체결 시세 미지원';
}
function renderDetailQuote(){
  const q=detailQuote;if(!q)return;
  $('detailTitle').textContent=(detailCompany?.name||q.name||currentSymbol)+(window.isAdmin?' · '+currentSymbol:'');
  const adminStamp=window.isAdmin?` · ${new Date(q.timestamp*1000).toLocaleString()} · ${q.data_status||q.source||'공급자 시세'}`:'';
  const offSession=q.session_tradeable===false&&!['closed','unknown'].includes(q.session);
  $('quoteInfo').textContent=`${viewMoney(q.native_price,q.currency)} · 실제 주문 통화 ${q.currency}${offSession?' · 현재 세션 체결 시세가 없어 주문할 수 없습니다.':q.stale?' · 새 시세를 기다립니다.':' · 체결 시 가격은 달라질 수 있습니다.'}${adminStamp}`;
  const badge=quoteBadge(q);
  const priceKey=JSON.stringify([q.native_price,q.currency,q.change,q.change_pct,displayMode,viewFx?.rate,badge]);
  if($('detailPrice').dataset.quoteKey!==priceKey){
  $('detailPrice').dataset.quoteKey=priceKey;
  const priceBlock=node('div',null,'detail-price-main');
  priceBlock.append(node('strong',viewMoney(q.native_price,q.currency)));
  if(badge)priceBlock.append(node('span',badge,'quote-badge'+(q.realtime?' live':'')));
  const changeBlock=node('div',null,'detail-change-block');
  changeBlock.append(node('span','전일 대비','detail-change-label'),signed(q.change,`${Number(q.change)>0?'+':''}${viewMoney(q.change,q.currency)}`),signed(detailChange,q.change_pct==null?'—':`${detailChange>0?'+':''}${pct(q.change_pct)}`));
  $('detailPrice').replaceChildren(priceBlock,changeBlock);
  $('detailPrice').className='detail-price';
  }
  const range=`고가 ${viewMoney(q.high,q.currency)} / 저가 ${viewMoney(q.low,q.currency)} · 거래량 ${q.volume==null?'미제공':Number(q.volume).toLocaleString()}`;
  $('detailMeta').textContent=window.isAdmin?`${q.data_status||q.source||'공급자 시세'} · ${new Date(q.timestamp*1000).toLocaleString()}${q.stale?' · 오래된 시세':''} · ${range}`:range+(q.stale?' · 오래된 시세':'');
}
async function loadCompany(symbol){
  $('companyInfo').textContent='회사 정보를 불러오는 중입니다.';
  try{
    const r=await api('company/'+encodeURIComponent(symbol));
    if(symbol!==currentSymbol)return;
    detailCompany=r;
    $('detailTitle').textContent=r.name+(window.isAdmin?' · '+symbol:'');
    const target=$('companyInfo');target.replaceChildren();
    const fields=[['회사 / 상품명',r.name],['업종',r.industry],['거래소',r.exchange],['국가',r.country],['상장일',r.ipo],['자산 종류',categories[r.category]]];
    for(const [label,value] of fields){
      if(!value)continue;
      const d=node('div');d.append(node('small',label),node('strong',value));target.append(d);
    }
    const dividend=r.dividend||{status:'unavailable'};
    const div=node('div',null,'dividend-field');
    div.append(node('small','배당률'),node('strong',dividend.status==='paid'?`연 ${Number(dividend.yield).toFixed(2)}%`:dividend.status==='none'?'없음':'정보 없음'));
    if(dividend.basis)div.append(node('span',dividend.basis,'field-help'));
    target.append(div);
    target.append(node('div',null,'valuation'));renderValuation();
    // Provider data: only an http(s) URL becomes a link, never javascript: or data:.
    if(r.website){
      try{
        const u=new URL(r.website);
        if(['http:','https:'].includes(u.protocol)){const a=node('a','공식 홈페이지 ↗');a.href=u.href;a.target='_blank';a.rel='noopener noreferrer';target.append(a);}
      }catch{}
    }
    target.append(node('p',[r.source,r.notice].filter(Boolean).join(' · '),'field-help'));
  }catch(e){if(symbol===currentSymbol)$('companyInfo').textContent=e.message;}
}
// Market cap follows the display currency like every other amount; the ratios have no currency.
function marketCapText(value,currency){
  if(value==null)return '정보 없음';
  const shown=viewValue(value,currency),unit=viewCurrency(currency);
  if(shown==null||!Number.isFinite(shown))return '환율 확인 대기';
  const one=n=>n.toLocaleString('ko-KR',{minimumFractionDigits:1,maximumFractionDigits:1});
  if(unit==='KRW')return shown>=1e12?one(shown/1e12)+'조원':Math.round(shown/1e8).toLocaleString('ko-KR')+'억원';
  const [size,suffix]=[[1e12,'T'],[1e9,'B'],[1e6,'M']].find(([s])=>shown>=s)||[1,''];
  return '$'+one(shown/size)+suffix;
}
function ratioText(value,unit){const n=Number(value);return value==null||!Number.isFinite(n)?'정보 없음':n.toLocaleString('ko-KR',{minimumFractionDigits:1,maximumFractionDigits:1})+unit;}
function renderValuation(){
  const box=document.querySelector('#companyInfo .valuation'),v=detailCompany?.valuation;
  if(!box||!v||detailCompany.symbol!==currentSymbol)return;
  const grid=node('div',null,'valuation-grid');
  for(const [label,value,title] of [['시가총액',marketCapText(v.market_cap,v.currency),'Market Cap'],['PER',Number(v.per)<0?'적자':ratioText(v.per,'배'),'주가수익비율 (Price / Earnings)'],['PBR',ratioText(v.pbr,'배'),'주가순자산비율 (Price / Book)'],['ROE',ratioText(v.roe,'%'),'자기자본이익률 (Return on Equity)'],['PSR',ratioText(v.psr,'배'),'주가매출비율 (Price / Sales)']]){
    const item=node('div',null,'valuation-item'),name=node('small',label);name.title=title;item.append(name,node('strong',value));grid.append(item);
  }
  box.replaceChildren(node('small','투자 지표','valuation-title'),grid,node('span',[v.basis,v.notice].filter(Boolean).join(' · '),'field-help'));
}
async function loadChart(){const symbol=currentSymbol,range=currentRange;chartRows=[];chartIndex=null;drawChart();$('chartNotice').textContent='차트 조회 중…';$('chartRetry').hidden=true;try{const r=await api('candles/'+encodeURIComponent(symbol)+'?range='+range);if(symbol!==currentSymbol||range!==currentRange)return;chartRows=r.candles;chartIndex=null;$('chartRetry').hidden=!!r.candles.length;const technical=window.isAdmin?`${r.source} · ${r.resolution} · ${r.data_status}`:'과거 가격 데이터';const partial=!window.isAdmin&&r.partial?' · 공급자가 제공한 범위만 표시합니다.':'';$('chartNotice').textContent=`${technical}${partial}${r.stale?' · 마지막 데이터가 오래되었습니다':''}${!r.candles.length?' · 데이터 없음':''}`;drawChart();}catch(e){if(symbol===currentSymbol&&range===currentRange){$('chartNotice').textContent=e.message+' 잠시 후 다시 불러오세요.';$('chartRetry').hidden=false;chartRows=[];drawChart();}}}
$('chartRetry').addEventListener('click',()=>loadChart());
$('chartRanges').addEventListener('click',e=>{if(e.target.dataset.range){currentRange=e.target.dataset.range;document.querySelectorAll('[data-range]').forEach(b=>b.setAttribute('aria-pressed',String(b===e.target)));loadChart();}});
function chartNativeCurrency(){return currentSymbol.startsWith('KR:')?'KRW':'USD';}
// Shared by drawing and pointer mapping; the left side holds the price labels.
function chartPlotBounds(width){return {left:Math.min(100,width*.25),right:width-15};}
// The canvas charts do not read the CSS .gain/.loss/--muted colours, so both repeat them here.
function trendColor(value){return value>0?'#d94b57':value<0?'#367ae7':'#697580';}
function drawChart(){
 const canvas=$('priceChart'),rect=canvas.getBoundingClientRect(),dpr=window.devicePixelRatio||1,w=Math.max(250,rect.width),h=320;canvas.width=w*dpr;canvas.height=h*dpr;const ctx=canvas.getContext('2d');ctx.scale(dpr,dpr);ctx.clearRect(0,0,w,h);
 const nativeCurrency=chartNativeCurrency(),currency=viewCurrency(nativeCurrency),convert=v=>viewValue(v,nativeCurrency);$('periodPerformance').replaceChildren();$('periodDates').textContent='';
 if(!chartRows.length){ctx.fillStyle='#697580';ctx.font='15px sans-serif';ctx.fillText('표시할 차트 데이터가 없습니다.',20,145);$('chartTooltip').textContent='';return;}
 const vals=chartRows.map(r=>convert(Number(r.close)));
 if(vals.some(v=>v===null||!Number.isFinite(v))){ctx.fillStyle='#697580';ctx.font='15px sans-serif';ctx.fillText('환율 확인 대기',20,145);$('periodPerformance').textContent='환율 확인 대기 · —';$('chartTooltip').textContent='';return;}
 const first=vals[0],last=vals.at(-1),change=last-first,percent=first?change/first*100:null;
 const title=node('strong',`${currentRange} · ${change>0?'+':''}${nativeMoney(change,currency)} (${change>0?'+':''}${pct(percent)})`,change>0?'gain':change<0?'loss':'flat');
 $('periodPerformance').append(title,node('span',`${nativeMoney(first,currency)} → ${nativeMoney(last,currency)}`));
 const date=x=>new Date(x.time*1000).toLocaleString('ko-KR');
 $('periodDates').textContent=`표시 구간 첫 종가 대비 · ${date(chartRows[0])} ~ ${date(chartRows.at(-1))}${currency!==nativeCurrency?' · 전 구간을 현재 기준환율로 환산 (과거 환율 수익률 아님)':''}`;
 const lo=Math.min(...vals),hi=Math.max(...vals),span=hi-lo||Math.max(1,hi*.02),{left,right}=chartPlotBounds(w),top=20,bottom=275;
 const x=i=>left+i*(right-left)/Math.max(1,vals.length-1),y=v=>bottom-(v-lo)/span*(bottom-top);
 ctx.font='12px sans-serif';for(let n=0;n<5;n++){const v=lo+span*n/4,yy=y(v);ctx.strokeStyle='#e7ebef';ctx.beginPath();ctx.moveTo(left,yy);ctx.lineTo(right,yy);ctx.stroke();ctx.fillStyle='#697580';ctx.fillText(v.toLocaleString('ko-KR',{maximumFractionDigits:viewCurrency(currency)==='KRW'?0:2}),2,yy+4);}
 ctx.strokeStyle=trendColor(change);ctx.lineWidth=2.5;ctx.beginPath();vals.forEach((v,i)=>i?ctx.lineTo(x(i),y(v)):ctx.moveTo(x(i),y(v)));ctx.stroke();ctx.fillStyle='#697580';ctx.fillText(new Date(chartRows[0].time*1000).toLocaleDateString(),left,306);ctx.fillText(new Date(chartRows.at(-1).time*1000).toLocaleDateString(),Math.max(left,right-85),306);
 const i=chartIndex===null?vals.length-1:Math.max(0,Math.min(vals.length-1,chartIndex)),r=chartRows[i],delta=convert(Number(r.close))-first;
 if(chartIndex!==null){ctx.strokeStyle='#748191';ctx.setLineDash([3,3]);ctx.beginPath();ctx.moveTo(x(i),top);ctx.lineTo(x(i),bottom);ctx.stroke();ctx.setLineDash([]);}
 $('chartTooltip').replaceChildren(node('span',`${date(r)} · 종가 ${nativeMoney(convert(Number(r.close)),currency)} `),signed(delta,`구간 시작 대비 ${delta>0?'+':''}${nativeMoney(delta,currency)} (${delta>0?'+':''}${pct(delta/first*100)})`),node('span',` · 고가 ${nativeMoney(convert(Number(r.high)),currency)} · 저가 ${nativeMoney(convert(Number(r.low)),currency)} · 거래량 ${Number(r.volume).toLocaleString()}`));
}
function chartPointer(e){const rect=$('priceChart').getBoundingClientRect(),{left,right}=chartPlotBounds(rect.width);chartIndex=Math.round((e.clientX-rect.left-left)/(right-left)*Math.max(1,chartRows.length-1));drawChart();}
$('priceChart').addEventListener('pointermove',chartPointer);$('priceChart').addEventListener('pointerdown',chartPointer);
$('priceChart').addEventListener('pointerleave',()=>{chartIndex=null;drawChart();});
$('priceChart').addEventListener('keydown',e=>{if(['ArrowLeft','ArrowRight'].includes(e.key)){e.preventDefault();chartIndex=Math.max(0,Math.min(chartRows.length-1,(chartIndex??0)+(e.key==='ArrowRight'?1:-1)));drawChart();}});
new ResizeObserver(drawChart).observe($('priceChart'));

function renderPublic(){if(!publicCache)return;const p=publicCache;$('publicTitle').textContent=p.username+'님의 투자 현황';
  if(window.renderProfileCard)renderProfileCard($('publicProfile'),{username:p.username,bio:p.profile?.bio,image_version:p.profile?.image_version,equity_usd:p.equity_usd,return_pct:p.return_pct,rank:rankOf(p.username),member_days:p.member_days,member_since:p.member_since},false);
  renderMetrics($('publicMetrics'),p);if(window.renderAllocation)renderAllocation($('publicAllocation'),p);renderPositions($('publicPositions'),p);
  $('publicNotice').textContent=(p.return_basis||'초기 KRW 평가액 대비 (외부 입출금 반영)')+(p.errors.length?' · '+p.errors.join(' · '):'')+(p.stale?' · 마지막 시세 기준 평가':'');}
function rankOf(username){if(!rankingCache||rankingCache.incomplete&&!rankingCache.rows?.length)return null;const row=(rankingCache.rows||[]).find(r=>r.username===username);return row?row.rank:null;}
function renderOrderPreview(){
  const r=orderPreview;if(!r)return;
  const target=$('orderEstimate');target.replaceChildren();
  const fields=[['보유 주식',r.holding+'주'],['주문 가능 현금',viewMoney(r.balance,r.currency)],['선택 수량',r.quantity+'주'],['체결 금액 참고',viewMoney(r.gross_amount,r.currency)],['수수료',viewMoney(r.fee,r.currency)],['세금',viewMoney(r.tax,r.currency)],[$('side').value==='buy'?'결제 금액':'수령 금액',viewMoney(r.net_amount,r.currency)],['주문 후 잔액',viewMoney(r.balance_after,r.currency)]];
  if($('side').value==='sell')fields.push(['매도 후 보유',r.holding_after+'주']);
  for(const [label,value] of fields){const row=node('div',null,'cost-row');row.append(node('span',label),node('strong',value));target.append(row);}
  target.append(node('p',`실제 ${$('side').value==='buy'?'결제':'수령'}: ${nativeMoney(r.net_amount,r.currency)} (${r.currency})${r.indicative_only?' · 이전 시세 참고':''}${maxMode?' · 최대 수량은 체결 시 다시 계산':''}`,'field-help'));
  $('submitOrder').disabled=!r.can_submit;
  if(r.market_closed)target.append(node('p',r.market_closed,'market-closed-note'));
  if(!r.can_submit)target.append(node('p',r.quantity<1?'선택한 비율로 주문할 수 있는 수량이 없습니다.':'잔액 또는 보유 수량을 초과했습니다.','order-error'));
}
let previewRunning=false, previewQueued=null;
async function estimate(share=null){
  if(previewRunning){++previewVersion;previewQueued=[share];return;}
  previewRunning=true;
  try{await requestEstimate(share);}finally{previewRunning=false;if(previewQueued){const args=previewQueued;previewQueued=null;if(detailVisible())estimate(...args);}}
}
async function requestEstimate(share=null){
  clearTimeout(previewTimer);
  const version=++previewVersion,symbol=$('symbol').value,side=$('side').value,quantity=Number($('quantity').value);
  $('submitOrder').disabled=true;
  if(share===null&&(!Number.isInteger(quantity)||quantity<1||quantity>1000000)){orderPreview=null;$('orderEstimate').textContent='1주 이상의 정수 수량을 입력하세요.';return;}
  if(!/^(?:[A-Z][A-Z0-9-]{0,14}|KR:[0-9]{6})$/.test(symbol)){orderPreview=null;$('orderEstimate').textContent='주문할 종목을 선택하세요.';return;}
  const params={symbol,side,quantity:share===null?quantity:1};if(share!==null)params.share=share;
  try{
    const r=await api('order-preview?'+new URLSearchParams(params));
    if(version!==previewVersion||symbol!==$('symbol').value||side!==$('side').value)return;
    if(share!==null){$('quantity').value=r.quantity;maxMode=share===100&&r.quantity>0;}
    orderPreview=r;renderOrderPreview();
  }catch(e){if(version===previewVersion){orderPreview=null;$('orderEstimate').textContent=e.message;}}
}
async function chooseOrderShare(share){await estimate(Math.round(share*100));}
document.querySelectorAll('[data-order-share]').forEach(button=>button.addEventListener('click',()=>chooseOrderShare(Number(button.dataset.orderShare))));
// 매수 = red, 매도 = blue. The hidden select stays the single source of truth.
function setSide(side){$('side').value=side;document.querySelectorAll('[data-side]').forEach(b=>{if(b.getAttribute('role')==='radio')b.setAttribute('aria-checked',String(b.dataset.side===side));});$('submitOrder').dataset.side=side;$('submitOrder').textContent=side==='buy'?'매수 주문':'매도 주문';}
document.querySelectorAll('.side-toggle [data-side]').forEach(b=>b.addEventListener('click',()=>{if($('side').value===b.dataset.side)return;setSide(b.dataset.side);$('side').dispatchEvent(new Event('input'));}));
$('side').addEventListener('change',()=>setSide($('side').value));
window.orderSymbolLabel=function(){return detailCompany?.name||detailQuote?.name||$('symbol').value;};
['quantity','side','symbol'].forEach(id=>$(id).addEventListener('input',()=>{maxMode=false;++previewVersion;orderPreview=null;$('submitOrder').disabled=true;clearTimeout(previewTimer);previewTimer=setTimeout(()=>estimate(),200);}));
handle('watchAdd','click',async()=>{await api('watchlist',{symbol:currentSymbol});toast('관심종목에 추가했습니다.','success');});
async function watchlist(){watchCache=await api('watchlist');renderWatchlist();}
function renderWatchlist(){
  $('watchRows').replaceChildren();
  for(const r of watchCache){
    const card=node('article',null,'watch-card'),head=node('div',null,'watch-card-head'),title=node('div',null,'watch-title');
    const b=node('button',r.name,'text-button watch-name'),remove=node('button','삭제','secondary');
    b.addEventListener('click',()=>openStock(r.symbol));
    remove.addEventListener('click',async()=>{try{await removeWatch(r.symbol);await watchlist();}catch(e){message(e.message);}});
    title.append(b,node('small',r.currency==='KRW'?'한국 · 원화 거래':'미국 · 달러 거래','watch-symbol'));
    head.append(title,remove);card.append(head);
    if(r.quote){
      const q=r.quote;card.append(node('div',viewMoney(q.native_price,r.currency),'watch-price'));
      const change=node('div',null,'watch-change');
      change.append(node('span','전일 대비','watch-change-label'),signed(q.change,(Number(q.change)>0?'+':'')+viewMoney(q.change,r.currency)),signedPct(q.change_pct));card.append(change);
      if(viewValue(q.native_price,r.currency)===null)card.append(node('p','환율 확인 대기','field-help'));
    }else card.append(node('p',r.error||'시세를 확인할 수 없습니다.','field-help'));
    $('watchRows').append(card);
  }
  if(!watchCache.length)$('watchRows').append(node('p','종목 상세나 시장 탐색의 별 버튼으로 관심종목을 추가하세요.','empty-state'));
}
async function removeWatch(symbol){const r=await fetch('/api/watchlist/'+encodeURIComponent(symbol),{method:'DELETE',headers:{'X-CSRF-Token':csrf}});if(!r.ok)throw Error('관심종목을 삭제하지 못했습니다.');}
async function fxEstimate(){
  const r=await api('fx/preview',{source:$('fxSource').value,amount:$('fxAmount').value});
  $('fxEstimate').textContent=`${r.date} 기준환율 ${r.rate} · 스프레드 ${r.spread_bps} bps · 수수료 ${nativeMoney(r.fee,r.source)}\n최종 수령 ${nativeMoney(r.received,r.target)}\n예상 잔액 ${money(r.balances_after.USD)} / ${nativeMoney(r.balances_after.KRW,'KRW')}`;
}
handle('fxPreview','click',fxEstimate);
// Keep in step with '30분마다 확인' in #fxRate.
const FX_REFRESH_MS=30*60*1000;
async function loadFxRate(){
  try{
    const r=await api('fx');
    const usd=Number(r.rate),next=new Date(Date.now()+FX_REFRESH_MS).toLocaleTimeString('ko-KR',{hour:'2-digit',minute:'2-digit'});
    $('fxRate').textContent=`1 USD = ${usd.toLocaleString('ko-KR',{maximumFractionDigits:2})} KRW  ·  1,000 KRW = ${(1000/usd).toLocaleString('ko-KR',{maximumFractionDigits:4})} USD  ·  ${r.date} 기준 · 30분마다 확인 (다음 ${next})`;
  }catch(e){$('fxRate').textContent='환율을 불러오지 못했습니다. '+e.message;}
}
setInterval(()=>{if(!document.hidden&&location.hash==='#fx'&&window.sessionUsername&&!window.isAdmin)loadFxRate();},FX_REFRESH_MS);
window.updateFxBalance=function(){
  const source=$('fxSource').value,balance=window.walletBalances?.[source];
  $('fxAvailable').textContent=source==='USD'?`보유 달러 ${nativeMoney(balance,'USD')}`:`보유 원화 ${nativeMoney(balance,'KRW')}`;
  $('fxAmountUnit').textContent=source;
  $('fxAmount').step=source==='USD'?'0.0001':'1';$('fxAmount').min=source==='USD'?'0.0001':'1';
};
$('fxSource').addEventListener('change',()=>{exchangePending=null;$('fxAmount').value='';$('fxEstimate').textContent='';updateFxBalance();});
$('fxAmount').addEventListener('input',()=>{$('fxEstimate').textContent='';});
// Quick amounts as a share of the balance; the server rounds to the currency unit.
document.querySelectorAll('[data-fx-share]').forEach(b=>b.addEventListener('click',async()=>{
  try{
    const r=await api('fx/share?'+new URLSearchParams({source:$('fxSource').value,percent:b.dataset.fxShare}));
    exchangePending=null;$('fxAmount').value=String(r.amount);
    if(Number(r.amount)<=0){$('fxEstimate').textContent=`${r.source} 보유 금액이 없어 환전할 수 있는 금액이 없습니다.`;return;}
    await fxEstimate();
  }catch(e){$('fxEstimate').textContent=e.message;}
}));
updateFxBalance();
handle('fxForm','submit',async()=>{
  const data={source:$('fxSource').value,amount:$('fxAmount').value},sig=JSON.stringify(data);
  exchangePending=reuseRequestId(exchangePending,sig);
  await api('fx/exchange',{...data,request_id:exchangePending.id});
  exchangePending=null;toast('모의 환전이 완료되었습니다.','success');
  await refresh();await fxHistory();
});
async function fxHistory(){
  const rows=await api('fx/history');
  table($('fxHistory'),['시각','보낸 금액','받은 금액','수수료','환율 기준일'],rows.map(r=>[new Date(r.created_at).toLocaleString(),nativeMoney(r.amount,r.source),nativeMoney(r.received,r.target),nativeMoney(r.fee,r.source),r.rate_date]));
}
let adminUsers=[],adminSelectedId=null,adminPending=null,adminSearchTimer=null,adminSearchVersion=0;
const adminLabel=u=>u.note?`${u.username} - ${u.note}`:u.username;
function adminSelected(){return adminUsers.find(u=>u.id===adminSelectedId)||null;}
function adminMarketText(name,m){
 if(!m)return '';
 const st=m.stream,rest=Object.entries(m.rest).map(([k,v])=>`${k} ${v?(v.ok?'ok':'fail '+(v.error||'')):'no data'}`).join(', ');
 return `${name} Session: ${m.session} (${m.label}) · open ${m.open} · tradable ${m.tradable} · venue ${m.venue||'—'}${m.venues_open?.length?' ['+m.venues_open.join('+')+']':''} · Price mode: ${m.price_mode} · Stream: ${st.state}${st.healthy?' (healthy)':''} · Last message: ${st.last_message_age==null?'—':st.last_message_age+'s ago'} · Subscribed: ${st.subscribed.join(' ')||'—'} (전체 ${st.all_subscribed.length} / ${st.limit??'—'}) · REST: ${rest}`;
}
function renderAdminStatus(r){
 $('adminHealth').textContent=`DB ${r.health.database} · Redis ${r.health.redis} · 국내 시세 ${r.providers.kr?'설정됨':'미설정'} · 미국 시세 ${r.providers.us?'설정됨':'미설정'}`;
 $('adminMarket').textContent=[adminMarketText('KR',r.kr_market),adminMarketText('US',r.us_market),r.us_market?`Queued: ${r.us_market.stream.queued.join(' ')||'—'} · Reconnects: ${r.us_market.stream.reconnects}${r.us_market.stream.last_error?' · Last error: '+r.us_market.stream.last_error:''} · Redis ${r.health.redis}`:''].filter(Boolean).join('\n');
 $('adminFees').textContent=Object.entries(r.fees).map(([k,v])=>k+': '+v+' bps').join(' · ');
 $('initialAmount').value=r.initial_usd;
}
function renderAdminOverview(r){
 $('adminOverview').replaceChildren();
 for(const [label,value] of [['사용자',r.counts.users],['체결',r.counts.transactions],['보유 종목',r.counts.positions],['대기 주문',r.counts.pending_orders]]){const box=node('div',null,'metric');box.append(node('small',label),node('strong',value));$('adminOverview').append(box);}
}
function renderAdminUsers(r){
 $('adminUsers').replaceChildren();
 for(const u of r.users){
  const row=node('div',null,'watch-row'),status=node('button',u.active?'계정 정지':'계정 활성화','secondary');
  row.append(node('strong',`${adminLabel(u)} · ${u.admin?'관리자':'일반'} · ${u.active?'활성':'정지'}`),node('span',nativeMoney(u.wallets.USD,'USD')+' / '+nativeMoney(u.wallets.KRW,'KRW')),status);
  status.addEventListener('click',async()=>{try{await api(`admin/users/${u.id}/active`,{active:!u.active});await admin();}catch(e){toast(e.message,'error');}});
  $('adminUsers').append(row);
 }
}
async function admin(){
 const r=await api('admin');adminUsers=r.users;
 renderAdminStatus(r);
 noticeTemplates=r.notice_templates||noticeTemplates;renderNoticeAdmin(r.notice);
 renderAdminOverview(r);
 renderAdminUsers(r);
 if(adminSelectedId!==null&&!adminSelected())adminSelectedId=null;
 await searchAdminUsers();renderAdminSelected();
 const rows=await api('admin/audit');
 table($('adminAudit'),['시각','운영자','대상','작업','사유'],rows.map(r=>[new Date(r.created_at).toLocaleString(),r.actor||'삭제된 계정',r.target||'삭제된 계정',r.action,r.reason]));
}
// Searches the server by ID or administrator memo.
async function searchAdminUsers(){
 const version=++adminSearchVersion,q=$('adminTargetSearch').value.trim();
 const rows=await api('admin/users/search?'+new URLSearchParams({q}));if(version!==adminSearchVersion)return;
 const target=$('adminSearchResults');target.replaceChildren();
 for(const u of rows){const b=node('button',null,'admin-result');b.type='button';b.setAttribute('role','option');b.setAttribute('aria-selected',String(u.id===adminSelectedId));b.append(node('strong',u.username),node('span',u.note||'메모 없음','admin-result-note'),node('span',`${u.admin?'관리자':'일반'} · ${u.active?'활성':'정지'}`,'admin-result-meta'));b.addEventListener('click',()=>{adminSelectedId=u.id;renderAdminSelected();target.querySelectorAll('.admin-result').forEach(x=>x.setAttribute('aria-selected',String(x===b)));});target.append(b);}
 if(!rows.length)target.append(node('p',q?'검색 결과가 없습니다. 아이디나 메모의 일부로 검색하세요.':'사용자가 없습니다.','field-help'));
}
$('adminTargetSearch').addEventListener('input',()=>{clearTimeout(adminSearchTimer);adminSearchTimer=setTimeout(()=>searchAdminUsers().catch(e=>toast(e.message,'error')),200);});
function renderAdminSelected(){
 const u=adminSelected();$('adminSelected').hidden=!u;if(!u)return;
 $('adminSelectedName').textContent=adminLabel(u);
 $('adminSelectedMeta').textContent=`${u.admin?'관리자':'일반'} · ${u.active?'활성':'정지'} · 현금 USD ${nativeMoney(u.wallets.USD,'USD')} · KRW ${nativeMoney(u.wallets.KRW,'KRW')}`;
 $('adminNote').value=u.note||'';
 const self=u.username===window.sessionUsername;
 document.querySelector('[data-admin-action="delete"]').disabled=self;
}
handle('adminNoteForm','submit',async()=>{const u=adminSelected();if(!u)return;const r=await api(`admin/users/${u.id}/note`,{note:$('adminNote').value});toast(r.note?`${u.username} 메모를 저장했습니다.`:`${u.username} 메모를 지웠습니다.`,'success');await admin();});
async function runAdminAction(action,extra={}){
 const u=adminSelected();if(!u)throw Error('대상 사용자를 먼저 선택하세요.');
 const body={action,currency:'USD',amount:'0',reason:$('adminReason').value.trim(),...extra},sig=JSON.stringify({target:u.id,...body});
 adminPending=reuseRequestId(adminPending,sig);
 const buttons=[...document.querySelectorAll('#adminSelected button')];buttons.forEach(b=>b.disabled=true);
 try{await api(`admin/users/${u.id}/manage`,{...body,request_id:adminPending.id});adminPending=null;
  const done={grant:'지원금을 지급했습니다.',rebase:'수익률 기준을 현재 평가금액으로 재설정했습니다.',clear:'회원가입 직후 상태로 초기화했습니다.',delete:'계정을 영구 삭제했습니다.'}[action];
  $('adminResult').textContent=`${u.username}: ${done}`;toast(`${u.username}: ${done}`,'success');$('adminReason').value='';
  if(action==='delete')adminSelectedId=null;await admin();
 }catch(e){toast(e.message,'error',7000);throw e;}finally{buttons.forEach(b=>b.disabled=false);renderAdminSelected();}
}
handle('adminGrantForm','submit',()=>runAdminAction('grant',{currency:$('adminCurrency').value,amount:$('adminAmount').value}));
document.querySelectorAll('[data-admin-action]').forEach(b=>b.addEventListener('click',()=>runAdminAction(b.dataset.adminAction).catch(e=>$('adminResult').textContent=e.message)));
for(const [select,input] of [['adminCurrency','adminAmount'],['adminBulkCurrency','adminBulkAmount']])$(select).addEventListener('change',()=>{$(input).step=$(select).value==='KRW'?'1':'0.0001';$(input).min=$(input).step;});
let noticeTemplates={},noticeKindShown=null;
function renderNoticeAdmin(notice){
 $('noticeStatus').textContent=notice?`게시 중 · ${notice.label} · ${notice.title} · ${new Date(notice.posted_at).toLocaleString()}`:'게시 중인 공지가 없습니다. 등록하면 사용자 화면 상단에 표시됩니다.';
 $('noticeClear').disabled=!notice;$('noticePost').textContent=notice?'새 공지로 교체':'공지 등록';
 if(noticeKindShown===null)fillNoticeTemplate();
}
// Picking a type fills in its template, unless the text was already edited.
function fillNoticeTemplate(){
 const previous=noticeTemplates[noticeKindShown],next=noticeTemplates[$('noticeKind').value]||{title:'',body:''};
 const untouched=!previous||($('noticeTitle').value===previous.title&&$('noticeBody').value===previous.body)||(!$('noticeTitle').value&&!$('noticeBody').value);
 if(untouched){$('noticeTitle').value=next.title;$('noticeBody').value=next.body;}
 noticeKindShown=$('noticeKind').value;updateNoticeCount();
}
function updateNoticeCount(){$('noticeCount').textContent=`${$('noticeBody').value.length}/500`;$('noticeError').textContent='';}
$('noticeKind').addEventListener('change',fillNoticeTemplate);
$('noticeBody').addEventListener('input',updateNoticeCount);$('noticeTitle').addEventListener('input',()=>{$('noticeError').textContent='';});
$('noticeForm').addEventListener('submit',async e=>{
 e.preventDefault();const body={kind:$('noticeKind').value,title:$('noticeTitle').value.trim(),body:$('noticeBody').value.trim()};
 if(!body.title||!body.body){$('noticeError').textContent='공지 제목과 내용을 입력하세요.';return;}
 $('noticePost').disabled=true;
 try{const r=await api('admin/notice',body);renderNoticeAdmin(r.notice);toast(`공지를 등록했습니다.\n${r.notice.label} · ${r.notice.title}`,'success');}catch(err){$('noticeError').textContent=err.message;}finally{$('noticePost').disabled=false;}
});
handle('noticeClear','click',async()=>{$('noticeClear').disabled=true;try{await api('admin/notice/clear',{});renderNoticeAdmin(null);toast('공지를 해제했습니다.','success');}catch(err){$('noticeClear').disabled=false;throw err;}});
let adminBulkPending=null;
handle('adminBulkForm','submit',async()=>{const body={action:'grant',currency:$('adminBulkCurrency').value,amount:$('adminBulkAmount').value,reason:$('adminReason').value.trim()},sig=JSON.stringify(body);adminBulkPending=reuseRequestId(adminBulkPending,sig);const r=await api('admin/users/manage-all',{...body,request_id:adminBulkPending.id});adminBulkPending=null;$('adminResult').textContent=`${r.count}명에게 지원금을 지급했습니다.`;toast(`${r.count}명에게 지원금을 지급했습니다.`,'success');await admin();});
handle('initialForm','submit',async()=>{await api('admin/initial',{amount:$('initialAmount').value});message('이후 생성/초기화되는 계좌의 지급액을 저장했습니다.');});
syncCurrency();routePage();

window.loadLimits=async function(){
  const rows=await api('limit-orders');$('limitRows').replaceChildren();$('legacyLimitRows').replaceChildren();$('legacyMarkets').hidden=!rows.some(r=>r.order_type==='market');$('legacyLimits').hidden=!rows.some(r=>r.order_type==='limit');
  const statuses={pending:'대기 중',filled:'체결',cancelled:'취소',rejected:'거절'};
  for(const r of rows){
    const row=node('div',null,'watch-row');row.append(node('span',`${r.symbol} · ${r.side==='buy'?'매수':'매도'} ${r.use_max?'최대':r.quantity+'주'} · ${statuses[r.status]||r.status}${r.reason?' · '+r.reason:''}`));
    if(r.status==='pending'){const b=node('button','취소','secondary');b.addEventListener('click',async()=>{try{await api(`limit-orders/${r.id}/cancel`,{});await loadLimits();}catch(e){message(e.message);}});row.append(b);}
    $(r.order_type==='market'?'limitRows':'legacyLimitRows').append(row);
  }
};


// Daily snapshots begin when collection starts; no earlier days are invented.
let performanceUser=null,performancePeriod='1M',performanceVersion=0;
async function loadPerformance(username,period=performancePeriod){
  performanceUser=username;performancePeriod=period;const version=++performanceVersion;
  document.querySelectorAll('#performanceRanges button').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.period===period)));
  $('performanceNotice').textContent='성과 기록 조회 중…';
  try{const r=await api('performance/'+encodeURIComponent(username)+'?period='+period);if(version!==performanceVersion)return;drawPerformance(r);}
  catch(e){if(version===performanceVersion){drawPerformance({snapshots:[]});$('performanceNotice').textContent=e.message;}}
}
function drawPerformance(r){
  const canvas=$('performanceChart'),ctx=canvas.getContext('2d'),rows=r.snapshots||[];
  canvas.width=canvas.clientWidth||600;ctx.clearRect(0,0,canvas.width,canvas.height);
  if(rows.length<2){$('performanceNotice').textContent=rows.length?'기록이 하루치뿐입니다. 매일 한 번 쌓입니다.':'아직 성과 기록이 없습니다. 매일 한 번 쌓입니다.';return;}
  const vals=rows.map(x=>Number(x.cumulative_return_pct)),min=Math.min(...vals,0),max=Math.max(...vals,0),span=max-min||1;
  const left=8,right=canvas.width-8,top=12,bottom=canvas.height-24,x=i=>left+(right-left)*i/(rows.length-1),y=v=>bottom-(bottom-top)*(v-min)/span;
  ctx.strokeStyle='#d0d5db';ctx.beginPath();ctx.moveTo(left,y(0));ctx.lineTo(right,y(0));ctx.stroke();
  const last=vals.at(-1);ctx.strokeStyle=trendColor(last);ctx.lineWidth=2.5;ctx.beginPath();vals.forEach((v,i)=>i?ctx.lineTo(x(i),y(v)):ctx.moveTo(x(i),y(v)));ctx.stroke();ctx.lineWidth=1;
  ctx.fillStyle='#697580';ctx.fillText(rows[0].date,left,canvas.height-6);ctx.fillText(rows.at(-1).date,Math.max(left,right-70),canvas.height-6);
  const period=r.period_return_pct==null?'':` · 기간 수익률 ${pct(r.period_return_pct)}`;
  $('performanceNotice').textContent=`누적 수익률 ${pct(last)}${period}${r.baseline_changed?' · 기간 중 수익률 기준 재설정 있음':''}${rows.some(x=>x.stale)?' · 일부 날짜는 마지막 확인 시세 기준':''}`;
}
document.querySelectorAll('#performanceRanges button').forEach(b=>b.addEventListener('click',()=>{if(performanceUser)loadPerformance(performanceUser,b.dataset.period);}));
