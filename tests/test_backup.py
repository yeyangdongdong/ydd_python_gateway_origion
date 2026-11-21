"""备份命令：按服务保存日志与数据库。"""

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from gateway.backup import backup
from gateway.config import Settings
from gateway.storage import Store


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ydd-backup-test-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.db = root / "gateway.sqlite3"
        Store(self.db).initialize("demo")
        self.logs = root / "logs"
        for relative in ("access/2026-11-21.log", "audit/2026-11-21.log",
                         "services/finance/2026-11-21.log",
                         "services/report/2026-11-21.log"):
            path = self.logs / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"event":"x"}\n', encoding="utf-8")
        self.dest = root / "backup" / "logs"
        self.settings = Settings(provider="demo", model="demo", db_path=self.db,
                                 routes_path=root / "no-routes.json",
                                 backup_dir=self.dest, backup_keep=2)

    def test_backup_copies_logs_by_service_and_database(self):
        result = backup(self.settings, self.logs,
                        now=datetime(2026, 11, 21, 12, 0, 0))
        snapshot = Path(result["snapshot"])
        self.assertEqual(snapshot.name, "2026-11-21_120000")
        self.assertTrue((snapshot / "logs" / "services" / "finance" /
                         "2026-11-21.log").exists())
        self.assertTrue((snapshot / "database" / "gateway.sqlite3").exists())

        manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(set(manifest["services"]), {"access", "audit", "finance", "report"})
        self.assertEqual(manifest["files"], 4)
        self.assertEqual(manifest["snapshot"], "2026-11-21_120000")

        with sqlite3.connect(snapshot / "database" / "gateway.sqlite3") as db:
            version = db.execute("SELECT version FROM schema_version").fetchone()[0]
        db.close()
        self.assertEqual(version, 5)

        actions = [event["action"] for event in Store(self.db).list_audit()]
        self.assertIn("backup.create", actions)

    def test_retention_keeps_latest_snapshots(self):
        for hour in (10, 11, 12):
            backup(self.settings, self.logs, keep=2,
                   now=datetime(2026, 11, 21, hour, 0, 0))
        snapshots = sorted(path.name for path in self.dest.iterdir() if path.is_dir())
        self.assertEqual(snapshots, ["2026-11-21_110000", "2026-11-21_120000"])

    def test_dry_run_writes_nothing(self):
        result = backup(self.settings, self.logs, dry_run=True,
                        now=datetime(2026, 11, 21, 12, 0, 0))
        self.assertTrue(result["dry_run"])
        self.assertFalse(self.dest.exists())


if __name__ == "__main__":
    unittest.main()
