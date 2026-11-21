"""SQLite 连接复用：同一 Store 复用连接、支持并发读、能显式关闭。"""

import tempfile
import threading
import unittest
from pathlib import Path

from gateway.storage import Store


class PooledStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ydd-pool-test-")
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "g.sqlite3", pooled=True)
        self.store.initialize("demo")

    def tearDown(self):
        self.store.close()

    def test_reuses_the_same_connection(self):
        with self.store.connect() as first:
            pass
        with self.store.connect() as second:
            self.assertIs(first, second)

    def test_connect_after_close_creates_new_connection(self):
        with self.store.connect() as before:
            pass
        self.store.close()
        with self.store.connect() as after:
            self.assertIsNot(before, after)
            self.assertEqual(after.execute("SELECT 1").fetchone()[0], 1)

    def test_concurrent_reads_are_safe(self):
        for index in range(5):
            self.store.create_user(f"user{index}", "hash", "member")
        results = []
        errors = []

        def read():
            try:
                results.append(len(self.store.list_users()))
            except Exception as error:      # pragma: no cover - 失败时给出原因
                errors.append(error)

        threads = [threading.Thread(target=read) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(results, [5] * 8)


if __name__ == "__main__":
    unittest.main()
