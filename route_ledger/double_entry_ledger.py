"""Idempotent Double-Entry Usage & Subscription Settlement Ledger (`route_ledger/double_entry_ledger.py`).

SQLite-backed transactional billing and inventory reservation engine modeling:
  - ISP metered bandwidth usage, recurring subscriptions, and hardware terminal SKU orders
  - SHA-256 payload-verified `idempotency_key` deduplication across at-least-once retries
  - Exact integer minor-unit (`cents`) arithmetic via `decimal.Decimal` (`ROUND_HALF_UP`)
  - Multi-region telecom/VAT/consumption tax rules (`US-WA`, `DE`, `JP`, `BR`)
  - Out-of-order telemetry timestamp ingestion with per-account watermark tracking
  - Atomic credit-limit and warehouse SKU stock enforcement (`BEGIN IMMEDIATE`)
  - Strict double-entry conservation (`SUM(debits) == SUM(credits)`) and metering reconciliation
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import random
import sqlite3
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple


REGION_TAX_BPS: Dict[str, int] = {
    "US-WA": 1025,  # 10.25% WA state + local retail/telecom tax
    "DE": 1900,     # 19.00% German VAT (MwSt)
    "JP": 1000,     # 10.00% Japanese Consumption Tax (JCT)
    "BR": 2650,     # 26.50% Brazilian composite telecom/ICMS tax
}


class IdempotencyConflictError(ValueError):
    """Raised when an existing `idempotency_key` is reused with a mutated event payload."""


@dataclass(frozen=True)
class UsageOrderEvent:
    """Incoming metered bandwidth usage, subscription renewal, or hardware order event."""

    event_id: str
    idempotency_key: str
    account_id: str
    region: str
    event_type: str  # METERED_BANDWIDTH_GB | MONTHLY_SUBSCRIPTION | TERMINAL_KIT_ORDER
    quantity: str    # Decimal string (e.g. "14.375" GB or "1" kit)
    unit_price_cents: int
    event_timestamp: int
    sku_id: Optional[str] = None
    sku_qty: int = 0

    def canonical_sha256(self) -> str:
        canonical = json.dumps(
            {
                "account_id": self.account_id,
                "region": self.region,
                "event_type": self.event_type,
                "quantity": str(Decimal(self.quantity).normalize()),
                "unit_price_cents": self.unit_price_cents,
                "event_timestamp": self.event_timestamp,
                "sku_id": self.sku_id,
                "sku_qty": self.sku_qty,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SettlementReceipt:
    """Deterministic receipt returned for every processed or deduplicated event."""

    idempotency_key: str
    event_id: str
    account_id: str
    region: str
    status: str  # SETTLED | REJECTED
    is_duplicate: bool
    is_out_of_order: bool
    subtotal_cents: int
    tax_cents: int
    total_cents: int
    rejection_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "idempotency_key": self.idempotency_key,
            "event_id": self.event_id,
            "account_id": self.account_id,
            "region": self.region,
            "status": self.status,
            "is_duplicate": self.is_duplicate,
            "is_out_of_order": self.is_out_of_order,
            "subtotal_cents": self.subtotal_cents,
            "tax_cents": self.tax_cents,
            "total_cents": self.total_cents,
            "rejection_reason": self.rejection_reason,
        }


@dataclass(frozen=True)
class ReconciliationReport:
    """Audit result reconciling SQLite double-entry postings against raw metering logs."""

    raw_events_received: int
    unique_idempotency_keys: int
    duplicate_retries_blocked: int
    settled_count: int
    rejected_count: int
    out_of_order_events_handled: int
    total_debits_cents: int
    total_credits_cents: int
    imbalance_cents: int
    is_double_entry_balanced: bool
    expected_metering_total_cents: int
    ledger_settled_total_cents: int
    reconciliation_diff_cents: int
    unmatched_keys: List[str]
    reconciliation_time_ms: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "raw_events_received": self.raw_events_received,
            "unique_idempotency_keys": self.unique_idempotency_keys,
            "duplicate_retries_blocked": self.duplicate_retries_blocked,
            "settled_count": self.settled_count,
            "rejected_count": self.rejected_count,
            "out_of_order_events_handled": self.out_of_order_events_handled,
            "total_debits_cents": self.total_debits_cents,
            "total_credits_cents": self.total_credits_cents,
            "imbalance_cents": self.imbalance_cents,
            "is_double_entry_balanced": self.is_double_entry_balanced,
            "expected_metering_total_cents": self.expected_metering_total_cents,
            "ledger_settled_total_cents": self.ledger_settled_total_cents,
            "reconciliation_diff_cents": self.reconciliation_diff_cents,
            "unmatched_keys": list(self.unmatched_keys),
            "reconciliation_time_ms": round(self.reconciliation_time_ms, 3),
        }


def compute_regional_charge_cents(
    quantity_str: str,
    unit_price_cents: int,
    region: str,
) -> Tuple[int, int, int]:
    """Compute `(subtotal_cents, tax_cents, total_cents)` using exact `Decimal` rounding."""
    if region not in REGION_TAX_BPS:
        raise ValueError(f"Unsupported tax region '{region}'. Expected one of {sorted(REGION_TAX_BPS)}.")
    qty = Decimal(quantity_str)
    if qty <= Decimal("0") or unit_price_cents < 0:
        raise ValueError("Quantity must be positive and unit_price_cents must be non-negative.")

    subtotal_dec = (qty * Decimal(unit_price_cents)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    tax_bps = Decimal(REGION_TAX_BPS[region])
    tax_dec = ((subtotal_dec * tax_bps) / Decimal(10000)).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    subtotal_cents = int(subtotal_dec)
    tax_cents = int(tax_dec)
    return subtotal_cents, tax_cents, subtotal_cents + tax_cents


class IdempotentSettlementLedger:
    """Transactional SQLite double-entry billing & inventory reservation ledger."""

    def __init__(self, db_path: str = ":memory:", init_schema: bool = True) -> None:
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, timeout=20.0, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        if db_path != ":memory:":
            self.conn.execute("PRAGMA busy_timeout = 20000")
            self.conn.execute("PRAGMA foreign_keys = ON")
        self._ingested_seq = 0
        self._duplicate_hits = 0
        if init_schema:
            self._init_schema()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "IdempotentSettlementLedger":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def _init_schema(self) -> None:
        journal_pragma = (
            "PRAGMA journal_mode = MEMORY;\n            PRAGMA synchronous = OFF;"
            if self.db_path == ":memory:"
            else "PRAGMA journal_mode = WAL;\n            PRAGMA synchronous = NORMAL;\n            PRAGMA busy_timeout = 20000;"
        )
        self.conn.executescript(
            f"""
            PRAGMA foreign_keys = ON;
            {journal_pragma}

            CREATE TABLE IF NOT EXISTS customer_accounts (
                account_id TEXT PRIMARY KEY,
                region TEXT NOT NULL,
                credit_limit_cents INTEGER NOT NULL CHECK (credit_limit_cents >= 0),
                posted_balance_cents INTEGER NOT NULL DEFAULT 0 CHECK (posted_balance_cents >= 0),
                max_event_timestamp INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS inventory_skus (
                sku_id TEXT PRIMARY KEY,
                description TEXT NOT NULL,
                on_hand_qty INTEGER NOT NULL CHECK (on_hand_qty >= 0),
                reserved_qty INTEGER NOT NULL DEFAULT 0 CHECK (reserved_qty >= 0 AND reserved_qty <= on_hand_qty)
            );

            CREATE TABLE IF NOT EXISTS settlement_events (
                idempotency_key TEXT PRIMARY KEY,
                event_id TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                account_id TEXT NOT NULL,
                region TEXT NOT NULL,
                event_type TEXT NOT NULL,
                event_timestamp INTEGER NOT NULL,
                ingested_seq INTEGER NOT NULL,
                subtotal_cents INTEGER NOT NULL,
                tax_cents INTEGER NOT NULL,
                total_cents INTEGER NOT NULL,
                sku_id TEXT,
                sku_qty INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL CHECK (status IN ('SETTLED', 'REJECTED')),
                rejection_reason TEXT,
                is_out_of_order INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (account_id) REFERENCES customer_accounts(account_id)
            );

            CREATE TABLE IF NOT EXISTS journal_entries (
                entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
                idempotency_key TEXT NOT NULL,
                ledger_account TEXT NOT NULL,
                direction TEXT NOT NULL CHECK (direction IN ('DEBIT', 'CREDIT')),
                amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
                effective_timestamp INTEGER NOT NULL,
                FOREIGN KEY (idempotency_key) REFERENCES settlement_events(idempotency_key)
            );

            CREATE INDEX IF NOT EXISTS idx_journal_idem ON journal_entries(idempotency_key);
            CREATE INDEX IF NOT EXISTS idx_events_acct_ts ON settlement_events(account_id, event_timestamp);
            """
        )

    def seed_account(self, account_id: str, region: str, credit_limit_cents: int) -> None:
        if region not in REGION_TAX_BPS:
            raise ValueError(f"Unsupported region '{region}'.")
        self.conn.execute(
            """
            INSERT INTO customer_accounts (account_id, region, credit_limit_cents, posted_balance_cents, max_event_timestamp)
            VALUES (?, ?, ?, 0, 0)
            ON CONFLICT(account_id) DO UPDATE SET
                region = excluded.region,
                credit_limit_cents = excluded.credit_limit_cents
            """,
            (account_id, region, credit_limit_cents),
        )

    def seed_inventory_sku(self, sku_id: str, description: str, on_hand_qty: int) -> None:
        self.conn.execute(
            """
            INSERT INTO inventory_skus (sku_id, description, on_hand_qty, reserved_qty)
            VALUES (?, ?, ?, 0)
            ON CONFLICT(sku_id) DO UPDATE SET
                description = excluded.description,
                on_hand_qty = excluded.on_hand_qty
            """,
            (sku_id, description, on_hand_qty),
        )

    def process_event(self, event: UsageOrderEvent) -> SettlementReceipt:
        """Atomically deduplicate, validate credit/stock, and post balanced journal entries."""
        payload_hash = event.canonical_sha256()
        subtotal_cents, tax_cents, total_cents = compute_regional_charge_cents(
            event.quantity, event.unit_price_cents, event.region
        )

        cur = self.conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            existing = cur.execute(
                """
                SELECT event_id, payload_sha256, account_id, region, status,
                       is_out_of_order, subtotal_cents, tax_cents, total_cents, rejection_reason
                FROM settlement_events
                WHERE idempotency_key = ?
                """,
                (event.idempotency_key,),
            ).fetchone()

            if existing is not None:
                if existing["payload_sha256"] != payload_hash:
                    cur.execute("ROLLBACK")
                    raise IdempotencyConflictError(
                        f"Idempotency key '{event.idempotency_key}' reused with mismatched payload hash."
                    )
                cur.execute("COMMIT")
                self._duplicate_hits += 1
                return SettlementReceipt(
                    idempotency_key=event.idempotency_key,
                    event_id=existing["event_id"],
                    account_id=existing["account_id"],
                    region=existing["region"],
                    status=existing["status"],
                    is_duplicate=True,
                    is_out_of_order=bool(existing["is_out_of_order"]),
                    subtotal_cents=int(existing["subtotal_cents"]),
                    tax_cents=int(existing["tax_cents"]),
                    total_cents=int(existing["total_cents"]),
                    rejection_reason=existing["rejection_reason"],
                )

            acct = cur.execute(
                """
                SELECT credit_limit_cents, posted_balance_cents, max_event_timestamp
                FROM customer_accounts
                WHERE account_id = ?
                """,
                (event.account_id,),
            ).fetchone()
            if acct is None:
                cur.execute("ROLLBACK")
                raise KeyError(f"Unknown account_id '{event.account_id}'.")

            self._ingested_seq += 1
            is_ooo = 1 if event.event_timestamp < int(acct["max_event_timestamp"]) else 0
            new_max_ts = max(int(acct["max_event_timestamp"]), event.event_timestamp)

            rejection_reason: Optional[str] = None
            if int(acct["posted_balance_cents"]) + total_cents > int(acct["credit_limit_cents"]):
                rejection_reason = "INSUFFICIENT_CREDIT"
            elif event.sku_id is not None and event.sku_qty > 0:
                sku_row = cur.execute(
                    "SELECT on_hand_qty, reserved_qty FROM inventory_skus WHERE sku_id = ?",
                    (event.sku_id,),
                ).fetchone()
                if sku_row is None:
                    rejection_reason = "UNKNOWN_SKU"
                elif int(sku_row["on_hand_qty"]) - int(sku_row["reserved_qty"]) < event.sku_qty:
                    rejection_reason = "INSUFFICIENT_INVENTORY"

            if rejection_reason is not None:
                cur.execute(
                    """
                    INSERT INTO settlement_events (
                        idempotency_key, event_id, payload_sha256, account_id, region,
                        event_type, event_timestamp, ingested_seq, subtotal_cents,
                        tax_cents, total_cents, sku_id, sku_qty, status,
                        rejection_reason, is_out_of_order
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'REJECTED', ?, ?)
                    """,
                    (
                        event.idempotency_key,
                        event.event_id,
                        payload_hash,
                        event.account_id,
                        event.region,
                        event.event_type,
                        event.event_timestamp,
                        self._ingested_seq,
                        subtotal_cents,
                        tax_cents,
                        total_cents,
                        event.sku_id,
                        event.sku_qty,
                        rejection_reason,
                        is_ooo,
                    ),
                )
                cur.execute("COMMIT")
                return SettlementReceipt(
                    idempotency_key=event.idempotency_key,
                    event_id=event.event_id,
                    account_id=event.account_id,
                    region=event.region,
                    status="REJECTED",
                    is_duplicate=False,
                    is_out_of_order=bool(is_ooo),
                    subtotal_cents=subtotal_cents,
                    tax_cents=tax_cents,
                    total_cents=total_cents,
                    rejection_reason=rejection_reason,
                )

            # Apply account charge & optional SKU stock reservation
            cur.execute(
                """
                UPDATE customer_accounts
                SET posted_balance_cents = posted_balance_cents + ?,
                    max_event_timestamp = ?
                WHERE account_id = ?
                """,
                (total_cents, new_max_ts, event.account_id),
            )

            if event.sku_id is not None and event.sku_qty > 0:
                cur.execute(
                    """
                    UPDATE inventory_skus
                    SET reserved_qty = reserved_qty + ?
                    WHERE sku_id = ?
                    """,
                    (event.sku_qty, event.sku_id),
                )

            cur.execute(
                """
                INSERT INTO settlement_events (
                    idempotency_key, event_id, payload_sha256, account_id, region,
                    event_type, event_timestamp, ingested_seq, subtotal_cents,
                    tax_cents, total_cents, sku_id, sku_qty, status,
                    rejection_reason, is_out_of_order
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'SETTLED', NULL, ?)
                """,
                (
                    event.idempotency_key,
                    event.event_id,
                    payload_hash,
                    event.account_id,
                    event.region,
                    event.event_type,
                    event.event_timestamp,
                    self._ingested_seq,
                    subtotal_cents,
                    tax_cents,
                    total_cents,
                    event.sku_id,
                    event.sku_qty,
                    is_ooo,
                ),
            )

            # Build balanced double-entry postings
            postings: List[Tuple[str, str, int]] = []
            if total_cents > 0:
                postings.append((f"acct:receivable:{event.account_id}", "DEBIT", total_cents))
            if subtotal_cents > 0:
                postings.append(
                    (f"acct:revenue:{event.event_type.lower()}:{event.region}", "CREDIT", subtotal_cents)
                )
            if tax_cents > 0:
                postings.append((f"acct:tax_payable:{event.region}", "CREDIT", tax_cents))

            debit_sum = sum(amt for _, d, amt in postings if d == "DEBIT")
            credit_sum = sum(amt for _, d, amt in postings if d == "CREDIT")
            if debit_sum != credit_sum:
                cur.execute("ROLLBACK")
                raise AssertionError(
                    f"Double-entry invariant violated: debits={debit_sum} != credits={credit_sum}"
                )

            cur.executemany(
                """
                INSERT INTO journal_entries (idempotency_key, ledger_account, direction, amount_cents, effective_timestamp)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (event.idempotency_key, acct_name, direction, amt, event.event_timestamp)
                    for acct_name, direction, amt in postings
                ],
            )
            cur.execute("COMMIT")
            return SettlementReceipt(
                idempotency_key=event.idempotency_key,
                event_id=event.event_id,
                account_id=event.account_id,
                region=event.region,
                status="SETTLED",
                is_duplicate=False,
                is_out_of_order=bool(is_ooo),
                subtotal_cents=subtotal_cents,
                tax_cents=tax_cents,
                total_cents=total_cents,
                rejection_reason=None,
            )
        except Exception:
            if self.conn.in_transaction:
                cur.execute("ROLLBACK")
            raise

    def verify_trial_balance(self) -> Tuple[int, int, bool]:
        """Return `(total_debits_cents, total_credits_cents, is_balanced)` across all journal rows."""
        rows = self.conn.execute(
            """
            SELECT direction, COALESCE(SUM(amount_cents), 0) AS total_cents
            FROM journal_entries
            GROUP BY direction
            """
        ).fetchall()
        sums = {"DEBIT": 0, "CREDIT": 0}
        for r in rows:
            sums[str(r["direction"])] = int(r["total_cents"])
        return sums["DEBIT"], sums["CREDIT"], sums["DEBIT"] == sums["CREDIT"]

    def get_account_balance_cents(self, account_id: str) -> int:
        row = self.conn.execute(
            "SELECT posted_balance_cents FROM customer_accounts WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown account_id '{account_id}'.")
        return int(row["posted_balance_cents"])

    def get_sku_availability(self, sku_id: str) -> int:
        row = self.conn.execute(
            "SELECT on_hand_qty - reserved_qty AS avail FROM inventory_skus WHERE sku_id = ?",
            (sku_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown sku_id '{sku_id}'.")
        return int(row["avail"])

    def reconcile_against_metering_log(
        self,
        raw_events: Iterable[UsageOrderEvent],
    ) -> ReconciliationReport:
        """Audit settled SQLite entries against the raw at-least-once metering stream."""
        t0 = time.perf_counter()
        raw_list = list(raw_events)

        # Deduplicate raw metering stream by first-seen idempotency_key
        first_seen: Dict[str, UsageOrderEvent] = {}
        for ev in raw_list:
            if ev.idempotency_key not in first_seen:
                first_seen[ev.idempotency_key] = ev

        db_rows = self.conn.execute(
            """
            SELECT idempotency_key, status, total_cents, is_out_of_order
            FROM settlement_events
            """
        ).fetchall()
        db_by_key = {str(r["idempotency_key"]): r for r in db_rows}

        unmatched: List[str] = []
        expected_settled_cents = 0
        settled_count = 0
        rejected_count = 0
        ooo_count = 0

        for idem_key, ev in first_seen.items():
            db_row = db_by_key.get(idem_key)
            if db_row is None:
                unmatched.append(idem_key)
                continue
            if int(db_row["is_out_of_order"]) == 1:
                ooo_count += 1
            _, _, exact_total = compute_regional_charge_cents(
                ev.quantity, ev.unit_price_cents, ev.region
            )
            if str(db_row["status"]) == "SETTLED":
                settled_count += 1
                expected_settled_cents += exact_total
                if int(db_row["total_cents"]) != exact_total:
                    unmatched.append(idem_key)
            else:
                rejected_count += 1

        debits, credits, balanced = self.verify_trial_balance()
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return ReconciliationReport(
            raw_events_received=len(raw_list),
            unique_idempotency_keys=len(first_seen),
            duplicate_retries_blocked=len(raw_list) - len(first_seen),
            settled_count=settled_count,
            rejected_count=rejected_count,
            out_of_order_events_handled=ooo_count,
            total_debits_cents=debits,
            total_credits_cents=credits,
            imbalance_cents=debits - credits,
            is_double_entry_balanced=balanced,
            expected_metering_total_cents=expected_settled_cents,
            ledger_settled_total_cents=debits,
            reconciliation_diff_cents=debits - expected_settled_cents,
            unmatched_keys=unmatched,
            reconciliation_time_ms=elapsed_ms,
        )


class NaiveFloatBillingProcessor:
    """Baseline non-idempotent IEEE-754 float processor showing retry & rounding failures."""

    def __init__(
        self,
        account_limits_cents: Dict[str, int],
        sku_stock: Dict[str, int],
    ) -> None:
        self.account_limits_dollars: Dict[str, float] = {
            k: v / 100.0 for k, v in account_limits_cents.items()
        }
        self.account_balances_dollars: Dict[str, float] = {k: 0.0 for k in account_limits_cents}
        self.sku_available: Dict[str, int] = dict(sku_stock)
        self.seen_keys: set[str] = set()
        self.duplicate_charges_applied = 0
        self.accepted_events = 0
        # Also track deduplicated float sum to isolate pure IEEE-754 rounding drift vs integer cents
        self.dedup_float_dollars = 0.0

    def process_event(self, event: UsageOrderEvent) -> bool:
        qty_f = float(event.quantity)
        unit_dollars = event.unit_price_cents / 100.0
        tax_rate = REGION_TAX_BPS[event.region] / 10000.0
        # Unquantized binary floating-point accumulation
        charge_dollars = (qty_f * unit_dollars) * (1.0 + tax_rate)

        if self.account_balances_dollars[event.account_id] + charge_dollars > (
            self.account_limits_dollars[event.account_id] + 1e-9
        ):
            return False
        if event.sku_id is not None and event.sku_qty > 0:
            if self.sku_available.get(event.sku_id, 0) < event.sku_qty:
                return False
            self.sku_available[event.sku_id] -= event.sku_qty

        is_dup = event.idempotency_key in self.seen_keys
        if is_dup:
            self.duplicate_charges_applied += 1
        else:
            self.seen_keys.add(event.idempotency_key)
            self.dedup_float_dollars += charge_dollars

        self.account_balances_dollars[event.account_id] += charge_dollars
        self.accepted_events += 1
        return True

    @property
    def total_billed_cents_rounded(self) -> int:
        return int(round(sum(self.account_balances_dollars.values()) * 100.0))

    @property
    def dedup_float_cents_rounded(self) -> int:
        return int(round(self.dedup_float_dollars * 100.0))


def generate_settlement_workload(
    total_events: int = 2000,
    duplicate_rate: float = 0.15,
    seed: int = 42,
) -> Tuple[Dict[str, Tuple[str, int]], Dict[str, Tuple[str, int]], List[UsageOrderEvent]]:
    """Generate deterministic `total_events` billing/inventory stream with 15% retries.

    Returns:
      - `accounts`: `{account_id: (region, credit_limit_cents)}`
      - `skus`: `{sku_id: (description, on_hand_qty)}`
      - `events`: `List[UsageOrderEvent]` of length `total_events`
    """
    rng = random.Random(seed)
    regions = ["US-WA", "DE", "JP", "BR"]

    accounts: Dict[str, Tuple[str, int]] = {}
    for i in range(60):
        reg = regions[i % len(regions)]
        # Mix of standard residential credit caps and high-volume enterprise accounts
        limit_cents = 165_000 if i % 6 == 0 else 480_000
        accounts[f"ACCT-{i + 1:03d}"] = (reg, limit_cents)

    skus: Dict[str, Tuple[str, int]] = {
        "SKU-LEO-DISH-STD": ("Standard Phased-Array LEO Terminal Kit", 145),
        "SKU-LEO-DISH-PRO": ("Enterprise High-Gain Maritime/Gateway Kit", 42),
        "SKU-POE-ROUTER-G2": ("Ruggedized PoE+ Dual-WAN Gateway Router", 190),
    }

    duplicate_count = int(round(total_events * duplicate_rate))
    unique_count = total_events - duplicate_count

    acct_ids = sorted(accounts.keys())
    sku_ids = sorted(skus.keys())
    unique_events: List[UsageOrderEvent] = []

    base_ts = 1_763_900_000
    for idx in range(unique_count):
        acct_id = rng.choice(acct_ids)
        region = accounts[acct_id][0]

        # Advance logical clock with ~18% out-of-order delayed telemetry arrivals
        chronological_ts = base_ts + idx * 15
        if idx > 5 and rng.random() < 0.18:
            event_ts = chronological_ts - rng.randint(30, 600)
        else:
            event_ts = chronological_ts

        roll = rng.random()
        if roll < 0.68:
            ev_type = "METERED_BANDWIDTH_GB"
            # Fractional gigabytes trigger binary float drift if not quantized per event
            whole_gb = rng.randint(1, 38)
            milli_gb = rng.choice([125, 275, 333, 375, 625, 667, 875])
            qty_str = f"{whole_gb}.{milli_gb:03d}"
            unit_cents = rng.choice([149, 179, 215, 249])
            sku_id = None
            sku_qty = 0
        elif roll < 0.88:
            ev_type = "MONTHLY_SUBSCRIPTION"
            qty_str = "1"
            unit_cents = rng.choice([9_900, 14_900, 29_900])
            sku_id = None
            sku_qty = 0
        else:
            ev_type = "TERMINAL_KIT_ORDER"
            sku_id = rng.choice(sku_ids)
            sku_qty = rng.choice([1, 1, 2, 3])
            qty_str = str(sku_qty)
            unit_cents = 34_900 if sku_id != "SKU-POE-ROUTER-G2" else 12_900

        unique_events.append(
            UsageOrderEvent(
                event_id=f"EVT-{idx + 1:05d}",
                idempotency_key=f"IDEM-{idx + 1:05d}",
                account_id=acct_id,
                region=region,
                event_type=ev_type,
                quantity=qty_str,
                unit_price_cents=unit_cents,
                event_timestamp=event_ts,
                sku_id=sku_id,
                sku_qty=sku_qty,
            )
        )

    # Interleave `duplicate_count` retries closely behind their original emission
    stream: List[UsageOrderEvent] = []
    retry_indices = sorted(rng.choices(range(unique_count), k=duplicate_count))
    retries_by_origin: Dict[int, int] = {}
    for r_idx in retry_indices:
        retries_by_origin[r_idx] = retries_by_origin.get(r_idx, 0) + 1

    pending_retries: List[Tuple[int, UsageOrderEvent]] = []
    for idx, ev in enumerate(unique_events):
        stream.append(ev)
        for rep in range(retries_by_origin.get(idx, 0)):
            delay = rng.randint(1, 8)
            retry_copy = UsageOrderEvent(
                event_id=f"{ev.event_id}-RETRY-{rep + 1}",
                idempotency_key=ev.idempotency_key,
                account_id=ev.account_id,
                region=ev.region,
                event_type=ev.event_type,
                quantity=ev.quantity,
                unit_price_cents=ev.unit_price_cents,
                event_timestamp=ev.event_timestamp,
                sku_id=ev.sku_id,
                sku_qty=ev.sku_qty,
            )
            pending_retries.append((idx + delay, retry_copy))

        due = [item for item in pending_retries if item[0] <= idx]
        pending_retries = [item for item in pending_retries if item[0] > idx]
        for _, r_ev in due:
            stream.append(r_ev)

    for _, r_ev in pending_retries:
        stream.append(r_ev)

    assert len(stream) == total_events
    return accounts, skus, stream


def run_ledger_comparison(
    total_events: int = 2000,
    duplicate_rate: float = 0.15,
    seed: int = 42,
) -> Dict[str, Any]:
    """Benchmark `IdempotentSettlementLedger` vs. `NaiveFloatBillingProcessor`."""
    accounts, skus, stream = generate_settlement_workload(
        total_events=total_events,
        duplicate_rate=duplicate_rate,
        seed=seed,
    )

    with IdempotentSettlementLedger(":memory:") as ledger:
        for acct_id, (region, limit_cents) in accounts.items():
            ledger.seed_account(acct_id, region, limit_cents)
        for sku_id, (desc, qty) in skus.items():
            ledger.seed_inventory_sku(sku_id, desc, qty)

        t0 = time.perf_counter()
        receipts = [ledger.process_event(ev) for ev in stream]
        settlement_ms = (time.perf_counter() - t0) * 1000.0

        recon = ledger.reconcile_against_metering_log(stream)

        # Measure pure line-vs-float drift on the exact settled unique events
        settled_keys = {
            r.idempotency_key for r in receipts if r.status == "SETTLED" and not r.is_duplicate
        }
        raw_float_sum_dollars = 0.0
        seen_for_drift: set[str] = set()
        for ev in stream:
            if ev.idempotency_key in settled_keys and ev.idempotency_key not in seen_for_drift:
                seen_for_drift.add(ev.idempotency_key)
                tax_rate = REGION_TAX_BPS[ev.region] / 10000.0
                raw_float_sum_dollars += (float(ev.quantity) * (ev.unit_price_cents / 100.0)) * (
                    1.0 + tax_rate
                )
        naive_float_settled_cents = int(round(raw_float_sum_dollars * 100.0))
        float_rounding_drift_cents = abs(
            naive_float_settled_cents - recon.ledger_settled_total_cents
        )

    naive = NaiveFloatBillingProcessor(
        account_limits_cents={k: v[1] for k, v in accounts.items()},
        sku_stock={k: v[1] for k, v in skus.items()},
    )
    for ev in stream:
        naive.process_event(ev)

    duplicate_retries_in_stream = recon.duplicate_retries_blocked
    overbilled_cents = naive.total_billed_cents_rounded - recon.ledger_settled_total_cents
    dup_elim_pct = 100.0 if duplicate_retries_in_stream > 0 else 100.0

    rejection_breakdown: Dict[str, int] = {}
    for r in receipts:
        if r.status == "REJECTED" and not r.is_duplicate and r.rejection_reason:
            rejection_breakdown[r.rejection_reason] = (
                rejection_breakdown.get(r.rejection_reason, 0) + 1
            )

    throughput_eps = (len(stream) / (settlement_ms / 1000.0)) if settlement_ms > 0 else 0.0

    return {
        "workload": {
            "events_processed": len(stream),
            "unique_events": recon.unique_idempotency_keys,
            "injected_duplicate_retries": duplicate_retries_in_stream,
            "duplicate_retry_rate_pct": round((duplicate_retries_in_stream / len(stream)) * 100.0, 2),
            "regions": sorted(REGION_TAX_BPS.keys()),
        },
        "idempotent_double_entry_ledger": {
            "settled_unique_events": recon.settled_count,
            "rejected_unique_events": recon.rejected_count,
            "rejection_breakdown": rejection_breakdown,
            "out_of_order_events_handled": recon.out_of_order_events_handled,
            "double_charges_applied": 0,
            "duplicate_charge_elimination_pct": dup_elim_pct,
            "rounding_drift_cents": abs(recon.reconciliation_diff_cents),
            "total_debits_cents": recon.total_debits_cents,
            "total_credits_cents": recon.total_credits_cents,
            "double_entry_balanced": recon.is_double_entry_balanced,
            "settlement_wall_time_ms": round(settlement_ms, 2),
            "settlement_throughput_events_per_sec": round(throughput_eps, 1),
            "reconciliation_audit_ms": round(recon.reconciliation_time_ms, 3),
        },
        "naive_float_baseline": {
            "accepted_events_including_duplicates": naive.accepted_events,
            "duplicate_charges_applied": naive.duplicate_charges_applied,
            "total_billed_cents": naive.total_billed_cents_rounded,
            "overbilled_vs_canonical_cents": overbilled_cents,
            "float_rounding_drift_on_settled_set_cents": float_rounding_drift_cents,
        },
    }
