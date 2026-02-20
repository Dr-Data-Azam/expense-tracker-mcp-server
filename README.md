# Expense Tracker MCP Server

A personal finance tracker built with [FastMCP](https://gofastmcp.com), implementing the **50/30/20 budgeting rule**. Stores data in [Turso](https://turso.tech) (cloud SQLite) for persistent storage across deployments, with automatic fallback to a local SQLite file for development.

## Tools (17 total)

| Group | Tools |
|-------|-------|
| Core | `add_transaction`, `delete_transaction`, `edit_transaction` |
| Balances | `get_balance`, `get_balance_for_period` |
| Budget | `get_budget_status`, `set_budget_limit`, `get_budget_alerts` |
| Reports | `get_summary_by_period`, `get_spending_trends`, `get_top_categories` |
| Search/List | `search_transactions`, `list_recent_transactions` |
| Recurring | `add_recurring_transaction`, `apply_due_recurring_transactions`, `list_recurring_transactions` |
| Export | `export_transactions_csv` |

## 50/30/20 Budget Categories

| Bucket | Target | Categories |
|--------|--------|------------|
| Needs | 50% | Housing, Groceries, Utilities, Transport |
| Wants | 30% | Dining Out, Hobbies, Subscriptions |
| Savings/Debt | 20% | Investments, Emergency Fund, Loan Repayments |

---

## Local Development Setup

**Requirements:** Python 3.13+, [uv](https://docs.astral.sh/uv/)

```bash
git clone <repo-url>
cd expense-tracker-mcp-server
uv sync
uv run python main.py   # starts in stdio mode with local expenses.db
```

No environment variables needed locally — the server falls back to a local `expenses.db` file automatically.

### Claude Desktop — Local (stdio)

Add to `%APPDATA%\Claude\claude_desktop_config.json` (Windows) or `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):

```json
{
  "mcpServers": {
    "expense-tracker": {
      "command": "uv",
      "args": ["run", "python", "C:/absolute/path/to/main.py"]
    }
  }
}
```

---

## Cloud Deployment (FastMCP Cloud + Turso)

### Step 1 — Set up Turso (free, no credit card)

1. Go to [turso.tech](https://turso.tech) and sign up for free
2. Create a new database (e.g. `expense-tracker`)
3. From the database page, copy:
   - **Database URL** — looks like `libsql://<db-name>-<org>.turso.io`
   - **Auth Token** — a long JWT string

### Step 2 — Deploy to FastMCP Cloud (free beta)

1. Push this repository to GitHub
2. Go to [fastmcp.cloud](https://fastmcp.cloud) and sign in with GitHub
3. Click **New Deployment** and connect your GitHub repository
4. Configure the deployment:
   - **Entry Point:** `main:mcp`
   - **Python version:** `3.13`
5. Add environment variables:
   - `TURSO_DATABASE_URL` = your Turso database URL from Step 1
   - `TURSO_AUTH_TOKEN` = your Turso auth token from Step 1
6. Click **Deploy** — the server goes live in ~60 seconds
7. Copy the generated URL: `https://<slug>.fastmcp.cloud/mcp`

### Step 3 — Connect Claude Desktop to the Cloud Server

Update `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "expense-tracker": {
      "url": "https://<your-slug>.fastmcp.cloud/mcp"
    }
  }
}
```

Restart Claude Desktop. Your expense tracker now runs in the cloud with persistent storage — data survives server restarts, redeployments, and idle timeouts.

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `TURSO_DATABASE_URL` | Cloud only | Turso database URL (`libsql://...`) |
| `TURSO_AUTH_TOKEN` | Cloud only | Turso authentication token (JWT) |

When neither variable is set, the server uses a local `expenses.db` SQLite file (development mode).
