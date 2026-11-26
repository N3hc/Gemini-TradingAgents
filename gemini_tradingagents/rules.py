"""Hard risk rules in plain Python. They run before the Carbon risk manager and win over it:
the model can tighten (veto, cut size) but can never loosen a rule.

Idea from kweinmeister/agentic-trading (Apache-2.0, RiskGuard + rules.py) and HKUDS/Vibe-Trading (MIT, mandate limits
+ kill switch). Reimplemented, no code copied.
"""
import os
from pathlib import Path

ORDER = {"NO-GO": 0, "GO SMALLER": 1, "GO": 2}


def mandate():
    f = lambda k, d: float(os.environ.get(k, d))
    return {
        "max_risk_pct": f("GD_MAX_RISK_PCT", 1.0),          # loss at stop, % of capital
        "max_position_pct": f("GD_MAX_POSITION_PCT", 20.0),  # position value, % of capital
        "min_reward_risk": f("GD_MIN_REWARD_RISK", 1.5),
        "min_stop_atr": f("GD_MIN_STOP_ATR", 0.5),           # stop must be at least this many ATRs away
        "max_entry_gap_pct": f("GD_MAX_ENTRY_GAP_PCT", 3.0),  # entry no further than this from last close
        "max_daily_loss_pct": f("GD_MAX_DAILY_LOSS_PCT", 2.0),
        "kill_switch_file": os.environ.get("GD_KILL_SWITCH", "KILL"),
    }


def halted(day_pnl_pct=0.0):
    """Why every new trade is blocked today (kill switch file, daily loss limit), or None."""
    m = mandate()
    if Path(m["kill_switch_file"]).exists():
        return f"kill switch file '{m['kill_switch_file']}' exists: all new trades blocked"
    if day_pnl_pct <= -m["max_daily_loss_pct"]:
        return f"daily loss {day_pnl_pct:.2f}% hit the {m['max_daily_loss_pct']}% limit"
    return None


def check(plan, calc, capital, day_pnl_pct=0.0):
    """Returns (verdict, reasons, size_factor). size_factor < 1 means 'cut the position to this fraction'."""
    m = mandate()
    reasons, verdict, factor = [], "GO", 1.0
    stop = halted(day_pnl_pct)
    if stop:
        return "NO-GO", [stop], 0.0
    if plan.get("decision") not in ("long", "short"):
        return "NO-GO", ["strategist chose no trade"], 0.0
    if not calc:
        return "NO-GO", ["plan has no complete entry/stop/target, so risk cannot be computed"], 0.0
    e, s, t = float(plan["entry"]), float(plan["stop"]), float(plan["target"])
    side_ok = (s < e < t) if plan["decision"] == "long" else (t < e < s)
    if not side_ok:
        return "NO-GO", [f"levels do not fit a {plan['decision']}: entry {e}, stop {s}, target {t}"], 0.0
    rr = calc.get("reward_risk") or 0
    if rr < m["min_reward_risk"]:
        verdict = "NO-GO"
        reasons.append(f"reward:risk {rr:.2f} < minimum {m['min_reward_risk']}")
    st = calc.get("stop_in_atr")
    if st is not None and st < m["min_stop_atr"]:
        verdict = "NO-GO"
        reasons.append(f"stop is {st:.2f} ATR away, inside normal noise (min {m['min_stop_atr']} ATR)")
    if abs(calc.get("entry_vs_last_pct", 0)) > m["max_entry_gap_pct"]:
        verdict = "NO-GO"
        reasons.append(f"entry is {calc['entry_vs_last_pct']:+.2f}% from last close (max {m['max_entry_gap_pct']}%)")
    pos = calc.get("position_pct_capital", 0)
    if pos > m["max_position_pct"]:
        factor = m["max_position_pct"] / pos
        if verdict == "GO":
            verdict = "GO SMALLER"
        reasons.append(f"position {pos:.1f}% of capital > cap {m['max_position_pct']}%: cut to {factor:.0%} of size")
    if not reasons:
        reasons.append("all hard rules pass")
    return verdict, reasons, factor


def combine(rule_verdict, model_verdict):
    """The stricter verdict wins."""
    a, b = ORDER.get(rule_verdict, 0), ORDER.get(model_verdict, 0)
    return rule_verdict if a <= b else model_verdict
