"""screen: pick which symbols the bot researches today. Plain technical filters computed in Python: no model, no cost.

Universe: mandates/universe.txt (or GD_UNIVERSE). A symbol passes when it is priced above GD_SCREEN_MIN_PRICE, trades
at least GD_SCREEN_MIN_DOLLAR_VOLUME a day, is in an uptrend (close > SMA50 > SMA200), is not overbought or washed out
(RSI14 inside GD_SCREEN_RSI_MIN..MAX) and is not wild (20-day volatility under GD_SCREEN_MAX_VOL_PCT). Passing symbols
are ranked by their 60-day change and the top GD_SCREEN_TOP go to the bot.
"""
import os
import time
from pathlib import Path

from . import data

UNIVERSE = Path(__file__).resolve().parent.parent / "mandates" / "universe.txt"


def screen_rules():
    f = lambda k, d: float(os.environ.get(k, d))
    return {"top": int(f("GD_SCREEN_TOP", 3)), "min_price": f("GD_SCREEN_MIN_PRICE", 5.0),
            "min_dollar_volume": f("GD_SCREEN_MIN_DOLLAR_VOLUME", 20e6), "rsi_min": f("GD_SCREEN_RSI_MIN", 40.0),
            "rsi_max": f("GD_SCREEN_RSI_MAX", 70.0), "max_vol": f("GD_SCREEN_MAX_VOL_PCT", 60.0) / 100}


def universe():
    p = Path(os.environ.get("GD_UNIVERSE", UNIVERSE))
    return [t for t in (ln.split("#")[0].strip().upper() for ln in p.read_text(encoding="utf-8").splitlines()) if t]


def why_not(m, r):
    """The first rule a symbol fails, or None if it passes. NaN (too little history) fails every comparison."""
    if not m["last_close"] >= r["min_price"]:
        return f"price {m['last_close']:.2f} < {r['min_price']:g}"
    if not m["avg_volume_20d"] * m["last_close"] >= r["min_dollar_volume"]:
        return f"illiquid: ${m['avg_volume_20d'] * m['last_close'] / 1e6:.1f}M a day"
    if not m["last_close"] > m["sma50"] > m["sma200"]:
        return "no uptrend (close > SMA50 > SMA200)"
    if not r["rsi_min"] <= m["rsi14"] <= r["rsi_max"]:
        return f"RSI {m['rsi14']:.0f} outside {r['rsi_min']:g}-{r['rsi_max']:g}"
    if not m["vol20_ann"] <= r["max_vol"]:
        return f"volatility {m['vol20_ann']:.0%} > {r['max_vol']:.0%}"
    return None


def screen(symbols=None, bars=None, pause=0.3):
    """Returns (picks, rows): today's symbols for the bot, and one (symbol, 60-day change, failed rule or None) per symbol."""
    r, bars = screen_rules(), bars or data.daily_bars
    rows = []
    for i, s in enumerate(symbols or universe()):
        if i:
            time.sleep(pause)  # gentle with Yahoo's free endpoint
        try:
            m = data.metrics(bars(s))
        except Exception as e:  # one bad ticker (delisted, renamed, rate-limited) must not stop the screen
            rows.append((s, float("nan"), f"no data ({type(e).__name__})"))
            continue
        rows.append((s, m["chg_60d"], why_not(m, r)))
    passed = sorted((x for x in rows if x[2] is None), key=lambda x: -x[1])
    return [x[0] for x in passed[:r["top"]]], rows


def report(picks, rows):
    """Print the screen, best first; returns the picks."""
    for s, chg, why in sorted(rows, key=lambda x: (x[2] is not None, -(x[1] if x[1] == x[1] else -1e9))):
        tag = "PICK" if s in picks else ("pass" if why is None else why)
        print(f"  {s:6s} 60d {'  n/a ' if chg != chg else f'{chg:+6.1%}'}  {tag}")
    print(f"Screen picks ({len(picks)}): {', '.join(picks) or 'none'}")
    return picks
