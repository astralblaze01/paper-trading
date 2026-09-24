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

function profileStat(label,value){const d=node('div');d.append(node('dt',label));const v=node('dd');v.append(value instanceof Node?value:document.createTextNode(value));d.append(v);return d;}

// Shared by the own portfolio (editable) and other users' profiles (read-only).
window.renderProfileCard=function(target,p,editable){
  target.replaceChildren();
  const media=node('div',null,'profile-media');media.append(avatar(p.username,p.image_version,'large'));
  if(editable){
    const tools=node('div',null,'profile-photo-tools');
    const pick=node('label','사진 변경','button-link secondary');pick.htmlFor='profileImageInput';
    tools.append(pick);
    if(p.image_version){const remove=node('button','사진 삭제','text-button');remove.type='button';remove.addEventListener('click',()=>deleteProfileImage().catch(e=>toast(e.message,'error')));tools.append(remove);}
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
  stats.append(profileStat('랭킹',p.rank?`${p.rank}위`:'—'),profileStat('누적 수익률',signedPct(p.return_pct)),profileStat('총 평가금액',viewMoney(p.equity_usd,'USD')));
  info.append(stats);
  target.append(media,info);
};

window.loadMyProfile=async function(){myProfile=await api('profile');renderMyProfile();};
window.renderMyProfile=function(){
  const target=$('myProfile');if(!target||!myProfile||myProfile.username!==window.sessionUsername)return;
  // Keep an open bio editor untouched by periodic refreshes.
  if(bioEditing&&target.querySelector('#bioInput'))return;
  const p=portfolioCache||{};
  renderProfileCard(target,{username:window.sessionUsername,bio:myProfile.bio,image_version:myProfile.image_version,equity_usd:p.equity_usd,return_pct:p.return_pct,rank:typeof rankOf==='function'?rankOf(window.sessionUsername):null},true);
};

async function uploadProfileImage(file){
  if(!IMAGE_TYPES.includes(file.type))throw Error('JPG, PNG, WEBP 이미지만 업로드할 수 있습니다.');
  if(file.size>IMAGE_MAX_BYTES)throw Error('이미지는 최대 5MB까지 업로드할 수 있습니다.');
  const response=await fetch('/api/profile/image',{method:'POST',headers:{'Content-Type':file.type,'X-CSRF-Token':csrf},body:file});
  let data;try{data=await response.json();}catch{throw Error(response.status===413?'이미지는 최대 5MB까지 업로드할 수 있습니다.':`업로드하지 못했습니다 (${response.status}).`);}
  if(!response.ok)throw Error(typeof data.detail==='string'?data.detail:'업로드하지 못했습니다.');
  myProfile={...myProfile,...data};renderMyProfile();if(rankingCache)renderRanking();toast('프로필 사진을 변경했습니다.','success');
}
async function deleteProfileImage(){myProfile={...myProfile,...await api('profile/image/delete',{})};renderMyProfile();if(rankingCache)renderRanking();toast('프로필 사진을 삭제했습니다.','success');}
(function(){const input=document.createElement('input');input.type='file';input.id='profileImageInput';input.accept=IMAGE_TYPES.join(',');input.hidden=true;document.body.append(input);
  input.addEventListener('change',async()=>{const file=input.files[0];input.value='';if(file)try{await uploadProfileImage(file);}catch(e){toast(e.message,'error');}});})();

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
