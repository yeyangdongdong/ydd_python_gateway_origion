"""审计保留期清理的测试。"""

import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from gateway.maintenance import audit_janitor, prune_audit
from gateway.storage import Store, now


class AuditRetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ydd-retention-test-")
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "g.sqlite3")
        self.store.initialize("demo")

    def add_event(self, action: str, timestamp: str) -> None:
        self.store.append_audit({
            "id": f"{action}-{timestamp}", "ts": timestamp, "trace_id": "",
            "actor_type": "system", "actor_id": "", "actor_name": "test",
            "action": action, "target_type": "", "target_id": "",
            "result": "ok", "reason": "", "ip": "",
            "details": "{}",
        })

    def test_prune_removes_only_old_rows(self):
        old = "2020-01-01T00:00:00+00:00"
        self.add_event("old.event", old)
        self.add_event("new.event", now())

        self.assertEqual(prune_audit(self.store, 180, dry_run=True), 1)
        self.assertEqual(len(self.store.list_audit()), 2)

        self.assertEqual(prune_audit(self.store, 180), 1)
        remaining = self.store.list_audit()
        self.assertEqual([event["action"] for event in remaining], ["new.event"])

    def test_janitor_prunes_and_audits(self):
        self.add_event("old.event", "2020-01-01T00:00:00+00:00")
        audit = mock.Mock()
        audit.append = mock.Mock()

        async def scenario():
            task = asyncio.create_task(
                audit_janitor(self.store, audit, 180, interval_seconds=0.02))
            await asyncio.sleep(0.3)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(scenario())
        self.assertEqual(len(self.store.list_audit()), 0)
        audit.append.assert_called()

    def test_prune_batches(self):
        for index in range(5):
            self.add_event(f"old-{index}", "2020-01-01T00:00:00+00:00")
        self.assertEqual(self.store.prune_audit(now(), batch=2), 5)
        self.assertEqual(self.store.list_audit(), [])


if __name__ == "__main__":
    unittest.main()
