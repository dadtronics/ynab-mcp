# ynab-mcp

An [MCP](https://modelcontextprotocol.io) server that lets an AI agent read and manage your
[YNAB](https://www.ynab.com) budget: spending insights, budget upkeep, debt payoff planning, and
category changes, with every write held for your approval.

- **One file, Python standard library only.** No pip install, no dependencies.
- **Confirm-first by default.** A write tool only *stages* a change and returns a before → after preview.
  Nothing reaches YNAB until a separate `confirm` call, which the agent is told to make only after you say yes.
  You can also make your MCP client prompt you before `confirm` runs (see below).
  Prefer less friction? Run with `--direct` and changes apply immediately.
- **Every applied change is logged** with before → after values, and `recent_changes` lets the agent read
  the log, so "undo that" works.
- **Works with local models.** Tool output is compact text with exact numbers, so smaller models
  (e.g. a 27B Qwen through llama.cpp) handle it well. With a local model your financial data never leaves your network.

Not affiliated with or endorsed by YNAB.

## Tools

| Read | What it does |
|---|---|
| `budgets` | List budgets |
| `overview` | Ready to Assign, income, age of money, account totals, overspent categories, underfunded targets, uncategorized/unapproved counts. Start here. |
| `accounts` | Open accounts and balances |
| `categories` | Assigned / activity / available and target progress for a month |
| `transactions` | Filter by date, account, category, payee, or uncategorized/unapproved. Splits are expanded. |
| `spending` | Totals by category, group, payee, or month (trends), excluding transfers |
| `recurring` | Detects subscriptions and other recurring charges, with yearly cost |
| `scheduled` | Upcoming scheduled transactions |
| `debts` | Cards, lines of credit and loans: owed, APR and minimum (loans), and whether each card's payment category covers its balance |
| `payoff_plan` | Months to debt-free and total interest: minimums vs avalanche vs snowball, with an extra monthly amount. Card APRs aren't in YNAB's API, so pass them in. |

| Write (staged; needs `confirm`) | What it does |
|---|---|
| `update_transactions` | Recategorize, approve, memo, flag (bulk) |
| `create_transaction` | Add a transaction |
| `transfer` | Record a transfer or card/loan payment between accounts |
| `assign` | Set or add to a category's assigned amount for a month |
| `move_money` | Move assigned money between categories |
| `create_category` | Create one or many categories (with targets) in one approval; missing groups are created in the same change |
| `update_category` | Rename, note, move to another group, add/change/remove targets; one or many in one approval |
| `create_category_group` / `rename_category_group` | Manage groups |
| `create_account` | Checking, savings, cash, credit card, other asset/liability |
| `confirm` | Apply staged changes by code (`codes` applies several approved changes at once). Not present in `--direct` mode. |
| `recent_changes` (read) | Applied changes, newest first, with before → after values for undo |

Budgets, categories and accounts can be referred to by name (case-insensitive; a unique substring is fine).
Categories also accept `Group / Name`, the form the `categories` tool prints.
All amounts are dollars; outflows are negative.

Not possible through YNAB's API, so not here: deleting, hiding, reordering or merging categories;
creating loan accounts or editing loan details; recategorizing split transactions.

## Setup

1. **Get a token.** In YNAB on the web: Account Settings → Developer Settings → New Token.
   It grants full access to all your budgets, so store it carefully:

   ```sh
   mkdir -p ~/.config/ynab && (umask 077; read -rs T && printf %s "$T" > ~/.config/ynab/token)
   ```

   Or set `YNAB_TOKEN` in the server's environment.

2. **Pick a default budget (optional).** Set `YNAB_BUDGET` to a budget name or id.
   Otherwise the server uses YNAB's "last-used" budget. Any tool also takes a `budget` argument.

3. **Add it to your MCP client.**

   **opencode**:

   ```sh
   opencode mcp add --global ynab -- python3 -I /path/to/ynab_mcp.py
   ```

   To have opencode ask you before any change is applied, add this to `opencode.jsonc`:

   ```jsonc
   "agents": {
     "build": {
       "permissions": [
         { "action": "ynab_confirm", "resource": "*", "effect": "ask" }
       ]
     }
   }
   ```

   **pi**: `pi mcp add ynab -- python3 -I /path/to/ynab_mcp.py`

   **Claude Code**: `claude mcp add ynab -- python3 -I /path/to/ynab_mcp.py`

   **Any other client** (Claude Desktop, Cursor, ...):

   ```json
   {
     "mcpServers": {
       "ynab": {
         "command": "python3",
         "args": ["-I", "/path/to/ynab_mcp.py"],
         "env": { "YNAB_BUDGET": "My Budget" }
       }
     }
   }
   ```

Then ask things like *"give me a YNAB overview"*, *"what are my recurring charges?"*,
*"categorize my uncategorized transactions"*, or *"how fast can I pay off my debts with $200/month extra?
My card APR is 24.99%"*.

## Write modes

| | Confirm (default) | Direct (`--direct` or `YNAB_WRITE_MODE=direct`) |
|---|---|---|
| Write tools | Stage a change, return preview + code | Apply immediately |
| Applying | `confirm` with the code(s) | n/a |
| Rule checks (e.g. card payment target limits) | Before staging | Before applying |
| Change log | Yes | Yes |

YNAB's API can't touch bank connections or move real money, and everything it changes can be changed back,
so direct mode is a reasonable choice for your own budget. Change log location: `~/.local/state/ynab-mcp/changes.jsonl`
(override with `YNAB_CHANGE_LOG`).

## Notes

- YNAB allows 200 API requests per hour per token. Read responses are cached for 2 minutes.
- Staged changes expire after 30 minutes and can be confirmed once.
- `payoff_plan` is an estimate: fixed APRs, no new charges, monthly compounding.
- Requires Python 3.9+.

## Tests

Offline, against a fake API with made-up data (no token or network needed):

```sh
python3 -m unittest discover -s tests
```

## License

MIT
