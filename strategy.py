"""
SuperTrend Strategy for AutoTrader.

Pure Python implementation of the SuperTrend indicator.
Originally conceived by Olivier Seban (2008).
No pip dependencies required.

SuperTrend Logic (public domain formula):
  ATR = Average True Range over N periods
  Upper Band = source - (multiplier * ATR)
  Lower Band = source + (multiplier * ATR)

  The bands ratchet (only move in the favourable direction):
    up = max(up, prev_up)  if prev_close > prev_up
    dn = min(dn, prev_dn)  if prev_close < prev_dn

  Trend flips:
    trend = 1 (bullish)  when close crosses above dn
    trend = -1 (bearish) when close crosses below up

  Signals:
    BUY  = trend flips from -1 to 1
    SELL = trend flips from 1 to -1

This is an independent clean-room implementation based on the publicly
documented mathematical formula. No third-party code was copied.
"""

from broker import calc_tp_price, calc_sl_price


def true_range(candle, prev_candle):
    """Calculate True Range for a candle."""
    high = candle["high"]
    low = candle["low"]
    prev_close = prev_candle["close"]
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def compute_atr(candles, period):
    """Compute ATR using Wilder's smoothing (RMA/SMMA).

    This matches Pine Script's atr() function which uses RMA.
    """
    if len(candles) < period + 1:
        return []

    # Calculate TR for each candle (starting from index 1)
    tr_values = []
    for i in range(1, len(candles)):
        tr_values.append(true_range(candles[i], candles[i - 1]))

    # First ATR is SMA of first 'period' TR values
    if len(tr_values) < period:
        return []

    atr_values = []
    first_atr = sum(tr_values[:period]) / period
    atr_values.append(first_atr)

    # Wilder's smoothing: ATR = (prev_ATR * (period-1) + current_TR) / period
    for i in range(period, len(tr_values)):
        prev_atr = atr_values[-1]
        current_atr = (prev_atr * (period - 1) + tr_values[i]) / period
        atr_values.append(current_atr)

    return atr_values


def compute_supertrend(candles, period=10, multiplier=3.0):
    """Compute SuperTrend indicator values.

    Returns a list of dicts (one per candle, aligned to end of candles list):
      {trend: 1 or -1, up: float, dn: float, signal: "buy"/"sell"/None}

    The output list length = len(candles) - period - 1
    (we need period+1 candles to get the first ATR value)
    """
    atr_values = compute_atr(candles, period)
    if not atr_values:
        return []

    # ATR values are aligned starting from candle index (period + 1)
    # because we skip index 0 for TR, then need 'period' TRs for first ATR
    start_idx = period + 1
    results = []

    prev_up = None
    prev_dn = None
    prev_trend = 1
    prev_close = candles[start_idx - 1]["close"]

    for i, atr_val in enumerate(atr_values):
        candle_idx = start_idx + i
        if candle_idx >= len(candles):
            break

        candle = candles[candle_idx]
        # Source = hl2 = (high + low) / 2
        src = (candle["high"] + candle["low"]) / 2.0

        # Basic bands
        basic_up = src - (multiplier * atr_val)
        basic_dn = src + (multiplier * atr_val)

        # Ratchet: up can only go higher, dn can only go lower
        if prev_up is not None:
            up = max(basic_up, prev_up) if prev_close > prev_up else basic_up
            dn = min(basic_dn, prev_dn) if prev_close < prev_dn else basic_dn
        else:
            up = basic_up
            dn = basic_dn

        # Trend determination (uses previous up/dn values per Pine Script)
        close = candle["close"]
        if prev_up is None:
            # First bar: determine trend from close vs bands
            trend = 1 if close > up else -1
        elif prev_trend == -1 and close > prev_dn:
            trend = 1
        elif prev_trend == 1 and close < prev_up:
            trend = -1
        else:
            trend = prev_trend

        # Signal detection (trend flip)
        signal = None
        if trend == 1 and prev_trend == -1:
            signal = "buy"
        elif trend == -1 and prev_trend == 1:
            signal = "sell"

        results.append({
            "trend": trend,
            "up": up,
            "dn": dn,
            "signal": signal,
            "close": close,
            "candle_idx": candle_idx,
        })

        prev_up = up
        prev_dn = dn
        prev_trend = trend
        prev_close = close

    return results


# ===================================================================
# PER-SYMBOL STRATEGY
# ===================================================================

class SymbolStrategy:
    """Manages SuperTrend signals and entry logic for one symbol."""

    def __init__(self, symbol, cfg, logger):
        self.symbol = symbol
        self.log = logger
        self.cfg = cfg

        trade = cfg.get("trade", {})
        risk = cfg.get("risk", {})
        strat_cfg = cfg.get("strategy", {})

        self.leverage_pct = float(trade.get("leverage_pct", 75))
        self.wallet_pct = float(trade.get("wallet_pct", 75))
        self.sl_roi = float(risk.get("stop_loss_roi_pct", 30))
        self.tp_roi = float(risk.get("take_profit_roi_pct", 80))

        # SuperTrend parameters
        self.atr_period = int(strat_cfg.get("atr_period", 10))
        self.atr_multiplier = float(strat_cfg.get("atr_multiplier", 3.0))

        # Instrument info (set later)
        self.max_leverage = 1
        self.qty_step = 0.001
        self.min_qty = 0.001
        self.price_step = 0.01

        # State
        self.pending = None  # signal waiting for execution
        self.last_signal_time = 0
        self.entered = False

    def set_instrument(self, info):
        """Set instrument precision from Bybit instrument info."""
        lot = info.get("lotSizeFilter", {})
        price_filter = info.get("priceFilter", {})
        lev = info.get("leverageFilter", {})

        self.min_qty = float(lot.get("minOrderQty", 0.001))
        self.qty_step = float(lot.get("qtyStep", 0.001))
        self.price_step = float(price_filter.get("tickSize", 0.01))
        self.max_leverage = float(lev.get("maxLeverage", 100))

    @property
    def actual_leverage(self):
        """Leverage = leverage_pct% of pair's max leverage.

        Example: 75% of 100x max = 75x
        Example: 50% of 12x max = 6x
        """
        lev = self.max_leverage * self.leverage_pct / 100.0
        return max(1, int(lev))

    def update(self, closed_candles, position_side=None):
        """Run SuperTrend on closed candles and check for signals.

        If not in a position: look for buy/sell signal to enter.
        If in a position: look for opposite signal to exit (reversal).
        """
        if len(closed_candles) < self.atr_period + 3:
            return

        results = compute_supertrend(closed_candles, self.atr_period,
                                      self.atr_multiplier)
        if not results:
            return

        # Check the most recent result for a signal
        latest = results[-1]
        signal = latest.get("signal")

        if signal is None:
            return

        # Avoid acting on the same signal twice
        candle_idx = latest.get("candle_idx", 0)
        signal_time = closed_candles[candle_idx]["start"] if candle_idx < len(closed_candles) else 0
        if signal_time <= self.last_signal_time:
            return

        # If in position, check for reversal (opposite direction)
        if position_side:
            if (position_side == "long" and signal == "sell") or \
               (position_side == "short" and signal == "buy"):
                self.pending = {
                    "direction": "long" if signal == "buy" else "short",
                    "signal": signal,
                    "price": latest["close"],
                    "is_reversal": True,
                }
                self.last_signal_time = signal_time
            return

        # Not in position: arm the entry
        self.pending = {
            "direction": "long" if signal == "buy" else "short",
            "signal": signal,
            "price": latest["close"],
            "is_reversal": False,
        }
        self.last_signal_time = signal_time
        self.entered = False

    def has_pending(self):
        """Return True if there's a signal waiting for entry."""
        return self.pending is not None and not self.entered and \
               not self.pending.get("is_reversal", False)

    def has_reversal(self):
        """Return True if a reversal signal was detected (exit signal)."""
        return self.pending is not None and self.pending.get("is_reversal", False)

    def prepare_entry(self, balance_usdt, current_price):
        """Calculate position size and TP/SL for an entry.

        Wallet % calculation:
          If balance is PHP 100,000 (in USDT) and wallet_pct is 75%,
          then margin = balance_usdt * 75/100.
          Notional = margin * leverage.
          Qty = notional / price.
        """
        if not self.has_pending():
            return None

        direction = self.pending["direction"]
        leverage = self.actual_leverage
        entry = current_price  # enter at market price on signal

        # Margin = balance * wallet_pct / 100
        margin = balance_usdt * self.wallet_pct / 100.0
        notional = margin * leverage
        qty = notional / entry

        # Round qty to step
        qty = self._round_qty(qty)
        if qty < self.min_qty:
            return None

        # Calculate TP/SL using Bybit ROI formula
        tp = calc_tp_price(entry, leverage, self.tp_roi, direction)
        sl = calc_sl_price(entry, leverage, self.sl_roi, direction)

        tp = self._round_price(tp)
        sl = self._round_price(sl)

        return {
            "symbol": self.symbol,
            "direction": direction,
            "side": direction,
            "entry": entry,
            "qty": qty,
            "tp": tp,
            "sl": sl,
            "leverage": leverage,
            "margin": margin,
        }

    def mark_entered(self, spec):
        """Mark this signal as acted upon."""
        self.entered = True
        if spec:
            self.pending = None

    def clear(self):
        """Reset strategy state after a position is closed."""
        self.pending = None
        self.entered = False

    def _round_qty(self, qty):
        if self.qty_step <= 0:
            return qty
        return round(qty - (qty % self.qty_step), 8)

    def _round_price(self, price):
        if self.price_step <= 0:
            return price
        return round(round(price / self.price_step) * self.price_step, 8)
