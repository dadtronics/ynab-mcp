#!/usr/bin/env python3
"""ynab MCP server: lets an AI agent (opencode, pi, Claude, ...) read and manage a YNAB budget.

Stdlib only, MCP over stdio (newline-delimited JSON-RPC 2.0). Talks to https://api.ynab.com/v1.

Token: $YNAB_TOKEN, else ~/.config/ynab/token (chmod 600). Personal Access Token from
YNAB > Account Settings > Developer Settings. YNAB allows 200 requests/hour per token.
Budget: tool arg `budget` (name or id), else $YNAB_BUDGET, else YNAB's "last-used".

Amounts in and out of every tool are in dollars (YNAB stores milliunits). Outflows are negative.

Writes are two-phase: a write tool only stages the change and returns a preview plus a code.
Nothing is sent to YNAB until `confirm` is called with that code, which the agent must only do
after the user has said yes to the preview.
"""
import json, os, secrets, statistics, sys, time, urllib.error, urllib.parse, urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta

API = "https://api.ynab.com/v1"
PROTOCOL = "2025-06-18"
PENDING_TTL = 1800  # staged writes expire after 30 min
CACHE_TTL = 120     # seconds to reuse GET responses (rate limit is 200/hour)

_cache = {}
_pending = {}


def log(*a):
    print("[ynab-mcp]", *a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- API

def token():
    t = os.environ.get("YNAB_TOKEN", "").strip()
    if not t:
        p = os.path.expanduser("~/.config/ynab/token")
        try:
            t = open(p).read().strip()
        except OSError:
            raise RuntimeError(f"No YNAB token: set YNAB_TOKEN or put it in {p} (chmod 600)")
    return t


def api(method, path, body=None, cache=True):
    key = (method, path)
    if method == "GET" and cache and key in _cache and time.time() - _cache[key][0] < CACHE_TTL:
        return _cache[key][1]
    if method != "GET":
        _cache.clear()
    req = urllib.request.Request(API + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token()}",
                                          "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read())["data"]
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read())["error"]
            msg = f"{err.get('name')}: {err.get('detail')}"
        except Exception:
            msg = str(e)
        if e.code == 429:
            msg += " (YNAB rate limit: 200 requests/hour; wait and retry)"
        raise RuntimeError(f"YNAB API {method} {path} failed ({e.code}) {msg}")
    if method == "GET":
        _cache[key] = (time.time(), data)
    return data


def money(milli):
    v = (milli or 0) / 1000
    return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"


def to_milli(dollars):
    return int(round(float(dollars) * 1000))


# ---------------------------------------------------------------- lookups

def budget_id(a):
    want = (a.get("budget") or os.environ.get("YNAB_BUDGET") or "last-used").strip()
    if want in ("last-used", "default"):
        return want
    budgets = api("GET", "/budgets")["budgets"]
    for b in budgets:
        if want == b["id"] or want.lower() == b["name"].lower():
            return b["id"]
    hits = [b for b in budgets if want.lower() in b["name"].lower()]
    if len(hits) == 1:
        return hits[0]["id"]
    raise RuntimeError(f"Budget '{want}' not found. Budgets: " + ", ".join(b["name"] for b in budgets))


def month_arg(m):
    m = (m or "current").strip()
    if m == "current":
        return m
    if len(m) == 7:
        m += "-01"
    datetime.strptime(m, "%Y-%m-%d")
    return m[:8] + "01"


def categories(b):
    """Flat list of visible categories with their group name."""
    out = []
    for g in api("GET", f"/budgets/{b}/categories")["category_groups"]:
        if g["deleted"] or g["hidden"]:
            continue
        for c in g["categories"]:
            if not c["deleted"] and not c["hidden"]:
                out.append({**c, "group": g["name"]})
    return out


def find(items, want, kind, label=lambda x: x["name"]):
    w = str(want).strip().lower()
    for x in items:
        if x["id"] == want or label(x).lower() == w:
            return x
    hits = [x for x in items if w in label(x).lower()]
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise RuntimeError(f"{kind} '{want}' is ambiguous: " + "; ".join(label(x) for x in hits[:15]))
    raise RuntimeError(f"{kind} '{want}' not found. Use the categories/accounts tool to see names.")


def find_category(b, want):
    return find(categories(b), want, "Category")


def find_account(b, want):
    accts = [x for x in api("GET", f"/budgets/{b}/accounts")["accounts"] if not x["deleted"] and not x["closed"]]
    return find(accts, want, "Account")


def transactions(b, since, type_=None):
    q = {"since_date": since}
    if type_:
        q["type"] = type_
    return [t for t in api("GET", f"/budgets/{b}/transactions?" + urllib.parse.urlencode(q))["transactions"]
            if not t["deleted"]]


def lines(t):
    """Split a transaction into (category_name, category_id, amount, payee) lines, expanding splits."""
    subs = [s for s in t.get("subtransactions") or [] if not s.get("deleted")]
    if subs:
        return [(s.get("category_name") or "Uncategorized", s.get("category_id"), s["amount"],
                 s.get("payee_name") or t.get("payee_name") or "", s.get("transfer_account_id")) for s in subs]
    return [(t.get("category_name") or "Uncategorized", t.get("category_id"), t["amount"],
             t.get("payee_name") or "", t.get("transfer_account_id"))]


def is_income(cat_name):
    return cat_name.startswith("Inflow:") or cat_name == "Ready to Assign"


def default_since(days):
    return (date.today() - timedelta(days=days)).isoformat()


# ---------------------------------------------------------------- read tools

def t_budgets(a):
    bs = api("GET", "/budgets")["budgets"]
    return "\n".join(f"{b['name']} | id {b['id']} | last modified {b.get('last_modified_on', '')[:10]}" for b in bs)


def t_overview(a):
    b = budget_id(a)
    m = api("GET", f"/budgets/{b}/months/{month_arg(a.get('month'))}")["month"]
    accts = [x for x in api("GET", f"/budgets/{b}/accounts")["accounts"] if not x["deleted"] and not x["closed"]]
    cats = {c["id"]: c for c in categories(b)}
    mc = [c for c in m["categories"] if c["id"] in cats]
    over = [c for c in mc if c["balance"] < 0]
    under = [c for c in mc if (c.get("goal_under_funded") or 0) > 0]
    since = default_since(365)
    unc = transactions(b, since, "uncategorized")
    unap = transactions(b, since, "unapproved")
    out = [f"Month {m['month'][:7]}",
           f"Ready to Assign: {money(m['to_be_budgeted'])}",
           f"Income this month: {money(m['income'])} | Assigned: {money(m['budgeted'])} | Activity: {money(m['activity'])}",
           f"Age of money: {m.get('age_of_money')} days",
           f"On-budget account total: {money(sum(x['balance'] for x in accts if x['on_budget']))}"
           f" | Net worth (all open accounts): {money(sum(x['balance'] for x in accts))}",
           f"Uncategorized transactions (last 12 months): {len(unc)} | Unapproved: {len(unap)}"]
    out.append(f"Overspent categories ({len(over)}):")
    out += [f"  {cats[c['id']]['group']} / {c['name']}: available {money(c['balance'])}" for c in over] or ["  none"]
    out.append(f"Goals underfunded this month ({len(under)}):")
    out += [f"  {cats[c['id']]['group']} / {c['name']}: needs {money(c['goal_under_funded'])} more" for c in under] or ["  none"]
    return "\n".join(out)


def t_accounts(a):
    b = budget_id(a)
    accts = [x for x in api("GET", f"/budgets/{b}/accounts")["accounts"] if not x["deleted"] and not x["closed"]]
    return "\n".join(f"{x['name']} | {x['type']} | {'on' if x['on_budget'] else 'off'}-budget | balance {money(x['balance'])}"
                     f" | cleared {money(x['cleared_balance'])} | id {x['id']}" for x in accts)


GOAL_TYPES = {"TB": "target balance", "TBD": "target balance by date", "MF": "monthly funding",
              "NEED": "plan your spending", "DEBT": "debt payoff"}


def t_categories(a):
    b = budget_id(a)
    m = api("GET", f"/budgets/{b}/months/{month_arg(a.get('month'))}")["month"]
    cats = {c["id"]: c for c in categories(b)}
    grp = a.get("group", "").lower()
    out = [f"Month {m['month'][:7]} | Ready to Assign {money(m['to_be_budgeted'])}",
           "group / category | assigned | activity | available | goal"]
    for c in m["categories"]:
        if c["id"] not in cats or (grp and grp not in cats[c["id"]]["group"].lower()):
            continue
        goal = ""
        if c.get("goal_type"):
            goal = f"{GOAL_TYPES.get(c['goal_type'], c['goal_type'])} {money(c.get('goal_target'))}"
            if c.get("goal_target_month"):
                goal += f" by {c['goal_target_month'][:7]}"
            if c.get("goal_percentage_complete") is not None:
                goal += f", {c['goal_percentage_complete']}% done"
            if c.get("goal_under_funded"):
                goal += f", underfunded {money(c['goal_under_funded'])}"
        out.append(f"{cats[c['id']]['group']} / {c['name']} | {money(c['budgeted'])} | {money(c['activity'])}"
                   f" | {money(c['balance'])} | {goal or '-'}")
    return "\n".join(out)


def t_transactions(a):
    b = budget_id(a)
    since = a.get("since") or default_since(30)
    until = a.get("until")
    status = a.get("status", "all")
    ts = transactions(b, since, status if status in ("uncategorized", "unapproved") else None)
    acc = find_account(b, a["account"])["id"] if a.get("account") else None
    cat = find_category(b, a["category"])["id"] if a.get("category") else None
    payee = (a.get("payee") or "").lower()
    rows = []
    for t in sorted(ts, key=lambda t: t["date"], reverse=True):
        if until and t["date"] > until:
            continue
        if acc and t["account_id"] != acc:
            continue
        if cat and cat not in [l[1] for l in lines(t)]:
            continue
        if payee and payee not in (t.get("payee_name") or "").lower():
            continue
        catname = " + ".join(f"{l[0]} {money(l[2])}" for l in lines(t)) if t.get("subtransactions") else ("Transfer" if t.get("transfer_account_id") else (t.get("category_name") or "Uncategorized"))
        flags = ("" if t["approved"] else " UNAPPROVED") + (f" flag:{t['flag_color']}" if t.get("flag_color") else "")
        rows.append(f"{t['date']} | {money(t['amount'])} | {t.get('payee_name') or ''} | {catname} | {t['account_name']}"
                    f" | {t.get('memo') or ''}{flags} | id {t['id']}")
    limit = int(a.get("limit", 100))
    head = f"{len(rows)} transactions since {since}" + (f" until {until}" if until else "")
    if len(rows) > limit:
        head += f" (showing newest {limit}; narrow the filter or raise limit)"
    return "\n".join([head] + rows[:limit])


def t_spending(a):
    b = budget_id(a)
    since = a.get("since") or date.today().replace(day=1).isoformat()
    until = a.get("until") or date.today().isoformat()
    by = a.get("group_by", "category")
    cat = find_category(b, a["category"])["id"] if a.get("category") else None
    groups = {c["id"]: c["group"] for c in categories(b)}
    spend, income, count = defaultdict(int), 0, defaultdict(int)
    for t in transactions(b, since):
        if t["date"] > until:
            continue
        for cname, cid, amt, payee, transfer in lines(t):
            if transfer:
                continue
            if is_income(cname):
                income += amt
                continue
            if cat and cid != cat:
                continue
            key = {"category": cname, "group": groups.get(cid, "Other"), "payee": payee or "(no payee)",
                   "month": t["date"][:7]}.get(by, cname)
            spend[key] += amt
            count[key] += 1
    total = sum(spend.values())
    out = [f"Spending {since} to {until} by {by}" + (f" (category {a['category']})" if cat else ""),
           f"Total net spending: {money(total)} | Income: {money(income)} | Net: {money(income + total)}"]
    order = sorted(spend, key=lambda k: k if by == "month" else spend[k])
    for k in order[: int(a.get("limit", 40))]:
        pct = f"{spend[k] / total * 100:.0f}%" if total else "-"
        out.append(f"{k} | {money(spend[k])} | {pct} | {count[k]} txns")
    return "\n".join(out)


def t_recurring(a):
    b = budget_id(a)
    months = int(a.get("months", 6))
    since = default_since(months * 31)
    by_payee = defaultdict(list)
    for t in transactions(b, since):
        if t["amount"] < 0 and not t.get("transfer_account_id") and t.get("payee_name"):
            by_payee[t["payee_name"]].append(t)
    out = []
    for payee, ts in by_payee.items():
        ts.sort(key=lambda t: t["date"])
        if len(ts) < 3 or len({t["date"][:7] for t in ts}) < 3:
            continue
        amts = [-t["amount"] for t in ts]
        med = statistics.median(amts)
        if med == 0 or statistics.pstdev(amts) / med > 0.15:
            continue
        ds = [date.fromisoformat(t["date"]) for t in ts]
        gap = statistics.median([(y - x).days for x, y in zip(ds, ds[1:])])
        if gap < 5:
            continue
        per_year = med * 365 / gap
        out.append((per_year, f"{payee} | typical {money(-med)} | every ~{gap:.0f} days | {len(ts)} charges"
                              f" | last {ts[-1]['date']} | ~{money(-per_year)}/year | {ts[-1].get('category_name') or 'Uncategorized'}"))
    out.sort(reverse=True)
    head = (f"Likely recurring charges (same payee, similar amount, 3+ months) over the last {months} months: "
            f"{len(out)} found, ~{money(-sum(p for p, _ in out))}/year total")
    return "\n".join([head] + [l for _, l in out])


def t_scheduled(a):
    b = budget_id(a)
    st = [s for s in api("GET", f"/budgets/{b}/scheduled_transactions")["scheduled_transactions"] if not s["deleted"]]
    st.sort(key=lambda s: s["date_next"])
    return "\n".join([f"{len(st)} scheduled transactions"] +
                     [f"next {s['date_next']} | {s['frequency']} | {money(s['amount'])} | {s.get('payee_name') or ''}"
                      f" | {s.get('category_name') or ''} | {s['account_name']} | {s.get('memo') or ''}" for s in st])


# ---------------------------------------------------------------- debt tools

DEBT_TYPES = {"creditCard", "lineOfCredit", "otherLiability", "mortgage", "autoLoan", "studentLoan",
              "personalLoan", "medicalDebt", "otherDebt"}
CARD_TYPES = {"creditCard", "lineOfCredit"}


def latest(periodic):
    """YNAB loan fields are {"YYYY-MM-DD": value} histories; return the newest value."""
    if not periodic:
        return None
    return periodic[max(periodic)]


def debt_accounts(b):
    return [x for x in api("GET", f"/budgets/{b}/accounts")["accounts"]
            if not x["deleted"] and not x["closed"] and x["type"] in DEBT_TYPES and x["balance"] < 0]


def t_debts(a):
    b = budget_id(a)
    m = api("GET", f"/budgets/{b}/months/current")["month"]
    pay = {c["name"]: c for c in m["categories"]
           if c.get("category_group_name") == "Credit Card Payments" and not c["deleted"]}
    if not pay:  # older responses lack category_group_name
        ids = {c["id"] for c in categories(b) if c["group"] == "Credit Card Payments"}
        pay = {c["name"]: c for c in m["categories"] if c["id"] in ids}
    out, total = ["account | type | owed | APR | minimum payment | notes"], 0
    for x in sorted(debt_accounts(b), key=lambda x: x["balance"]):
        owed = -x["balance"]
        total += owed
        rate = latest(x.get("debt_interest_rates"))
        minp = latest(x.get("debt_minimum_payments"))
        esc = latest(x.get("debt_escrow_amounts"))
        notes = []
        if x["type"] in CARD_TYPES:
            pc = pay.get(x["name"])
            if pc:
                gap = owed - pc["balance"]
                notes.append(f"payment category has {money(pc['balance'])}"
                             + (" - fully covered" if gap <= 0 else f" - carrying {money(gap)} not covered by the budget"))
            else:
                notes.append("no payment category found")
        if esc:
            notes.append(f"escrow {money(esc)}/month")
        out.append(f"{x['name']} | {x['type']} | {money(owed)} | {f'{rate / 1000:g}%' if rate is not None else 'unknown'}"
                   f" | {money(minp) if minp is not None else 'unknown'} | {'; '.join(notes) or '-'}")
    out.insert(0, f"Total debt: {money(total)} across {len(out) - 1} accounts. "
                  "Credit card APRs are not in YNAB; ask the user for them before payoff math.")
    return "\n".join(out)


def simulate(debts, extra, order_key):
    """Pay minimums on all, extra (plus freed-up minimums) to the first debt by order_key. Monthly steps."""
    ds = [dict(d) for d in debts]
    month, interest, paid_off = 0, 0.0, {}
    budget = sum(d["min"] for d in ds) + extra
    while any(d["bal"] > 0.005 for d in ds) and month < 600:
        month += 1
        for d in ds:
            if d["bal"] > 0:
                i = d["bal"] * d["apr"] / 1200
                d["bal"] += i
                interest += i
        left = budget
        for d in ds:
            if d["bal"] > 0:
                p = min(d["min"], d["bal"], left)
                d["bal"] -= p
                left -= p
        for d in sorted([d for d in ds if d["bal"] > 0], key=order_key):
            p = min(left, d["bal"])
            d["bal"] -= p
            left -= p
        for d in ds:
            if d["bal"] <= 0.005 and d["name"] not in paid_off:
                paid_off[d["name"]] = month
    return month, interest, paid_off


def t_payoff_plan(a):
    b = budget_id(a)
    aprs = {k.lower(): float(v) for k, v in (a.get("aprs") or {}).items()}
    mins = {k.lower(): float(v) for k, v in (a.get("minimum_payments") or {}).items()}
    only = [s.lower() for s in a.get("accounts") or []]
    debts, notes = [], []
    for x in debt_accounts(b):
        n = x["name"]
        if only and not any(o in n.lower() for o in only):
            continue
        bal = -x["balance"] / 1000
        apr = next((v for k, v in aprs.items() if k in n.lower()), None)
        if apr is None:
            r = latest(x.get("debt_interest_rates"))
            apr = r / 1000 if r is not None else None
        if apr is None:
            apr = 0.0
            notes.append(f"{n}: no APR known, assumed 0% (pass aprs to fix)")
        mp = next((v for k, v in mins.items() if k in n.lower()), None)
        if mp is None:
            r = latest(x.get("debt_minimum_payments"))
            mp = r / 1000 if r is not None else None
        if mp is None:
            mp = max(25.0, bal * 0.01 + bal * apr / 1200)
            notes.append(f"{n}: no minimum payment known, assumed ${mp:,.2f} (1% + interest, min $25)")
        if mp <= bal * apr / 1200:
            notes.append(f"{n}: minimum ${mp:,.2f} doesn't cover monthly interest; it never pays off without extra")
        debts.append({"name": n, "bal": bal, "apr": apr, "min": mp})
    if not debts:
        return "No debt accounts with a balance."
    extra = float(a.get("extra_monthly", 0))
    out = [f"Debts: " + "; ".join(f"{d['name']} ${d['bal']:,.2f} at {d['apr']:g}%, min ${d['min']:,.2f}" for d in debts),
           f"Monthly payment budget: minimums ${sum(d['min'] for d in debts):,.2f} + extra ${extra:,.2f}"]
    plans = [("minimums only (a paid-off debt's minimum rolls to the next)", 0, lambda d: 0)]
    strat = a.get("strategy", "both")
    if strat in ("avalanche", "both"):
        plans.append(("avalanche (highest APR first)", extra, lambda d: -d["apr"]))
    if strat in ("snowball", "both"):
        plans.append(("snowball (smallest balance first)", extra, lambda d: d["bal"]))
    for label, ex, key in plans:
        months, interest, done = simulate(debts, ex, key)
        tail = " (still not paid off after 50 years)" if months >= 600 else ""
        out.append(f"\n{label}: debt-free in {months} months (~{months / 12:.1f} years){tail}, total interest ${interest:,.2f}")
        out += [f"  {n} paid off in month {m}" for n, m in sorted(done.items(), key=lambda kv: kv[1])]
    out.append("\nEstimate only: fixed APRs, no new charges, interest compounded monthly.")
    return "\n".join(out + [f"Note: {n}" for n in notes])


# ---------------------------------------------------------------- write tools (staged)

def stage(description, method, path, body, budget):
    code = secrets.token_hex(3)
    now = time.time()
    for k in [k for k, v in _pending.items() if now - v["at"] > PENDING_TTL]:
        del _pending[k]
    _pending[code] = {"at": now, "method": method, "path": path, "body": body, "desc": description, "budget": budget}
    return (f"STAGED, NOT APPLIED. Proposed change:\n{description}\n\n"
            f"Show this to the user exactly and ask them to approve it. Only if they say yes, call "
            f"confirm with code {code}. If they say no, do nothing (it expires in 30 minutes).")


def t_update_transactions(a):
    b = budget_id(a)
    changes = a.get("changes") or []
    if not changes:
        raise RuntimeError("changes is empty")
    ts = {t["id"]: t for t in transactions(b, default_since(730))}
    body, desc = [], []
    for ch in changes:
        t = ts.get(ch["id"])
        if not t:
            raise RuntimeError(f"Transaction {ch['id']} not found in the last 2 years")
        u, what = {"id": t["id"]}, []
        if ch.get("category"):
            if t.get("subtransactions"):
                raise RuntimeError(f"Transaction {t['id']} is a split; recategorize it in YNAB itself")
            c = find_category(b, ch["category"])
            u["category_id"] = c["id"]
            what.append(f"category {t.get('category_name') or 'Uncategorized'} -> {c['name']}")
        if "approved" in ch:
            u["approved"] = bool(ch["approved"])
            what.append(f"approved -> {u['approved']}")
        if "memo" in ch:
            u["memo"] = ch["memo"]
            what.append(f"memo -> '{ch['memo']}'")
        if "flag_color" in ch:
            u["flag_color"] = ch["flag_color"] or None
            what.append(f"flag -> {ch['flag_color'] or 'none'}")
        if len(u) == 1:
            raise RuntimeError(f"No change given for {ch['id']}")
        body.append(u)
        desc.append(f"- {t['date']} {money(t['amount'])} {t.get('payee_name') or ''}: " + "; ".join(what))
    return stage(f"Update {len(body)} transaction(s):\n" + "\n".join(desc), "PATCH",
                 f"/budgets/{b}/transactions", {"transactions": body}, b)


def t_create_transaction(a):
    b = budget_id(a)
    acc = find_account(b, a["account"])
    d = a.get("date") or date.today().isoformat()
    date.fromisoformat(d)
    tx = {"account_id": acc["id"], "date": d, "amount": to_milli(a["amount"]),
          "payee_name": a.get("payee"), "memo": a.get("memo"), "cleared": "uncleared", "approved": True}
    cname = "Uncategorized"
    if a.get("category"):
        c = find_category(b, a["category"])
        tx["category_id"], cname = c["id"], c["name"]
    return stage(f"Create transaction: {d} | {money(tx['amount'])} | {a.get('payee') or ''} | {cname} | {acc['name']}"
                 f" | {a.get('memo') or ''}", "POST", f"/budgets/{b}/transactions", {"transaction": tx}, b)


def _month_cat(b, m, cid):
    return api("GET", f"/budgets/{b}/months/{m}/categories/{cid}", cache=False)["category"]


def t_assign(a):
    b = budget_id(a)
    m = month_arg(a.get("month"))
    c = find_category(b, a["category"])
    cur = _month_cat(b, m, c["id"])["budgeted"]
    new = to_milli(a["amount"]) + (cur if a.get("mode", "set") == "add" else 0)
    return stage(f"Assigned in {c['group']} / {c['name']} for {m}: {money(cur)} -> {money(new)}"
                 f" (Ready to Assign changes by {money(cur - new)})", "PATCH",
                 f"/budgets/{b}/months/{m}/categories/{c['id']}", {"category": {"budgeted": new}}, b)


def t_move_money(a):
    b = budget_id(a)
    m = month_arg(a.get("month"))
    amt = to_milli(a["amount"])
    if amt <= 0:
        raise RuntimeError("amount must be positive")
    src, dst = find_category(b, a["from_category"]), find_category(b, a["to_category"])
    s, d = _month_cat(b, m, src["id"]), _month_cat(b, m, dst["id"])
    warn = "" if s["balance"] >= amt else f"\nWARNING: {src['name']} only has {money(s['balance'])} available; it will go negative."
    desc = (f"Move {money(amt)} for {m}:\n- {src['name']}: assigned {money(s['budgeted'])} -> {money(s['budgeted'] - amt)},"
            f" available {money(s['balance'])} -> {money(s['balance'] - amt)}\n- {dst['name']}: assigned {money(d['budgeted'])}"
            f" -> {money(d['budgeted'] + amt)}, available {money(d['balance'])} -> {money(d['balance'] + amt)}{warn}")
    return stage(desc, "MULTI", None, [
        ("PATCH", f"/budgets/{b}/months/{m}/categories/{src['id']}", {"category": {"budgeted": s["budgeted"] - amt}}),
        ("PATCH", f"/budgets/{b}/months/{m}/categories/{dst['id']}", {"category": {"budgeted": d["budgeted"] + amt}}),
    ], b)


INTERNAL_GROUPS = {"Internal Master Category", "Credit Card Payments", "Hidden Categories"}


def category_groups(b):
    return [g for g in api("GET", f"/budgets/{b}/categories")["category_groups"]
            if not g["deleted"] and not g["hidden"] and g["name"] not in INTERNAL_GROUPS]


def target_fields(a, body, what, old=None):
    """Shared target (goal) options for create/update_category."""
    old = old or {}
    if a.get("remove_target"):
        body["goal_target"] = None
        what.append(f"remove target ({money(old.get('goal_target'))})")
        return
    if a.get("target_amount") is not None:
        body["goal_target"] = to_milli(a["target_amount"])
        what.append(f"target {money(old.get('goal_target')) if old.get('goal_type') else 'none'} -> {money(body['goal_target'])}")
    if a.get("target_date"):
        date.fromisoformat(a["target_date"])
        body["goal_target_date"] = a["target_date"]
        what.append(f"target date -> {a['target_date']}")
    if a.get("target_frequency"):
        body["goal_frequency"] = a["target_frequency"]
        what.append(f"repeats {a['target_frequency']}")
    if a.get("target_mode"):
        body["goal_needs_whole_amount"] = a["target_mode"] == "set_aside"
        what.append("'Set aside another' each period" if body["goal_needs_whole_amount"] else "'Refill up to' the target")


def t_create_category(a):
    b = budget_id(a)
    g = find(category_groups(b), a["group"], "Category group")
    if any(c["name"].lower() == a["name"].strip().lower() for c in categories(b)):
        raise RuntimeError(f"A category named '{a['name']}' already exists")
    body, what = {"name": a["name"].strip(), "category_group_id": g["id"]}, []
    if a.get("note"):
        body["note"] = a["note"]
        what.append(f"note '{a['note']}'")
    target_fields(a, body, what)
    return stage(f"Create category '{body['name']}' in group {g['name']}" + (": " + "; ".join(what) if what else ""),
                 "POST", f"/budgets/{b}/categories", {"category": body}, b)


def t_update_category(a):
    b = budget_id(a)
    c = find_category(b, a["category"])
    body, what = {}, []
    if a.get("new_name"):
        body["name"] = a["new_name"].strip()
        what.append(f"rename -> '{body['name']}'")
    if "note" in a:
        body["note"] = a["note"] or None
        what.append(f"note -> '{a['note']}'" if a["note"] else "clear note")
    if a.get("group"):
        g = find(category_groups(b), a["group"], "Category group")
        if g["name"] != c["group"]:
            body["category_group_id"] = g["id"]
            what.append(f"move from group {c['group']} -> {g['name']}")
    target_fields(a, body, what, c)
    if not body:
        raise RuntimeError("Nothing to change")
    return stage(f"Update category {c['group']} / {c['name']}:\n- " + "\n- ".join(what), "PATCH",
                 f"/budgets/{b}/categories/{c['id']}", {"category": body}, b)


def t_create_category_group(a):
    b = budget_id(a)
    name = a["name"].strip()
    if len(name) > 50:
        raise RuntimeError("Group names are limited to 50 characters")
    return stage(f"Create category group '{name}'", "POST", f"/budgets/{b}/category_groups",
                 {"category_group": {"name": name}}, b)


def t_rename_category_group(a):
    b = budget_id(a)
    g = find(category_groups(b), a["group"], "Category group")
    name = a["new_name"].strip()
    return stage(f"Rename category group '{g['name']}' -> '{name}'", "PATCH",
                 f"/budgets/{b}/category_groups/{g['id']}", {"category_group": {"name": name}}, b)


def t_transfer(a):
    b = budget_id(a)
    src, dst = find_account(b, a["from_account"]), find_account(b, a["to_account"])
    amt = to_milli(a["amount"])
    if amt <= 0:
        raise RuntimeError("amount must be positive")
    d = a.get("date") or date.today().isoformat()
    date.fromisoformat(d)
    tx = {"account_id": src["id"], "date": d, "amount": -amt, "payee_id": dst["transfer_payee_id"],
          "memo": a.get("memo"), "cleared": "uncleared", "approved": True}
    kind = "payment" if dst["type"] in DEBT_TYPES else "transfer"
    return stage(f"Record {kind} on {d}: {money(amt)} from {src['name']} (balance {money(src['balance'])}) to {dst['name']}"
                 f" (balance {money(dst['balance'])})" + (f" | memo '{a['memo']}'" if a.get("memo") else "")
                 + "\nThis only records it in YNAB; it does not move real money at the bank.",
                 "POST", f"/budgets/{b}/transactions", {"transaction": tx}, b)


def t_create_account(a):
    b = budget_id(a)
    body = {"name": a["name"].strip(), "type": a["type"], "balance": to_milli(a.get("balance", 0))}
    return stage(f"Create {a['type']} account '{body['name']}' with starting balance {money(body['balance'])}",
                 "POST", f"/budgets/{b}/accounts", {"account": body}, b)


def t_confirm(a):
    code = str(a.get("code", "")).strip()
    p = _pending.pop(code, None)
    if not p or time.time() - p["at"] > PENDING_TTL:
        raise RuntimeError("Unknown or expired code. Stage the change again and get the user's approval.")
    steps = p["body"] if p["method"] == "MULTI" else [(p["method"], p["path"], p["body"])]
    for i, (method, path, body) in enumerate(steps):
        try:
            api(method, path, body)
        except RuntimeError as e:
            done = f" Steps 1-{i} were applied; step {i + 1} failed, so check YNAB." if i else ""
            raise RuntimeError(f"{e}.{done}")
    log("applied", code, p["desc"].splitlines()[0])
    return f"APPLIED:\n{p['desc']}"


# ---------------------------------------------------------------- MCP plumbing

B = {"budget": {"type": "string", "description": "Budget name or id (default: last-used budget)"}}
MONTH = {"month": {"type": "string", "description": "YYYY-MM (default: current month)"}}
TARGET = {"target_amount": {"type": "number", "description": "Target dollars (creates a target if none)"},
          "target_date": {"type": "string", "description": "YYYY-MM-DD target date"},
          "target_frequency": {"type": "string", "enum": ["monthly", "weekly", "yearly"]},
          "target_mode": {"type": "string", "enum": ["set_aside", "refill"],
                          "description": "set_aside = 'Set aside another' each period; refill = 'Refill up to'"}}


def obj(props, req=()):
    return {"type": "object", "properties": {**B, **props}, "required": list(req)}


RO = {"readOnlyHint": True, "openWorldHint": True}
RW = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True}

TOOLS = [
    ("budgets", t_budgets, "List YNAB budgets.", {"type": "object", "properties": {}}, RO),
    ("overview", t_overview, "Budget health for a month: Ready to Assign, income, age of money, account totals, "
     "overspent categories, underfunded goals, uncategorized/unapproved counts. Start here.", obj(MONTH), RO),
    ("accounts", t_accounts, "Open accounts with balances.", obj({}), RO),
    ("categories", t_categories, "Every category for a month: assigned, activity, available, goal/target progress.",
     obj({**MONTH, "group": {"type": "string", "description": "Only groups whose name contains this"}}), RO),
    ("transactions", t_transactions, "List transactions with ids (needed for update_transactions). Splits are expanded.",
     obj({"since": {"type": "string", "description": "YYYY-MM-DD (default 30 days ago)"},
          "until": {"type": "string", "description": "YYYY-MM-DD"},
          "status": {"type": "string", "enum": ["all", "uncategorized", "unapproved"]},
          "account": {"type": "string"}, "category": {"type": "string"},
          "payee": {"type": "string", "description": "Substring of the payee name"},
          "limit": {"type": "integer", "description": "Max rows (default 100)"}}), RO),
    ("spending", t_spending, "Totals spending (excluding transfers) by category, group, payee or month, plus income. "
     "Use group_by month with a category for trends.",
     obj({"since": {"type": "string", "description": "YYYY-MM-DD (default 1st of this month)"},
          "until": {"type": "string", "description": "YYYY-MM-DD (default today)"},
          "group_by": {"type": "string", "enum": ["category", "group", "payee", "month"]},
          "category": {"type": "string", "description": "Only this category"},
          "limit": {"type": "integer", "description": "Max rows (default 40)"}}), RO),
    ("recurring", t_recurring, "Detect subscriptions and other recurring charges from history, with yearly cost.",
     obj({"months": {"type": "integer", "description": "History to scan (default 6)"}}), RO),
    ("scheduled", t_scheduled, "Upcoming scheduled transactions (bills entered in YNAB).", obj({}), RO),
    ("update_transactions", t_update_transactions,
     "STAGE changes to existing transactions (category, approve, memo, flag). Returns a preview and a code; "
     "nothing changes until the user approves and you call confirm.",
     obj({"changes": {"type": "array", "items": {"type": "object", "properties": {
         "id": {"type": "string"}, "category": {"type": "string"}, "approved": {"type": "boolean"},
         "memo": {"type": "string"},
         "flag_color": {"type": "string", "enum": ["", "red", "orange", "yellow", "green", "blue", "purple"]}},
         "required": ["id"]}}}, ["changes"]), RW),
    ("create_transaction", t_create_transaction, "STAGE a new transaction (amount in dollars, negative = outflow). "
     "Needs user approval + confirm.",
     obj({"account": {"type": "string"}, "amount": {"type": "number"}, "payee": {"type": "string"},
          "category": {"type": "string"}, "date": {"type": "string", "description": "YYYY-MM-DD (default today)"},
          "memo": {"type": "string"}}, ["account", "amount"]), RW),
    ("assign", t_assign, "STAGE assigning money to a category for a month (mode set = new total, add = add to current). "
     "Needs user approval + confirm.",
     obj({**MONTH, "category": {"type": "string"}, "amount": {"type": "number"},
          "mode": {"type": "string", "enum": ["set", "add"]}}, ["category", "amount"]), RW),
    ("move_money", t_move_money, "STAGE moving assigned money between two categories. Needs user approval + confirm.",
     obj({**MONTH, "from_category": {"type": "string"}, "to_category": {"type": "string"},
          "amount": {"type": "number", "description": "Positive dollars"}}, ["from_category", "to_category", "amount"]), RW),
    ("debts", t_debts, "All credit cards, lines of credit and loans: amount owed, APR and minimum payment (loans), "
     "and whether each card's payment category covers its balance.", obj({}), RO),
    ("payoff_plan", t_payoff_plan, "Estimate months to debt-free and total interest: minimums only vs avalanche vs snowball "
     "with an extra monthly amount. Card APRs are not in YNAB: ask the user and pass them in aprs.",
     obj({"extra_monthly": {"type": "number", "description": "Extra dollars per month on top of minimums"},
          "strategy": {"type": "string", "enum": ["both", "avalanche", "snowball"]},
          "aprs": {"type": "object", "description": "Account name -> APR percent, e.g. {\"Visa\": 24.99}",
                   "additionalProperties": {"type": "number"}},
          "minimum_payments": {"type": "object", "description": "Account name -> monthly minimum in dollars",
                               "additionalProperties": {"type": "number"}},
          "accounts": {"type": "array", "items": {"type": "string"}, "description": "Only these debts"}}), RO),
    ("create_category", t_create_category, "STAGE creating a category in an existing group, optionally with a target. "
     "Needs user approval + confirm.",
     obj({"name": {"type": "string"}, "group": {"type": "string"}, "note": {"type": "string"}, **TARGET},
         ["name", "group"]), RW),
    ("update_category", t_update_category, "STAGE renaming a category, editing its note, moving it to another group, "
     "or adding/changing/removing its target. Needs user approval + confirm. (Deleting/hiding categories is not "
     "possible through the API; the user must do that in YNAB.)",
     obj({"category": {"type": "string"}, "new_name": {"type": "string"}, "note": {"type": "string"},
          "group": {"type": "string", "description": "Move to this group"},
          "remove_target": {"type": "boolean"}, **TARGET}, ["category"]), RW),
    ("create_category_group", t_create_category_group, "STAGE creating a category group. Needs user approval + confirm.",
     obj({"name": {"type": "string"}}, ["name"]), RW),
    ("rename_category_group", t_rename_category_group, "STAGE renaming a category group. Needs user approval + confirm.",
     obj({"group": {"type": "string"}, "new_name": {"type": "string"}}, ["group", "new_name"]), RW),
    ("transfer", t_transfer, "STAGE recording a transfer between accounts, e.g. a credit card or loan payment from "
     "checking. Records it in YNAB only. Needs user approval + confirm.",
     obj({"from_account": {"type": "string"}, "to_account": {"type": "string"},
          "amount": {"type": "number", "description": "Positive dollars"},
          "date": {"type": "string", "description": "YYYY-MM-DD (default today)"}, "memo": {"type": "string"}},
         ["from_account", "to_account", "amount"]), RW),
    ("create_account", t_create_account, "STAGE creating a budget account (loan accounts can't be created through the API). "
     "Needs user approval + confirm.",
     obj({"name": {"type": "string"},
          "type": {"type": "string", "enum": ["checking", "savings", "cash", "creditCard", "otherAsset", "otherLiability"]},
          "balance": {"type": "number", "description": "Starting balance in dollars (negative for a card owing money)"}},
         ["name", "type"]), RW),
    ("confirm", t_confirm, "Apply a staged change. ONLY call this after the user has seen the preview and explicitly "
     "said yes in their own message.", {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]},
     {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True}),
]
BY_NAME = {t[0]: t for t in TOOLS}

INSTRUCTIONS = (
    "YNAB budget tools. Amounts are dollars; outflows negative. Category and account arguments accept names "
    "(case-insensitive, unique substring ok). Start with overview. Write tools only stage a change: show the "
    "preview to the user word for word, wait for an explicit yes, then call confirm with the code. Never call "
    "confirm in the same turn you staged, and never on your own initiative. Report numbers exactly as the tools give them."
)


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None:
            continue  # notification
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": params.get("protocolVersion", PROTOCOL),
                "capabilities": {"tools": {}}, "instructions": INSTRUCTIONS,
                "serverInfo": {"name": "ynab", "version": "1.0"}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [
                {"name": n, "description": d, "inputSchema": s, "annotations": ann} for n, _, d, s, ann in TOOLS]}})
        elif method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            if name not in BY_NAME:
                send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"unknown tool {name}"}})
                continue
            try:
                text, err = BY_NAME[name][1](args), False
            except Exception as e:
                text, err = f"Error: {e}", True
                log(name, "failed:", e)
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}], "isError": err}})
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        else:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}})


if __name__ == "__main__":
    main()
