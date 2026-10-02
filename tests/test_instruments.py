from app.instruments import market_of, currency_of, instrument


def test_market_and_currency_follow_the_kr_prefix():
    for symbol, market, currency in (('KR:005930', 'KR', 'KRW'), ('AAPL', 'US', 'USD'), ('BRK-B', 'US', 'USD'),
                                     ('KR:411060', 'KR', 'KRW')):
        assert (market_of(symbol), currency_of(symbol)) == (market, currency)
    # Symbols outside the catalog fall back to the same rule.
    assert instrument('KR:123456')['currency'] == 'KRW' and instrument('ZZZZ')['currency'] == 'USD'


def test_us_names_are_rendered_per_listing_not_for_the_whole_master(monkeypatch):
    # Regression: every master reload (each 5 minutes) rendered all ~12k names, 5.6k of
    # them ETF translations, which cost a request most of a second.
    import app.us_names
    import app.us_symbols as us
    rows = [{'symbol': f'E{n}', 'name': 'ISHARES CORE BOND', 'etf': True, 'exchange': 'NAS'} for n in range(3000)]
    monkeypatch.setattr(us, '_rows', lambda: rows)
    calls = []
    render = app.us_names.korean_name
    monkeypatch.setattr(app.us_names, 'korean_name', lambda row: calls.append(row['symbol']) or render(row))
    assert us.name_of('E7') == '아이셰어즈 코어 채권 ETF' and us.name_of('E7') == '아이셰어즈 코어 채권 ETF'
    assert calls == ['E7']                     # rendered once, and only the one asked for
    assert us.exchange_of('E7') == 'NAS' and us.exchange_of('NOPE') is None
