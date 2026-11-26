"""Hard rules and paper-order building, offline."""
import json
import os
from gemini_tradingagents import desk, rules, paper


def plan(e=100, s=95, t=112.5, d="long"):
    return {"decision": d, "entry": e, "stop": s, "target": t}


def calc(qty=20, rr=2.5, atr=1.0, pos=20.0, gap=0.1):
    return {"quantity": qty, "reward_risk": rr, "stop_in_atr": atr, "position_pct_capital": pos, "entry_vs_last_pct": gap, "position_usd": 2000}


def test_rules():
    assert rules.check(plan(), calc(), 10_000)[0] == "GO"
    assert rules.check(plan(), calc(rr=1.2), 10_000)[0] == "NO-GO"
    assert rules.check(plan(s=105), calc(), 10_000)[0] == "NO-GO"              # stop above entry on a long
    v, why, f = rules.check(plan(), calc(pos=40), 10_000)
    assert v == "GO SMALLER" and abs(f - .5) < 1e-9
    assert rules.check(plan(), calc(atr=.2), 10_000)[0] == "NO-GO"
    assert rules.combine("GO", "NO-GO") == "NO-GO" and rules.combine("GO SMALLER", "GO") == "GO SMALLER"
    assert rules.halted(-1.9) is None and "daily loss" in rules.halted(-2.0)
    assert rules.check(plan(), calc(), 10_000, day_pnl_pct=-2.5)[0] == "NO-GO"
    open("KILL", "w").close()
    try:
        assert rules.check(plan(), calc(), 10_000)[0] == "NO-GO"
    finally:
        os.remove("KILL")


def test_paper_orders_two_targets():
    o = {"symbol": "AAPL", "side": "buy", "qty": 7.9, "entry": 355.5, "stop": 341.2, "target_1": 369.8, "target_2": 383.1, "target": 383.1}
    orders, err = paper.build_orders(o)
    assert err is None and [x["qty"] for x in orders] == ["3", "4"]
    assert [x["take_profit"]["limit_price"] for x in orders] == ["369.80", "383.10"]
    assert all(x["order_class"] == "bracket" and x["stop_loss"]["stop_price"] == "341.20" for x in orders)
    assert all(x["time_in_force"] == "gtc" for x in orders)       # stop and target stay live past today's close
    one, _ = paper.build_orders({**o, "qty": 1.2})
    assert len(one) == 1 and one[0]["take_profit"]["limit_price"] == "383.10"      # 1 share: single bracket to Target 2
    assert paper.build_orders({**o, "qty": .4})[0] is None
    assert "paper-api.alpaca.markets" in paper.PAPER_ORDERS_URL and "api.alpaca.markets" not in paper.PAPER_ORDERS_URL.replace("paper-api.", "")


def test_one_percent_risk_and_twenty_percent_cap():
    assert rules.mandate()["max_risk_pct"] == 1.0 and rules.mandate()["max_position_pct"] == 20.0   # the defaults
    m = {"atr14": 2.0, "last_close": 100.0}
    c = desk.sizing({"decision": "long", "entry": 100, "stop": 95, "target": 112.5}, m, 10_000)
    assert abs(c["risk_usd"] - 100) < 1e-9 and abs(c["quantity"] - 20) < 1e-9          # 1% of $10,000 lost at the stop
    assert rules.check({"decision": "long", "entry": 100, "stop": 95, "target": 112.5}, c, 10_000)[0] == "GO"   # exactly 20%: allowed
    tight = {"decision": "long", "entry": 100, "stop": 99, "target": 102.5}
    c = desk.sizing(tight, m, 10_000)                                                       # 100 shares = 100% of capital
    v, _, f = rules.check(tight, c, 10_000)
    assert v == "GO SMALLER" and abs(c["quantity"] * f * 100 / 10_000 - 0.20) < 1e-9       # cut to the 20% cap
    assert c["quantity"] * f * (100 - 99) <= 100                                            # still never more than 1% at risk
