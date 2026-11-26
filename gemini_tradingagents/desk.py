"""One command, four models, three proven repos, one verdict.

gemini-tradingagents AAPL:
  1. Hydrogen -> Vibe-Trading   2. Argon -> ai-hedge-fund   3. Neon -> TradingAgents   4. Carbon (ADK) judge
  then hard Python rules, then a target position (entry, stop, Target 1 at 1R, Target 2) for the optional Alpaca PAPER order.
"""
import json
import os
from datetime import datetime, timezone

from pathlib import Path

from . import brain, data, rules, stages
from .judge import judge
from .roles import model_for


def add_targets(v, fallback_rr=2.5):
    """Two targets from the judge's levels: Target 1 at 1R, Target 2 = the judge's target (or 2.5R if it gave none).
    The reward:risk rule then checks Target 2."""
    try:
        e, s = float(v["entry"]), float(v["stop"])
    except (KeyError, TypeError, ValueError):
        return v
    if v.get("decision") not in ("long", "short") or e == s:
        return v
    sg, r = (1 if v["decision"] == "long" else -1), abs(e - s)
    try:
        t2 = float(v["target"])
    except (KeyError, TypeError, ValueError):
        t2 = e + sg * fallback_rr * r
    v["target_1"], v["target_2"], v["target"] = round(e + sg * r, 6), t2, t2
    return v


def sizing(plan, m, capital):
    """Position size and reward:risk computed in Python from the judge's levels."""
    try:
        e, s, t = float(plan["entry"]), float(plan["stop"]), float(plan["target"])
    except (KeyError, TypeError, ValueError):
        return {}
    risk_usd = capital * rules.mandate()["max_risk_pct"] / 100
    per_unit = abs(e - s)
    if not per_unit:
        return {}
    qty = risk_usd / per_unit
    return {"risk_usd": risk_usd, "risk_per_unit": per_unit, "quantity": qty, "position_usd": qty * e,
            "position_pct_capital": qty * e / capital * 100, "reward_risk": abs(t - e) / per_unit,
            "stop_in_atr": per_unit / m["atr14"] if m.get("atr14") else None, "entry_vs_last_pct": (e / m["last_close"] - 1) * 100}


def data_pack(sym, mtxt, vibe, out_dir, stamp, plan_txt=""):
    """One pack for every later stage: Python metrics (Yahoo, as-of date inside) + Vibe-Trading's output. Saved as JSON and Markdown."""
    pack = {"symbol": sym, "neon_plan": plan_txt, "python_metrics": {"source": "Yahoo Finance chart endpoint, computed by gemini-tradingagents", "text": mtxt},
            "vibe_trading": {"model": vibe.get("model"), "status": vibe["status"], "output": vibe["output"]}}
    md = f"# Data pack {sym}\n\n## Neon plan\n{plan_txt}\n\n## Python metrics (Yahoo Finance)\n```\n{mtxt}\n```\n\n## Vibe-Trading ({vibe.get('model')}, {vibe['status']})\n{vibe['output'] or '(no output)'}\n"
    out = Path(out_dir)
    (out / f"{sym}-{stamp}-datapack.json").write_text(json.dumps(pack, indent=1), encoding="utf-8")
    (out / f"{sym}-{stamp}-datapack.md").write_text(md, encoding="utf-8")
    return md


DEBATE = """You are an investment committee in one pass: a bull advocate, a bear advocate, a risk officer and a
portfolio manager. Asset: {sym}. Debate the lead analyst's thesis against the data pack. Use only numbers in the pack.
Write: BULL (3 points), BEAR (3 points), RISK (3 points with numbers), PM DECISION (long / short / no trade, size
small / normal / none, and why). Under 350 words.

LEAD ANALYST PLAN
{plan}

DATA PACK
{pack}"""


def argon_debate(llm, sym, plan_txt, pack, dry_run=False):
    """--fast: one Argon call instead of Vibe-Trading's 4-agent committee."""
    model = model_for("analyst")
    try:
        text = llm.ask("committee (one call)", model, "You run a buy-side investment committee.",
                       DEBATE.format(sym=sym, plan=plan_txt, pack=pack[:16_000]))
        return {"stage": "Investment committee (one Argon call)", "status": "ok", "model": model, "command": "", "output": text, "stderr": ""}
    except Exception as e:
        return {"stage": "Investment committee (one Argon call)", "status": "failed", "model": model, "command": "", "output": "", "stderr": str(e)[:600]}


def run(llm, sym, out_dir, capital=10_000, dry_run=False, only=None, deep=False, fast=False, day_pnl_pct=0.0):
    sym = sym.upper()
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    m = data.metrics(data.daily_bars(sym))
    mtxt = data.fmt_metrics(m)
    todo = only or ["brain", "vibe", "committee"] + (["aihf"] if stages.has_aihf_data() else [])
    st = []
    # 1. Neon is the brain and runs first: thesis + test plan
    if "brain" in todo:
        plan, brain_stage = brain.think(llm, sym, mtxt, dry_run)
        st.append(brain_stage)
    else:
        plan = dict(brain.DEFAULT_PLAN)
    ptxt = brain.plan_text(plan)
    # 2. Hydrogen backtests Neon's plan, 3. Argon's committee debates, at the same time
    from concurrent.futures import ThreadPoolExecutor
    iters = os.environ.get("GD_VIBE_MAX_ITER_FAST", "4") if fast else None
    with ThreadPoolExecutor(2) as ex:
        f_vibe = ex.submit(stages.vibe_trading, sym, dry_run, deep, ptxt, iters) if "vibe" in todo else None
        f_com = ex.submit(stages.committee, sym, dry_run) if "committee" in todo and not fast else None
        vibe = f_vibe.result() if f_vibe else {"stage": "Vibe-Trading", "status": "not run", "output": "", "model": ""}
        com = f_com.result() if f_com else None
    if "vibe" in todo:
        st.append(vibe)
    pack_md = data_pack(sym, mtxt, vibe, out_dir, stamp, ptxt)
    if com:
        st.append(com)
    if fast and "committee" in todo:
        st.append(argon_debate(llm, sym, ptxt, pack_md, dry_run))
    if "aihf" in todo:
        st.append(stages.ai_hedge_fund(sym, out_dir, dry_run))
    mand = rules.mandate()
    try:
        verdict, engine = judge(llm, sym, mtxt, st, mand, dry_run)
    except Exception as e:
        verdict, engine = {"verdict": "NO-GO", "decision": "no trade", "reasons": [f"judge failed: {str(e)[:300]}"]}, "failed"
    verdict = add_targets(verdict)
    calc = sizing(verdict, m, capital)
    rv, rreasons, factor = rules.check(verdict, calc, capital, day_pnl_pct)
    if calc and factor < 1:
        for k in ("quantity", "position_usd", "position_pct_capital"):
            calc[k] *= factor
    final = rules.combine(rv, verdict.get("verdict", "NO-GO"))
    order = None
    if final in ("GO", "GO SMALLER") and calc:
        order = {"symbol": sym, "side": "buy" if verdict["decision"] == "long" else "sell", "qty": round(calc["quantity"], 6),
                 "entry": float(verdict["entry"]), "stop": float(verdict["stop"]), "target_1": verdict["target_1"], "target_2": verdict["target_2"],
                 "target": verdict["target_2"], "verdict": final}
    return {"plan_txt": ptxt, "symbol": sym, "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "metrics_txt": mtxt,
            "stages": st, "judge": verdict, "judge_engine": engine, "calc": calc,
            "rules": {"verdict": rv, "reasons": rreasons, "size_factor": factor}, "final": final, "order": order}


def report_md(r, usage_table):
    b = lambda xs: "\n".join(f"- {x}" for x in (xs or [])) or "- (none)"
    j = r["judge"]
    calc = "\n".join(f"- {k.replace('_', ' ')}: {v:,.2f}" for k, v in r["calc"].items() if isinstance(v, (int, float))) or "- no complete levels"
    sec = []
    for s in r["stages"]:
        sec.append(f"""### {s['stage']} · {s.get('model', '')} · {s['status']}
Command: `{s['command'] or '(not run)'}`

```
{(s['output'] or s.get('stderr') or '(no output)')[:6000]}
```""")
    return f"""# {r['symbol']} · gemini-tradingagents

{r['generated']}. Research and paper trading only. Not financial advice.

## Final verdict: **{r['final']}**
Hard rules (Python): **{r['rules']['verdict']}**
{b(r['rules']['reasons'])}

Judge ({model_for('judge')}, {r['judge_engine']}): **{j.get('verdict', 'n/a')}**, {j.get('decision', 'n/a')} · entry {j.get('entry')} · stop {j.get('stop')} · Target 1 {j.get('target_1')} · Target 2 {j.get('target_2')}
{b(j.get('reasons'))}

Where the systems disagree:
{b(j.get('conflicts'))}

Computed in Python:
{calc}

## Target position (input for `--paper`)
```
{json.dumps(r['order'], indent=1) if r['order'] else 'none: verdict is ' + r['final']}
```

## Neon's thesis and test plan
{r['plan_txt']}

## What each system said (in pipeline order)
""" + "\n\n".join(sec) + f"""

## Price metrics (Python)
```
{r['metrics_txt']}
```

## Cost
{tool_cost_table(r['stages'])}

Direct Gemini calls by gemini-tradingagents:
{usage_table}

List prices per 1M tokens: Hydrogen $1.50/$7.50, Argon and Carbon $2/$10 (until Nov 30 2026), Neon $3/$15. Cached input is billed lower by Google, so real cost is at or below these numbers. On the free tier (Hydrogen) the actual cost is $0.
"""


def tool_cost_table(stages_):
    from .roles import PRICES
    rows = ["| Stage | Model | Calls | Input tokens | Cached | Output tokens | List price (USD) |", "|---|---|---|---|---|---|---|"]
    for s in stages_:
        u = s.get("usage")
        if not u:
            continue
        pin, pout = PRICES.get(s.get("model", ""), (0, 0))
        usd = u["input_tokens"] / 1e6 * pin + u["output_tokens"] / 1e6 * pout
        rows.append(f"| {s['stage']} | {s.get('model', '')} | {u.get('calls', '')} | {u['input_tokens']:,} | {u.get('cache_read_tokens', 0):,} | {u['output_tokens']:,} | {usd:.3f} |")
    return "\n".join(rows) if len(rows) > 2 else "(no tool usage reported)"
