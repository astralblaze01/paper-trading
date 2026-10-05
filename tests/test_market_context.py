import importlib.util
from datetime import datetime, timezone
from pathlib import Path
import urllib.error
import pytest

spec = importlib.util.spec_from_file_location('market_context', Path(__file__).resolve().parents[1] / 'scripts' / 'market_context.py')
world = importlib.util.module_from_spec(spec)
spec.loader.exec_module(world)
NOW = datetime(2026, 10, 5, 8, tzinfo=timezone.utc)


def feed(stamp='Mon, 05 Oct 2026 07:00:00 GMT', url='https://example.org/report'):
    return f'<rss><channel><item><title>Market &amp; rates</title><link>{url}</link><pubDate>{stamp}</pubDate><source>Publisher</source></item></channel></rss>'.encode()


def test_evidence_keeps_provenance_and_never_promises_full_article():
    research = world.WorldResearch(loader=lambda _: feed(), now=NOW)
    result = research.news('AAPL earnings')
    item, = result['items']
    assert item['publisher'] == 'Publisher' and item['url'] == 'https://example.org/report'
    assert item['coverage'] == 'headline_only' and item['published_at'] != item['retrieved_at']
    assert research.citations({'sources': [item['id'], item['id'], 'invented']}) == [item]
    assert not research.citations({'sources': 'invented'})


@pytest.mark.parametrize('stamp', ['Sun, 20 Sep 2026 07:00:00 GMT', 'Tue, 06 Oct 2026 07:00:00 GMT', 'invalid', 'Mon, 05 Oct 2026 07:00:00'])
def test_old_future_or_undated_news_is_not_current_evidence(stamp):
    assert world.parse_feed(feed(stamp), 'source', NOW, 3) == []


def test_policy_is_dated_background_not_current_headlines():
    old = feed('Wed, 16 Sep 2026 18:00:00 GMT')
    item, = world.parse_feed(old, 'Federal Reserve', NOW, world.POLICY_DAYS, True)
    assert item['kind'] == 'official_policy' and item['published_at'].startswith('2026-09-16')


def test_failed_feed_does_not_reuse_old_or_invent_sources():
    def broken(_): raise urllib.error.URLError('failed')
    research = world.WorldResearch(loader=broken, now=NOW)
    result = research.overview(['KR', 'US'])
    assert all(v['status'] == 'unavailable' for v in result['feeds'].values())
    assert not research.evidence and not research.citations({'sources': ['guessed']})


def test_news_requests_are_encoded_cached_and_limited():
    urls = []
    research = world.WorldResearch(loader=lambda url: urls.append(url) or feed(), now=NOW)
    research.news('삼성전자 & 금리 after:2020 when:1y', 'KR')
    research.news('삼성전자 & 금리 after:2020 when:1y', 'KR')
    assert len(urls) == 1 and all(u.startswith('https://news.google.com/rss/search?') for u in urls)
    for i in range(20): research.news(str(i))
    assert len(urls) == 8


@pytest.mark.parametrize('payload', [b'<html>denied</html>', b'<!DOCTYPE x><rss/>', b'<!ENTITY x "bad"><rss/>', b'x'*2_000_001])
def test_invalid_feed_rejected(payload):
    with pytest.raises(ValueError): world.parse_feed(payload, 'source', NOW, 3)


@pytest.mark.parametrize('url', ['javascript:alert(1)', 'file:///etc/passwd', 'https://user:password@example.org/'])
def test_unsafe_article_links_are_discarded(url):
    assert not world.parse_feed(feed(url=url), 'source', NOW, 3)
