from app.instruments import market_of, currency_of, instrument


def test_market_and_currency_follow_the_kr_prefix():
    for symbol, market, currency in (('KR:005930', 'KR', 'KRW'), ('AAPL', 'US', 'USD'), ('BRK-B', 'US', 'USD'),
                                     ('KR:411060', 'KR', 'KRW')):
        assert (market_of(symbol), currency_of(symbol)) == (market, currency)
    # Symbols outside the catalog fall back to the same rule.
    assert instrument('KR:123456')['currency'] == 'KRW' and instrument('ZZZZ')['currency'] == 'USD'
