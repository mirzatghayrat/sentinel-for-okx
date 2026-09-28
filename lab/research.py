"""Unoptimized baseline. Signal at close t; hypothetical fill at open t+1.

Both segments use prior bars for indicator warmup; each starts with fresh cash.
This is a short historical sanity check, not a claim of an established edge.
"""
from .strategy import signal

def backtest(raw, spec):
    bars = sorted({int(r[0]): r for r in raw if len(r) >= 9 and r[8] == '1'}.values(), key=lambda r: int(r[0]))
    if len(bars) < 250:
        raise ValueError('至少需要 250 根已收盘小时 K 线')
    for a, b in zip(bars, bars[1:]):
        if int(b[0])-int(a[0]) != 3600000:
            raise ValueError('历史行情存在缺口，不能假设连续成交')
    fee, slip = spec.fee_bps / 10000, spec.slippage_bps / 10000
    split = 100 + int((len(bars)-100)*.7)
    def segment(start, end):
        cash, qty, peak, dd = spec.budget, 0., spec.budget, 0.
        curve, trades, costs, daily = [], 0, 0., {}
        stopped = False
        for i in range(start, end):
            row, previous = bars[i], bars[max(0, i-100):i]
            px, close = float(row[1]), float(row[4])
            equity = cash + qty * px
            loss = spec.budget - equity
            s = signal(previous, spec.strategy)
            day = int(row[0]) // 86400000
            capacity = min(spec.order_quote, cash/(1+fee), spec.budget*spec.max_position_pct/100 - qty*px)
            side = s['side'] if daily.get(day, 0) < spec.max_orders_day and not stopped else 'hold'
            if loss >= spec.max_loss:
                side, stopped = 'sell', True
            if side == 'buy' and capacity >= 5:
                fill_px = px * (1+slip)
                qty += capacity / fill_px
                cash -= capacity * (1+fee)
                costs += capacity*fee + capacity - capacity/fill_px*px
                trades += 1
                daily[day] = daily.get(day, 0)+1
            elif side == 'sell' and qty > 0:
                gross = qty*px*(1-slip)
                cash += gross*(1-fee)
                costs += qty*px-gross + gross*fee
                qty = 0
                trades += 1
                daily[day] = daily.get(day, 0)+1
            equity = cash + qty*close*(1-slip)*(1-fee)
            peak = max(peak, equity)
            dd = max(dd, (peak-equity)/peak*100)
            curve.append({'ts': int(row[0]), 'equity': round(equity, 4)})
        end_equity = curve[-1]['equity']
        initial, final = float(bars[start][1]), float(bars[end-1][4])
        weight = spec.max_position_pct/100
        benchmark = spec.budget*(1-weight) + spec.budget*weight/(1+fee)/(initial*(1+slip))*final*(1-slip)*(1-fee)
        return {'start': int(bars[start][0]), 'end': int(bars[end-1][0]), 'equity': end_equity,
                'return_pct': (end_equity/spec.budget-1)*100, 'max_drawdown_pct': dd,
                'orders': trades, 'costs': round(costs,4), 'stopped': stopped,
                'benchmark_return_pct': (benchmark/spec.budget-1)*100, 'curve': curve[::max(1,len(curve)//180)]}
    return {'pair': spec.pair, 'strategy': spec.strategy, 'budget': spec.budget,
            'fee_bps': spec.fee_bps, 'slippage_bps': spec.slippage_bps, 'bars': len(bars),
            'max_position_pct': spec.max_position_pct, 'order_quote': spec.order_quote, 'max_orders_day': spec.max_orders_day,
            'sample_days': len(bars)/24, 'development': segment(100,split), 'holdout': segment(split,len(bars)),
            'note': '固定参数，后 30% 时段单独评估；仅约一个月数据，不足以证明盈利。费用为假设，未模拟盘口、最小订单和延迟；最大亏损是触发线，不是保证。'}
