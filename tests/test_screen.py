"""Screener on synthetic price series, offline."""
from datetime import date, timedelta
from gemini_tradingagents import screen


def series(drift, volume=1e6, n=300, base=50.0):
    """Zigzag around a trend: closes alternate +/-0.5 so RSI stays near 50 instead of pinning at 0 or 100."""
    out = []
    for i in range(n):
        c = base + drift * i + (0.5 if i % 2 else -0.5)
        out.append(((date(2025, 1, 1) + timedelta(i)).isoformat(), c, c + 0.3, c - 0.3, c, volume))
    return out


BARS = {"UP": series(0.05), "FAST": series(0.08), "DOWN": series(-0.05, base=80), "THIN": series(0.05, volume=1000),
        "NEW": series(0.05, n=120)}


def fake_bars(s):
    if s == "GONE":
        raise KeyError("delisted")
    return BARS[s]


def test_picks_uptrends_ranked_by_60d_change():
    picks, rows = screen.screen(["UP", "DOWN", "FAST", "THIN", "NEW", "GONE"], bars=fake_bars, pause=0)
    assert picks == ["FAST", "UP"]                                    # both pass; faster 60-day gain first
    why = {s: w for s, _, w in rows}
    assert why["DOWN"].startswith("no uptrend") and why["THIN"].startswith("illiquid")
    assert why["NEW"].startswith("no uptrend")                         # 120 bars: no SMA200 yet, never passes
    assert why["GONE"] == "no data (KeyError)"                         # one bad ticker does not stop the screen


def test_top_n_and_rsi_rules(monkeypatch):
    monkeypatch.setenv("GD_SCREEN_TOP", "1")
    assert screen.screen(["UP", "FAST"], bars=fake_bars, pause=0)[0] == ["FAST"]
    monkeypatch.setenv("GD_SCREEN_RSI_MAX", "45")                      # zigzag RSI is ~52: now too high
    picks, rows = screen.screen(["UP"], bars=fake_bars, pause=0)
    assert picks == [] and rows[0][2].startswith("RSI")


def test_universe_file(tmp_path, monkeypatch):
    f = tmp_path / "u.txt"
    f.write_text("# mine\naapl\n\n msft  # big\n", encoding="utf-8")
    monkeypatch.setenv("GD_UNIVERSE", str(f))
    assert screen.universe() == ["AAPL", "MSFT"]
    monkeypatch.delenv("GD_UNIVERSE")
    assert len(screen.universe()) == 30                                # the bundled starting list
