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
CACHE_TTL = 30      # seconds to reuse GET responses (rate limit is 200/hour; short so edits made in the YNAB app show up)

_cache = {}
_pending = {}
# Write mode: "confirm" (default) stages every change for approval; "direct" (--direct or YNAB_WRITE_MODE=direct)
# applies changes immediately. Either way every applied change is appended to the change log.
DIRECT = os.environ.get("YNAB_WRITE_MODE", "").lower() == "direct"
CHANGE_LOG = os.path.expanduser(os.environ.get("YNAB_CHANGE_LOG", "~/.local/state/ynab-mcp/changes.jsonl"))


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
        if e.code == 404 and "/months/" in path:
            msg += " (that month is outside this budget's range; the budgets tool shows each budget's first/last month)"
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
    w = " ".join(str(want).split()).lower()
    full = lambda x: f"{x['group']} / {label(x)}".lower() if x.get("group") else label(x).lower()
    for x in items:
        if x["id"] == want or label(x).lower() == w or full(x) == w:
            return x
    hits = [x for x in items if w in label(x).lower()] or [x for x in items if w in full(x)]
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
    return f"{len(bs)} budgets:\n" + "\n".join(f"{b['name']} | months {(b.get('first_month') or '')[:7]} to {(b.get('last_month') or '')[:7]}"
                     f" | last modified {(b.get('last_modified_on') or '')[:10]} | id {b['id']}" for b in bs)


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
    month = {c["id"]: c for c in m["categories"]}
    grp = a.get("group", "").lower()
    cats = [c for c in categories(b) if c["id"] in month and (not grp or grp in c["group"].lower())]
    ngroups = len({c["group"] for c in cats})
    out = [f"Month {m['month'][:7]} | Ready to Assign {money(m['to_be_budgeted'])}",
           f"{len(cats)} categories in {ngroups} groups (complete list" + (f", groups matching '{a['group']}'" if grp else "")
           + "). Each '## ' line is a group; the lines under it are its categories:",
           "category | assigned | activity | available | target"]
    current = None
    for cat in cats:
        c = month[cat["id"]]
        if cat["group"] != current:
            current = cat["group"]
            out.append(f"\n## {current}")
        goal = ""
        if c.get("goal_type"):
            goal = f"{GOAL_TYPES.get(c['goal_type'], c['goal_type'])} {money(c.get('goal_target'))}"
            if c.get("goal_target_month"):
                goal += f" by {c['goal_target_month'][:7]}"
            if c.get("goal_percentage_complete") is not None:
                goal += f", {c['goal_percentage_complete']}% done"
            if c.get("goal_under_funded"):
                goal += f", underfunded {money(c['goal_under_funded'])}"
        out.append(f"{c['name']} | {money(c['budgeted'])} | {money(c['activity'])} | {money(c['balance'])} | {goal or '-'}")
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
                      f" | {s.get('category_name') or ''} | {s['account_name']} | {s.get('memo') or ''} | id {s['id']}"
                      for s in st])


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
    if DIRECT:
        return _apply(code, _pending.pop(code)) + "\n(Applied immediately. recent_changes shows what changed, for undo.)"
    return (f"STAGED, NOT APPLIED. Proposed change:\n{description}\n\n"
            f"Show the user this change (every item) and get their go-ahead, then call confirm with code {code}. "
            f"It expires in 30 minutes.")


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


FREQUENCIES = ["never", "daily", "weekly", "everyOtherWeek", "twiceAMonth", "every4Weeks", "monthly", "everyOtherMonth",
               "every3Months", "every4Months", "twiceAYear", "yearly", "everyOtherYear"]


def _scheduled_list(b):
    return [x for x in api("GET", f"/budgets/{b}/scheduled_transactions", cache=False)["scheduled_transactions"]
            if not x["deleted"]]


def find_scheduled(b, want):
    """By id, or by a payee/memo substring that matches exactly one scheduled transaction."""
    st = _scheduled_list(b)
    return find(st, want, "Scheduled transaction",
                label=lambda x: f"{x.get('payee_name') or ''} {money(x['amount'])} {x['frequency']} next {x['date_next']}"
                                + (f" ({x['memo']})" if x.get("memo") else ""))


def _sched_fields(b, a, cur=None):
    """Build a SaveScheduledTransaction body from tool args, starting from cur (an existing one) if given."""
    cur = cur or {}
    body = {k: cur.get(k) for k in ("account_id", "date", "amount", "payee_id", "category_id", "memo", "flag_color",
                                     "frequency")} if cur else {}
    if cur:
        body["date"] = cur["date_next"]
    labels = {"account": cur.get("account_name"), "payee": cur.get("payee_name"), "category": cur.get("category_name")}
    if a.get("account"):
        acc = find_account(b, a["account"])
        body["account_id"], labels["account"] = acc["id"], acc["name"]
    if a.get("date"):
        d = date.fromisoformat(a["date"])
        if d < date.today() or d > date.today().replace(year=date.today().year + 5):
            raise RuntimeError("Scheduled date must be today or later, and at most 5 years out.")
        body["date"] = a["date"]
    if a.get("amount") is not None:
        body["amount"] = to_milli(a["amount"])
    if a.get("to_account"):
        dst = find_account(b, a["to_account"])
        body["payee_id"], labels["payee"] = dst["transfer_payee_id"], f"Transfer : {dst['name']}"
        body.pop("payee_name", None)
    elif a.get("payee"):
        body["payee_id"], body["payee_name"], labels["payee"] = None, a["payee"], a["payee"]
    if a.get("category"):
        c = find_category(b, a["category"])
        if c["group"] == "Credit Card Payments":
            raise RuntimeError("YNAB doesn't allow scheduling into a credit card payment category; "
                               "schedule a transfer to the card account instead (to_account).")
        body["category_id"], labels["category"] = c["id"], c["name"]
    if "memo" in a:
        body["memo"] = a["memo"] or None
    if a.get("frequency"):
        if a["frequency"] not in FREQUENCIES:
            raise RuntimeError("frequency must be one of: " + ", ".join(FREQUENCIES))
        body["frequency"] = a["frequency"]
    return body, labels


def _sched_line(body, labels):
    return (f"{body.get('frequency') or 'never'} starting {body.get('date')} | {money(body.get('amount'))} | "
            f"{labels.get('payee') or ''} | {labels.get('category') or ('(transfer)' if str(labels.get('payee') or '').startswith('Transfer :') else 'Uncategorized')} | {labels.get('account')}"
            + (f" | memo '{body['memo']}'" if body.get("memo") else ""))


def t_create_scheduled(a):
    b = budget_id(a)
    if not a.get("date"):
        raise RuntimeError("date is required (the first occurrence, today or later)")
    body, labels = _sched_fields(b, {"frequency": "monthly", **a})
    if "payee_name" in body and body.get("payee_id") is None:
        body.pop("payee_id")
    return stage("Create scheduled transaction: " + _sched_line(body, labels), "POST",
                 f"/budgets/{b}/scheduled_transactions", {"scheduled_transaction": body}, b)


def t_update_scheduled(a):
    b = budget_id(a)
    cur = find_scheduled(b, a["scheduled"])
    body, labels = _sched_fields(b, a, cur)
    if body.get("payee_id") is None and "payee_name" not in body:
        body.pop("payee_id", None)
    before = _sched_line({**{k: cur.get(k) for k in ("frequency", "amount", "memo")}, "date": cur["date_next"]},
                         {"payee": cur.get("payee_name"), "category": cur.get("category_name"),
                          "account": cur.get("account_name")})
    return stage(f"Update scheduled transaction:\n- before: {before}\n- after:  {_sched_line(body, labels)}", "PUT",
                 f"/budgets/{b}/scheduled_transactions/{cur['id']}", {"scheduled_transaction": body}, b)


def t_delete_scheduled(a):
    b = budget_id(a)
    cur = find_scheduled(b, a["scheduled"])
    line = _sched_line({"frequency": cur["frequency"], "amount": cur["amount"], "memo": cur.get("memo"),
                        "date": cur["date_next"]},
                       {"payee": cur.get("payee_name"), "category": cur.get("category_name"),
                        "account": cur.get("account_name")})
    return stage(f"Delete scheduled transaction: {line}\n(To undo, create it again with these details.)", "DELETE",
                 f"/budgets/{b}/scheduled_transactions/{cur['id']}", None, b)


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


LOAN_TYPES = DEBT_TYPES - CARD_TYPES - {"otherLiability"}


def payment_kind(b, c):
    """'card' for a credit card payment category, 'loan' for one paired to a loan account, else None."""
    if c.get("group") == "Credit Card Payments":
        return "card"
    if c.get("goal_type") == "DEBT":
        return "loan"
    loans = {x["name"].lower() for x in api("GET", f"/budgets/{b}/accounts")["accounts"]
             if x["type"] in LOAN_TYPES and not x["deleted"]}
    return "loan" if c["name"].lower() in loans else None


def target_fields(a, body, what, old=None, kind=None):
    """Shared target (goal) options for create/update_category. Checks YNAB's rules before anything is staged."""
    old = old or {}
    name = old.get("name") or a.get("name") or "this category"
    if a.get("remove_target"):
        body["goal_target"] = None
        what.append(f"remove target ({money(old.get('goal_target'))})")
        return
    freq, tdate, mode = a.get("target_frequency"), a.get("target_date"), a.get("target_mode")
    if kind and (freq or mode):
        label = "credit card payment" if kind == "card" else "loan payment"
        raise RuntimeError(f"{name} is a {label} category: YNAB only lets the API set its target amount"
                           + (" and target date" if kind == "card" else "")
                           + ". Drop target_frequency/target_mode for it (it keeps its existing target style).")
    if kind == "loan" and tdate:
        raise RuntimeError(f"{name} is a loan payment category: YNAB doesn't support a target date for it. Drop target_date.")
    if freq and a.get("target_amount") is None:
        raise RuntimeError(f"{name}: target_frequency needs target_amount too.")
    if freq == "yearly" and not tdate:
        raise RuntimeError(f"{name}: a yearly target needs target_date (when it's next due).")
    if freq == "weekly" and tdate:
        raise RuntimeError(f"{name}: a weekly target can't have a target_date.")
    if mode and not freq and old.get("goal_type") not in (None, "NEED"):
        raise RuntimeError(f"{name}: target_mode only applies to plan-your-spending (NEED) targets; "
                           f"this one is {GOAL_TYPES.get(old.get('goal_type'), old.get('goal_type'))}. "
                           "Pass target_frequency too to replace it with a NEED target.")
    if a.get("target_amount") is not None:
        if float(a["target_amount"]) < 0:
            raise RuntimeError(f"{name}: target_amount can't be negative.")
        body["goal_target"] = to_milli(a["target_amount"])
        what.append(f"target {money(old.get('goal_target')) if old.get('goal_type') else 'none'} -> {money(body['goal_target'])}")
    if tdate:
        date.fromisoformat(tdate)
        body["goal_target_date"] = tdate
        what.append(f"target date -> {tdate}")
    if freq:
        body["goal_frequency"] = freq
        what.append(f"repeats {freq}")
    if mode:
        body["goal_needs_whole_amount"] = mode == "set_aside"
        what.append("'Set aside another' each period" if body["goal_needs_whole_amount"] else "'Refill up to' the target")


def t_create_category(a):
    """One category (name + group) or many (categories list). Missing groups are created first, in the same change."""
    b = budget_id(a)
    items = a.get("categories") or [a]
    groups = category_groups(b)
    taken = {c["name"].lower() for c in categories(b)}
    new_groups, steps, lines_ = {}, [], []
    for it in items:
        name, gname = (it.get("name") or "").strip(), (it.get("group") or "").strip()
        if not name or not gname:
            raise RuntimeError(f"Each category needs a name and a group: {it}")
        if name.lower() in taken:
            raise RuntimeError(f"A category named '{name}' already exists (or is listed twice)")
        if gname.lower() in {g.lower() for g in INTERNAL_GROUPS}:
            raise RuntimeError(f"Can't add categories to '{gname}': YNAB manages that group itself "
                               "(credit card payment categories are created automatically per card)")
        taken.add(name.lower())
        try:
            g = find(groups, gname, "Category group")
            gid, glabel = g["id"], g["name"]
        except RuntimeError as e:
            if "ambiguous" in str(e):
                raise
            key = gname.lower()
            if key not in new_groups:
                if len(gname) > 50:
                    raise RuntimeError("Group names are limited to 50 characters")
                new_groups[key] = gname
                steps.append(("POST", f"/budgets/{b}/category_groups", {"category_group": {"name": gname}}, f"group:{key}"))
            gid, glabel = ("$ref", f"group:{key}"), f"{new_groups[key]} (NEW GROUP)"
        body, what = {"name": name, "category_group_id": gid}, []
        if it.get("note"):
            body["note"] = it["note"]
            what.append(f"note '{it['note']}'")
        target_fields(it, body, what)
        steps.append(("POST", f"/budgets/{b}/categories", {"category": body}, None))
        lines_.append(f"- '{name}' in {glabel}" + (": " + "; ".join(what) if what else ""))
    head = f"Create {len(lines_)} categor{'y' if len(lines_) == 1 else 'ies'}"
    if new_groups:
        head += f" and {len(new_groups)} new group(s): " + ", ".join(new_groups.values())
    return stage(head + "\n" + "\n".join(lines_), "MULTI", None, steps, b)


def t_update_category(a):
    """One category (category + fields) or many (changes list), staged as a single change."""
    b = budget_id(a)
    items = a.get("changes") or [a]
    groups = None
    steps, out = [], []
    for it in items:
        c = find_category(b, it["category"])
        body, what = {}, []
        if it.get("new_name"):
            body["name"] = it["new_name"].strip()
            what.append(f"rename -> '{body['name']}'")
        if "note" in it:
            body["note"] = it["note"] or None
            what.append(f"note -> '{it['note']}'" if it["note"] else "clear note")
        if it.get("group"):
            groups = groups or category_groups(b)
            g = find(groups, it["group"], "Category group")
            if g["name"] != c["group"]:
                body["category_group_id"] = g["id"]
                what.append(f"move from group {c['group']} -> {g['name']}")
        target_fields(it, body, what, c, payment_kind(b, c))
        if not body:
            raise RuntimeError(f"Nothing to change for {c['name']}")
        steps.append(("PATCH", f"/budgets/{b}/categories/{c['id']}", {"category": body}))
        out.append(f"- {c['group']} / {c['name']}: " + "; ".join(what))
    return stage(f"Update {len(out)} categor{'y' if len(out) == 1 else 'ies'}:\n" + "\n".join(out), "MULTI", None, steps, b)


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


def _apply(code, p):
    steps = p["body"] if p["method"] == "MULTI" else [(p["method"], p["path"], p["body"])]
    refs = {}

    def resolve(v):
        if isinstance(v, tuple) and v and v[0] == "$ref":
            return refs[v[1]]
        if isinstance(v, dict):
            return {k: resolve(x) for k, x in v.items()}
        return v

    for i, step in enumerate(steps):
        method, path, body = step[:3]
        try:
            data = api(method, path, resolve(body))
        except RuntimeError as e:
            done = f" Steps 1-{i} of {len(steps)} were applied; step {i + 1} failed, so check YNAB." if i else ""
            raise RuntimeError(f"{e}.{done}")
        if len(step) > 3 and step[3]:
            refs[step[3]] = data["category_group"]["id"]
    log("applied", code, p["desc"].splitlines()[0])
    try:
        os.makedirs(os.path.dirname(CHANGE_LOG), exist_ok=True)
        with open(CHANGE_LOG, "a") as f:
            f.write(json.dumps({"time": datetime.now().isoformat(timespec="seconds"), "code": code,
                                "budget": p.get("budget"), "description": p["desc"]}) + "\n")
    except OSError as e:
        log("could not write change log:", e)
    return f"APPLIED:\n{p['desc']}"


def t_confirm(a):
    codes = [str(c).strip() for c in (a.get("codes") or [a.get("code", "")])]
    now = time.time()
    missing = [c for c in codes if c not in _pending or now - _pending[c]["at"] > PENDING_TTL]
    if missing:
        raise RuntimeError(f"Unknown or expired code(s) {', '.join(missing)}; nothing was applied. "
                           "Stage the change again and get the user's approval.")
    done = []
    for c in codes:
        p = _pending.pop(c)
        try:
            done.append(_apply(c, p))
        except RuntimeError as e:
            raise RuntimeError(f"{e}" + (f"\nAlready applied before this failure:\n" + "\n".join(done) if done else ""))
    return "\n\n".join(done)


def t_recent_changes(a):
    n = int(a.get("limit", 10))
    try:
        with open(CHANGE_LOG) as f:
            entries = [json.loads(l) for l in f if l.strip()]
    except FileNotFoundError:
        return "No changes have been applied yet."
    out = [f"Last {min(n, len(entries))} of {len(entries)} applied changes (newest first). Each shows before -> after, "
           "so to undo, apply the reverse with the normal tools."]
    for e in reversed(entries[-n:]):
        out.append(f"\n[{e['time']}]\n{e['description']}")
    return "\n".join(out)


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

UPDATE_CAT = {"category": {"type": "string", "description": "Category name (or 'Group / Name')"},
              "new_name": {"type": "string"}, "note": {"type": "string"},
              "group": {"type": "string", "description": "Move to this existing group"},
              "remove_target": {"type": "boolean"}, **TARGET}

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
    ("scheduled", t_scheduled, "Upcoming scheduled transactions (bills, paychecks, transfers) with ids.", obj({}), RO),
    ("update_transactions", t_update_transactions,
     "Change existing transactions in bulk (category, approve, memo, flag).",
     obj({"changes": {"type": "array", "items": {"type": "object", "properties": {
         "id": {"type": "string"}, "category": {"type": "string"}, "approved": {"type": "boolean"},
         "memo": {"type": "string"},
         "flag_color": {"type": "string", "enum": ["", "red", "orange", "yellow", "green", "blue", "purple"]}},
         "required": ["id"]}}}, ["changes"]), RW),
    ("create_transaction", t_create_transaction, "Create a transaction (amount in dollars, negative = outflow).",
     obj({"account": {"type": "string"}, "amount": {"type": "number"}, "payee": {"type": "string"},
          "category": {"type": "string"}, "date": {"type": "string", "description": "YYYY-MM-DD (default today)"},
          "memo": {"type": "string"}}, ["account", "amount"]), RW),
    ("create_scheduled", t_create_scheduled, "Create a scheduled (recurring) transaction, e.g. a bill or paycheck. "
     "Amount in dollars, negative = outflow. Use to_account instead of payee for a scheduled transfer or card/loan "
     "payment. Splits aren't supported by YNAB's API.",
     obj({"account": {"type": "string"}, "date": {"type": "string", "description": "First occurrence, YYYY-MM-DD (today or later)"},
          "amount": {"type": "number"}, "payee": {"type": "string"},
          "to_account": {"type": "string", "description": "Make it a transfer to this account instead of a payee"},
          "category": {"type": "string"}, "memo": {"type": "string"},
          "frequency": {"type": "string", "enum": FREQUENCIES, "description": "Default monthly; 'never' = one-time"}},
         ["account", "date", "amount"]), RW),
    ("update_scheduled", t_update_scheduled, "Change a scheduled transaction (only the fields you pass change). "
     "Find it by id from the scheduled tool, or by a payee name that matches only one.",
     obj({"scheduled": {"type": "string", "description": "Id or unique payee name"},
          "account": {"type": "string"}, "date": {"type": "string", "description": "Next occurrence, YYYY-MM-DD"},
          "amount": {"type": "number"}, "payee": {"type": "string"}, "to_account": {"type": "string"},
          "category": {"type": "string"}, "memo": {"type": "string"},
          "frequency": {"type": "string", "enum": FREQUENCIES}}, ["scheduled"]), RW),
    ("delete_scheduled", t_delete_scheduled, "Delete a scheduled transaction (by id or unique payee name).",
     obj({"scheduled": {"type": "string", "description": "Id or unique payee name"}}, ["scheduled"]), RW),
    ("assign", t_assign, "Assign money to a category for a month (mode set = new total, add = add to current).",
     obj({**MONTH, "category": {"type": "string"}, "amount": {"type": "number"},
          "mode": {"type": "string", "enum": ["set", "add"]}}, ["category", "amount"]), RW),
    ("move_money", t_move_money, "Move assigned money between two categories.",
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
    ("create_category", t_create_category, "Create one or many categories, optionally with targets. "
     "For several, pass them all in `categories`. A group that doesn't exist yet is "
     "created in the same change.",
     obj({"name": {"type": "string", "description": "Single category name (or use categories)"},
          "group": {"type": "string", "description": "Group name; created if it doesn't exist"},
          "note": {"type": "string"}, **TARGET,
          "categories": {"type": "array", "description": "Several categories at once",
                         "items": {"type": "object", "properties": {
                             "name": {"type": "string"}, "group": {"type": "string"}, "note": {"type": "string"},
                             **TARGET}, "required": ["name", "group"]}}}), RW),
    ("update_category", t_update_category, "Rename one or many (via changes) categories, edit notes, move to another group, "
     "or add/change/remove targets. (Deleting/hiding categories is not "
     "possible through the API; the user must do that in YNAB.)",
     obj({**UPDATE_CAT, "changes": {"type": "array", "description": "Several categories at once (one approval)",
                                     "items": {"type": "object", "properties": UPDATE_CAT, "required": ["category"]}}}), RW),
    ("create_category_group", t_create_category_group, "Create a category group.",
     obj({"name": {"type": "string"}}, ["name"]), RW),
    ("rename_category_group", t_rename_category_group, "Rename a category group.",
     obj({"group": {"type": "string"}, "new_name": {"type": "string"}}, ["group", "new_name"]), RW),
    ("transfer", t_transfer, "Record a transfer between accounts, e.g. a credit card or loan payment from "
     "checking. Records it in YNAB only.",
     obj({"from_account": {"type": "string"}, "to_account": {"type": "string"},
          "amount": {"type": "number", "description": "Positive dollars"},
          "date": {"type": "string", "description": "YYYY-MM-DD (default today)"}, "memo": {"type": "string"}},
         ["from_account", "to_account", "amount"]), RW),
    ("create_account", t_create_account, "Create a budget account (loan accounts can't be created through the API).",
     obj({"name": {"type": "string"},
          "type": {"type": "string", "enum": ["checking", "savings", "cash", "creditCard", "otherAsset", "otherLiability"]},
          "balance": {"type": "number", "description": "Starting balance in dollars (negative for a card owing money)"}},
         ["name", "type"]), RW),
    ("recent_changes", t_recent_changes, "Changes this server applied to YNAB, newest first, with before -> after values. "
     "Use it to answer 'what did you change?' or to undo something.",
     {"type": "object", "properties": {"limit": {"type": "integer", "description": "How many (default 10)"}}}, RO),
    ("confirm", t_confirm, "Apply staged changes after the user has seen them and given the go-ahead. "
     "Pass several approved codes in codes to apply them together.",
     {"type": "object", "properties": {"code": {"type": "string"},
                                       "codes": {"type": "array", "items": {"type": "string"}}}},
     {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True}),
]
BY_NAME = {t[0]: t for t in TOOLS}

COMMON = (
    "YNAB budget tools. Amounts are dollars; outflows negative. Refer to budgets by name (from the budgets tool) "
    "and to categories/accounts by name ('Group / Name' also works). "
    "Read and analyze freely, interpret requests by intent, and point out problems or improvements you notice. "
    "Group related changes into as few calls as possible: create_category with a categories list (missing groups "
    "are created automatically), update_category with a changes list. Use the numbers the tools return; don't estimate. "
)
INSTRUCTIONS_CONFIRM = COMMON + (
    "Write tools only STAGE a change and return a preview + code. Summarize the previews for the user (every change "
    "must be listed), then wait for their go-ahead and call confirm once with all approved codes. Don't confirm "
    "without the user's go-ahead. Changes staged together don't see each other until confirmed."
)
INSTRUCTIONS_DIRECT = COMMON + (
    "Write tools apply changes IMMEDIATELY. For a clear request, just do it and then tell the user briefly what "
    "changed. If a request is ambiguous or would change a lot at once (e.g. more than ~15 items), outline the plan "
    "in a few lines first. recent_changes lists everything applied, with before -> after, for undo."
)


def instructions():
    return INSTRUCTIONS_DIRECT if DIRECT else INSTRUCTIONS_CONFIRM


def tool_list():
    out = []
    for n, _, d, sch, ann in TOOLS:
        if n == "confirm" and DIRECT:
            continue
        if not ann.get("readOnlyHint") and n != "confirm":
            d += (" Applies immediately." if DIRECT else
                  " Staged only: returns a preview + code; nothing changes until confirm is called with the code.")
        out.append({"name": n, "description": d, "inputSchema": sch, "annotations": ann})
    return out


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main():
    global DIRECT
    if "--direct" in sys.argv[1:]:
        DIRECT = True
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
                "capabilities": {"tools": {}}, "instructions": instructions(),
                "serverInfo": {"name": "ynab", "version": "1.0"}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": tool_list()}})
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
