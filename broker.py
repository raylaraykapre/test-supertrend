"""
Broker abstraction for AutoTrader.

Two implementations:
  * LiveBroker  - real orders on Bybit via the BybitClient.
  * PaperBroker - built-in demo with simulated PHP wallet, marks positions
                  against live Bybit prices, fills TP/SL/trailing locally.

All display is in Philippine Peso (PHP). Internal accounting in USDT.

Bybit TP/SL by ROI formulas (USDT Linear Perpetual):
  ROI = P&L / Initial_Margin * 100
  P&L (long)  = Qty * (Exit - Entry)
  P&L (short) = Qty * (Entry - Exit)
  Initial_Margin = (Qty * Entry) / Leverage

  Target price from ROI%:
    Long  TP = entry * (1 + roi_pct / 100 / leverage)
    Long  SL = entry * (1 - roi_pct / 100 / leverage)
    Short TP = entry * (1 - roi_pct / 100 / leverage)
    Short SL = entry * (1 + roi_pct / 100 / leverage)

Bybit Trailing Stop:
  Long:  trigger = highest_price * (1 - callback_pct / 100)
  Short: trigger = lowest_price  * (1 + callback_pct / 100)
  Activation: trail starts only after ROI >= activate_roi_pct
"""

import json
import os
import time

from bybit_client import BybitClient, BybitError


def make_broker(client, cfg, logger, fx=58.0):
    """Factory: return LiveBroker or PaperBroker based on config mode."""
    mode = str(cfg.get("mode", "demo")).lower()
    if mode == "live":
        return LiveBroker(client, cfg, logger, fx)
    return PaperBroker(client, cfg, logger, fx)


def calc_tp_price(entry, leverage, roi_pct, side):
    """Calculate take-profit price from ROI% (Bybit formula)."""
    if side == "long":
        return entry * (1 + roi_pct / 100.0 / leverage)
    return entry * (1 - roi_pct / 100.0 / leverage)


def calc_sl_price(entry, leverage, roi_pct, side):
    """Calculate stop-loss price from ROI% (Bybit formula)."""
    if side == "long":
        return entry * (1 - roi_pct / 100.0 / leverage)
    return entry * (1 + roi_pct / 100.0 / leverage)


def calc_roi(entry, current, leverage, side):
    """Calculate current ROI% for a position."""
    if entry <= 0:
        return 0.0
    if side == "long":
        return (current - entry) / entry * leverage * 100.0
    return (entry - current) / entry * leverage * 100.0


def calc_pnl_usdt(entry, current, qty, side):
    """Calculate P&L in USDT."""
    if side == "long":
        return qty * (current - entry)
    return qty * (entry - current)


class LiveBroker:
    """Real trading on Bybit."""

    def __init__(self, client, cfg, logger, fx=58.0):
        self.client = client
        self.cfg = cfg
        self.log = logger
        self.category = cfg["trade"]["category"]
        self.settle_coin = "USDT"
        self.fx = fx

    def name(self):
        return "LIVE"

    def validate(self):
        try:
            w, a = self.client.get_coin_balance("USDT")
            self.log.info("Live wallet: %.2f USDT (PHP %.2f)" % (w, w * self.fx))
            return True
        except BybitError as e:
            self.log.error("API validation failed: %s" % e)
            return False

    def get_balance_usdt(self):
        _, avail = self.client.get_coin_balance("USDT")
        return avail

    def get_balance_php(self):
        return self.get_balance_usdt() * self.fx

    def get_equity_usdt(self):
        w, _ = self.client.get_coin_balance("USDT")
        return w

    def get_equity_php(self):
        return self.get_equity_usdt() * self.fx

    def open_positions_map(self):
        positions = self.client.get_open_positions(
            self.category, settle_coin=self.settle_coin)
        out = {}
        for p in positions:
            sym = p.get("symbol")
            if sym:
                out[sym] = p
        return out

    def set_leverage(self, symbol, leverage):
        self.client.set_leverage(self.category, symbol, leverage)

    def open_position(self, symbol, side, qty, entry_price, tp, sl, leverage=None):
        """Open a market position with TP/SL."""
        bybit_side = "Buy" if side == "long" else "Sell"
        self.client.place_market_order(
            self.category, symbol, bybit_side, qty,
            take_profit=tp, stop_loss=sl)
        return True

    def close_position(self, symbol, pos, price, reason=""):
        """Close position with a market order."""
        side = pos.get("side", "")
        qty = float(pos.get("size", 0))
        close_side = "Sell" if side == "Buy" else "Buy"
        try:
            self.client.place_market_order(
                self.category, symbol, close_side, qty, reduce_only=True)
            return True
        except BybitError as e:
            self.log.error("Close failed: %s" % e)
            return False

    def set_trailing_stop(self, symbol, trailing_stop, active_price):
        """Set trailing stop on Bybit server."""
        self.client.set_trading_stop(
            self.category, symbol,
            trailing_stop=trailing_stop,
            active_price=active_price)

    def on_tick(self, price_map):
        pass  # Live broker: exchange handles TP/SL/trailing


class PaperBroker:
    """Built-in demo that simulates trading with a PHP wallet."""

    def __init__(self, client, cfg, logger, fx=58.0):
        self.client = client
        self.cfg = cfg
        self.log = logger
        self.category = cfg["trade"]["category"]
        self.fx = fx

        demo_cfg = cfg.get("demo", {})
        self.starting_php = float(demo_cfg.get("starting_balance_php", 100000))
        self.state_file = demo_cfg.get("state_file", "demo_state.json")
        self.reset_on_start = demo_cfg.get("reset_on_start", False)

        risk = cfg.get("risk", {})
        self.sl_roi = float(risk.get("stop_loss_roi_pct", 30))
        self.tp_roi = float(risk.get("take_profit_roi_pct", 80))
        self.trail_enabled = risk.get("trailing_stop_enabled", True)
        self.trail_activate_roi = float(risk.get("trailing_activate_roi_pct", 50))
        self.trail_callback_pct = float(risk.get("trailing_callback_pct", 30))

        # State
        self.balance_usdt = self.starting_php / self.fx
        self.positions = {}  # symbol -> position dict
        self.closed_trades = []
        self.total_trades = 0
        self.winning_trades = 0

        self._load_state()

    def name(self):
        return "DEMO"

    def validate(self):
        self.log.info("Demo wallet: PHP %.2f (%.2f USDT)" %
                      (self.balance_usdt * self.fx, self.balance_usdt))
        return True

    def get_balance_usdt(self):
        return self.balance_usdt

    def get_balance_php(self):
        return self.balance_usdt * self.fx

    def get_equity_usdt(self):
        equity = self.balance_usdt
        for pos in self.positions.values():
            equity += pos.get("unrealised_pnl", 0)
        return equity

    def get_equity_php(self):
        return self.get_equity_usdt() * self.fx

    def open_positions_map(self):
        return dict(self.positions)

    def set_leverage(self, symbol, leverage):
        pass  # Paper mode: leverage is just a number for calculations

    def open_position(self, symbol, side, qty, entry_price, tp, sl, leverage=None):
        """Simulate opening a position.
        
        leverage: actual leverage value (already calculated as max_lev * pct/100)
        """
        if leverage is None:
            leverage = 1
        margin = (qty * entry_price) / leverage
        if margin > self.balance_usdt:
            self.log.warning("[DEMO] Insufficient margin: need %.2f, have %.2f USDT"
                            % (margin, self.balance_usdt))
            return False

        self.balance_usdt -= margin
        self.positions[symbol] = {
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "entry": entry_price,
            "tp": tp,
            "sl": sl,
            "leverage": leverage,
            "margin": margin,
            "time": time.time(),
            "best_price": entry_price,
            "trail_active": False,
            "trail_sl": None,
            "unrealised_pnl": 0.0,
        }
        self.log.info("[DEMO] OPENED %s %s @ %.6f | TP=%.6f SL=%.6f | Margin=PHP %.2f"
                     % (side.upper(), symbol, entry_price, tp, sl, margin * self.fx))
        self._save_state()
        return True

    def close_position(self, symbol, pos, price, reason=""):
        """Simulate closing a position."""
        if symbol not in self.positions:
            return False
        p = self.positions.pop(symbol)
        pnl = calc_pnl_usdt(p["entry"], price, p["qty"], p["side"])
        self.balance_usdt += p["margin"] + pnl
        self.total_trades += 1
        if pnl > 0:
            self.winning_trades += 1

        self.log.info("[DEMO] CLOSED %s %s @ %.6f | PnL: PHP %.2f | Reason: %s"
                     % (p["side"].upper(), symbol, price, pnl * self.fx, reason))
        self._save_state()
        return True

    def set_trailing_stop(self, symbol, trailing_stop, active_price):
        pass  # Paper mode handles trailing in on_tick

    def on_tick(self, price_map):
        """Check TP/SL/trailing for all open positions each tick."""
        to_close = []
        for symbol, pos in list(self.positions.items()):
            price = price_map.get(symbol)
            if price is None:
                continue

            side = pos["side"]
            entry = pos["entry"]
            leverage = pos["leverage"]

            # Update unrealised PnL
            pos["unrealised_pnl"] = calc_pnl_usdt(entry, price, pos["qty"], side)

            # Check stop loss
            if side == "long" and price <= pos["sl"]:
                to_close.append((symbol, pos["sl"], "stop loss"))
                continue
            if side == "short" and price >= pos["sl"]:
                to_close.append((symbol, pos["sl"], "stop loss"))
                continue

            # Check take profit
            if side == "long" and price >= pos["tp"]:
                to_close.append((symbol, pos["tp"], "take profit"))
                continue
            if side == "short" and price <= pos["tp"]:
                to_close.append((symbol, pos["tp"], "take profit"))
                continue

            # Trailing stop logic
            if not self.trail_enabled:
                continue

            current_roi = calc_roi(entry, price, leverage, side)

            # Track best price
            if side == "long":
                if price > pos["best_price"]:
                    pos["best_price"] = price
            else:
                if price < pos["best_price"]:
                    pos["best_price"] = price

            # Activate trailing
            if not pos["trail_active"] and current_roi >= self.trail_activate_roi:
                pos["trail_active"] = True
                self._update_trail_sl(pos)
                self.log.info("[DEMO] Trail activated for %s at ROI %.1f%%" %
                             (symbol, current_roi))

            # Update trailing stop level
            if pos["trail_active"]:
                self._update_trail_sl(pos)
                # Check if trailing stop triggered
                if side == "long" and price <= pos["trail_sl"]:
                    to_close.append((symbol, pos["trail_sl"], "trailing stop"))
                    continue
                if side == "short" and price >= pos["trail_sl"]:
                    to_close.append((symbol, pos["trail_sl"], "trailing stop"))
                    continue

        for symbol, close_price, reason in to_close:
            self.close_position(symbol, self.positions.get(symbol, {}),
                               close_price, reason)

    def _update_trail_sl(self, pos):
        """Ratchet trailing stop using Bybit's callback rate formula."""
        best = pos["best_price"]
        if pos["side"] == "long":
            new_sl = best * (1 - self.trail_callback_pct / 100.0)
            if pos["trail_sl"] is None or new_sl > pos["trail_sl"]:
                pos["trail_sl"] = new_sl
        else:
            new_sl = best * (1 + self.trail_callback_pct / 100.0)
            if pos["trail_sl"] is None or new_sl < pos["trail_sl"]:
                pos["trail_sl"] = new_sl

    def _save_state(self):
        state = {
            "balance_usdt": self.balance_usdt,
            "positions": self.positions,
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
        }
        try:
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, default=str)
        except Exception:
            pass

    def _load_state(self):
        if self.reset_on_start:
            self.balance_usdt = self.starting_php / self.fx
            self.positions = {}
            self.total_trades = 0
            self.winning_trades = 0
            self._save_state()
            return

        if not os.path.exists(self.state_file):
            return
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                state = json.load(f)
            self.balance_usdt = float(state.get("balance_usdt", self.starting_php / self.fx))
            self.positions = state.get("positions", {})
            self.total_trades = int(state.get("total_trades", 0))
            self.winning_trades = int(state.get("winning_trades", 0))
        except Exception:
            pass
