"""Bot pass over a watchlist with a fake Alpaca PAPER account and fake research, offline."""
from gemini_tradingagents import bot as bot_mod, paper, stages

ORDER = {"symbol": "X", "side": "buy", "qty": 7.9, "entry": 100.0, "stop": 95.0, "target_1": 105.0, "target_2": 112.5, "target": 112.5}
PRE_OPEN = {"timestamp": "2026-11-27T08:00:00-05:00", "is_open": False, "next_open": "2026-11-27T09:30:00-05:00"}
WEEKEND = {"timestamp": "2026-11-28T08:00:00-05:00", "is_open": False, "next_open": "2026-11-30T09:30:00-05:00"}


def fake_account(mp, calls, equity="100000", positions=(), orders=(), clock=PRE_OPEN):
    mp.setenv("ALPACA_KEY", "test-key")
    mp.setenv("ALPACA_SECRET", "test-secret")
    replies = {"/clock": clock, "/account": {"equity": equity, "last_equity": "100000"}, "/positions": list(positions),
               "/orders?status=open&nested=true&limit=500": list(orders)}

    def api(method, path, body=None):
        calls.append((method, path, body))
        return replies.get(path) if method == "GET" else {"id": "new", "status": "accepted"}
    mp.setattr(paper, "api", api)


def fake_research(seen, go=("AAPL", "NVDA")):
    def research(sym, capital, pnl):
        seen.append((sym, capital, pnl))
        return {"order": {**ORDER, "symbol": sym} if sym in go else None, "final": "GO" if sym in go else "NO-GO"}
    return research


def test_one_pass(monkeypatch):
    calls, seen = [], []
    fake_account(monkeypatch, calls, positions=[{"symbol": "MSFT"}],
                 orders=[{"id": "old", "symbol": "NVDA", "order_class": "bracket"}, {"id": "mine", "symbol": "NVDA", "order_class": "simple"}])
    code = bot_mod.bot(["aapl", "MSFT", "NVDA", "TSLA", "BTCUSDT", "AAPL"], fake_research(seen))
    assert code == 0
    assert [s[0] for s in seen] == ["AAPL", "NVDA", "TSLA"]          # MSFT held, crypto skipped, duplicate AAPL once
    assert all(s[1] == 100_000.0 for s in seen)                      # sized on the account's equity
    assert ("DELETE", "/orders/old", None) in calls                  # stale bracket entry canceled...
    assert not any(c[1] == "/orders/mine" for c in calls)            # ...a hand-placed order is left alone
    posts = [c[2] for c in calls if c[0] == "POST"]
    assert [p["symbol"] for p in posts] == ["AAPL", "AAPL", "NVDA", "NVDA"]   # 7 shares: 3 to Target 1, 4 to Target 2
    assert all(p["time_in_force"] == "gtc" and p["order_class"] == "bracket" for p in posts)


def test_daily_loss_stops_before_research(monkeypatch):
    calls, seen = [], []
    fake_account(monkeypatch, calls, equity="97000")                 # -3% today, limit is 2%
    assert bot_mod.bot(["AAPL"], fake_research(seen)) == 0
    assert seen == [] and not any(c[0] == "POST" for c in calls)


def test_market_closed_does_nothing(monkeypatch):
    calls, seen = [], []
    fake_account(monkeypatch, calls, clock=WEEKEND)
    assert bot_mod.bot(["AAPL"], fake_research(seen)) == 0
    assert seen == [] and [c[1] for c in calls] == ["/clock"]


def test_dry_run_needs_no_account(monkeypatch):
    monkeypatch.setattr(paper, "api", lambda *a, **k: (_ for _ in ()).throw(AssertionError("dry run called Alpaca")))
    seen = []
    assert bot_mod.bot(["AAPL"], fake_research(seen), dry_run=True) == 0 and seen[0][0] == "AAPL"


def test_tools_never_see_broker_keys(monkeypatch):
    monkeypatch.setenv("ALPACA_KEY", "test-key")
    monkeypatch.setenv("ALPACA_SECRET", "test-secret")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini")
    env = stages.tool_env(EXTRA="1")
    assert not any(k.upper().startswith("ALPACA") for k in env)
    assert env["GEMINI_API_KEY"] == "test-gemini" and env["EXTRA"] == "1"
