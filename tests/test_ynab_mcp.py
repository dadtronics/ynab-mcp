"""Offline tests: every tool runs against a fake YNAB API (made-up data, no token, no network).

Run: python3 -m unittest discover -s tests
"""
import json, os, subprocess, sys, unittest
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import ynab_mcp as y  # noqa: E402


def ago(n):
    return (date.today() - timedelta(days=n)).isoformat()


def cat(i, name, budgeted=0, activity=0, balance=0, goal_type=None, goal_target=None, under=None, pct=None):
    return {"id": i, "name": name, "deleted": False, "hidden": False, "budgeted": budgeted, "activity": activity,
            "balance": balance, "goal_type": goal_type, "goal_target": goal_target, "goal_under_funded": under,
            "goal_percentage_complete": pct, "goal_target_month": None}


def tx(i, d, amount, payee, cat_id, cat_name, approved=True, transfer=None, subs=None):
    return {"id": i, "date": d, "amount": amount, "payee_name": payee, "category_id": cat_id,
            "category_name": cat_name, "account_id": "a1", "account_name": "Checking", "approved": approved,
            "flag_color": None, "memo": None, "deleted": False, "transfer_account_id": transfer,
            "subtransactions": subs or []}


CATS = [cat("c1", "Groceries", 600000, -412330, 187670, "NEED", 600000, 0, 100),
        cat("c2", "Dining Out", 100000, -150000, -50000),
        cat("c3", "Streaming", 30000, -15990, 14010, "MF", 30000, 10000, 60),
        cat("c4", "Inflow: Ready to Assign"),
        cat("cc1", "Example Card", balance=300000)]
GROUPS = [{"id": "g1", "name": "Everyday", "deleted": False, "hidden": False, "categories": CATS[:3]},
          {"id": "g9", "name": "Credit Card Payments", "deleted": False, "hidden": False, "categories": [CATS[4]]},
          {"id": "g0", "name": "Internal Master Category", "deleted": False, "hidden": False, "categories": [CATS[3]]}]
ACCOUNTS = [
    {"id": "a1", "name": "Checking", "type": "checking", "on_budget": True, "balance": 5000000,
     "cleared_balance": 4900000, "deleted": False, "closed": False, "transfer_payee_id": "tp1"},
    {"id": "a2", "name": "Savings", "type": "savings", "on_budget": False, "balance": 10000000,
     "cleared_balance": 10000000, "deleted": False, "closed": False, "transfer_payee_id": "tp2"},
    {"id": "a3", "name": "Example Card", "type": "creditCard", "on_budget": True, "balance": -1200000,
     "cleared_balance": 0, "deleted": False, "closed": False, "transfer_payee_id": "tp3"},
    {"id": "a4", "name": "Car Loan", "type": "autoLoan", "on_budget": False, "balance": -15000000,
     "cleared_balance": 0, "deleted": False, "closed": False, "transfer_payee_id": "tp4",
     "debt_interest_rates": {"2024-01-01": 6500, "2025-06-01": 5900}, "debt_minimum_payments": {"2024-01-01": 350000}},
]
TXS = [tx(f"n{i}", ago(30 * i + 2), -15990, "StreamCo", "c3", "Streaming") for i in range(5)] + [
    tx("g1", ago(3), -120500, "Grocer", "c1", "Groceries", approved=False),
    tx("x1", ago(4), -30000, "Mystery", None, None),
    tx("p1", ago(5), 3000000, "Employer", "c4", "Inflow: Ready to Assign"),
    tx("t1", ago(6), -50000, "Transfer : Savings", None, None, transfer="a2"),
    tx("s1", ago(7), -80000, "Store", None, "Split", subs=[
        {"amount": -50000, "category_id": "c1", "category_name": "Groceries", "deleted": False},
        {"amount": -30000, "category_id": "c2", "category_name": "Dining Out", "deleted": False}]),
]
MONTH = {"month": date.today().replace(day=1).isoformat(), "to_be_budgeted": 25000, "income": 3000000,
         "budgeted": 730000, "activity": -578320, "age_of_money": 42, "categories": CATS}


class FakeAPI:
    def __init__(self):
        self.calls = []

    def __call__(self, method, path, body=None, cache=True):
        self.calls.append((method, path, body))
        if method != "GET":
            return {}
        if path == "/budgets":
            return {"budgets": [{"id": "b1", "name": "Example Budget", "last_modified_on": "2026-01-01T00:00:00Z"}]}
        if path.endswith("/accounts"):
            return {"accounts": ACCOUNTS}
        if path.endswith("/categories"):
            return {"category_groups": GROUPS}
        if "/months/" in path and "/categories/" in path:
            return {"category": next(c for c in CATS if c["id"] == path.rsplit("/", 1)[1])}
        if "/months/" in path:
            return {"month": MONTH}
        if "/scheduled_transactions" in path:
            return {"scheduled_transactions": []}
        if "/transactions" in path:
            if "type=uncategorized" in path:
                return {"transactions": [t for t in TXS if not t["category_name"] and not t["transfer_account_id"]]}
            if "type=unapproved" in path:
                return {"transactions": [t for t in TXS if not t["approved"]]}
            return {"transactions": TXS}
        raise AssertionError(f"unexpected GET {path}")


class Tools(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self._real = y.api
        y.api = self.api
        y._pending.clear()

    def tearDown(self):
        y.api = self._real

    def run_tool(self, tool, /, **args):
        return y.BY_NAME[tool][1](args)

    def staged_code(self, text):
        self.assertIn("STAGED, NOT APPLIED", text)
        return text.split("confirm with code ")[1].split(".")[0]

    def writes(self):
        return [c for c in self.api.calls if c[0] != "GET"]

    def test_overview(self):
        r = self.run_tool("overview")
        self.assertIn("Ready to Assign: $25.00", r)
        self.assertIn("Dining Out: available -$50.00", r)
        self.assertIn("Streaming: needs $10.00 more", r)

    def test_transactions_and_splits(self):
        r = self.run_tool("transactions", since=ago(200))
        self.assertIn("Groceries -$50.00 + Dining Out -$30.00", r)
        self.assertIn("| Transfer |", r)
        self.assertIn("UNAPPROVED", r)
        self.assertIn("id s1", self.run_tool("transactions", category="dining"))

    def test_spending_excludes_transfers_and_income(self):
        r = self.run_tool("spending", since=ago(60))
        self.assertIn("Groceries | -$170.50", r)  # 120.50 + 50 split line
        self.assertNotIn("Savings", r)
        self.assertIn("Income: $3,000.00", r)

    def test_recurring(self):
        r = self.run_tool("recurring")
        self.assertIn("StreamCo | typical -$15.99", r)
        self.assertNotIn("Grocer", r)

    def test_debts(self):
        r = self.run_tool("debts")
        self.assertIn("Total debt: $16,200.00", r)
        self.assertIn("Car Loan | autoLoan | $15,000.00 | 5.9% | $350.00", r)
        self.assertIn("carrying $900.00 not covered", r)

    def test_payoff_plan(self):
        r = self.run_tool("payoff_plan", extra_monthly=200, aprs={"example": 24.99})
        self.assertIn("avalanche", r)
        self.assertIn("Example Card paid off in month", r)
        self.assertIn("no minimum payment known", r)
        self.assertIn("no APR known", self.run_tool("payoff_plan"))

    def test_writes_are_only_staged(self):
        self.staged_code(self.run_tool("update_transactions", changes=[{"id": "x1", "category": "groceries", "approved": True}]))
        self.staged_code(self.run_tool("create_transaction", account="check", amount=-12.5, payee="Gas", category="Groceries"))
        self.staged_code(self.run_tool("assign", category="Dining", amount=50, mode="add"))
        self.staged_code(self.run_tool("move_money", from_category="Groceries", to_category="Dining Out", amount=50))
        self.staged_code(self.run_tool("create_category", name="Gifts", group="everyday", target_amount=50))
        self.staged_code(self.run_tool("update_category", category="Streaming", remove_target=True))
        self.staged_code(self.run_tool("create_category_group", name="Kids"))
        self.staged_code(self.run_tool("rename_category_group", group="every", new_name="Monthly"))
        self.staged_code(self.run_tool("transfer", from_account="Checking", to_account="Example Card", amount=300))
        self.staged_code(self.run_tool("create_account", name="HYSA", type="savings", balance=1000))
        self.assertEqual(self.writes(), [])

    def test_confirm_applies_once(self):
        code = self.staged_code(self.run_tool("assign", category="Dining", amount=50, mode="add"))
        self.assertIn("APPLIED", self.run_tool("confirm", code=code))
        self.assertEqual(self.writes(), [("PATCH", "/budgets/last-used/months/current/categories/c2",
                                          {"category": {"budgeted": 150000}})])
        with self.assertRaises(RuntimeError):
            self.run_tool("confirm", code=code)

    def test_move_money_two_patches(self):
        code = self.staged_code(self.run_tool("move_money", from_category="Groceries", to_category="Dining Out", amount=50))
        self.run_tool("confirm", code=code)
        self.assertEqual([c[2]["category"]["budgeted"] for c in self.writes()], [550000, 150000])

    def test_transfer_uses_transfer_payee(self):
        code = self.staged_code(self.run_tool("transfer", from_account="Checking", to_account="Example Card", amount=300))
        self.run_tool("confirm", code=code)
        body = self.writes()[0][2]["transaction"]
        self.assertEqual((body["account_id"], body["amount"], body["payee_id"]), ("a1", -300000, "tp3"))

    def test_refusals(self):
        for tool, args in [("update_transactions", {"changes": [{"id": "s1", "category": "Groceries"}]}),
                           ("create_category", {"name": "Groceries", "group": "everyday"}),
                           ("create_category", {"name": "X", "group": "Credit Card Payments"}),
                           ("move_money", {"from_category": "Groceries", "to_category": "Dining", "amount": -5}),
                           ("confirm", {"code": "nope"})]:
            with self.assertRaises(RuntimeError, msg=tool):
                self.run_tool(tool, **args)


class Protocol(unittest.TestCase):
    def test_stdio(self):
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "confirm", "arguments": {"code": "x"}}}]
        env = {**os.environ, "YNAB_TOKEN": "test"}
        p = subprocess.run([sys.executable, "-I", os.path.join(ROOT, "ynab_mcp.py")], env=env, text=True,
                           input="\n".join(json.dumps(m) for m in msgs) + "\n", capture_output=True, timeout=30)
        out = [json.loads(l) for l in p.stdout.splitlines()]
        self.assertEqual(out[0]["result"]["serverInfo"]["name"], "ynab")
        self.assertEqual(len(out[1]["result"]["tools"]), len(y.TOOLS))
        self.assertTrue(out[2]["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
