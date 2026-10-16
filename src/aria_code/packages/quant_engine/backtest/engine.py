from typing import List, Dict, Any
import pandas as pd
from .core import Portfolio, Order

class BacktestEngine:
    """Minimal Event-Driven Backtest Engine for Agent Signals"""

    def __init__(self, initial_cash: float = 100000.0):
        self.portfolio = Portfolio(initial_cash=initial_cash)
        self.orders: List[Order] = []

    def run_signals(self, price_data: pd.DataFrame, signals: List[Dict[str, Any]]):
        """
        Run backtest based on daily prices and agent signals.
        price_data expects index to be datetime, with 'close' column.
        signals expects format: [{'date': 'YYYY-MM-DD', 'signal': 'BUY', 'confidence': 0.8, 'symbol': 'AAPL'}]
        """
        # Convert signals to a dictionary indexed by date for O(1) lookup
        signal_dict = {}
        for s in signals:
            try:
                date_str = pd.to_datetime(s['date']).strftime('%Y-%m-%d')
                signal_dict[date_str] = s
            except (ValueError, TypeError, KeyError):
                # 裸 except 会连 KeyboardInterrupt / SystemExit 一起吞掉——
                # 循环里跑着时用户按 Ctrl+C 会被静默忽略。这里真正要容忍的
                # 只是"某条信号的日期字段解析不了"，收窄到具体异常。
                pass

        equity_curve = []

        for date, row in price_data.iterrows():
            current_price = float(row['close'])
            date_str = date.strftime('%Y-%m-%d')
            symbol = "UNKNOWN" # Simplification for single-asset backtest for now

            # Check for signals on this date
            if date_str in signal_dict:
                sig = signal_dict[date_str]
                symbol = sig.get('symbol', 'UNKNOWN')
                action = sig.get('signal', 'HOLD').upper()
                confidence = float(sig.get('confidence', 0.5))

                if action == 'BUY':
                    # Allocate portion of cash based on confidence
                    target_investment = self.portfolio.cash * confidence
                    qty = int(target_investment / current_price)
                    if qty > 0:
                        cost = qty * current_price
                        self.portfolio.cash -= cost
                        pos = self.portfolio.get_position(symbol)
                        pos.update(qty, current_price, True)
                        self.orders.append(Order(symbol, qty, current_price, direction="BUY", status="FILLED", timestamp=date))

                elif action in ['SELL', 'REDUCE']:
                    pos = self.portfolio.get_position(symbol)
                    if pos.quantity > 0:
                        # Sell portion based on confidence
                        sell_qty = int(pos.quantity * confidence)
                        if sell_qty > 0:
                            proceeds = sell_qty * current_price
                            self.portfolio.cash += proceeds
                            pos.update(sell_qty, current_price, False)
                            self.orders.append(Order(symbol, sell_qty, current_price, direction="SELL", status="FILLED", timestamp=date))

            # Record daily equity
            total_val = self.portfolio.total_value({symbol: current_price})
            equity_curve.append({
                'date': date,
                'total_value': total_val,
                'cash': self.portfolio.cash
            })

        return pd.DataFrame(equity_curve).set_index('date')

    def calculate_metrics(self, equity_df: pd.DataFrame) -> Dict[str, float]:
        if equity_df.empty:
            return {}

        returns = equity_df['total_value'].pct_change().dropna()
        total_return = (equity_df['total_value'].iloc[-1] / self.portfolio.initial_cash) - 1.0

        # Periods a year from the equity curve's own dates; 252 when it has none.
        from aria_code.domain import performance_stats as stats

        periods = stats.periods_per_year_from_dates(
            len(returns), equity_df.index[0], equity_df.index[-1]
        ) or float(stats.FALLBACK_PERIODS_PER_YEAR)
        ann_vol = returns.std() * (periods ** 0.5) if not returns.empty else 0.0
        sharpe = (returns.mean() * periods) / ann_vol if ann_vol > 0 else 0.0

        cum_ret = (1 + returns).cumprod()
        peak = cum_ret.cummax()
        drawdown = (cum_ret - peak) / peak
        max_dd = drawdown.min()

        return {
            "total_return_pct": round(total_return * 100, 2),
            "annualized_volatility_pct": round(ann_vol * 100, 2),
            "sharpe_ratio": round(sharpe, 2),
            "max_drawdown_pct": round(max_dd * 100, 2) if not pd.isna(max_dd) else 0.0
        }
