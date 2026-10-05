"""The ranking heading note shows when the ranking was computed, whatever the browser's date format."""
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
