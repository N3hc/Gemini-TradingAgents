"""The three proven repos, each driven by one Gemini 4 model. gemini-tradingagents only calls their CLIs; it copies none of their code.

  Hydrogen -> HKUDS/Vibe-Trading (MIT)                 data pack: prices, fundamentals, news, backtest, walk-forward
  Argon    -> Vibe-Trading investment_committee preset   bull vs bear debate, risk officer, portfolio manager
  Neon     -> TauricResearch/TradingAgents (Apache-2.0) deep research on the data pack (no Tauric key needed)
  optional -> virattt/ai-hedge-fund (MIT)               investor committee, only if FINANCIAL_DATASETS_API_KEY is set
"""
import json
import os
import platform
import shutil
import subprocess
from datetime import date, timedelta
from pathlib import Path

from .roles import model_for

ROOT = Path(__file__).resolve().parent.parent
TIMEOUT = int(os.environ.get("GD_STAGE_TIMEOUT", "900"))
KEEP = 14_000  # characters of each tool's output kept for the judge and the report


def wsl_ready():
    """True if WSL has at least one Linux distribution installed."""
    if not shutil.which("wsl"):
        return False
    try:
        p = subprocess.run(["wsl", "-l", "-q"], capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    raw = p.stdout or b""
    txt = raw.decode("utf-16-le" if b"\x00" in raw else "utf-8", errors="ignore")
    return p.returncode == 0 and any(line.strip() for line in txt.splitlines())


def _key():
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""


SECRETS = ("ALPACA_KEY", "ALPACA_SECRET")  # broker keys stay in this process: no outside tool needs them


def tool_env(**extra):
    """Environment for the outside tools: this process's environment minus the broker keys, plus `extra`."""
    return {**{k: v for k, v in os.environ.items() if k.upper() not in SECRETS}, **extra}


def _run(name, cmd, env_extra, dry_run, cwd=None):
    shown = " ".join(f'"{c}"' if " " in c else c for c in cmd)
    if dry_run:
        return {"stage": name, "status": "dry-run", "command": shown, "output": "", "stderr": ""}
    from .certs import env as ca_env
    env = tool_env(**ca_env(), **env_extra)
    try:
        p = subprocess.run(cmd, env=env, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=TIMEOUT)
    except FileNotFoundError:
        return {"stage": name, "status": "not installed", "command": shown, "output": "", "stderr": "run install.ps1 / install.sh first"}
    except subprocess.TimeoutExpired:
        return {"stage": name, "status": f"timed out after {TIMEOUT}s", "command": shown, "output": "", "stderr": ""}
    status = "ok" if p.returncode == 0 else f"exit {p.returncode}"
    err = p.stderr[-3000:]
    if "429" in (p.stdout + p.stderr):
        status = "quota (429)"
        err = (f"{env_extra.get('LANGCHAIN_MODEL_NAME', 'this model')}: Google returned 429 (free-tier quota or no billing). "
               "Enable billing in AI Studio for full runs.\n") + err
    return {"stage": name, "status": status, "command": shown, "output": p.stdout[-KEEP:], "stderr": err}


# 1. Hydrogen -> Vibe-Trading -------------------------------------------------------------------------
VIBE_PROMPT_FAST = ("Data pack for {sym}, keep it short. Load 1 year of daily prices; report last close, 20/60/250-day "
                    "returns, volatility and max drawdown. Backtest buy-and-hold plus the strategies in the PLAN below, with "
                    "fees, and run a walk-forward check (3 windows); report CAGR, Sharpe, max drawdown and trade count for "
                    "each. List up to 5 news headlines from the last 14 days with date and URL. Source and as-of date for "
                    "every number; only numbers your tools returned. Under 400 words.\n\nPLAN (from the desk's lead analyst)\n{plan}")
VIBE_PROMPT = ("Build a data pack for {sym}. 1) Load 2 years of daily prices; report last close, 20/60/250-day returns, "
               "volatility, max drawdown. 2) Load the latest financial statements and key ratios (revenue, margins, EPS, "
               "debt, free cash flow) if the asset has them. 3) List news headlines from the last 14 days with date and URL. "
               "4) Backtest buy-and-hold and each strategy in the PLAN below with realistic fees; run walk-forward validation "
               "(5 windows), a Monte Carlo test and a bootstrap Sharpe confidence interval; report CAGR, Sharpe, max "
               "drawdown, win rate and trade count for each. Give the source and as-of date for every number. "
               "Use only numbers your tools returned.\n\nPLAN (from the desk's lead analyst)\n{plan}")


def vibe_home():
    """Every vibe call uses <repo>/.vibe-home, so run and swarm folders are at known paths."""
    return Path(os.environ.get("VIBE_TRADING_HOME", ROOT / ".vibe-home"))


TRACE_READER = (
    "import json,sys\n"
    "from src.agent.trace import TraceWriter\n"
    "d=TraceWriter.find_trace_dir(sys.argv[1])\n"
    "e=TraceWriter.read(d, resolve_offloads=True, resolve_fields={'content'})\n"
    "a=[x['content'] for x in e if x.get('type')=='answer' and x.get('content')]\n"
    "print(json.dumps(a[-1] if a else ''))\n")


def _vibe_trace_answer(run_id):
    """The answer text lives in Vibe-Trading's trace; read it with Vibe's own TraceWriter in its own venv."""
    py = Path(vibe_exe()).with_name("python.exe" if os.name == "nt" else "python")
    if not py.exists():
        return ""
    try:
        p = subprocess.run([str(py), "-c", TRACE_READER, run_id], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60, env=tool_env(VIBE_TRADING_HOME=str(vibe_home())))
        return json.loads(p.stdout.strip().splitlines()[-1]) if p.returncode == 0 and p.stdout.strip() else ""
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, IndexError):
        return ""


def _vibe_answer(stdout, since):
    """vibe-trading --json prints only {status, run_id, run_dir, reason}. The answer is the last trace entry of type
    "answer"; backtest metrics are in <run_dir>/artifacts/metrics.csv. Falls back to the newest session transcript."""
    for line in reversed(stdout.strip().splitlines()):
        try:
            j = json.loads(line)
        except json.JSONDecodeError:
            continue
        ans = _vibe_trace_answer(j["run_id"]) if j.get("run_id") else ""
        mfile = Path(j.get("run_dir") or "") / "artifacts" / "metrics.csv"
        if mfile.is_file():
            ans += "\n\nBacktest metrics (artifacts/metrics.csv):\n" + mfile.read_text(encoding="utf-8", errors="ignore")[:4000]
        if ans.strip():
            return ans
        break
    sessions = sorted((p for p in (vibe_home() / "sessions").glob("*/transcript_*.jsonl") if p.stat().st_mtime >= since),
                      key=lambda p: p.stat().st_mtime)
    for t in reversed(sessions):
        for line in reversed(t.read_text(encoding="utf-8", errors="ignore").splitlines()):
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            if m.get("role") == "assistant" and isinstance(m.get("content"), str) and m["content"].strip():
                return m["content"]
    return ""


def vibe_usage(since):
    """Tokens Vibe-Trading reported for runs started after `since` (its own llm_usage.json)."""
    tot = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "calls": 0}
    for f in (vibe_home() / "runs").glob("*/llm_usage.json"):
        if f.stat().st_mtime >= since:
            t = json.loads(f.read_text(encoding="utf-8")).get("totals", {})
            for k in tot:
                tot[k] += int(t.get(k, 0) or 0)
    for f in (vibe_home() / "swarm" / "runs").glob("*/run.json"):
        if f.stat().st_mtime >= since:
            r = json.loads(f.read_text(encoding="utf-8"))
            tot["input_tokens"] += int(r.get("total_input_tokens") or 0)
            tot["output_tokens"] += int(r.get("total_output_tokens") or 0)
    return tot


def vibe_trading(sym, dry_run=False, deep=False, plan="(no plan: test a 20/50 SMA crossover)", iters=None):
    import time
    since = time.time() - 1
    iters = str(iters or os.environ.get("GD_VIBE_MAX_ITER", "30" if deep else "10"))
    prompt = (VIBE_PROMPT if deep else VIBE_PROMPT_FAST).format(sym=sym, plan=plan)
    cmd = [vibe_exe(), "--no-rich", "-p", prompt, "--max-iter", iters, "--json"]
    env = {"LANGCHAIN_PROVIDER": "gemini", "LANGCHAIN_MODEL_NAME": model_for("collector"), "GEMINI_API_KEY": _key(),
           "VIBE_TRADING_HOME": str(vibe_home())}
    r = _run("Vibe-Trading", cmd, env, dry_run)
    r["model"] = model_for("collector")
    if not dry_run:
        r["output"] = (_vibe_answer(r["output"], since) or r["output"])[-KEEP:]
        r["usage"] = vibe_usage(since)
    return r


# 2. Argon -> Vibe-Trading investment committee ---------------------------------------------------------
def committee(sym, dry_run=False):
    import time
    since = time.time() - 1
    market = "crypto" if sym.upper().endswith(("USDT", "-USDT", "-USD")) else "US"
    cmd = [vibe_exe(), "--swarm-run", "investment_committee", json.dumps({"target": sym, "market": market})]
    # bundled presets pin no model, so LANGCHAIN_MODEL_NAME decides what every committee agent runs on
    env = {"LANGCHAIN_PROVIDER": "gemini", "LANGCHAIN_MODEL_NAME": model_for("analyst"), "GEMINI_API_KEY": _key(),
           "VIBE_TRADING_HOME": str(vibe_home())}
    r = _run("Investment committee (Vibe-Trading)", cmd, env, dry_run)
    r["model"] = model_for("analyst")
    if not dry_run:
        runs = sorted((vibe_home() / "swarm" / "runs").glob("*/run.json"), key=lambda p: p.stat().st_mtime)
        if runs and runs[-1].stat().st_mtime >= since:
            run = json.loads(runs[-1].read_text(encoding="utf-8"))
            summaries = [f"[{t.get('agent_id', t.get('id'))}] {t['summary']}" for t in run.get("tasks", []) if t.get("summary")]
            if run.get("final_report") or summaries:
                r["output"] = (str(run.get("final_report") or "") + "\n\n" + "\n\n".join(summaries)).strip()[-KEEP:]
            if run.get("status") == "failed":
                errs = [json.loads(t.read_text(encoding="utf-8")).get("error") or "" for t in (runs[-1].parent / "tasks").glob("*.json")]
                msg = next((e for e in errs if "429" in e), next((e for e in errs if e), ""))
                r["status"] = "quota (429)" if "429" in msg else "failed"
                r["stderr"] = ((f"{model_for('analyst')}: Google returned 429 (free-tier quota or no billing). "
                                "Enable billing in AI Studio for full runs.\n") if "429" in msg else "") + msg[:600]
        r["usage"] = vibe_usage(since)
    return r


# optional: Argon -> ai-hedge-fund (needs paid market data) ----------------------------------------------
MANDATE = ROOT / "mandates" / "committee.yaml"


def tool_exe(tool, name):
    """Each tool lives in its own venv under .tools/ (like pipx), because their dependencies clash."""
    for p in (ROOT / ".tools" / tool / "Scripts" / f"{name}.exe", ROOT / ".tools" / tool / "bin" / name):
        if p.exists():
            return str(p)
    return shutil.which(name) or name


def aihf_exe():
    return tool_exe("aihf", "aihf")


def vibe_exe():
    return tool_exe("vibe", "vibe-trading")


def has_aihf_data():
    return bool(os.environ.get("FINANCIAL_DATASETS_API_KEY") or os.environ.get("X402_WALLET_PRIVATE_KEY"))


def ai_hedge_fund(sym, out_dir, dry_run=False):
    model = model_for("analyst")
    if not (os.environ.get("FINANCIAL_DATASETS_API_KEY") or os.environ.get("X402_WALLET_PRIVATE_KEY")) and not dry_run:
        return {"stage": "ai-hedge-fund", "status": "skipped", "model": model, "command": "",
                "output": "", "stderr": "needs FINANCIAL_DATASETS_API_KEY (financialdatasets.ai) or an x402 wallet; see README"}
    out = Path(out_dir) / f"aihf-{sym}.json"
    end = date.today()
    cmd = [aihf_exe(), "--model", model, "backtest", str(MANDATE), "--universe", sym,
           "--start", (end - timedelta(days=365)).isoformat(), "--end", end.isoformat(), "--out", str(out)]
    r = _run("ai-hedge-fund", cmd, {"GOOGLE_API_KEY": _key()}, dry_run)
    r["model"] = model
    if out.exists():
        try:
            r["output"] = json.dumps(json.loads(out.read_text(encoding="utf-8")), indent=1)[:KEEP]
        except json.JSONDecodeError:
            pass
    return r


# 3. Neon -> TradingAgents ------------------------------------------------------------------------------
TA_PROMPT = ("Research {sym} for a 2-6 week swing trade, working from the DATA PACK below (it was fetched minutes ago "
             "by Vibe-Trading and Python; treat it as your source and cite it). Cover price action and volatility, "
             "fundamentals, recent news, what the backtest and its walk-forward check say, and the 3 biggest risks. End "
             "with a bull case, a bear case and the levels that would invalidate each. Do not invent numbers that are "
             "not in the pack or in your own tool results.\n\nDATA PACK\n{pack}")


def neon_prompt(sym, pack):
    return TA_PROMPT.format(sym=sym, pack=pack[:24_000])


def trading_agents(sym, pack="", dry_run=False):
    return trading_agents_raw(neon_prompt(sym, pack), dry_run)


def trading_agents_raw(q, dry_run=False):
    model = model_for("strategist")
    reasoning = os.environ.get("GD_NEON_REASONING", "high")
    args = ["-p", q, "--model", f"google/{model}", "--reasoning", reasoning, "--limit", "question.time=20m"]
    env = {"GEMINI_API_KEY": _key()}
    if os.environ.get("TAURIC_API_KEY"):
        env["TAURIC_API_KEY"] = os.environ["TAURIC_API_KEY"]
    exe = shutil.which("tradingagents")
    if exe or platform.system() != "Windows":
        cmd = [exe or "tradingagents", *args]
    elif not wsl_ready():
        return {"stage": "TradingAgents", "status": "not available", "model": model, "command": "",
                "output": "", "stderr": "Windows without a WSL Linux distribution: run 'wsl --install' once as administrator"}
    else:
        # TradingAgents ships Linux/macOS builds only: on Windows it runs inside WSL. WSLENV passes the keys
        # through the environment, so they never appear on a command line.
        env["WSL_UTF8"] = "1"
        env["WSLENV"] = ":".join(k + "/u" for k in env if k != "WSL_UTF8")
        env["GD_TA_Q"] = q
        env["WSLENV"] += ":GD_TA_Q/u"
        cmd = ["wsl", "-e", "bash", "-lc",
               f'~/.local/bin/tradingagents -p "$GD_TA_Q" --model google/{model} --reasoning {reasoning} --limit question.time=20m']
    r = _run("TradingAgents", cmd, env, dry_run)
    r["model"] = model
    return r
