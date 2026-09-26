/* Profiles, the asset-allocation chart and account withdrawal. */
const AVATAR_DEFAULT='/static/avatar-default.svg';
const IMAGE_TYPES=['image/jpeg','image/png','image/webp'], IMAGE_MAX_BYTES=5*1024*1024;
let myProfile=null, bioEditing=false, bioDraft='';

window.avatar=function(username,version,size='large'){
  const img=document.createElement('img');img.className='avatar avatar-'+size;img.alt=size==='small'?'':username+' 프로필 사진';
  img.decoding='async';img.loading='lazy';
  img.src=version?`/api/users/${encodeURIComponent(username)}/avatar?v=${version}`:AVATAR_DEFAULT;
  img.addEventListener('error',()=>{if(!img.src.endsWith(AVATAR_DEFAULT))img.src=AVATAR_DEFAULT;},{once:true});
  return img;
};

// Tiers come with the ranking rows (server: app/tiers.py); emblems live in /static/tiers/.
const TIER_LABELS={master:'마스터',diamond:'다이아몬드',platinum:'플래티넘',gold:'골드',silver:'실버',bronze:'브론즈'};
// Tier icons (static/tiers/icons/*.svg): a cut gem in the tier's color with its letter, solved.ac style.
window.tierIcon=function(tier,size='small'){
  const img=document.createElement('img');img.className='tier-icon tier-icon-'+size;img.src=`/static/tiers/icons/${tier}.svg`;
  img.alt=TIER_LABELS[tier]+' 티어';img.title=TIER_LABELS[tier]+' 티어';img.decoding='async';return img;
};
// The profile photo inside a ring of the tier's color, the tier icon on its corner.
function tierFrame(photo,tier){
  const f=document.createElement('div');f.className='tier-ring tier-ring-'+tier;f.append(photo,tierIcon(tier,'corner'));return f;
}
// Daily place change: ▲n green when up, ▼n red when down, the number in plain ink.
window.rankChange=function(rank,previous){
  const box=document.createElement('span');box.className='rank-change';
  if(previous==null||rank==null)return box;
  const n=previous-rank;
  if(!n){box.classList.add('same');box.textContent='–';box.title='어제와 같은 순위';return box;}
  const arrow=document.createElement('span');arrow.className='rank-arrow '+(n>0?'up':'down');arrow.textContent=n>0?'▲':'▼';
  box.append(arrow,document.createTextNode(String(Math.abs(n))));
  box.title=`오늘 아침 ${previous}위 → 지금 ${rank}위`;box.setAttribute('aria-label',`${Math.abs(n)}계단 ${n>0?'상승':'하락'}`);
  return box;
};
// Realized P&L (sells since the return baseline) summed in the displayed basis.
function realizedTotal(p){
  const r=p.realized_pnl,rate=Number(p.fx?.rate);if(!r||!Number.isFinite(rate)||rate<=0)return null;
  return returnBasis()==='USD'?Number(r.USD)+Number(r.KRW)/rate:Number(r.KRW)+Number(r.USD)*rate;
}
function profileStat(label,value){const d=node('div');d.append(node('dt',label));const v=node('dd');v.append(value instanceof Node?value:document.createTextNode(value));d.append(v);return d;}

// Shared by the own portfolio (editable) and other users' profiles (read-only).
window.renderProfileCard=function(target,p,editable){
  target.replaceChildren();
  const media=node('div',null,'profile-media'),photo=avatar(p.username,p.image_version,'large');
  media.append(p.tier?tierFrame(photo,p.tier):photo);
  if(editable){
    const tools=node('div',null,'profile-photo-tools');
    const pick=node('label','사진 변경','button-link secondary');pick.htmlFor='profileImageInput';
    tools.append(pick);
    if(p.image_version){const remove=node('button','사진 삭제','text-button');remove.type='button';remove.addEventListener('click',()=>{$('photoDeleteConfirm').disabled=false;$('photoDeleteDialog').showModal();$('photoDeleteCancel').focus();});tools.append(remove);}
    media.append(tools);
  }
  const info=node('div',null,'profile-info');info.append(node('h2',p.username,'profile-name'));
  if(editable&&bioEditing){
    const form=node('form',null,'bio-form'),area=node('textarea');area.id='bioInput';area.maxLength=myProfile?.bio_max_length||160;area.rows=3;area.value=bioDraft;area.setAttribute('aria-label','한 줄 소개');area.placeholder='예: 미국 성장주와 ETF 위주로 투자합니다.';
    const count=node('span',`${area.value.length}/${area.maxLength}`,'field-help bio-count');
    area.addEventListener('input',()=>{bioDraft=area.value;count.textContent=`${area.value.length}/${area.maxLength}`;});
    const actions=node('div',null,'bio-actions'),cancel=node('button','취소','secondary'),save=node('button','소개 저장');cancel.type='button';
    cancel.addEventListener('click',()=>{bioEditing=false;renderMyProfile();});
    form.addEventListener('submit',async e=>{e.preventDefault();save.disabled=true;try{myProfile={...myProfile,...await api('profile',{bio:area.value})};bioEditing=false;toast('소개를 저장했습니다.','success');renderMyProfile();}catch(err){toast(err.message,'error');save.disabled=false;}});
    actions.append(count,cancel,save);form.append(area,actions);info.append(form);
    setTimeout(()=>area.focus({preventScroll:true}));
  }else{
    info.append(node('p',p.bio||(editable?'아직 소개가 없습니다. 나를 소개하는 한 줄을 남겨보세요.':'아직 소개가 없습니다.'),'profile-bio'+(p.bio?'':' empty')));
    if(editable){const edit=node('button','소개 수정','text-button');edit.type='button';edit.addEventListener('click',()=>{bioEditing=true;bioDraft=myProfile?.bio||'';renderMyProfile();});info.append(edit);}
  }
  const stats=node('dl',null,'profile-stats');
  if(p.tier){const v=node('span',null,'tier-value');v.append(tierIcon(p.tier,'medium'),node('span',TIER_LABELS[p.tier],'tier-name tier-text-'+p.tier));const t=profileStat('티어',v);t.classList.add('profile-tier');stats.append(t);}
  const rank=node('span');rank.append(p.rank?`${p.rank}위`:'—',rankChange(p.rank,p.previous_rank));
  const realized=realizedTotal(p);
  stats.append(profileStat('랭킹',rank),profileStat(`평가 수익률 (${basisLabel()})`,signedPct(accountReturn(p))),
    profileStat('실현 손익 (매도 확정)',realized==null?'—':signed(realized,(realized>0?'+':'')+nativeMoney(realized,returnBasis()))),
    profileStat('총 평가금액',viewMoney(p.equity_usd,'USD')));
  // Days since sign-up in Korea time, the sign-up day counting as day 1 (own and public profiles).
  if(p.member_days){const since=profileStat('가입 기간',`${p.member_days.toLocaleString()}일`);since.classList.add('member-days');if(p.member_since)since.title=new Date(p.member_since).toLocaleDateString('ko-KR',{timeZone:'Asia/Seoul'})+' 가입';stats.append(since);}
  info.append(stats);
  target.append(media,info);
};

window.loadMyProfile=async function(){myProfile=await api('profile');renderMyProfile();};
window.renderMyProfile=function(){
  const target=$('myProfile');if(!target||!myProfile||myProfile.username!==window.sessionUsername)return;
  // Keep an open bio editor untouched by periodic refreshes.
  if(bioEditing&&target.querySelector('#bioInput'))return;
  const p=portfolioCache||{};
  renderProfileCard(target,{username:window.sessionUsername,bio:myProfile.bio,image_version:myProfile.image_version,equity_usd:p.equity_usd,return_pct:p.return_pct,return_pct_usd:p.return_pct_usd,realized_pnl:p.realized_pnl,fx:p.fx,...(typeof rankRow==='function'?rankRow(window.sessionUsername):{}),member_days:myProfile.member_days,member_since:myProfile.member_since},true);
};

async function uploadProfileImage(file){
  if(!IMAGE_TYPES.includes(file.type))throw Error('JPG, PNG, WEBP 이미지만 업로드할 수 있습니다.');
  if(file.size>IMAGE_MAX_BYTES)throw Error('이미지는 최대 5MB까지 업로드할 수 있습니다.');
  const response=await fetch('/api/profile/image',{method:'POST',headers:{'Content-Type':file.type,'X-CSRF-Token':csrf},body:file});
  let data;try{data=await response.json();}catch{throw Error(response.status===413?'이미지는 최대 5MB까지 업로드할 수 있습니다.':`업로드하지 못했습니다 (${response.status}).`);}
  if(!response.ok)throw Error(typeof data.detail==='string'?data.detail:'업로드하지 못했습니다.');
  myProfile={...myProfile,...data};renderMyProfile();if(rankingCache)renderRanking();toast('프로필 사진을 변경했습니다.','success');
}
// Deleting the photo asks once; cancel (button or Esc) keeps the current photo.
$('photoDeleteCancel').addEventListener('click',()=>$('photoDeleteDialog').close());
$('photoDeleteForm').addEventListener('submit',async e=>{e.preventDefault();$('photoDeleteConfirm').disabled=true;try{await deleteProfileImage();$('photoDeleteDialog').close();}catch(err){toast(err.message,'error');$('photoDeleteConfirm').disabled=false;}});
async function deleteProfileImage(){myProfile={...myProfile,...await api('profile/image/delete',{})};renderMyProfile();if(rankingCache)renderRanking();toast('프로필 사진을 삭제했습니다.','success');}
(function(){const input=document.createElement('input');input.type='file';input.id='profileImageInput';input.accept=IMAGE_TYPES.join(',');input.hidden=true;document.body.append(input);
  input.addEventListener('change',async()=>{const file=input.files[0];input.value='';if(file)try{await openCropper(file);}catch(e){toast(e.message,'error');}});})();

/* Area selection before upload. The chosen square is drawn to a 512px canvas
   and uploaded as JPEG; the server still decodes, checks and re-encodes it.
   createImageBitmap avoids blob: URLs, which the page's CSP does not allow. */
const CROP_STAGE=300, CROP_OUTPUT=512, CROP_MAX_ZOOM=4;
let crop=null;
async function openCropper(file){
  if(!IMAGE_TYPES.includes(file.type))throw Error('JPG, PNG, WEBP 이미지만 업로드할 수 있습니다.');
  if(file.size>IMAGE_MAX_BYTES)throw Error('이미지는 최대 5MB까지 업로드할 수 있습니다.');
  let image;
  try{image=await createImageBitmap(file);}catch{throw Error('올바른 이미지 파일이 아닙니다.');}
  if(crop?.image)crop.image.close();
  // Zoom 1 fills the frame; zooming out goes down to half of "whole photo visible".
  const fit=Math.min(image.width,image.height)/Math.max(image.width,image.height);
  crop={image,zoom:1,minZoom:fit*.5,x:0,y:0,pointers:new Map(),pinch:null};
  const canvas=$('cropCanvas'),dpr=window.devicePixelRatio||1;canvas.width=CROP_STAGE*dpr;canvas.height=CROP_STAGE*dpr;
  $('cropZoom').min=String(crop.minZoom);$('cropZoom').value='1';$('cropApply').disabled=false;
  $('cropDialog').showModal();drawCrop();canvas.focus();
}
// Image scale that covers the square stage at zoom 1.
function cropScale(){return CROP_STAGE/Math.min(crop.image.width,crop.image.height)*crop.zoom;}
// A photo larger than the frame must keep covering it; a smaller one must stay inside it.
function clampCrop(){const s=cropScale(),mx=Math.abs(crop.image.width*s-CROP_STAGE)/2,my=Math.abs(crop.image.height*s-CROP_STAGE)/2;crop.x=Math.min(mx,Math.max(-mx,crop.x));crop.y=Math.min(my,Math.max(-my,crop.y));}
function paintCrop(ctx,size){
  const k=size/CROP_STAGE,s=cropScale()*k,w=crop.image.width*s,h=crop.image.height*s;
  ctx.fillStyle='#fff';ctx.fillRect(0,0,size,size);
  ctx.imageSmoothingQuality='high';
  ctx.drawImage(crop.image,size/2+crop.x*k-w/2,size/2+crop.y*k-h/2,w,h);
}
function drawCrop(){
  if(!crop)return;clampCrop();
  const canvas=$('cropCanvas'),ctx=canvas.getContext('2d'),size=canvas.width;
  paintCrop(ctx,size);
  // Shade outside the circle the avatar is shown in.
  ctx.fillStyle='rgba(20,30,40,.55)';ctx.beginPath();ctx.rect(0,0,size,size);ctx.arc(size/2,size/2,size/2-1,0,Math.PI*2,true);ctx.fill();
  ctx.strokeStyle='rgba(255,255,255,.9)';ctx.lineWidth=2*(window.devicePixelRatio||1);ctx.beginPath();ctx.arc(size/2,size/2,size/2-2,0,Math.PI*2);ctx.stroke();
}
function setCropZoom(zoom){
  const next=Math.min(CROP_MAX_ZOOM,Math.max(crop.minZoom,zoom)),ratio=next/crop.zoom;
  crop.x*=ratio;crop.y*=ratio;crop.zoom=next;$('cropZoom').value=String(next);drawCrop();
}
function stageUnits(){return CROP_STAGE/$('cropCanvas').getBoundingClientRect().width;}
$('cropZoom').addEventListener('input',()=>{if(crop)setCropZoom(Number($('cropZoom').value));});
$('cropZoomOut').addEventListener('click',()=>{if(crop)setCropZoom(crop.zoom/1.15);});
$('cropZoomIn').addEventListener('click',()=>{if(crop)setCropZoom(crop.zoom*1.15);});
$('cropCanvas').addEventListener('wheel',e=>{if(!crop)return;e.preventDefault();setCropZoom(crop.zoom*(e.deltaY<0?1.08:1/1.08));},{passive:false});
$('cropCanvas').addEventListener('pointerdown',e=>{if(!crop)return;e.currentTarget.setPointerCapture(e.pointerId);crop.pointers.set(e.pointerId,{x:e.clientX,y:e.clientY});e.currentTarget.classList.add('dragging');});
$('cropCanvas').addEventListener('pointermove',e=>{
  if(!crop||!crop.pointers.has(e.pointerId))return;
  const last=crop.pointers.get(e.pointerId),unit=stageUnits();crop.pointers.set(e.pointerId,{x:e.clientX,y:e.clientY});
  if(crop.pointers.size>=2){const [a,b]=[...crop.pointers.values()],distance=Math.hypot(a.x-b.x,a.y-b.y);if(crop.pinch)setCropZoom(crop.zoom*distance/crop.pinch);crop.pinch=distance;return;}
  crop.x+=(e.clientX-last.x)*unit;crop.y+=(e.clientY-last.y)*unit;drawCrop();
});
for(const type of ['pointerup','pointercancel'])$('cropCanvas').addEventListener(type,e=>{if(!crop)return;crop.pointers.delete(e.pointerId);if(crop.pointers.size<2)crop.pinch=null;if(!crop.pointers.size)e.currentTarget.classList.remove('dragging');});
$('cropCanvas').addEventListener('keydown',e=>{
  if(!crop)return;const moves={ArrowLeft:[-10,0],ArrowRight:[10,0],ArrowUp:[0,-10],ArrowDown:[0,10]};
  if(moves[e.key]){e.preventDefault();crop.x+=moves[e.key][0];crop.y+=moves[e.key][1];drawCrop();}
  else if(e.key==='+'||e.key==='='){e.preventDefault();setCropZoom(crop.zoom*1.1);}
  else if(e.key==='-'){e.preventDefault();setCropZoom(crop.zoom/1.1);}
});
function closeCropper(){$('cropDialog').close();}
$('cropDialog').addEventListener('close',()=>{if(crop?.image)crop.image.close();crop=null;});
$('cropCancel').addEventListener('click',closeCropper);
$('cropForm').addEventListener('submit',async e=>{
  e.preventDefault();if(!crop)return;$('cropApply').disabled=true;
  try{
    const out=document.createElement('canvas');out.width=out.height=CROP_OUTPUT;paintCrop(out.getContext('2d'),CROP_OUTPUT);
    const blob=await new Promise(resolve=>out.toBlob(resolve,'image/jpeg',.92));
    if(!blob)throw Error('사진을 처리하지 못했습니다.');
    await uploadProfileImage(blob);closeCropper();
  }catch(err){toast(err.message,'error');$('cropApply').disabled=false;}
});

/* Asset allocation donut: every holding by value, plus cash.
   Holdings take the validated categorical slots in fixed order (seven, with
   cash as the eighth); any further holdings share a neutral tone. The legend
   lists every holding with its share and value, so identity never relies on
   color alone and no holding is ever folded away. */
const SLOT_COLORS=['#2a78d6','#eb6834','#1baf7a','#eda100','#e87ba4','#4a3aa7','#e34948'], EXTRA_COLOR='#8b949d', CASH_COLOR='#008300', SVG_NS='http://www.w3.org/2000/svg';
function svg(tag,attrs){const n=document.createElementNS(SVG_NS,tag);for(const [k,v] of Object.entries(attrs))n.setAttribute(k,v);return n;}
window.renderAllocation=function(target,p){
  if(!target)return;target.replaceChildren();
  const rate=Number(p?.fx?.rate);
  if(!p||!Number.isFinite(rate)||rate<=0){target.append(node('p','기준환율을 확인한 뒤 자산 비중을 표시합니다.','empty-state'));return;}
  const toKRW=(v,c)=>c==='KRW'?Number(v):Number(v)*rate;
  const priced=p.positions.filter(x=>x.value!=null).map(x=>({label:x.name,note:x.currency==='KRW'?'원화':'달러',krw:toKRW(x.value,x.currency),value:viewMoney(x.value,x.currency)})).sort((a,b)=>b.krw-a.krw);
  const pending=p.positions.filter(x=>x.value==null);
  const slices=priced.map((x,i)=>({...x,color:SLOT_COLORS[i]||EXTRA_COLOR}));
  const cashKRW=toKRW(p.wallets.USD,'USD')+Number(p.wallets.KRW);
  if(cashKRW>0)slices.push({label:'현금',note:'USD·KRW 지갑',krw:cashKRW,value:`USD ${nativeMoney(p.wallets.USD,'USD')} · KRW ${nativeMoney(p.wallets.KRW,'KRW')}`,color:CASH_COLOR});
  const total=slices.reduce((a,x)=>a+x.krw,0);
  if(total<=0&&!pending.length){target.append(node('p','평가할 자산이 없습니다.','empty-state'));return;}
  if(total<=0){target.append(node('p','시세를 준비 중입니다. 잠시 후 자동으로 다시 확인합니다.','empty-state'));return;}
  for(const s of slices)s.share=s.krw/total*100;
  const size=220,r=82,width=30,C=2*Math.PI*r,gap=slices.length>1?2:0;
  const chart=node('div',null,'allocation-chart'),figure=svg('svg',{viewBox:`0 0 ${size} ${size}`,role:'img','aria-label':'자산 비중: '+slices.map(s=>`${s.label} ${s.share.toFixed(1)}%`).join(', ')});
  figure.append(svg('circle',{cx:size/2,cy:size/2,r,fill:'none',stroke:'#eef1f4','stroke-width':width}));
  const tip=node('div',null,'allocation-tip');tip.hidden=true;
  const segments=[];let offset=0;
  for(const s of slices){
    // Small holdings keep a visible 3px sliver instead of disappearing.
    const len=s.share/100*C,visible=Math.min(len,Math.max(len-gap,3));
    const seg=svg('circle',{cx:size/2,cy:size/2,r,fill:'none',stroke:s.color,'stroke-width':width,'stroke-dasharray':`${visible} ${C-visible}`,'stroke-dashoffset':String(-offset),transform:`rotate(-90 ${size/2} ${size/2})`,class:'allocation-segment',tabindex:'0'});
    seg.append(svg('title',{}));seg.firstChild.textContent=`${s.label} ${s.share.toFixed(1)}%`;
    const show=e=>{tip.replaceChildren(node('strong',s.label),node('span',`${s.share.toFixed(1)}%`),node('span',s.value));tip.hidden=false;const box=chart.getBoundingClientRect(),x=(e?.clientX??box.left+box.width/2)-box.left,y=(e?.clientY??box.top+box.height/2)-box.top;tip.style.left=Math.min(Math.max(x,70),box.width-70)+'px';tip.style.top=Math.max(y-12,0)+'px';highlight(s);};
    seg.addEventListener('pointermove',show);seg.addEventListener('focus',()=>show());
    seg.addEventListener('pointerleave',()=>{tip.hidden=true;highlight(null);});seg.addEventListener('blur',()=>{tip.hidden=true;highlight(null);});
    segments.push([s,seg]);figure.append(seg);offset+=len;
  }
  const center=svg('text',{x:size/2,y:size/2-6,'text-anchor':'middle',class:'allocation-total-label'});center.textContent='총 자산';
  const value=svg('text',{x:size/2,y:size/2+18,'text-anchor':'middle',class:'allocation-total'});value.textContent=viewMoney(total,'KRW');
  figure.append(center,value);chart.append(figure,tip);
  const legend=node('ul',null,'allocation-legend'),rows=[];
  for(const s of slices){
    const li=node('li'),swatch=node('span',null,'swatch');swatch.style.background=s.color;
    const name=node('span',null,'legend-name');name.append(node('strong',s.label));if(s.note)name.append(node('small',s.note));
    li.append(swatch,name,node('span',`${s.share.toFixed(1)}%`,'legend-share'),node('span',s.value,'legend-value'));
    li.addEventListener('pointerenter',()=>highlight(s));li.addEventListener('pointerleave',()=>highlight(null));
    rows.push([s,li]);legend.append(li);
  }
  for(const x of pending){
    const li=node('li','','pending'),swatch=node('span',null,'swatch');
    const name=node('span',null,'legend-name');name.append(node('strong',x.name),node('small',x.currency==='KRW'?'원화':'달러'));
    li.append(swatch,name,node('span','—','legend-share'),node('span','시세 준비 중 · 잠시 후 자동으로 반영됩니다','legend-value'));legend.append(li);
  }
  function highlight(active){for(const [s,seg] of segments)seg.classList.toggle('dimmed',!!active&&s!==active);for(const [s,li] of rows)li.classList.toggle('active',s===active);}
  target.append(chart,legend);
  if(pending.length)target.append(node('p',`시세를 준비 중인 ${pending.length}개 종목은 가격이 확인되면 비중에 반영됩니다.`,'field-help allocation-note'));
  else if(!p.positions.length)target.append(node('p','보유 종목이 없어 자산이 모두 현금입니다.','field-help allocation-note'));
};
window.addEventListener('displaycurrencychange',()=>{if(portfolioCache)renderAllocation($('allocation'),portfolioCache);if(publicCache)renderAllocation($('publicAllocation'),publicCache);});

/* Self-service withdrawal: one confirmation dialog with the current password. */
$('withdrawOpen').addEventListener('click',()=>{$('withdrawForm').reset();$('withdrawError').textContent='';$('withdrawDialog').showModal();$('withdrawPassword').focus();});
$('withdrawCancel').addEventListener('click',()=>$('withdrawDialog').close());
$('withdrawForm').addEventListener('submit',async e=>{
  e.preventDefault();const password=$('withdrawPassword').value;
  if(!password){$('withdrawError').textContent='비밀번호를 입력하세요.';return;}
  $('withdrawConfirm').disabled=true;
  try{
    await api('account/delete',{password});
    try{localStorage.removeItem(storageNamespace+':recent:'+window.sessionUsername);}catch{}
    $('withdrawDialog').close();myProfile=null;portfolioCache=null;rankingCache=null;
    location.hash='';
    toast('회원 탈퇴가 완료되었습니다. 이용해 주셔서 감사합니다.','success',6000);
    await boot();
  }catch(err){$('withdrawError').textContent=err.message;}finally{$('withdrawConfirm').disabled=false;}
});
