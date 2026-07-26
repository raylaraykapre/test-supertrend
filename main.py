#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AutoTrader2.0 - Termux CLI SuperTrend Bot

Pure Python. No pip dependencies. Runs entirely on Python 3 stdlib.
Designed for Termux on Android.

Usage:
    python3 main.py
    python3 main.py --config /path/to/config.json
"""

import json
import os
import sys
import time
import traceback
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from bybit_client import BybitClient, BybitError, resolve_api
from broker import make_broker
from strategy import SymbolStrategy
from fx import get_usdt_php_rate, format_php

CONFIG_FILE = os.path.join(BASE_DIR, "config.json")


# ===================================================================
# TERMINAL OUTPUT HELPERS
# ===================================================================

# ANSI colors (Termux supports these)
C_RESET = "\033[0m"
C_BOLD = "\033[1m"
C_DIM = "\033[2m"
C_GREEN = "\033[32m"
C_RED = "\033[31m"
C_YELLOW = "\033[33m"
C_CYAN = "\033[36m"
C_MAGENTA = "\033[35m"
C_WHITE = "\033[97m"
C_BG_GREEN = "\033[42m"
C_BG_RED = "\033[41m"


def clear_screen():
    os.system("clear")


def timestamp():
    return datetime.now().strftime("%H:%M:%S")


def log(msg, level="INFO"):
    ts = timestamp()
    if level == "ERROR":
        color = C_RED
    elif level == "WARN":
        color = C_YELLOW
    elif level == "TRADE":
        color = C_GREEN
    elif level == "SIGNAL":
        color = C_MAGENTA
    else:
        color = C_DIM
    print("%s[%s] [%s]%s %s" % (color, ts, level, C_RESET, msg))


class TermuxLogger:
    """Logger compatible with broker/strategy interface."""
    def info(self, msg):
        log(msg)

    def warning(self, msg):
        log(msg, "WARN")

    def error(self, msg):
        log(msg, "ERROR")

    def debug(self, msg):
        pass  # suppress debug noise in terminal

    def log(self, msg):
        # Parse [LEVEL] prefix if present
        if msg.startswith("["):
            end = msg.find("]")
            if end > 0:
                level = msg[1:end]
                text = msg[end+1:].strip()
                log(text, level)
                return
        log(msg)


# ===================================================================
# DASHBOARD DISPLAY
# ===================================================================

def print_dashboard(data, fx):
    """Print a compact live dashboard to terminal."""
    bal = data.get("balance", "?")
    eq = data.get("equity", "?")
    pnl = data.get("pnl_raw", 0)
    trades = data.get("trades", 0)
    win_rate = data.get("win_rate", "0%")
    open_pos = data.get("open_positions", 0)
    positions = data.get("positions", [])

    pnl_color = C_GREEN if pnl >= 0 else C_RED
    pnl_str = "PHP {:,.2f}".format(pnl)

    print()
    print("%s%s ══════ AutoTrader2.0 ══════ %s" % (C_BOLD, C_CYAN, C_RESET))
    print("%s Balance:%s  %s" % (C_DIM, C_RESET, bal))
    print("%s Equity: %s  %s" % (C_DIM, C_RESET, eq))
    print("%s PnL:    %s  %s%s%s" % (C_DIM, C_RESET, pnl_color, pnl_str, C_RESET))
    print("%s Trades: %s  %d  |  Win: %s  |  Open: %d" %
          (C_DIM, C_RESET, trades, win_rate, open_pos))
    print("%s FX Rate:%s  1 USDT = PHP %.2f" % (C_DIM, C_RESET, fx))

    if positions:
        print()
        print("%s Positions:%s" % (C_BOLD, C_RESET))
        for p in positions:
            side_color = C_GREEN if p["side"] == "LONG" else C_RED
            print("   %s%s%s %s  entry=%s  pnl=%s" %
                  (side_color, p["side"], C_RESET, p["symbol"],
                   p["entry"], p["pnl_php"]))

    print("%s%s ═══════════════════════════ %s" % (C_DIM, C_CYAN, C_RESET))
    print()


# ===================================================================
# BOT ENGINE (CLI version)
# ===================================================================

def load_config(path=None):
    """Load config from JSON file."""
    cfg_path = path or CONFIG_FILE
    if not os.path.exists(cfg_path):
        log("Config not found: %s" % cfg_path, "ERROR")
        sys.exit(1)
    with open(cfg_path, "r", encoding="utf-8") as f:
        return json.load(f)


def run_bot(cfg):
    """Main bot loop - runs until interrupted."""
    logger = TermuxLogger()

    mode = cfg.get("mode", "demo").lower()
    trade = cfg["trade"]

    # Auto-fetch FX rate
    fx = get_usdt_php_rate()
    log("USDT/PHP rate: %.2f (auto-fetched)" % fx)

    # Setup client
    api = resolve_api(cfg)
    client = BybitClient(
        api_key=api["api_key"], api_secret=api["api_secret"],
        demo=api["demo"], testnet=api["testnet"],
        recv_window=api["recv_window"], logger=logger)

    # Setup broker
    broker = make_broker(client, cfg, logger, fx)
    category = trade["category"]
    timeframe = str(trade["timeframe"])
    max_open = int(trade["max_open_positions"])
    scan_seconds = int(trade.get("scan_seconds", 60))
    kline_limit = int(cfg.get("engine", {}).get("kline_limit", 200))
    scan_batch = int(cfg.get("engine", {}).get("scan_batch", 50))

    log("Mode: %s | Timeframe: %s | Max positions: %d" %
        (mode.upper(), timeframe, max_open))
    log("Strategy: SuperTrend (ATR %s, Mult %s)" %
        (cfg.get("strategy", {}).get("atr_period", 10),
         cfg.get("strategy", {}).get("atr_multiplier", 3.0)))
    strat_cfg = cfg.get("strategy", {})
    tp_mult = strat_cfg.get("tp_atr_multiplier", strat_cfg.get("atr_multiplier", 3.0))
    sl_mult = strat_cfg.get("sl_atr_multiplier", 1.0)
    log("TP/SL: TP = %s*ATR, SL = %s*ATR (R:R = %.1f:1)" %
        (tp_mult, sl_mult, float(tp_mult) / float(sl_mult)))
    risk = cfg.get("risk", {})
    if risk.get("trailing_stop_enabled"):
        log("Trailing: activate @%s%% ROI, callback %s%%" %
            (risk["trailing_activate_roi_pct"], risk["trailing_callback_pct"]))
    log("Wallet: %s%% per position | Leverage: %s%% of max" %
        (trade["wallet_pct"], trade["leverage_pct"]))

    # Validate broker connection
    if not broker.validate():
        log("Broker validation failed.", "ERROR")
        return

    # Check existing positions (live mode)
    if mode == "live":
        try:
            existing = client.get_open_positions(category, settle_coin="USDT")
            if existing:
                log("Found %d existing position(s)" % len(existing))
                for p in existing:
                    log("  %s %s %s @ %s" % (
                        p.get("symbol"), p.get("side"),
                        p.get("size"), p.get("avgPrice")))
        except BybitError:
            log("Could not check existing positions", "WARN")

    # Discover symbols
    log("Loading USDT perpetual pairs...")
    try:
        instruments = client.get_all_instruments(
            category, status="Trading",
            quote_coin=trade.get("quote_coin", "USDT"),
            contract_type="LinearPerpetual")
    except BybitError as e:
        log("Failed to load instruments: %s" % e, "ERROR")
        return

    symbols_cfg = trade.get("symbols", "ALL")
    if isinstance(symbols_cfg, list) and symbols_cfg:
        wanted = set(symbols_cfg)
    else:
        wanted = None

    strategies = {}
    for info in instruments:
        sym = info.get("symbol")
        if not sym:
            continue
        if wanted and sym not in wanted:
            continue
        strat = SymbolStrategy(sym, cfg, logger)
        strat.set_instrument(info)
        strategies[sym] = strat

    scan_order = list(strategies.keys())
    log("Tracking %d symbol(s)" % len(scan_order))
    if not scan_order:
        log("No symbols found matching config.", "ERROR")
        return

    scan_idx = 0
    tick = 0
    status_every = max(1, int(60 / scan_seconds))
    fx_refresh = 30
    dashboard_every = 3  # print dashboard every N ticks

    log("Bot started. Press Ctrl+C to stop.", "TRADE")
    print()

    while True:
        try:
            # Refresh FX rate periodically
            if tick > 0 and tick % fx_refresh == 0:
                fx = get_usdt_php_rate()

            # Fetch all prices
            prices = {}
            try:
                for t in client.get_all_tickers(category):
                    lp = t.get("lastPrice")
                    if lp:
                        prices[t.get("symbol")] = float(lp)
            except BybitError:
                log("Price fetch failed, retrying...", "WARN")

            # Tick the broker (checks TP/SL/trailing for paper mode)
            broker.on_tick(prices)
            open_map = broker.open_positions_map()

            # Determine which symbols to scan this tick
            # Always scan regardless of position count - we need signals
            # for potential new entries when slots are available
            n = len(scan_order)
            batch = min(scan_batch, n)
            targets = []
            for _ in range(batch):
                targets.append(scan_order[scan_idx % n])
                scan_idx = (scan_idx + 1) % n

            # Scan klines and run strategy
            for sym in targets:
                strat = strategies.get(sym)
                if not strat:
                    continue
                try:
                    candles = client.get_kline(category, sym, timeframe,
                                               limit=kline_limit)
                    if len(candles) < 4:
                        continue
                    closed = candles[:-1]
                    strat.update(closed)
                except BybitError:
                    pass
                time.sleep(0.1)

            open_map = broker.open_positions_map()

            # Process new entries (only if we have open slots)
            if len(open_map) < max_open:
                balance = broker.get_balance_usdt()
                for sym, strat in strategies.items():
                    if not strat.has_pending():
                        continue
                    if sym in open_map:
                        continue
                    price = prices.get(sym)
                    if price is None:
                        continue
                    if len(open_map) >= max_open:
                        break

                    spec = strat.prepare_entry(balance, price)
                    if spec is None:
                        strat.mark_entered(None)
                        continue

                    broker.set_leverage(sym, spec["leverage"])
                    if broker.open_position(sym, spec["side"], spec["qty"],
                                            spec["entry"], spec["tp"], spec["sl"],
                                            leverage=spec["leverage"]):
                        strat.mark_entered(spec)
                        open_map[sym] = True
                        log("ENTRY %s %s @ %.6f | TP=%.6f SL=%.6f" %
                            (spec["side"].upper(), sym, spec["entry"],
                             spec["tp"], spec["sl"]), "TRADE")
                        break
                    else:
                        strat.mark_entered(None)

            # Dashboard output
            if tick % dashboard_every == 0:
                open_map = broker.open_positions_map()
                equity_php = broker.get_equity_usdt() * fx
                balance_php = broker.get_balance_usdt() * fx
                pnl = equity_php - balance_php

                win_rate = "0%"
                total_trades = 0
                if hasattr(broker, "total_trades"):
                    total_trades = broker.total_trades
                    if total_trades > 0:
                        win_rate = "%.0f%%" % (
                            broker.winning_trades / total_trades * 100)

                positions_fmt = []
                for sym, pos in open_map.items():
                    p_side = pos.get("side", "?")
                    entry = float(pos.get("entry", 0) or
                                  pos.get("avgPrice", 0) or 0)
                    p_price = prices.get(sym, entry)
                    p_pnl = 0
                    if entry > 0:
                        qty = float(pos.get("qty", 0) or
                                    pos.get("size", 0) or 0)
                        if p_side == "long":
                            p_pnl = qty * (p_price - entry)
                        else:
                            p_pnl = qty * (entry - p_price)
                    positions_fmt.append({
                        "symbol": sym,
                        "side": p_side.upper(),
                        "entry": "%.4f" % entry,
                        "pnl_php": format_php(p_pnl, fx),
                    })

                print_dashboard({
                    "balance": format_php(broker.get_balance_usdt(), fx),
                    "equity": format_php(broker.get_equity_usdt(), fx),
                    "pnl_raw": pnl,
                    "trades": total_trades,
                    "win_rate": win_rate,
                    "open_positions": len(open_map),
                    "positions": positions_fmt,
                }, fx)

            # Status log
            if tick % status_every == 0 and tick > 0:
                armed = sum(1 for s in strategies.values() if s.has_pending())
                log("Equity: %s | Open: %d | Armed: %d | Tick: %d" %
                    (format_php(broker.get_equity_usdt(), fx),
                     len(open_map), armed, tick))

        except BybitError as e:
            log("API error: %s" % e, "ERROR")
        except Exception:
            log(traceback.format_exc(), "ERROR")

        tick += 1

        # Sleep with mini-ticks for responsive PnL updates
        mini_interval = 2
        elapsed = 0
        while elapsed < scan_seconds:
            time.sleep(mini_interval)
            elapsed += mini_interval

            # Quick price refresh for real-time PnL
            try:
                mini_prices = {}
                for t in client.get_all_tickers(category):
                    lp = t.get("lastPrice")
                    if lp:
                        mini_prices[t.get("symbol")] = float(lp)
                if mini_prices:
                    prices = mini_prices
                    broker.on_tick(prices)
            except Exception:
                pass


# ===================================================================
# ENTRY POINT
# ===================================================================

def main():
    print()
    print("%s%s  AutoTrader2.0 - Termux SuperTrend Bot  %s" %
          (C_BOLD, C_CYAN, C_RESET))
    print("%s  Pure Python | No pip required | Bybit V5%s" %
          (C_DIM, C_RESET))
    print()

    # Parse args
    cfg_path = CONFIG_FILE
    if "--config" in sys.argv:
        idx = sys.argv.index("--config")
        if idx + 1 < len(sys.argv):
            cfg_path = sys.argv[idx + 1]

    cfg = load_config(cfg_path)
    log("Config loaded: %s" % cfg_path)

    try:
        run_bot(cfg)
    except KeyboardInterrupt:
        print()
        log("Bot stopped by user.", "WARN")
        sys.exit(0)


if __name__ == "__main__":
    main()
