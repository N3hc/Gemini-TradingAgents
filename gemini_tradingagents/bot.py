"""bot: the desk on autopilot, Alpaca PAPER only. One pass over a watchlist; schedule it once a day before the open.

For each symbol: an open position is left alone (its bracket's stop and take-profit manage the exit), an unfilled
bracket entry from an older run is canceled (that research is stale), then fresh research runs and a GO / GO SMALLER
sends new bracket orders sized on the account's equity. The kill switch and the daily loss limit stop the whole pass
before any model is paid for.
"""
import urllib.error

from . import paper, rules


def account():
    """(equity, today's P&L in %, symbols with a position, open bracket entries) from the Alpaca PAPER account."""
    a = paper.api("GET", "/account")
    equity, last = float(a["equity"]), float(a["last_equity"])  # last_equity: equity at the previous close
    held = {p["symbol"] for p in paper.api("GET", "/positions")}
    orders = paper.api("GET", "/orders?status=open&nested=true&limit=500")  # nested: legs sit under their entry
    return equity, (equity / last - 1) * 100 if last else 0.0, held, orders


def market_day():
    """True while the market is open or opens later today (orders sent before the open wait for it)."""
    c = paper.api("GET", "/clock")
    return c["is_open"] or c["next_open"][:10] == c["timestamp"][:10]  # both timestamps are New York time


def bot(symbols, research, dry_run=False):
    """research(symbol, capital, day_pnl_pct) -> run dict (cli.research). Returns the exit code."""
    if dry_run:  # no keys, no account: show what one pass would do
        capital, pnl, held, entries = 10_000.0, 0.0, set(), []
    else:
        if not paper.has_keys():
            return 1
        if not market_day():
            print("Market closed today: nothing to do.")
            return 0
        capital, pnl, held, entries = account()
    print(f"Bot: {len(symbols)} symbol(s), capital {capital:,.2f}, today {pnl:+.2f}%")
    stop = rules.halted(pnl)
    if stop:
        print("Bot stopped: " + stop)
        return 0
    # shortcut: no cap on total exposure across symbols (each position is capped at 20%); Alpaca rejects what buying
    # power can't cover. Add a gross-exposure rule if the watchlist grows past ~5 symbols.
    failed = 0
    for sym in dict.fromkeys(s.upper() for s in symbols):
        if paper.is_crypto(sym):
            print(f"{sym}: crypto, Alpaca paper brackets are stocks-only: skipped")
            continue
        if sym in held:
            print(f"{sym}: position open, its stop and take-profit manage the exit: skipped")
            continue
        try:
            for o in entries:
                if o["symbol"] == sym and o.get("order_class") == "bracket":
                    paper.api("DELETE", f"/orders/{o['id']}")  # canceling the entry cancels its legs
                    print(f"{sym}: canceled unfilled entry {o['id']} from an older run")
        except urllib.error.HTTPError as e:  # e.g. it filled a moment ago: never stack a second trade on it
            print(f"{sym}: could not cancel an older entry ({e.code}): skipped")
            failed += 1
            continue
        print(f"--- {sym}")
        try:
            r = research(sym, capital, pnl)
        except Exception as e:  # one bad symbol (no data, API error) must not stop the others
            print(f"{sym}: research failed: {str(e)[:300]}")
            failed += 1
            continue
        orders, err = paper.build_orders(r["order"]) if r["order"] else (None, f"verdict {r['final']}: no order")
        if err:
            print(f"{sym}: {err}")
        elif dry_run:
            print(f"{sym}: would send {len(orders)} bracket order(s) (dry run)")
        elif not paper.send_orders(orders):
            failed += 1
    return 1 if failed else 0
