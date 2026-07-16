from __future__ import annotations

import unittest
from datetime import UTC, datetime

from vtrade.domain.types import MicroDollars
from vtrade.ledger import AppendOnlyLedger, LedgerAccount, LedgerEntry, Posting


def entry(key: str, amount: int = 100) -> LedgerEntry:
    return LedgerEntry(
        id="entry-1",
        agent_id="agent-1",
        idempotency_key=key,
        event_type="initial_capital",
        occurred_at=datetime(2026, 7, 13, tzinfo=UTC),
        postings=(
            Posting(LedgerAccount.CASH, MicroDollars(amount)),
            Posting(LedgerAccount.PROCEEDS, MicroDollars(-amount)),
        ),
    )


class LedgerTests(unittest.TestCase):
    def test_unbalanced_entry_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "balance"):
            LedgerEntry(
                id="bad",
                agent_id="agent-1",
                idempotency_key="bad",
                event_type="bad",
                occurred_at=datetime.now(UTC),
                postings=(Posting(LedgerAccount.CASH, MicroDollars(1)),),
            )

    def test_duplicate_is_idempotent(self) -> None:
        ledger = AppendOnlyLedger()
        original = entry("same")
        self.assertIs(ledger.append(original), ledger.append(original))
        self.assertEqual(len(ledger.entries), 1)

    def test_key_reuse_with_different_event_is_rejected(self) -> None:
        ledger = AppendOnlyLedger((entry("same"),))
        with self.assertRaisesRegex(ValueError, "different"):
            ledger.append(entry("same", 200))

    def test_replay_reconstructs_balances(self) -> None:
        ledger = AppendOnlyLedger((entry("one", 10_000_000_000),))
        balances = ledger.balances("agent-1")
        self.assertEqual(balances[LedgerAccount.CASH], 10_000_000_000)
        self.assertEqual(sum(balances.values()), 0)


if __name__ == "__main__":
    unittest.main()

