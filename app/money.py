"""All settlement arithmetic uses Decimal. No provider calls inside pure calculations."""
import os
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN
from fastapi import HTTPException
from .db import Wallet, Settings

D = Decimal

def bps(name, default='0'):
    value = D(os.getenv(name, default))
    if not value.is_finite() or not 0 <= value < 10000: raise ValueError('Invalid basis-point setting: ' + name)
    return value

def unit(currency): return D('1') if currency == 'KRW' else D('.0001')
def rounded(value, currency, up=False): return value.quantize(unit(currency), rounding=ROUND_CEILING if up else ROUND_DOWN)
def initial_amount(db):
    setting = db.get(Settings, 'INITIAL_USD')
    amount = D(setting.value if setting else os.getenv('INITIAL_USD','100000'))
    if not amount.is_finite() or not 1 <= amount <= 1000000000: raise ValueError('Invalid INITIAL_USD')
    return amount

def wallets(db, user):
    result = {}
    for currency in ('USD','KRW'):
        wallet = db.get(Wallet, (user.id,currency))
        if wallet is None:
            wallet = Wallet(user_id=user.id,currency=currency,balance=user.cash if currency=='USD' else D(0))
            db.add(wallet)
        result[currency] = wallet
    return result

def native_cost_basis(position):
    # Positions from before native-currency accounting only carry the USD-equivalent average.
    return position.native_average_cost if position.native_average_cost is not None else position.average_cost

def costs(symbol, side, price, quantity):
    currency = 'KRW' if symbol.startswith('KR:') else 'USD'
    prefix = 'KR' if currency == 'KRW' else 'US'
    fee_bps = bps(f'{prefix}_{side.upper()}_FEE_BPS')
    tax_bps = bps('KR_SELL_TAX_BPS') if currency=='KRW' and side=='sell' else D(0)
    gross = rounded(price*quantity,currency,up=side=='buy')
    fee = rounded(gross*fee_bps/D(10000),currency,up=True)
    tax = rounded(gross*tax_bps/D(10000),currency,up=True)
    net = gross+fee+tax if side=='buy' else gross-fee-tax
    if net < 0: raise HTTPException(409,'수수료/세금 설정을 확인하세요.')
    return dict(currency=currency,gross_amount=gross,fee=fee,tax=tax,net_amount=net,fee_bps=fee_bps,tax_bps=tax_bps)

def maximum(symbol, price, balance):
    lo, hi = 0, min(1000000, int(balance/price))
    while lo < hi:
        mid = (lo+hi+1)//2
        if costs(symbol,'buy',price,mid)['net_amount'] <= balance: lo=mid
        else: hi=mid-1
    return lo
