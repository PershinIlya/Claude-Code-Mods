"""The SQLite store: opening a new database."""

import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest

from ctempo.store import SCHEMA_VERSION, Store


class FirstOpen(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ctempo-test-")
        self.path = os.path.join(self.dir, "tempo.db")
        # Another opener of the same new file in the middle of its own first
        # open: it holds the write lock that the switch to WAL needs.
        self.other = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.other.execute("BEGIN IMMEDIATE")

    def tearDown(self):
        self.other.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_a_concurrent_switch_to_wal_is_waited_out(self):
        # Review 7 (fresh-eyes#1): when several sessions opened a new tempo.db
        # at once (install makes every open session re-render), the losers'
        # PRAGMA journal_mode=WAL got "database is locked" at once instead of
        # waiting the busy timeout: the statusline fell back to the bare line
        # and lost its reading (D-05). Here the other opener is done in 50 ms.
        timer = threading.Timer(0.05, self.other.execute, ("COMMIT",))
        timer.start()
        try:
            st = Store(self.path, timeout_ms=1000)
        finally:
            timer.join()
        try:
            self.assertEqual(st.db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(st.db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        finally:
            st.close()

    def test_it_waits_no_longer_than_the_busy_timeout(self):
        # Review 7 (fresh-eyes#1): it waits up to the busy timeout, not
        # forever, then fails as before.
        t = time.monotonic()
        with self.assertRaises(sqlite3.OperationalError):
            Store(self.path, timeout_ms=100)
        self.assertGreaterEqual(time.monotonic() - t, 0.09)
        self.assertLess(time.monotonic() - t, 2.0)


if __name__ == "__main__":
    unittest.main()
