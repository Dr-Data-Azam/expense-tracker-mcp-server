"""
Expense Tracker MCP Server
A personal finance tracker implementing the 50/30/20 budgeting rule,
powered by FastMCP and SQLite for persistent storage.

Tools (17 total):
  Core        : add_transaction, delete_transaction, edit_transaction
  Balances    : get_balance, get_balance_for_period
  Budget      : get_budget_status, set_budget_limit, get_budget_alerts
  Reports     : get_summary_by_period, get_spending_trends, get_top_categories
  Search/List : search_transactions, list_recent_transactions
  Recurring   : add_recurring_transaction, apply_due_recurring_transactions,
                list_recurring_transactions
  Export      : export_transactions_csv
"""

import calendar
import csv
import io
import os
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

try:
    import libsql
    _LIBSQL_AVAILABLE = True
except ImportError:
    _LIBSQL_AVAILABLE = False

from fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Constants – 50/30/20 Budget Rule
# ---------------------------------------------------------------------------

NEEDS_CATEGORIES    = ["Housing", "Groceries", "Utilities", "Transport"]
WANTS_CATEGORIES    = ["Dining Out", "Hobbies", "Subscriptions"]
SAVINGS_CATEGORIES  = ["Investments", "Emergency Fund", "Loan Repayments"]
ALL_EXPENSE_CATEGORIES = NEEDS_CATEGORIES + WANTS_CATEGORIES + SAVINGS_CATEGORIES

BUDGET_TARGETS: dict[str, float] = {
    "Needs": 0.50,
    "Wants": 0.30,
    "Savings/Debt": 0.20,
}

CATEGORY_TO_BUCKET: dict[str, str] = {}
for _c in NEEDS_CATEGORIES:
    CATEGORY_TO_BUCKET[_c] = "Needs"
for _c in WANTS_CATEGORIES:
    CATEGORY_TO_BUCKET[_c] = "Wants"
for _c in SAVINGS_CATEGORIES:
    CATEGORY_TO_BUCKET[_c] = "Savings/Debt"

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

_LOCAL_DB_PATH = Path(__file__).parent / "expenses.db"


class _Row:
    """Wraps a libsql tuple row so columns can be accessed by name (row["col"])."""

    __slots__ = ("_data", "_index")

    def __init__(self, row: tuple, description: list) -> None:
        self._data = row
        self._index = {col[0]: i for i, col in enumerate(description)}

    def __getitem__(self, key: str | int):
        if isinstance(key, int):
            return self._data[key]
        return self._data[self._index[key]]

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)


class _Connection:
    """
    Thin wrapper around either a libsql or sqlite3 connection that:
    - Makes fetchone()/fetchall() return _Row objects (named column access).
    - Supports the context manager protocol (with _get_connection() as conn).
    """

    def __init__(self, conn) -> None:
        self._conn = conn

    # ── context manager ──────────────────────────────────────────────────────

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            self._conn.commit()
        self._conn.close()
        return False

    # ── delegation helpers ────────────────────────────────────────────────────

    def execute(self, sql: str, params=()) -> "_Cursor":
        raw = self._conn.execute(sql, params)
        return _Cursor(raw)

    def commit(self):
        self._conn.commit()

    def sync(self):
        if hasattr(self._conn, "sync"):
            self._conn.sync()

    def close(self):
        self._conn.close()


class _Cursor:
    """Wraps a raw cursor so fetchone/fetchall return _Row instances."""

    def __init__(self, cursor) -> None:
        self._cursor = cursor

    def fetchone(self):
        row = self._cursor.fetchone()
        if row is None:
            return None
        desc = self._cursor.description or []
        return _Row(tuple(row), desc)

    def fetchall(self):
        rows = self._cursor.fetchall()
        desc = self._cursor.description or []
        return [_Row(tuple(r), desc) for r in rows]

    def __iter__(self):
        desc = self._cursor.description or []
        for row in self._cursor:
            yield _Row(tuple(row), desc)

    @property
    def lastrowid(self):
        return self._cursor.lastrowid


def _get_connection() -> _Connection:
    """Return a connection — Turso (cloud) when env vars are set, local SQLite otherwise."""
    url = os.getenv("TURSO_DATABASE_URL")
    token = os.getenv("TURSO_AUTH_TOKEN")

    if url and token and _LIBSQL_AVAILABLE:
        # Direct remote connection to Turso — no local file, no WAL
        raw = libsql.connect(database=url, auth_token=token)
    else:
        raw = sqlite3.connect(_LOCAL_DB_PATH)
        raw.execute("PRAGMA journal_mode=WAL")
        raw.execute("PRAGMA synchronous=NORMAL")
        raw.execute("PRAGMA foreign_keys=ON")

    return _Connection(raw)


def _db_error(e: Exception) -> str:
    """Return a user-friendly database error message."""
    return f"Database error: {e}. Please try again or check your database."


def _cents(amount: float) -> int:
    """Convert a dollar float to integer cents (avoids float imprecision)."""
    return round(amount * 100)


def _dollars(cents: int) -> float:
    """Convert integer cents to a dollar float."""
    return cents / 100.0


def _date_range(year: int, month: int) -> tuple[str, str]:
    """
    Return (start, end) datetime strings for the given year+month.
    If month == 0, returns the full year range.
    """
    if month == 0:
        return (f"{year:04d}-01-01 00:00:00", f"{year:04d}-12-31 23:59:59")
    last_day = calendar.monthrange(year, month)[1]
    return (
        f"{year:04d}-{month:02d}-01 00:00:00",
        f"{year:04d}-{month:02d}-{last_day:02d} 23:59:59",
    )


def _init_db() -> None:
    """Create all tables and indexes (idempotent – safe to call every startup)."""
    with _get_connection() as conn:
        # ── Main transactions table ──────────────────────────────────────────
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                type        TEXT    NOT NULL CHECK(type IN ('Income', 'Expense')),
                category    TEXT    NOT NULL,
                amount      REAL    NOT NULL CHECK(amount > 0),
                description TEXT    NOT NULL,
                date        TEXT    NOT NULL
            )
            """
        )

        # ── Indexes ──────────────────────────────────────────────────────────
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tx_date      ON transactions (date)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tx_type_date ON transactions (type, date)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tx_category  ON transactions (category)"
        )

        # ── Budget limits table ───────────────────────────────────────────────
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS budget_limits (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                category      TEXT    NOT NULL UNIQUE,
                monthly_limit REAL    NOT NULL CHECK(monthly_limit > 0),
                created_at    TEXT    NOT NULL
            )
            """
        )

        # ── Recurring transactions table ──────────────────────────────────────
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS recurring_transactions (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                type           TEXT    NOT NULL CHECK(type IN ('Income', 'Expense')),
                category       TEXT    NOT NULL,
                amount         REAL    NOT NULL CHECK(amount > 0),
                description    TEXT    NOT NULL,
                frequency      TEXT    NOT NULL
                               CHECK(frequency IN ('daily','weekly','monthly','yearly')),
                next_due_date  TEXT    NOT NULL,
                last_applied   TEXT,
                is_active      INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        conn.commit()
        conn.sync()


# ---------------------------------------------------------------------------
# FastMCP server – initialise DB on import
# ---------------------------------------------------------------------------

_init_db()
mcp = FastMCP("ExpenseTracker")


# ===========================================================================
# ── CORE TOOLS ──────────────────────────────────────────────────────────────
# ===========================================================================


@mcp.tool()
def add_transaction(
    type: Literal["Income", "Expense"],
    amount: float,
    category: str,
    description: str,
    date: str | None = None,
) -> str:
    """Record a new financial transaction (income or expense) in the database.

    For **Expense** entries the category MUST be one of the 50/30/20 categories:

    Needs (50% target)        – Housing, Groceries, Utilities, Transport
    Wants (30% target)        – Dining Out, Hobbies, Subscriptions
    Savings/Debt (20% target) – Investments, Emergency Fund, Loan Repayments

    For **Income** entries use a descriptive category such as "Salary",
    "Freelance", "Bonus", etc. — no restriction applies.

    Args:
        type:        "Income" or "Expense".
        amount:      Positive monetary value (e.g. 1500.00).
        category:    Category string (see above for Expense restrictions).
        description: Short human-readable note about the transaction.
        date:        Optional date as "YYYY-MM-DD". Defaults to today if omitted.
                     Use this to backdate past receipts or transactions.

    Returns:
        A confirmation message with the stored transaction details.
    """
    if amount <= 0:
        return "Error: amount must be a positive number."

    if type == "Expense" and category not in ALL_EXPENSE_CATEGORIES:
        valid = "\n  ".join(ALL_EXPENSE_CATEGORIES)
        return (
            f"Error: '{category}' is not a valid expense category.\n"
            f"Please choose one of:\n  {valid}"
        )

    if date is not None:
        try:
            datetime.strptime(date, "%Y-%m-%d")
            date_str = f"{date} 00:00:00"
        except ValueError:
            return "Error: date must be in YYYY-MM-DD format (e.g. 2026-01-15)."
    else:
        date_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        with _get_connection() as conn:
            cursor = conn.execute(
                "INSERT INTO transactions (type, category, amount, description, date) "
                "VALUES (?, ?, ?, ?, ?)",
                (type, category, amount, description, date_str),
            )
            conn.commit()
            conn.sync()
            row_id = cursor.lastrowid
    except Exception as e:
        return _db_error(e)

    bucket = CATEGORY_TO_BUCKET.get(category, "") if type == "Expense" else ""
    bucket_info = f" [{bucket}]" if bucket else ""

    return (
        f"Transaction #{row_id} recorded successfully.\n"
        f"  Type       : {type}\n"
        f"  Category   : {category}{bucket_info}\n"
        f"  Amount     : ${amount:,.2f}\n"
        f"  Description: {description}\n"
        f"  Date       : {date_str}"
    )


@mcp.tool()
def delete_transaction(transaction_id: int) -> str:
    """Permanently delete a transaction by its ID.

    Use list_recent_transactions or search_transactions first to find the
    correct ID before deleting.

    Args:
        transaction_id: The integer ID of the transaction to delete.

    Returns:
        Confirmation showing what was deleted, or an error if ID not found.
    """
    try:
        with _get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM transactions WHERE id = ?", (transaction_id,)
            ).fetchone()
            if row is None:
                return f"Error: No transaction found with ID #{transaction_id}."
            conn.execute(
                "DELETE FROM transactions WHERE id = ?", (transaction_id,)
            )
            conn.commit()
            conn.sync()
    except Exception as e:
        return _db_error(e)

    return (
        f"Transaction #{transaction_id} deleted successfully.\n"
        f"  Type       : {row['type']}\n"
        f"  Category   : {row['category']}\n"
        f"  Amount     : ${row['amount']:,.2f}\n"
        f"  Description: {row['description']}\n"
        f"  Date       : {row['date']}"
    )


@mcp.tool()
def edit_transaction(
    transaction_id: int,
    amount: float | None = None,
    category: str | None = None,
    description: str | None = None,
    date: str | None = None,
) -> str:
    """Update one or more fields of an existing transaction.

    Only the fields you supply are changed; omitted fields keep their
    current values. Patch-style: supply only what you want to change.

    Args:
        transaction_id: ID of the transaction to modify.
        amount:         New positive amount, or None to keep existing.
        category:       New category, or None to keep existing.
                        Must be a valid 50/30/20 category if the transaction
                        is an Expense.
        description:    New description text, or None to keep existing.
        date:           New date as "YYYY-MM-DD", or None to keep existing.

    Returns:
        The updated transaction details, or an error message.
    """
    if amount is not None and amount <= 0:
        return "Error: amount must be a positive number."

    if date is not None:
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            return "Error: date must be in YYYY-MM-DD format (e.g. 2026-01-15)."

    try:
        with _get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM transactions WHERE id = ?", (transaction_id,)
            ).fetchone()
            if row is None:
                return f"Error: No transaction found with ID #{transaction_id}."

            # Resolve final values
            new_amount      = amount      if amount      is not None else row["amount"]
            new_category    = category    if category    is not None else row["category"]
            new_description = description if description is not None else row["description"]
            new_date        = f"{date} 00:00:00" if date is not None else row["date"]

            # Validate category for Expense transactions
            if row["type"] == "Expense" and new_category not in ALL_EXPENSE_CATEGORIES:
                valid = "\n  ".join(ALL_EXPENSE_CATEGORIES)
                return (
                    f"Error: '{new_category}' is not a valid expense category.\n"
                    f"Please choose one of:\n  {valid}"
                )

            conn.execute(
                "UPDATE transactions SET amount=?, category=?, description=?, date=? "
                "WHERE id=?",
                (new_amount, new_category, new_description, new_date, transaction_id),
            )
            conn.commit()
            conn.sync()
    except Exception as e:
        return _db_error(e)

    bucket = CATEGORY_TO_BUCKET.get(new_category, "") if row["type"] == "Expense" else ""
    bucket_info = f" [{bucket}]" if bucket else ""

    return (
        f"Transaction #{transaction_id} updated successfully.\n"
        f"  Type       : {row['type']}\n"
        f"  Category   : {new_category}{bucket_info}\n"
        f"  Amount     : ${new_amount:,.2f}\n"
        f"  Description: {new_description}\n"
        f"  Date       : {new_date}"
    )


# ===========================================================================
# ── BALANCE TOOLS ────────────────────────────────────────────────────────────
# ===========================================================================


@mcp.tool()
def get_balance() -> str:
    """Return a summary of total (all-time) income, expenses, and net balance.

    Net balance = Total Income − Total Expenses.
    A positive balance indicates a surplus; negative indicates a deficit.

    Returns:
        A formatted string showing income, expenses, and net balance.
    """
    try:
        with _get_connection() as conn:
            row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN type='Income'  THEN amount ELSE 0 END), 0) AS total_income,
                    COALESCE(SUM(CASE WHEN type='Expense' THEN amount ELSE 0 END), 0) AS total_expenses
                FROM transactions
                """
            ).fetchone()
    except Exception as e:
        return _db_error(e)

    total_income:   float = row["total_income"]
    total_expenses: float = row["total_expenses"]
    net = total_income - total_expenses
    label = "SURPLUS" if net >= 0 else "DEFICIT"

    return (
        "========== All-Time Balance ==========\n"
        f"  Total Income   : ${total_income:>12,.2f}\n"
        f"  Total Expenses : ${total_expenses:>12,.2f}\n"
        "---------------------------------------\n"
        f"  Net Balance    : ${net:>12,.2f}  ({label})\n"
        "======================================="
    )


@mcp.tool()
def get_balance_for_period(start_date: str, end_date: str) -> str:
    """Return income, expenses, and net balance for a specific date range.

    Use this for queries like "last 30 days", "Q1 2026", or any custom range.

    Args:
        start_date: Start of the range as "YYYY-MM-DD" (inclusive).
        end_date:   End of the range as "YYYY-MM-DD" (inclusive).

    Returns:
        Balance summary for the specified period only.
    """
    try:
        datetime.strptime(start_date, "%Y-%m-%d")
        datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError:
        return "Error: dates must be in YYYY-MM-DD format."

    start_str = f"{start_date} 00:00:00"
    end_str   = f"{end_date} 23:59:59"

    try:
        with _get_connection() as conn:
            row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN type='Income'  THEN amount ELSE 0 END), 0) AS total_income,
                    COALESCE(SUM(CASE WHEN type='Expense' THEN amount ELSE 0 END), 0) AS total_expenses
                FROM transactions
                WHERE date >= ? AND date <= ?
                """,
                (start_str, end_str),
            ).fetchone()
    except Exception as e:
        return _db_error(e)

    total_income:   float = row["total_income"]
    total_expenses: float = row["total_expenses"]
    net = total_income - total_expenses
    label = "SURPLUS" if net >= 0 else "DEFICIT"

    return (
        f"========== Balance: {start_date} to {end_date} ==========\n"
        f"  Total Income   : ${total_income:>12,.2f}\n"
        f"  Total Expenses : ${total_expenses:>12,.2f}\n"
        f"  -----------------------------------------------\n"
        f"  Net Balance    : ${net:>12,.2f}  ({label})\n"
        f"=================================================="
    )


# ===========================================================================
# ── BUDGET TOOLS ─────────────────────────────────────────────────────────────
# ===========================================================================


@mcp.tool()
def get_budget_status(year: int | None = None, month: int | None = None) -> str:
    """Analyse spending against the 50/30/20 budgeting rule with a Health Check.

    Defaults to the **current calendar month** so the analysis stays relevant.
    Pass year/month explicitly to analyse a different period.

    The 50/30/20 rule:
      • 50% – Needs        (Housing, Groceries, Utilities, Transport)
      • 30% – Wants        (Dining Out, Hobbies, Subscriptions)
      • 20% – Savings/Debt (Investments, Emergency Fund, Loan Repayments)

    Args:
        year:  Year to analyse (default: current year).
        month: Month 1-12 to analyse (default: current month).

    Returns:
        A detailed budget status report including a Health Check section.
    """
    now = datetime.now()
    year  = year  if year  is not None else now.year
    month = month if month is not None else now.month

    if not (1 <= month <= 12):
        return "Error: month must be between 1 and 12."

    start, end = _date_range(year, month)
    period_label = f"{calendar.month_name[month]} {year}"

    try:
        with _get_connection() as conn:
            total_income: float = conn.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM transactions "
                "WHERE type='Income' AND date >= ? AND date <= ?",
                (start, end),
            ).fetchone()[0]

            expense_rows = conn.execute(
                "SELECT category, SUM(amount) AS total FROM transactions "
                "WHERE type='Expense' AND date >= ? AND date <= ? "
                "GROUP BY category",
                (start, end),
            ).fetchall()
    except Exception as e:
        return _db_error(e)

    if total_income == 0:
        return (
            f"No income recorded for {period_label}. "
            "Add at least one Income transaction before checking budget status."
        )

    bucket_spending: dict[str, float] = {"Needs": 0.0, "Wants": 0.0, "Savings/Debt": 0.0}
    for row in expense_rows:
        bucket = CATEGORY_TO_BUCKET.get(row["category"])
        if bucket:
            bucket_spending[bucket] += row["total"]

    total_expenses = sum(bucket_spending.values())

    lines: list[str] = [
        f"========== 50/30/20 Budget Status — {period_label} ==========",
        f"  Total Income   : ${total_income:>12,.2f}",
        f"  Total Expenses : ${total_expenses:>12,.2f}",
        "",
        f"  {'Bucket':<16} {'Spent':>10}  {'% of Income':>11}  {'Target':>8}  {'Status':>6}",
        "  " + "-" * 60,
    ]

    health_notes: list[str] = []

    for bucket, target_pct in BUDGET_TARGETS.items():
        spent      = bucket_spending[bucket]
        actual_pct = spent / total_income
        delta      = actual_pct - target_pct
        target_lbl = f"{target_pct * 100:.0f}%"
        actual_lbl = f"{actual_pct * 100:.1f}%"
        status     = "OK" if abs(delta) <= 0.02 else ("HIGH" if delta > 0 else "LOW")

        lines.append(
            f"  {bucket:<16} ${spent:>9,.2f}  {actual_lbl:>11}  {target_lbl:>8}  {status:>6}"
        )

        if bucket == "Needs":
            if status == "HIGH":
                health_notes.append(
                    f"Needs at {actual_lbl} (target 50%). Review utility plans or cheaper transport."
                )
            elif status == "LOW":
                health_notes.append(
                    f"Needs low at {actual_lbl}. Ensure all essential bills are accounted for."
                )
        elif bucket == "Wants":
            if status == "HIGH":
                health_notes.append(
                    f"Wants at {actual_lbl} (target 30%). Cut back on dining out or subscriptions."
                )
            elif status == "LOW":
                health_notes.append(
                    f"Wants low at {actual_lbl}. Room for guilt-free discretionary spending."
                )
        elif bucket == "Savings/Debt":
            if status == "HIGH":
                health_notes.append(
                    f"Savings/Debt at {actual_lbl} (target 20%). Great — consider faster loan repayment."
                )
            elif status == "LOW":
                health_notes.append(
                    f"Savings/Debt only {actual_lbl} (target 20%). Automate at least 20% into savings."
                )

    lines.append("  " + "-" * 60)
    lines.append("")
    lines.append("  --- Health Check ---")
    if health_notes:
        for note in health_notes:
            lines.append(f"  • {note}")
    else:
        lines.append("  • Spending is well-aligned with the 50/30/20 rule. Keep it up!")

    lines.append("=" * (44 + len(period_label)))
    return "\n".join(lines)


@mcp.tool()
def set_budget_limit(category: str, monthly_limit: float) -> str:
    """Set or update a monthly spending limit for a specific expense category.

    Once set, use get_budget_alerts to check whether you have exceeded any limit.

    Args:
        category:      An expense category from the 50/30/20 list.
        monthly_limit: Maximum monthly spend in dollars (e.g. 200.00).

    Returns:
        Confirmation of the limit set, and current month spend vs the new limit.
    """
    if category not in ALL_EXPENSE_CATEGORIES:
        valid = "\n  ".join(ALL_EXPENSE_CATEGORIES)
        return (
            f"Error: '{category}' is not a valid expense category.\n"
            f"Please choose one of:\n  {valid}"
        )
    if monthly_limit <= 0:
        return "Error: monthly_limit must be a positive number."

    now = datetime.now()
    start, end = _date_range(now.year, now.month)

    try:
        with _get_connection() as conn:
            conn.execute(
                """
                INSERT INTO budget_limits (category, monthly_limit, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(category) DO UPDATE SET monthly_limit=excluded.monthly_limit
                """,
                (category, monthly_limit, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            conn.commit()
            conn.sync()
            spent_row = conn.execute(
                "SELECT COALESCE(SUM(amount), 0) AS spent FROM transactions "
                "WHERE type='Expense' AND category=? AND date >= ? AND date <= ?",
                (category, start, end),
            ).fetchone()
    except Exception as e:
        return _db_error(e)

    spent = spent_row["spent"]
    remaining = monthly_limit - spent
    status = "[!] OVER LIMIT" if remaining < 0 else "[OK] Within limit"

    return (
        f"Budget limit set for '{category}'.\n"
        f"  Monthly Limit : ${monthly_limit:,.2f}\n"
        f"  Spent (this month): ${spent:,.2f}\n"
        f"  Remaining     : ${remaining:,.2f}\n"
        f"  Status        : {status}"
    )


@mcp.tool()
def get_budget_alerts() -> str:
    """Check all categories with budget limits against current month spending.

    Highlights any category that has exceeded or is close to (>80%) its limit.

    Returns:
        A report showing each limit, current spend, remaining, and alert flags.
    """
    now = datetime.now()
    start, end = _date_range(now.year, now.month)
    period_label = f"{calendar.month_name[now.month]} {now.year}"

    try:
        with _get_connection() as conn:
            limits = conn.execute(
                "SELECT category, monthly_limit FROM budget_limits ORDER BY category"
            ).fetchall()
            if not limits:
                return "No budget limits set. Use set_budget_limit to create one."

            spent_rows = conn.execute(
                "SELECT category, COALESCE(SUM(amount), 0) AS spent "
                "FROM transactions "
                "WHERE type='Expense' AND date >= ? AND date <= ? "
                "GROUP BY category",
                (start, end),
            ).fetchall()
    except Exception as e:
        return _db_error(e)

    spent_map = {r["category"]: r["spent"] for r in spent_rows}

    header = f"  {'Category':<20} {'Limit':>10}  {'Spent':>10}  {'Remaining':>10}  Status"
    separator = "  " + "-" * 72
    lines = [
        f"========== Budget Alerts — {period_label} ==========",
        header,
        separator,
    ]

    any_alert = False
    for lim in limits:
        cat   = lim["category"]
        limit = lim["monthly_limit"]
        spent = spent_map.get(cat, 0.0)
        rem   = limit - spent
        pct   = (spent / limit * 100) if limit > 0 else 0

        if rem < 0:
            flag = "[!!] OVER LIMIT"
            any_alert = True
        elif pct >= 80:
            flag = "[!]  WARNING (>80%)"
            any_alert = True
        else:
            flag = "[OK]"

        lines.append(
            f"  {cat:<20} ${limit:>9,.2f}  ${spent:>9,.2f}  ${rem:>9,.2f}  {flag}"
        )

    lines.append(separator)
    if not any_alert:
        lines.append("  All categories are within their budget limits.")
    return "\n".join(lines)


# ===========================================================================
# ── REPORT TOOLS ─────────────────────────────────────────────────────────────
# ===========================================================================


@mcp.tool()
def get_summary_by_period(year: int, month: int = 0) -> str:
    """Return an income/expense summary filtered by year, or by year + month.

    Use this tool when the user asks things like:
      - "show my expenses for January 2026"
      - "how much did I spend in 2025?"
      - "summarise last month"

    Args:
        year:  The calendar year to filter by (e.g. 2026).
        month: The calendar month as an integer 1-12.
               Pass 0 (default) to get the full-year summary.

    Returns:
        A formatted breakdown of income and expenses for the period,
        including per-category totals and a net balance.
    """
    if month < 0 or month > 12:
        return "Error: month must be between 1 and 12 (or 0 for the full year)."

    start, end = _date_range(year, month)
    period_label = str(year) if month == 0 else f"{calendar.month_name[month]} {year}"

    try:
        with _get_connection() as conn:
            rows = conn.execute(
                "SELECT type, category, SUM(amount) AS total "
                "FROM transactions "
                "WHERE date >= ? AND date <= ? "
                "GROUP BY type, category "
                "ORDER BY type DESC, total DESC",
                (start, end),
            ).fetchall()
    except Exception as e:
        return _db_error(e)

    if not rows:
        return f"No transactions found for {period_label}."

    income_rows  = [r for r in rows if r["type"] == "Income"]
    expense_rows = [r for r in rows if r["type"] == "Expense"]

    total_income   = sum(r["total"] for r in income_rows)
    total_expenses = sum(r["total"] for r in expense_rows)
    net = total_income - total_expenses

    lines: list[str] = [f"========== Summary: {period_label} =========="]

    lines.append(f"\n  INCOME  (total: ${total_income:,.2f})")
    lines.append("  " + "-" * 40)
    if income_rows:
        for r in income_rows:
            lines.append(f"  {r['category']:<22}  ${r['total']:>10,.2f}")
    else:
        lines.append("  No income recorded.")

    lines.append(f"\n  EXPENSES  (total: ${total_expenses:,.2f})")
    lines.append("  " + "-" * 40)
    if expense_rows:
        bucket_totals: dict[str, float] = {}
        bucket_rows:   dict[str, list]  = {}
        for r in expense_rows:
            bucket = CATEGORY_TO_BUCKET.get(r["category"], "Other")
            bucket_totals[bucket] = bucket_totals.get(bucket, 0) + r["total"]
            bucket_rows.setdefault(bucket, []).append(r)

        for bucket in ["Needs", "Wants", "Savings/Debt", "Other"]:
            if bucket not in bucket_rows:
                continue
            lines.append(f"\n  [{bucket}]")
            for r in bucket_rows[bucket]:
                lines.append(f"    {r['category']:<20}  ${r['total']:>10,.2f}")
            lines.append(f"    {'Subtotal':<20}  ${bucket_totals[bucket]:>10,.2f}")
    else:
        lines.append("  No expenses recorded.")

    label = "SURPLUS" if net >= 0 else "DEFICIT"
    lines.append("\n  " + "=" * 40)
    lines.append(f"  Net Balance  ({label})  :  ${net:>10,.2f}")
    lines.append("=" * (22 + len(period_label)))
    return "\n".join(lines)


@mcp.tool()
def get_spending_trends(months: int = 6, category: str | None = None) -> str:
    """Show month-by-month income and expense totals for the last N months.

    Use this to spot whether spending is going up or down over time. Optionally
    filter to a single category to track its trend (e.g. "Dining Out").

    Args:
        months:   Number of recent months to include (default 6, max 24).
        category: Optional category name to isolate its monthly trend.

    Returns:
        A table with monthly totals, delta vs prior month, and % change.
    """
    months = max(1, min(months, 24))

    if category and category not in ALL_EXPENSE_CATEGORIES:
        valid = "\n  ".join(ALL_EXPENSE_CATEGORIES)
        return (
            f"Error: '{category}' is not a valid expense category.\n"
            f"Please choose one of:\n  {valid}"
        )

    # Build list of (year, month) tuples going back N months from today
    today = date.today()
    periods: list[tuple[int, int]] = []
    yr, mo = today.year, today.month
    for _ in range(months):
        periods.insert(0, (yr, mo))
        mo -= 1
        if mo == 0:
            mo = 12
            yr -= 1

    try:
        with _get_connection() as conn:
            results: list[dict] = []
            for yr, mo in periods:
                start, end = _date_range(yr, mo)
                lbl = f"{calendar.month_abbr[mo]} {yr}"

                if category:
                    expense = conn.execute(
                        "SELECT COALESCE(SUM(amount),0) FROM transactions "
                        "WHERE type='Expense' AND category=? AND date>=? AND date<=?",
                        (category, start, end),
                    ).fetchone()[0]
                    income = 0.0
                else:
                    row = conn.execute(
                        "SELECT "
                        "COALESCE(SUM(CASE WHEN type='Income'  THEN amount ELSE 0 END),0) AS inc,"
                        "COALESCE(SUM(CASE WHEN type='Expense' THEN amount ELSE 0 END),0) AS exp "
                        "FROM transactions WHERE date>=? AND date<=?",
                        (start, end),
                    ).fetchone()
                    income  = row["inc"]
                    expense = row["exp"]

                results.append({"label": lbl, "income": income, "expense": expense})
    except Exception as e:
        return _db_error(e)

    title = f"Spending Trends — Last {months} Months"
    if category:
        title += f" ({category})"

    if category:
        header = f"  {'Month':<10}  {'Spent':>10}  {'Change':>10}  {'% Change':>10}"
    else:
        header = f"  {'Month':<10}  {'Income':>10}  {'Expenses':>10}  {'Net':>10}  {'Exp Change':>10}"
    separator = "  " + "-" * (len(header) - 2)

    lines = [f"========== {title} ==========", header, separator]

    for i, r in enumerate(results):
        if category:
            prev_exp = results[i - 1]["expense"] if i > 0 else None
            delta    = r["expense"] - prev_exp if prev_exp is not None else None
            pct      = (delta / prev_exp * 100) if (prev_exp and prev_exp != 0) else None
            delta_str = f"${delta:+,.2f}" if delta is not None else "  —"
            pct_str   = f"{pct:+.1f}%"   if pct   is not None else "  —"
            lines.append(
                f"  {r['label']:<10}  ${r['expense']:>9,.2f}  {delta_str:>10}  {pct_str:>10}"
            )
        else:
            net = r["income"] - r["expense"]
            prev_exp = results[i - 1]["expense"] if i > 0 else None
            delta    = r["expense"] - prev_exp if prev_exp is not None else None
            delta_str = f"${delta:+,.2f}" if delta is not None else "  —"
            lines.append(
                f"  {r['label']:<10}  ${r['income']:>9,.2f}  ${r['expense']:>9,.2f}"
                f"  ${net:>9,.2f}  {delta_str:>10}"
            )

    lines.append(separator)
    return "\n".join(lines)


@mcp.tool()
def get_top_categories(
    top_n: int = 5,
    type: Literal["Expense", "Income"] = "Expense",
    start_date: str | None = None,
    end_date: str | None = None,
) -> str:
    """Rank categories by total spending or income for a given period.

    Instantly answers "where is my money going?" with percentages and bucket labels.

    Args:
        top_n:      Number of top categories to return (default 5, max 20).
        type:       "Expense" (default) or "Income".
        start_date: Filter start date "YYYY-MM-DD", or None for all time.
        end_date:   Filter end date "YYYY-MM-DD", or None for all time.

    Returns:
        Ranked list with amounts, % of total, and bucket labels.
    """
    top_n = max(1, min(top_n, 20))

    where_clauses = ["type = ?"]
    params: list = [type]

    if start_date:
        try:
            datetime.strptime(start_date, "%Y-%m-%d")
        except ValueError:
            return "Error: start_date must be in YYYY-MM-DD format."
        where_clauses.append("date >= ?")
        params.append(f"{start_date} 00:00:00")

    if end_date:
        try:
            datetime.strptime(end_date, "%Y-%m-%d")
        except ValueError:
            return "Error: end_date must be in YYYY-MM-DD format."
        where_clauses.append("date <= ?")
        params.append(f"{end_date} 23:59:59")

    where_sql = " AND ".join(where_clauses)

    try:
        with _get_connection() as conn:
            rows = conn.execute(
                f"SELECT category, SUM(amount) AS total "
                f"FROM transactions WHERE {where_sql} "
                f"GROUP BY category ORDER BY total DESC LIMIT ?",
                params + [top_n],
            ).fetchall()
            grand_total = conn.execute(
                f"SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE {where_sql}",
                params,
            ).fetchone()[0]
    except Exception as e:
        return _db_error(e)

    if not rows:
        return f"No {type.lower()} transactions found for the specified period."

    period_str = ""
    if start_date or end_date:
        period_str = f" ({start_date or 'all'} to {end_date or 'present'})"

    lines = [
        f"========== Top {top_n} {type} Categories{period_str} ==========",
        f"  {'Rank':<5} {'Category':<20} {'Amount':>10}  {'% of Total':>10}  Bucket",
        "  " + "-" * 62,
    ]

    for i, row in enumerate(rows, 1):
        pct    = (row["total"] / grand_total * 100) if grand_total else 0
        bucket = CATEGORY_TO_BUCKET.get(row["category"], "—")
        lines.append(
            f"  {i:<5} {row['category']:<20} ${row['total']:>9,.2f}  {pct:>9.1f}%  {bucket}"
        )

    lines.append("  " + "-" * 62)
    lines.append(f"  {'Total':<26} ${grand_total:>9,.2f}")
    return "\n".join(lines)


# ===========================================================================
# ── SEARCH / LIST TOOLS ──────────────────────────────────────────────────────
# ===========================================================================


@mcp.tool()
def search_transactions(
    keyword: str | None = None,
    category: str | None = None,
    type: Literal["Income", "Expense"] | None = None,
    min_amount: float | None = None,
    max_amount: float | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 50,
) -> str:
    """Search and filter transactions by any combination of criteria.

    All parameters are optional — omit any you don't need.

    Args:
        keyword:    Case-insensitive substring match on description.
        category:   Exact category name to filter by.
        type:       "Income" or "Expense".
        min_amount: Lower bound for amount (inclusive).
        max_amount: Upper bound for amount (inclusive).
        start_date: Earliest date to include as "YYYY-MM-DD".
        end_date:   Latest date to include as "YYYY-MM-DD".
        limit:      Max results to return (default 50, max 200).

    Returns:
        Formatted table of matching transactions with a result count.
    """
    limit = max(1, min(limit, 200))
    where_clauses: list[str] = []
    params: list = []

    if type is not None:
        where_clauses.append("type = ?")
        params.append(type)
    if category is not None:
        where_clauses.append("category = ?")
        params.append(category)
    if keyword is not None:
        where_clauses.append("description LIKE ?")
        params.append(f"%{keyword}%")
    if min_amount is not None:
        where_clauses.append("amount >= ?")
        params.append(min_amount)
    if max_amount is not None:
        where_clauses.append("amount <= ?")
        params.append(max_amount)
    if start_date is not None:
        try:
            datetime.strptime(start_date, "%Y-%m-%d")
        except ValueError:
            return "Error: start_date must be in YYYY-MM-DD format."
        where_clauses.append("date >= ?")
        params.append(f"{start_date} 00:00:00")
    if end_date is not None:
        try:
            datetime.strptime(end_date, "%Y-%m-%d")
        except ValueError:
            return "Error: end_date must be in YYYY-MM-DD format."
        where_clauses.append("date <= ?")
        params.append(f"{end_date} 23:59:59")

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    try:
        with _get_connection() as conn:
            rows = conn.execute(
                f"SELECT id, type, category, amount, description, date "
                f"FROM transactions {where_sql} "
                f"ORDER BY date DESC LIMIT ?",
                params + [limit],
            ).fetchall()
    except Exception as e:
        return _db_error(e)

    if not rows:
        return "No transactions matched your search criteria."

    header    = (
        f"  {'ID':>4}  {'Date':<19}  {'Type':<7}  {'Category':<18}  "
        f"{'Amount':>10}  Description"
    )
    separator = "  " + "-" * 82
    lines     = [f"===== {len(rows)} Result(s) =====", header, separator]

    for row in rows:
        desc = row["description"]
        if len(desc) > 30:
            desc = desc[:27] + "..."
        lines.append(
            f"  {row['id']:>4}  {row['date']:<19}  {row['type']:<7}  "
            f"{row['category']:<18}  ${row['amount']:>9,.2f}  {desc}"
        )

    lines.append(separator)
    return "\n".join(lines)


@mcp.tool()
def list_recent_transactions(
    limit: int = 10,
    type: Literal["Income", "Expense"] | None = None,
) -> str:
    """Return a formatted list of the most recent transactions (newest first).

    Args:
        limit: Maximum number of transactions to return (default 10, max 100).
        type:  Optional filter — "Income" or "Expense". Returns both if omitted.

    Returns:
        A formatted table of recent transactions, or a message if none exist.
    """
    limit = max(1, min(limit, 100))
    where = "WHERE type = ?" if type else ""
    params: list = [type] if type else []

    try:
        with _get_connection() as conn:
            rows = conn.execute(
                f"SELECT id, type, category, amount, description, date "
                f"FROM transactions {where} ORDER BY id DESC LIMIT ?",
                params + [limit],
            ).fetchall()
    except Exception as e:
        return _db_error(e)

    if not rows:
        return "No transactions found. Use add_transaction to record your first entry."

    header    = (
        f"  {'ID':>4}  {'Date':<19}  {'Type':<7}  {'Category':<18}  "
        f"{'Amount':>10}  Description"
    )
    separator = "  " + "-" * 82
    lines     = [f"===== Last {len(rows)} Transaction(s) =====", header, separator]

    for row in rows:
        desc = row["description"]
        if len(desc) > 30:
            desc = desc[:27] + "..."
        lines.append(
            f"  {row['id']:>4}  {row['date']:<19}  {row['type']:<7}  "
            f"{row['category']:<18}  ${row['amount']:>9,.2f}  {desc}"
        )

    lines.append(separator)
    return "\n".join(lines)


# ===========================================================================
# ── RECURRING TRANSACTION TOOLS ──────────────────────────────────────────────
# ===========================================================================


@mcp.tool()
def add_recurring_transaction(
    type: Literal["Income", "Expense"],
    amount: float,
    category: str,
    description: str,
    frequency: Literal["daily", "weekly", "monthly", "yearly"],
    start_date: str,
) -> str:
    """Register a recurring transaction template that repeats automatically.

    After adding, call apply_due_recurring_transactions to post all entries
    that are due. Great for subscriptions, rent, salary, loan payments, etc.

    Args:
        type:        "Income" or "Expense".
        amount:      Amount per occurrence.
        category:    Category (same rules as add_transaction).
        description: Description (e.g. "Netflix subscription").
        frequency:   How often: "daily", "weekly", "monthly", or "yearly".
        start_date:  First due date as "YYYY-MM-DD".

    Returns:
        Confirmation with the next due date.
    """
    if amount <= 0:
        return "Error: amount must be a positive number."

    if type == "Expense" and category not in ALL_EXPENSE_CATEGORIES:
        valid = "\n  ".join(ALL_EXPENSE_CATEGORIES)
        return (
            f"Error: '{category}' is not a valid expense category.\n"
            f"Please choose one of:\n  {valid}"
        )

    try:
        datetime.strptime(start_date, "%Y-%m-%d")
    except ValueError:
        return "Error: start_date must be in YYYY-MM-DD format."

    try:
        with _get_connection() as conn:
            cursor = conn.execute(
                "INSERT INTO recurring_transactions "
                "(type, category, amount, description, frequency, next_due_date) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (type, category, amount, description, frequency, start_date),
            )
            conn.commit()
            conn.sync()
            row_id = cursor.lastrowid
    except Exception as e:
        return _db_error(e)

    return (
        f"Recurring transaction #{row_id} registered.\n"
        f"  Type       : {type}\n"
        f"  Category   : {category}\n"
        f"  Amount     : ${amount:,.2f}\n"
        f"  Description: {description}\n"
        f"  Frequency  : {frequency}\n"
        f"  First Due  : {start_date}\n"
        f"\nRun apply_due_recurring_transactions to post it when it's due."
    )


@mcp.tool()
def apply_due_recurring_transactions() -> str:
    """Post all recurring transactions that are due today or overdue.

    Inserts actual rows into the transactions table for each due recurring
    entry, then advances next_due_date to the following occurrence.
    Call this once a day (or at the start of each session) to stay current.

    Returns:
        List of transactions created, or a message if none were due.
    """
    today_str = date.today().isoformat()  # "YYYY-MM-DD"

    try:
        with _get_connection() as conn:
            due = conn.execute(
                "SELECT * FROM recurring_transactions "
                "WHERE is_active=1 AND next_due_date <= ?",
                (today_str,),
            ).fetchall()

            if not due:
                return f"No recurring transactions are due as of {today_str}."

            created: list[str] = []
            for r in due:
                # Insert the actual transaction
                conn.execute(
                    "INSERT INTO transactions (type, category, amount, description, date) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        r["type"],
                        r["category"],
                        r["amount"],
                        r["description"],
                        f"{r['next_due_date']} 00:00:00",
                    ),
                )
                created.append(
                    f"  • [{r['type']}] {r['category']} — ${r['amount']:,.2f} "
                    f"({r['description']}) on {r['next_due_date']}"
                )

                # Advance next_due_date
                due_date = date.fromisoformat(r["next_due_date"])
                freq = r["frequency"]
                if freq == "daily":
                    next_due = due_date + timedelta(days=1)
                elif freq == "weekly":
                    next_due = due_date + timedelta(weeks=1)
                elif freq == "monthly":
                    mo = due_date.month + 1
                    yr = due_date.year + (1 if mo > 12 else 0)
                    mo = 1 if mo > 12 else mo
                    last = calendar.monthrange(yr, mo)[1]
                    next_due = date(yr, mo, min(due_date.day, last))
                else:  # yearly
                    next_due = date(due_date.year + 1, due_date.month, due_date.day)

                conn.execute(
                    "UPDATE recurring_transactions "
                    "SET last_applied=?, next_due_date=? WHERE id=?",
                    (today_str, next_due.isoformat(), r["id"]),
                )

            conn.commit()
            conn.sync()
    except Exception as e:
        return _db_error(e)

    return (
        f"Posted {len(created)} recurring transaction(s):\n"
        + "\n".join(created)
    )


@mcp.tool()
def list_recurring_transactions() -> str:
    """Show all active recurring transaction templates with their next due dates.

    Returns:
        A formatted table of recurring templates, or a message if none exist.
    """
    try:
        with _get_connection() as conn:
            rows = conn.execute(
                "SELECT id, type, category, amount, description, frequency, "
                "next_due_date, last_applied "
                "FROM recurring_transactions WHERE is_active=1 ORDER BY next_due_date"
            ).fetchall()
    except Exception as e:
        return _db_error(e)

    if not rows:
        return "No active recurring transactions. Use add_recurring_transaction to set one up."

    header    = (
        f"  {'ID':>4}  {'Type':<7}  {'Category':<18}  {'Amount':>10}  "
        f"{'Freq':<10}  {'Next Due':<12}  Description"
    )
    separator = "  " + "-" * 90
    lines     = ["===== Active Recurring Transactions =====", header, separator]

    for row in rows:
        desc = row["description"]
        if len(desc) > 25:
            desc = desc[:22] + "..."
        lines.append(
            f"  {row['id']:>4}  {row['type']:<7}  {row['category']:<18}  "
            f"${row['amount']:>9,.2f}  {row['frequency']:<10}  "
            f"{row['next_due_date']:<12}  {desc}"
        )

    lines.append(separator)
    return "\n".join(lines)


# ===========================================================================
# ── EXPORT TOOL ──────────────────────────────────────────────────────────────
# ===========================================================================


@mcp.tool()
def export_transactions_csv(
    start_date: str | None = None,
    end_date: str | None = None,
    type: Literal["Income", "Expense"] | None = None,
) -> str:
    """Export transactions as a CSV-formatted string.

    The output can be pasted into a spreadsheet or saved as a .csv file.
    Columns: id, date, type, category, amount, description.

    Args:
        start_date: Filter start date "YYYY-MM-DD", or None for all time.
        end_date:   Filter end date "YYYY-MM-DD", or None for all time.
        type:       "Income", "Expense", or None for both.

    Returns:
        CSV text with a header row.
    """
    where_clauses: list[str] = []
    params: list = []

    if type:
        where_clauses.append("type = ?")
        params.append(type)
    if start_date:
        try:
            datetime.strptime(start_date, "%Y-%m-%d")
        except ValueError:
            return "Error: start_date must be in YYYY-MM-DD format."
        where_clauses.append("date >= ?")
        params.append(f"{start_date} 00:00:00")
    if end_date:
        try:
            datetime.strptime(end_date, "%Y-%m-%d")
        except ValueError:
            return "Error: end_date must be in YYYY-MM-DD format."
        where_clauses.append("date <= ?")
        params.append(f"{end_date} 23:59:59")

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    try:
        with _get_connection() as conn:
            rows = conn.execute(
                f"SELECT id, date, type, category, amount, description "
                f"FROM transactions {where_sql} ORDER BY date ASC",
                params,
            ).fetchall()
    except Exception as e:
        return _db_error(e)

    if not rows:
        return "No transactions found for the specified filters."

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "date", "type", "category", "amount", "description"])
    for row in rows:
        writer.writerow([
            row["id"],
            row["date"],
            row["type"],
            row["category"],
            f"{row['amount']:.2f}",
            row["description"],
        ])

    csv_text = output.getvalue()
    row_count = len(rows)
    return f"CSV export — {row_count} transaction(s):\n\n{csv_text}"


# ===========================================================================
# ── Entry point ──────────────────────────────────────────────────────────────
# ===========================================================================

if __name__ == "__main__":
    mcp.run()
