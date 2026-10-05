"""Text the pages build in the browser, checked with node: it must read the same in every browser."""
import shutil
import subprocess

import pytest


@pytest.mark.skipif(not shutil.which('node'), reason='node is not installed')
def test_ranking_note_shows_the_whole_update_time():
    """ko-KR with hour12:false formats as '2026. 10. 5. 22시 50분 40초' in current
    browsers; the note took the last word of that and showed only '40초'."""
    source = open('app/static/app.js').read()
    start = source.index('window.rankingStatusText=')
    body = source[start:source.index('};', start) + 2]
    script = 'const window={};' + body + '''
const r={updated_at:'2026-10-05T13:50:40Z',next_refresh_at:'2026-10-05T13:50:50Z',market_open:true};
const check=(got,want)=>{if(got!==want)throw new Error(JSON.stringify(got)+' !== '+JSON.stringify(want));};
check(window.rankingStatusText(r)[0],'USD 환산 · 10초 단위 · 22:50:40 기준');
// Another engine's date text must not change it.
Date.prototype.toLocaleString=function(){return '2026. 10. 5. 오후 10:50:40';};
check(window.rankingStatusText(r)[0],'USD 환산 · 10초 단위 · 22:50:40 기준');
check(window.rankingStatusText({updated_at:'2026-10-05T03:05:09Z',market_open:true})[0],'USD 환산 · 10초 단위 · 12:05:09 기준');'''
    subprocess.run(['node', '-e', script], check=True)


@pytest.mark.skipif(not shutil.which('node'), reason='node is not installed')
def test_krw_profit_is_split_into_investing_and_the_fx_move():
    """Every account starts with dollars, so the KRW-basis profit also moves with USD/KRW."""
    source = open('app/static/app.js').read()
    helpers = source[source.index('const usdFormat'):source.index('const money =')]
    start = source.index('window.fxSplitText=')
    body = source[start:source.index('\n};', start) + 3]
    script = 'const window={};' + helpers + body + '''
const check=(got,want)=>{if(JSON.stringify(got)!==JSON.stringify(want))throw new Error(JSON.stringify(got)+' !== '+JSON.stringify(want));};
// Signed up at 1,368.60, now 1,348.28: investing made 3,867,044원, the rate took 2,032,000원.
const p={pnl:'1835044',other_pnl:'3867044',initial_fx_effect:'-2032000',initial_usd:'100000',initial_equity:'136860000',
         initial_fx_date:'2026-09-24',fx:{rate:'1348.28'}};
const t=window.fxSplitText(p);
check([t.invest,t.fx],[['투자 손익','+3,867,044원'],['환율 영향','-2,032,000원']]);
check(t.title,'환율 영향: 시작 자금 $100,000.00을 처음 환율 1,368.60원(2026-09-24)과 지금 기준환율 1,348.28원으로 원화 환산한 차이');
check(window.fxSplitText({...p,initial_fx_effect:null}),null);
check(window.fxSplitText({...p,initial_fx_effect:'0.3',other_pnl:'1835043.7'}),null);  // joined at today's rate'''
    subprocess.run(['node', '-e', script], check=True)
