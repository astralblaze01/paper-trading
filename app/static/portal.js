/* Page navigation and chart rendering. Prices used for settlement stay on the server. */
let currentSymbol = '', currentRange = '1D', chartRows = [], chartIndex = null, loadVersion = 0, stockLoading = false;
let exchangePending = null;
let detailChange = null, detailQuote=null, publicCache=null;
function recentKey(){return 'paper-harbor:recent:'+window.sessionUsername;}
window.renderRecentStocks=function(){
  $('searchResults').replaceChildren();
  let rows=[];try{rows=JSON.parse(localStorage.getItem(recentKey())||'[]');}catch{}
  $('searchResults').append(node('p','최근 본 종목','field-help'));
  for(const r of rows.slice(0,8)){const b=node('button',r.name+' · '+r.symbol,'text-button');b.type='button';b.addEventListener('click',()=>openStock(r.symbol));$('searchResults').append(b);}
  if(!rows.length)$('searchResults').append(node('p','종목을 조회하면 여기에 표시됩니다.','field-help'));
};
function rememberStock(symbol,name){if(!window.sessionUsername)return;try{let rows=JSON.parse(localStorage.getItem(recentKey())||'[]');rows=[{symbol,name:name||symbol},...rows.filter(r=>r.symbol!==symbol)].slice(0,8);localStorage.setItem(recentKey(),JSON.stringify(rows));}catch{}renderRecentStocks();}

let exploreVersion = 0;
let exploreMode = 'ranking';
let exploreRowsCache = [], explorePopular = false, displayFx = null;
function node(tag, text, cls) { const n = document.createElement(tag); if(text!=null)n.textContent=text; if(cls)n.className=cls; return n; }
function uuid() { const b=crypto.getRandomValues(new Uint8Array(16)); b[6]=(b[6]&15)|64;b[8]=(b[8]&63)|128;const h=Array.from(b,x=>x.toString(16).padStart(2,'0')).join('');return `${h.slice(0,8)}-${h.slice(8,12)}-${h.slice(12,16)}-${h.slice(16,20)}-${h.slice(20)}`; }
window.routePage = async function() {
  const [pageName, symbol] = location.hash.slice(1).split('/');
  const selected=['explore','portfolio','history','fx','watchlist','ranking','admin','detail','public','transfer'].includes(pageName)?pageName:'explore';
  document.querySelectorAll('[data-page]').forEach(el=>el.hidden=el.dataset.page!==selected);
  document.querySelectorAll('.app-nav a').forEach(a=>a.setAttribute('aria-current',a.hash==='#'+selected?'page':'false'));
  if($('dashboard').hidden)return;
  try {
    if(selected==='explore')await explore();
    if(selected==='public' && symbol){publicCache=null;$('publicTitle').textContent='포트폴리오 조회 중…';$('publicPositions').replaceChildren();$('publicMetrics').replaceChildren();publicCache=await api('portfolios/'+encodeURIComponent(decodeURIComponent(symbol)));renderPublic();}
    if(selected==='detail' && symbol) { currentSymbol=decodeURIComponent(symbol); $('symbol').value=currentSymbol;maxMode=false;loadCompany(currentSymbol);await loadStock(true);await api('popularity',{symbol:currentSymbol,kind:'view'}); }
    if(selected==='transfer'){updateTransferBalance();await transferHistory();}
    if(selected==='fx'){await Promise.all([fxHistory(),loadFxRate()]);}
    if(selected==='watchlist')await watchlist();
    if(selected==='admin')await admin();
  } catch(e){message(e.message);}
};
window.openStock = function(symbol) { if(location.hash==='#detail/'+encodeURIComponent(symbol))loadStock(true).catch(e=>message(e.message));else location.hash='detail/'+encodeURIComponent(symbol); };
window.addEventListener('hashchange',routePage);
function displayedPrice(r) {
  const currency=$('exploreCurrency').value;
  if(r.price==null)return '—';
  if(currency==='native'||currency===r.currency)return nativeMoney(r.price,r.currency);
  if(!displayFx)return '환율 확인 대기';
  const rate=Number(displayFx.rate);
  return nativeMoney(Number(r.price)*(r.currency==='USD'?rate:1/rate),currency);
}
function displayedTurnover(r){
  if(r.turnover==null || !Number.isFinite(Number(r.turnover)) || Number(r.turnover)<0)return '—';
  const selected=$('exploreCurrency').value,currency=selected==='native'?r.currency:selected;
  if(currency!==r.currency&&!displayFx)return '환율 확인 대기';
  const rate=Number(displayFx?.rate||1),value=Number(r.turnover)*(currency===r.currency?1:r.currency==='USD'?rate:1/rate);
  const unit=currency==='KRW'?'억원':'백만 달러',divisor=currency==='KRW'?1e8:1e6;
  return `${(value/divisor).toLocaleString('ko-KR',{maximumFractionDigits:1})}${unit}${r.turnover_estimated?' (추정)':''}`;
}
function stockTable(target,rows,popular=false){
 target.replaceChildren();const t=node('table',null,'market-table'),head=node('tr');
 [['#','rank-number'],['종목','stock-name'],['현재가','price-cell'],['등락률','change-cell'],['거래대금','turnover-cell'],[popular?'인기 점수':'거래량','volume-cell'],['시장','market-cell']].forEach(([label,cls])=>head.append(node('th',label,cls)));const thead=node('thead');thead.append(head);t.append(thead);const body=node('tbody');
 rows.forEach((r,i)=>{const tr=node('tr'),first=node('td',null,'stock-name'),b=node('button',r.name||r.symbol,'text-button stock-link');b.addEventListener('click',()=>openStock(r.symbol));first.append(b);tr.append(node('td',i+1,'rank-number'),first,node('td',displayedPrice(r),'price-cell'));const change=Number(r.change_pct);tr.append(node('td',r.change_pct==null?'—':`${change>0?'+':''}${pct(change)}`,'change-cell '+(change>0?'gain':change<0?'loss':'flat')),node('td',displayedTurnover(r),'turnover-cell'),node('td',popular?r.score:r.volume==null?'—':Number(r.volume).toLocaleString(),'volume-cell'),node('td',r.market==='KR'||r.currency==='KRW'?'한국':'미국','market-cell'));body.append(tr);});t.append(body);target.append(t);if(!rows.length)target.append(node('p','표시할 종목이 없습니다.','empty-state'));
}
async function loadDisplayFx(){try{displayFx=await api('fx');$('exploreFxNote').textContent=`환산 표시는 ${displayFx.date} ECB 일별 기준환율 (1 USD = ${Number(displayFx.rate).toLocaleString('ko-KR',{maximumFractionDigits:2})} KRW) 기준입니다. 실제 거래 통화와 주문 가격은 바뀌지 않습니다.`;}catch(e){displayFx=null;$('exploreFxNote').textContent='환율을 확인하지 못해 다른 통화로 환산한 가격을 표시할 수 없습니다.';}}
async function explore(silent=false) {
  exploreMode='ranking';
  const version=++exploreVersion,asset=$('exploreMarket').value,kind=$('exploreKind').value;
  document.querySelectorAll('[data-asset]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.asset===asset)));
  document.querySelectorAll('[data-kind]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.kind===kind)));
  if(!silent){$('exploreNotice').textContent='목록을 불러오는 중입니다.';$('exploreRows').replaceChildren();}
  try{
    const [r]=await Promise.all([api('explore?'+new URLSearchParams({asset,kind})),loadDisplayFx()]);if(version!==exploreVersion)return;
    $('exploreNotice').textContent=[r.scope,r.notice].filter(Boolean).join(' · ');
    if($('status').textContent==='입력값을 확인하세요.')$('status').textContent='';
    exploreRowsCache=r.rows;explorePopular=kind==='popular';stockTable($('exploreRows'),r.rows,explorePopular);
  }catch(e){if(version===exploreVersion){$('exploreNotice').textContent=e.message;stockTable($('exploreRows'),[]);}}
}
$('exploreMarkets').addEventListener('click',e=>{const b=e.target.closest('[data-asset]');if(!b)return;$('exploreMarket').value=b.dataset.asset;explore();});
$('exploreKinds').addEventListener('click',e=>{const b=e.target.closest('[data-kind]');if(!b)return;$('exploreKind').value=b.dataset.kind;explore();});
handle('exploreMarket','change',()=>explore());handle('exploreKind','change',()=>explore());
$('exploreCurrency').addEventListener('change',e=>changeDisplayCurrency(e.target.value));
$('detailCurrency').addEventListener('change',e=>changeDisplayCurrency(e.target.value));
window.addEventListener('displaycurrencychange',()=>{displayFx=viewFx;stockTable($('exploreRows'),exploreRowsCache,explorePopular);renderDetailQuote();drawChart();renderPublic();if(location.hash==='#watchlist')watchlist().catch(e=>message(e.message));});
handle('discoverySearch','submit',async()=>{exploreMode='search';++exploreVersion;const query=$('discoveryQuery').value;const rows=await api('search?'+new URLSearchParams({q:query,category:$('exploreMarket').value}));exploreRowsCache=rows;explorePopular=false;stockTable($('exploreRows'),rows);$('exploreNotice').textContent='검색 결과 · 등록 종목 목록이며 가격은 종목 상세에서 확인합니다.';if(rows.length===1)await api('popularity',{symbol:rows[0].symbol,kind:'search'});});
setInterval(()=>{if(!document.hidden&&(!location.hash||location.hash==='#explore')&&!$('dashboard').hidden&&exploreMode==='ranking')explore(true);},30000);
window.loadStock = async function(withChart=true) {
  if(!currentSymbol)return;
  const version=++loadVersion, symbol=currentSymbol;
  $('detailTitle').textContent=symbol;$('detailPrice').textContent='시세 조회 중…';$('detailPrice').className='detail-price';detailChange=null;detailQuote=null;
  const results=await Promise.allSettled([api('quote/'+encodeURIComponent(symbol)),api('market-status/'+encodeURIComponent(symbol))]);
  if(version!==loadVersion)return;
  if(results[0].status==='fulfilled') {
    const q=results[0].value;detailQuote=q;detailChange=q.change_pct==null?null:Number(q.change_pct);rememberStock(symbol,q.name);renderDetailQuote();
    $('quoteInfo').textContent=nativeMoney(q.native_price,q.currency)+' · '+(q.stale?'시장가 주문은 새 시세가 올 때까지 대기합니다.':'주문 시 가격이 달라질 수 있습니다.');
  } else { $('detailPrice').textContent='시세를 불러오지 못했습니다.';$('detailMeta').textContent=results[0].reason.message; }
  $('marketState').textContent=results[1].status==='fulfilled'?`${results[1].value.label} · ${results[1].value.timezone}${results[1].value.verified?'':' · 확정 상태 아님'}`:'장 상태를 확인할 수 없습니다.';
  try { await estimate(false); } catch(e) { $('orderEstimate').textContent=e.message; }
  if(withChart)await loadChart();else drawChart();
};
function renderDetailQuote(){
  const q=detailQuote;if(!q)return;
  $('detailPrice').replaceChildren(node('strong',viewMoney(q.native_price,q.currency)),node('span',' 전일 대비 '+(Number(q.change)>0?'+':'')+viewMoney(q.change,q.currency)+' ('+(detailChange>0?'+':'')+pct(q.change_pct)+')','day-change'));
  $('detailPrice').className='detail-price '+(detailChange>0?'gain':detailChange<0?'loss':'flat');
  $('detailMeta').textContent=`${q.data_status||q.source||'공급자 시세'} · ${new Date(q.timestamp*1000).toLocaleString()}${q.stale?' · 오래된 시세':''} · 고가 ${viewMoney(q.high,q.currency)} / 저가 ${viewMoney(q.low,q.currency)} · 거래량 ${q.volume==null?'미제공':Number(q.volume).toLocaleString()}`;
}
async function loadCompany(symbol){$('companyInfo').textContent='회사 정보를 불러오는 중입니다.';try{const r=await api('company/'+encodeURIComponent(symbol));if(symbol!==currentSymbol)return;$('detailTitle').textContent=r.name+' · '+symbol;const target=$('companyInfo');target.replaceChildren();const fields=[['회사 / 상품명',r.name],['업종',r.industry],['거래소',r.exchange],['국가',r.country],['상장일',r.ipo],['자산 종류',categories[r.category]]];for(const [label,value] of fields){if(!value)continue;const d=node('div');d.append(node('small',label),node('strong',value));target.append(d);}if(r.website){try{const u=new URL(r.website);if(['http:','https:'].includes(u.protocol)){const a=node('a','공식 홈페이지 ↗');a.href=u.href;a.target='_blank';a.rel='noopener noreferrer';target.append(a);}}catch{}}target.append(node('p',[r.source,r.notice].filter(Boolean).join(' · '),'field-help'));}catch(e){if(symbol===currentSymbol)$('companyInfo').textContent=e.message;}}
async function loadChart(){const symbol=currentSymbol,range=currentRange;chartRows=[];chartIndex=null;drawChart();$('chartNotice').textContent='차트 조회 중…';try{const r=await api('candles/'+encodeURIComponent(symbol)+'?range='+range);if(symbol!==currentSymbol||range!==currentRange)return;chartRows=r.candles;chartIndex=null;$('chartNotice').textContent=`${r.source} · ${r.resolution} · ${r.data_status}${r.stale?' · 마지막 데이터가 오래되었습니다':''}${r.partial?' · 요청 기간 중 공급자가 제공한 범위만 표시':''}${!r.candles.length?' · 데이터 없음':''}`;drawChart();}catch(e){if(symbol===currentSymbol&&range===currentRange){$('chartNotice').textContent=e.message;chartRows=[];drawChart();}}}
$('chartRanges').addEventListener('click',e=>{if(e.target.dataset.range){currentRange=e.target.dataset.range;document.querySelectorAll('[data-range]').forEach(b=>b.setAttribute('aria-pressed',String(b===e.target)));loadChart();}});
function chartCurrency(){return currentSymbol.startsWith('KR:')?'KRW':'USD';}
function drawChart(){
 const canvas=$('priceChart'),rect=canvas.getBoundingClientRect(),dpr=window.devicePixelRatio||1,w=Math.max(250,rect.width),h=320;canvas.width=w*dpr;canvas.height=h*dpr;const ctx=canvas.getContext('2d');ctx.scale(dpr,dpr);ctx.clearRect(0,0,w,h);
 const currency=chartCurrency();$('periodPerformance').replaceChildren();$('periodDates').textContent='';
 if(!chartRows.length){ctx.fillStyle='#697580';ctx.font='15px sans-serif';ctx.fillText('표시할 차트 데이터가 없습니다.',20,145);$('chartTooltip').textContent='';return;}
 const first=Number(chartRows[0].close),last=Number(chartRows.at(-1).close),change=last-first,percent=first?change/first*100:null;
 const title=node('strong',`${currentRange} · ${change>0?'+':''}${viewMoney(change,currency)} (${change>0?'+':''}${pct(percent)})`,change>0?'gain':change<0?'loss':'flat');
 $('periodPerformance').append(title,node('span',`${viewMoney(first,currency)} → ${viewMoney(last,currency)}`));
 const date=x=>new Date(x.time*1000).toLocaleString('ko-KR');
 $('periodDates').textContent=`표시 구간 첫 종가 대비 · ${date(chartRows[0])} ~ ${date(chartRows.at(-1))}${viewCurrency(currency)!==currency?' · 전 구간을 현재 기준환율로 환산 (과거 환율 수익률 아님)':''}`;
 const vals=chartRows.map(r=>viewValue(r.close,currency));if(vals.some(v=>v===null)){ctx.fillText('환율을 확인한 뒤 차트를 표시합니다.',15,140);return;}
 const lo=Math.min(...vals),hi=Math.max(...vals),span=hi-lo||Math.max(1,hi*.02),left=Math.min(100,w*.25),right=w-15,top=20,bottom=275;
 const x=i=>left+i*(right-left)/Math.max(1,vals.length-1),y=v=>bottom-(v-lo)/span*(bottom-top);
 ctx.font='12px sans-serif';for(let n=0;n<5;n++){const v=lo+span*n/4,yy=y(v);ctx.strokeStyle='#e7ebef';ctx.beginPath();ctx.moveTo(left,yy);ctx.lineTo(right,yy);ctx.stroke();ctx.fillStyle='#697580';ctx.fillText(v.toLocaleString('ko-KR',{maximumFractionDigits:viewCurrency(currency)==='KRW'?0:2}),2,yy+4);}
 ctx.strokeStyle=change>0?'#d94b57':change<0?'#367ae7':'#697580';ctx.lineWidth=2.5;ctx.beginPath();vals.forEach((v,i)=>i?ctx.lineTo(x(i),y(v)):ctx.moveTo(x(i),y(v)));ctx.stroke();ctx.fillStyle='#697580';ctx.fillText(new Date(chartRows[0].time*1000).toLocaleDateString(),left,306);ctx.fillText(new Date(chartRows.at(-1).time*1000).toLocaleDateString(),Math.max(left,right-85),306);
 const i=chartIndex===null?vals.length-1:Math.max(0,Math.min(vals.length-1,chartIndex)),r=chartRows[i],delta=Number(r.close)-first;
 if(chartIndex!==null){ctx.strokeStyle='#748191';ctx.setLineDash([3,3]);ctx.beginPath();ctx.moveTo(x(i),top);ctx.lineTo(x(i),bottom);ctx.stroke();ctx.setLineDash([]);}
 $('chartTooltip').replaceChildren(node('span',`${date(r)} · 종가 ${viewMoney(r.close,currency)} `),signed(delta,`구간 시작 대비 ${delta>0?'+':''}${viewMoney(delta,currency)} (${delta>0?'+':''}${pct(delta/first*100)})`),node('span',` · 고가 ${viewMoney(r.high,currency)} · 저가 ${viewMoney(r.low,currency)} · 거래량 ${Number(r.volume).toLocaleString()}`));
}
function chartPointer(e){const rect=$('priceChart').getBoundingClientRect(),left=Math.min(100,rect.width*.25);chartIndex=Math.round((e.clientX-rect.left-left)/(rect.width-left-15)*Math.max(1,chartRows.length-1));drawChart();}
$('priceChart').addEventListener('pointermove',chartPointer);$('priceChart').addEventListener('pointerdown',chartPointer);
$('priceChart').addEventListener('pointerleave',()=>{chartIndex=null;drawChart();});
$('priceChart').addEventListener('keydown',e=>{if(['ArrowLeft','ArrowRight'].includes(e.key)){e.preventDefault();chartIndex=Math.max(0,Math.min(chartRows.length-1,(chartIndex??0)+(e.key==='ArrowRight'?1:-1)));drawChart();}});
new ResizeObserver(drawChart).observe($('priceChart'));
setInterval(async()=>{if(document.hidden||!location.hash.startsWith('#detail/')||$('dashboard').hidden||stockLoading)return;stockLoading=true;try{await loadStock(false);}catch(e){message(e.message);}finally{stockLoading=false;}},30000);
function renderPublic(){if(!publicCache)return;const p=publicCache;$('publicTitle').textContent=p.username+'님의 포트폴리오';$('publicMetrics').replaceChildren();for(const [label,value,change] of [['총자산',viewMoney(p.equity,'KRW')],['가상 현금',viewMoney(p.wallets.USD,'USD')+' / '+viewMoney(p.wallets.KRW,'KRW')],['총 손익',viewMoney(p.pnl,'KRW'),p.pnl],['수익률',pct(p.return_pct),p.return_pct]]){const d=node('div',null,'metric');d.append(node('small',label),signed(change,value));$('publicMetrics').append(d);}renderPositions($('publicPositions'),p);$('publicNotice').textContent=(p.return_basis||'초기 KRW 평가액 대비 (외부 입출금 반영)')+(p.errors.length?' · '+p.errors.join(' · '):'')+(p.stale?' · 마지막 시세 기준 평가':'');}
async function estimate(max=false){const r=await api('order-preview?'+new URLSearchParams({symbol:$('symbol').value,side:$('side').value,quantity:$('quantity').value}));if(max){$('quantity').value=r.max_quantity;maxMode=true;}
  const actual=max && r.max_quantity>0?await api('order-preview?'+new URLSearchParams({symbol:$('symbol').value,side:$('side').value,quantity:r.max_quantity})):r;
  const target=$('orderEstimate');target.replaceChildren();
  for(const [label,value] of [['보유 주식',r.holding+'주'],['평균 매입가',nativeMoney(r.average_cost,r.currency)],['미실현 손익',nativeMoney(r.unrealized_pnl,r.currency)],['주문 가능 현금',nativeMoney(r.balance,r.currency)],['주식 금액',nativeMoney(actual.gross_amount,r.currency)],['수수료',nativeMoney(actual.fee,r.currency)],['세금',nativeMoney(actual.tax,r.currency)],[$('side').value==='buy'?'예상 결제 금액':'예상 수령 금액',nativeMoney(actual.net_amount,r.currency)],['주문 후 잔액',nativeMoney(actual.balance_after,r.currency)]]){const row=node('div',null,'cost-row');row.append(node('span',label),node('strong',value));target.append(row);}
  target.append(node('p',`${r.currency} 결제 · 주문 비용은 확정 체결금액이 아닙니다.${actual.indicative_only?' 오래된 시세 기준입니다.':''}${maxMode?' 최대 수량은 체결 시 다시 계산합니다.':''}`,'field-help'));
}
handle('maxQuantity','click',()=>estimate(true));handle('orderPreview','click',()=>estimate(false));['quantity','side','symbol'].forEach(id=>$(id).addEventListener('input',()=>{maxMode=false;$('orderEstimate').textContent='입력값이 바뀌었습니다. 예상 금액을 다시 확인하세요.';}));
handle('watchAdd','click',async()=>{await api('watchlist',{symbol:currentSymbol});message('관심종목에 추가했습니다.');});
async function watchlist(){const rows=await api('watchlist');$('watchRows').replaceChildren();for(const r of rows){const div=node('div',null,'watch-row'),b=node('button',r.name+' · '+r.symbol,'text-button'),remove=node('button','삭제','secondary');b.addEventListener('click',()=>openStock(r.symbol));remove.addEventListener('click',async()=>{try{const response=await fetch('/api/watchlist/'+encodeURIComponent(r.symbol),{method:'DELETE',headers:{'X-CSRF-Token':csrf}});if(!response.ok)throw Error('삭제 실패');await watchlist();}catch(e){message(e.message);}});div.append(b,signed(r.quote?.change_pct,r.quote?viewMoney(r.quote.native_price,r.currency)+' · '+pct(r.quote.change_pct):r.error),remove);$('watchRows').append(div);}if(!rows.length)$('watchRows').append(node('p','아직 관심종목이 없습니다.','empty-state'));}
handle('fxPreview','click',async()=>{const r=await api('fx/preview',{source:$('fxSource').value,amount:$('fxAmount').value});$('fxEstimate').textContent=`${r.date} 기준환율 ${r.rate} · 스프레드 ${r.spread_bps} bps · 수수료 ${nativeMoney(r.fee,r.source)}\n최종 수령 ${nativeMoney(r.received,r.target)}\n예상 잔액 ${money(r.balances_after.USD)} / ${nativeMoney(r.balances_after.KRW,'KRW')}`;});
async function loadFxRate(){try{const r=await api('fx');const usd=Number(r.rate);$('fxRate').textContent=`1 USD = ${usd.toLocaleString('ko-KR',{maximumFractionDigits:2})} KRW  ·  1,000 KRW = ${(1000/usd).toLocaleString('ko-KR',{maximumFractionDigits:4})} USD  ·  ${r.date} 기준`;}catch(e){$('fxRate').textContent='환율을 불러오지 못했습니다. '+e.message;}}
window.updateFxBalance=function(){const source=$('fxSource').value,balance=window.walletBalances?.[source];$('fxAvailable').textContent=source==='USD'?`보유 달러 ${nativeMoney(balance,'USD')}`:`보유 원화 ${nativeMoney(balance,'KRW')}`;$('fxAmountUnit').textContent=source;$('fxAmount').step=source==='USD'?'0.0001':'1';$('fxAmount').min=source==='USD'?'0.0001':'1';};
$('fxSource').addEventListener('change',()=>{exchangePending=null;$('fxAmount').value='';$('fxEstimate').textContent='';updateFxBalance();});
$('fxAmount').addEventListener('input',()=>{$('fxEstimate').textContent='';});
handle('fxAll','click',()=>{$('fxAmount').value=String(window.walletBalances?.[$('fxSource').value]??'');$('fxEstimate').textContent='';});
updateFxBalance();
handle('fxForm','submit',async()=>{const data={source:$('fxSource').value,amount:$('fxAmount').value},sig=JSON.stringify(data);if(!exchangePending || exchangePending.sig!==sig)exchangePending={sig,id:uuid()};await api('fx/exchange',{...data,request_id:exchangePending.id});exchangePending=null;message('모의 환전이 완료되었습니다.');await refresh();await fxHistory();});
async function fxHistory(){const rows=await api('fx/history');table($('fxHistory'),['시각','보낸 금액','받은 금액','수수료','환율 기준일'],rows.map(r=>[new Date(r.created_at).toLocaleString(),nativeMoney(r.amount,r.source),nativeMoney(r.received,r.target),nativeMoney(r.fee,r.source),r.rate_date]));}
let adminUsers=[],adminPending=null;
async function admin(){const r=await api('admin');adminUsers=r.users;$('adminHealth').textContent='DB '+r.health.status+' · 국내 시세 '+(r.providers.kr?'설정됨':'미설정')+' · 미국 시세 '+(r.providers.us?'설정됨':'미설정');$('adminFees').textContent=Object.entries(r.fees).map(([k,v])=>k+': '+v+' bps').join(' · ');$('initialAmount').value=r.initial_usd;$('adminUsers').replaceChildren();const previous=$('adminTarget').value;$('adminTarget').replaceChildren();
 for(const u of r.users){const row=node('div',null,'watch-row'),status=node('button',u.active?'계정 정지':'계정 활성화','secondary');row.append(node('strong',`${u.username} · ${u.admin?'관리자':'일반'} · ${u.active?'활성':'정지'}`),node('span',nativeMoney(u.wallets.USD,'USD')+' / '+nativeMoney(u.wallets.KRW,'KRW')),status);status.addEventListener('click',async()=>{try{await api(`admin/users/${u.id}/active`,{active:!u.active});await admin();}catch(e){message(e.message);}});$('adminUsers').append(row);const option=node('option',u.username);option.value=u.id;$('adminTarget').append(option);}
 if(adminUsers.some(u=>String(u.id)===previous))$('adminTarget').value=previous;adminImpact();const rows=await api('admin/audit');table($('adminAudit'),['시각','운영자','대상','작업','사유'],rows.map(r=>[new Date(r.created_at).toLocaleString(),r.actor,r.target,r.action,r.reason]));}
function adminImpact(){const u=adminUsers.find(u=>String(u.id)===$('adminTarget').value),action=$('adminAction').value;$('adminGrantFields').hidden=action!=='grant';$('adminAmount').required=action==='grant';$('adminConfirm').value='';if(!u)return;const confirmation=action==='clear'?'CLEAR '+u.username:u.username;$('adminConfirm').placeholder=confirmation;$('adminImpact').textContent=action==='grant'?`${u.username}의 선택 통화 지갑에 가상 지원금을 지급합니다. 지급액은 투자 수익에서 제외하며 모든 작업을 기록합니다. 확인: ${confirmation}`:action==='rebase'?`${u.username}의 현재 총자산을 새 기준으로 삼아 누적 수익률을 0%로 시작합니다. 자산·거래내역은 보존하고 주간 비교 기준은 새로 시작합니다. 확인: ${confirmation}`:`${u.username}의 보유 종목·거래·환전·주문·관심종목·인기 활동·과거 주간 순위 참여 기록을 초기화하고 설정된 USD 초기자금을 지급합니다. 로그인 계정은 유지하며 관리자 감사/복구 기록은 보관합니다. 확인: ${confirmation}`;}
for(const id of ['adminTarget','adminAction'])$(id).addEventListener('change',adminImpact);
$('adminCurrency').addEventListener('change',()=>{$('adminAmount').step=$('adminCurrency').value==='KRW'?'1':'0.0001';});
handle('adminManageForm','submit',async()=>{const body={action:$('adminAction').value,currency:$('adminCurrency').value,amount:$('adminAction').value==='grant'?$('adminAmount').value:'0',reason:$('adminReason').value,confirmation:$('adminConfirm').value},target=$('adminTarget').value,sig=JSON.stringify({target,...body});if(!adminPending||adminPending.sig!==sig)adminPending={sig,id:uuid()};$('adminExecute').disabled=true;try{await api(`admin/users/${target}/manage`,{...body,request_id:adminPending.id});adminPending=null;$('adminResult').textContent='작업을 완료하고 감사 기록에 저장했습니다.';await admin();await refresh();}finally{$('adminExecute').disabled=false;}});
handle('initialForm','submit',async()=>{await api('admin/initial',{amount:$('initialAmount').value});message('이후 생성/초기화되는 계좌의 지급액을 저장했습니다.');});
syncCurrency();routePage();

$('orderType').addEventListener('change',()=>{const isLimit=$('orderType').value==='limit';$('limitPriceLabel').hidden=!isLimit;$('limitPrice').required=isLimit;$('submitOrder').textContent=isLimit?'모의 지정가 주문':'모의 시장가 주문';maxMode=false;});
window.loadLimits=async function(){const rows=await api('limit-orders');$('limitRows').replaceChildren();for(const r of rows){const row=node('div',null,'watch-row');row.append(node('span',`${r.symbol} · ${r.side==='buy'?'매수':'매도'} ${r.use_max?'최대':r.quantity+'주'} · ${r.order_type==='market'?'시장가 대기':'지정가 '+r.limit_price} · ${r.status}${r.reason?' · '+r.reason:''}`));if(r.status==='pending'){const b=node('button','취소','secondary');b.addEventListener('click',async()=>{try{await api(`limit-orders/${r.id}/cancel`,{});await loadLimits();}catch(e){message(e.message);}});row.append(b);}$('limitRows').append(row);}};

let transferPending=null,transferPreviewSignature=null;
function transferData(){return {recipient:$('transferRecipient').value.trim().toLowerCase(),currency:$('transferCurrency').value,amount:$('transferAmount').value};}
function updateTransferBalance(){$('transferBalance').textContent='보유 금액 '+nativeMoney(window.walletBalances?.[$('transferCurrency').value],$('transferCurrency').value);}
for(const id of ['transferRecipient','transferCurrency','transferAmount'])$(id).addEventListener('input',()=>{transferPreviewSignature=null;$('transferSubmit').disabled=true;$('transferEstimate').textContent='';$('transferAmount').step=$('transferCurrency').value==='KRW'?'1':'0.0001';updateTransferBalance();});
handle('transferPreview','click',async()=>{const body=transferData(),signature=JSON.stringify(body),r=await api('transfers/preview',body);if(JSON.stringify(transferData())!==signature)return;transferPreviewSignature=signature;$('transferEstimate').replaceChildren();for(const [label,value] of [['받는 사용자',r.recipient],['받는 금액',nativeMoney(r.received,r.currency)],['이체 수수료',nativeMoney(r.fee,r.currency)+' ('+(Number(r.fee_bps)/100)+'%)'],['총 차감 금액',nativeMoney(r.total,r.currency)],['이체 후 잔액',nativeMoney(r.balance_after,r.currency)]]){const row=node('div',null,'cost-row');row.append(node('span',label),node('strong',value));$('transferEstimate').append(row);}$('transferSubmit').disabled=Number(r.balance_after)<0;});
handle('transferForm','submit',async()=>{const body=transferData(),sig=JSON.stringify(body);if(sig!==transferPreviewSignature)throw Error('수신자와 수수료를 먼저 확인하세요.');if(!transferPending||transferPending.sig!==sig)transferPending={sig,id:uuid()};$('transferSubmit').disabled=true;try{const r=await api('transfers',{...body,request_id:transferPending.id});transferPending=null;transferPreviewSignature=null;message(r.replayed?'이미 완료된 이체입니다.':'가상자금 이체를 완료했습니다.');await refresh();updateTransferBalance();await transferHistory();}catch(e){$('transferSubmit').disabled=false;throw e;}});
async function transferHistory(){const rows=await api('transfers');table($('transferHistory'),['시각','구분','상대방','금액','수수료'],rows.map(r=>[new Date(r.created_at).toLocaleString(),r.direction==='sent'?'보냄':'받음',r.counterparty,nativeMoney(r.amount,r.currency),nativeMoney(r.fee,r.currency)]));}
