"""
Auto-fetch USDT/PHP exchange rate from the internet.

Pure Python - uses only urllib (standard library).
Tries multiple free public APIs as fallbacks.
"""

import json
import urllib.request
import urllib.error
import time

# Cache the rate for 10 minutes to avoid hammering APIs
_cached_rate = None
_cached_time = 0
_CACHE_TTL = 600  # seconds


def get_usdt_php_rate():
    """Fetch current USDT to PHP rate. Returns float or fallback 58.0.

    Tries multiple free APIs in order:
    1. Bybit ticker (USDTPHP or via BTC cross-rate)
    2. CoinGecko free API
    3. ExchangeRate API (USD/PHP as proxy for USDT/PHP)
    """
    global _cached_rate, _cached_time

    now = time.time()
    if _cached_rate and (now - _cached_time) < _CACHE_TTL:
        return _cached_rate

    rate = _try_coingecko()
    if rate is None:
        rate = _try_exchangerate_api()
    if rate is None:
        rate = _try_binance_p2p()
    if rate is None:
        rate = 58.0  # fallback

    _cached_rate = rate
    _cached_time = now
    return rate


def _fetch_json(url, timeout=10):
    """Fetch JSON from a URL. Returns parsed dict or None on failure."""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "AutoTrader/1.0",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _try_coingecko():
    """CoinGecko free API: tether price in PHP."""
    data = _fetch_json(
        "https://api.coingecko.com/api/v3/simple/price?ids=tether&vs_currencies=php"
    )
    if data and "tether" in data and "php" in data["tether"]:
        rate = float(data["tether"]["php"])
        if rate > 0:
            return rate
    return None


def _try_exchangerate_api():
    """ExchangeRate.host free API: USD to PHP (close enough to USDT/PHP)."""
    # Try open.er-api.com (free, no key needed)
    data = _fetch_json(
        "https://open.er-api.com/v6/latest/USD"
    )
    if data and data.get("result") == "success":
        rates = data.get("rates", {})
        php = rates.get("PHP")
        if php and float(php) > 0:
            return float(php)
    return None


def _try_binance_p2p():
    """Fallback: Binance public ticker for USDTPHP if available."""
    data = _fetch_json(
        "https://api.binance.com/api/v3/ticker/price?symbol=USDTPHP"
    )
    if data and data.get("price"):
        rate = float(data["price"])
        if rate > 0:
            return rate
    return None


def format_php(usdt_amount, rate=None):
    """Convert USDT to PHP and format as string."""
    if rate is None:
        rate = get_usdt_php_rate()
    return "PHP {:,.2f}".format(usdt_amount * rate)
