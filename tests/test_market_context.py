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
    research = world.WorldResearch(loader=lambda _: feed(), article_loader=lambda _: {'url': 'https://example.org/report', 'body': 'A sufficiently long article body with verified paragraphs and market context. ' * 5}, now=NOW)
    result = research.news('AAPL earnings')
    item, = result['items']
    assert item['publisher'] == 'Publisher' and item['url'] == 'https://example.org/report'
    assert item['coverage'] == 'article_body' and item['published_at'] != item['retrieved_at']
    assert 'verified paragraphs' in item['body']
    assert research.citations({'sources': [item['id'], item['id'], 'invented']}) == [item]
    assert not research.citations({'sources': 'invented'})


@pytest.mark.parametrize('stamp', ['Sun, 20 Sep 2026 07:00:00 GMT', 'Tue, 06 Oct 2026 07:00:00 GMT', 'invalid', 'Mon, 05 Oct 2026 07:00:00'])
def test_old_future_or_undated_news_is_not_current_evidence(stamp):
    assert world.parse_feed(feed(stamp), 'source', NOW, 3) == []


def test_policy_is_dated_background_not_current_headlines():
    old = feed('Wed, 16 Sep 2026 18:00:00 GMT')
    item, = world.parse_feed(old, 'Federal Reserve', NOW, world.POLICY_DAYS, True)
    assert item['kind'] == 'official_policy' and item['published_at'].startswith('2026-09-16')


def test_memory_survives_restart_and_is_not_current_order_evidence(tmp_path):
    first = world.WorldResearch(loader=lambda _: feed(), article_loader=lambda _: {'url': 'https://example.org/report', 'body': 'A long saved article body. ' * 20}, now=NOW, storage_path=tmp_path/'world.jsonl')
    item, = first.news('AAPL').get('items')
    assert item['coverage'] == 'article_body'
    second = world.WorldResearch(loader=lambda _: b'<rss/>', now=NOW + world.timedelta(hours=1), storage_path=tmp_path/'world.jsonl')
    assert second.memory_for('market') and second.memory_for('market')[0]['id'] == item['id']
    assert second.citations({'sources': [item['id']]}) == []


def test_article_failure_is_explicit_and_keeps_feed_evidence():
    research = world.WorldResearch(loader=lambda _: feed(), article_loader=lambda _: (_ for _ in ()).throw(OSError('blocked')), now=NOW)
    item, = research.news('AAPL')['items']
    assert item['coverage'] == 'headline_only' and item['article_error'] == 'OSError'


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


def test_articles_are_read_in_parallel_within_a_budget(monkeypatch):
    monkeypatch.setattr(world, 'FEED_BUDGET', 1)
    many = ('<rss><channel>' + ''.join(f'<item><title>Story {i}</title><link>https://example.org/{i}</link><pubDate>Mon, 05 Oct 2026 07:0{i}:00 GMT</pubDate></item>' for i in range(6)) + '</channel></rss>').encode()
    def article(url):
        if url.endswith('/5'): world.time.sleep(3)   # one slow site
        else: world.time.sleep(0.5)
        return {'url': url, 'body': 'Readable body text for the market. ' * 10}
    research = world.WorldResearch(loader=lambda _: many, article_loader=article, now=NOW)
    started = world.time.monotonic()
    items = research.feed('https://example.org/feed', 'Pub')['items']
    # Five half-second reads one after another would take 2.5 s; in parallel they fit the 1 s budget.
    assert world.time.monotonic() - started < 2
    coverage = {i['title']: i['coverage'] for i in items}
    assert coverage['Story 5'] == 'headline_only' and sum(c == 'article_body' for c in coverage.values()) == 5
    assert next(i for i in items if i['title'] == 'Story 5')['article_error'] == 'TimeoutError'


def test_google_news_links_are_resolved_to_the_publisher_before_reading(monkeypatch):
    seen = []
    monkeypatch.setattr(world, 'resolve_google_news', lambda url: seen.append(url) or 'https://publisher.example/story')
    monkeypatch.setattr(world, 'download_article', lambda url: {'url': url, 'body': 'body ' * 50})
    assert world.read_article('https://news.google.com/rss/articles/CBMiabc')['url'] == 'https://publisher.example/story'
    assert seen == ['https://news.google.com/rss/articles/CBMiabc']
    assert world.read_article('https://direct.example/a')['url'] == 'https://direct.example/a' and len(seen) == 1


@pytest.mark.parametrize('url', ['https://evil.example/rss/articles/CBMiabcdefghijklmnopqrstuv', 'http://news.google.com/rss/articles/CBMiabcdefghijklmnopqrstuv',
                                 'https://news.google.com/rss/articles/bad id'])
def test_only_genuine_google_news_article_links_are_resolved(url):
    with pytest.raises(ValueError): world.resolve_google_news(url)


def test_the_overview_reads_publisher_feeds_that_link_to_articles():
    urls = []
    research = world.WorldResearch(loader=lambda url: urls.append(url) or feed(), article_loader=lambda u: {'url': u, 'body': 'x ' * 100}, now=NOW)
    result = research.overview(['KR', 'US'])
    publishers = {url for feeds in world.PUBLISHER_FEEDS.values() for _, url in feeds}
    assert publishers <= set(urls) and not any(u.startswith('https://news.google.com') for u in urls)
    assert all(world.urllib.parse.urlsplit(u).hostname in world.HOSTS for u in urls)
    assert all(len(v['items']) <= world.FEED_ITEMS for v in result['feeds'].values())


@pytest.mark.parametrize('stamp,naive_tz,expected', [
    ('Mon, 05 Oct 2026 16:56:56 +09:00', None, '2026-10-05T07:56:56+00:00'),   # 매일경제
    ('2026-10-05 16:09:44', world.KST, '2026-10-05T07:09:44+00:00'),           # 연합인포맥스: local time, no zone
    ('2026-10-05 16:09:44', None, None),                                       # no zone and none known: not evidence
    ('Mon, 05 Oct 2026 07:00:00 GMT', None, '2026-10-05T07:00:00+00:00')])
def test_publisher_date_formats(stamp, naive_tz, expected):
    got = world.feed_time(stamp, naive_tz)
    assert (got.isoformat() if got else None) == expected
