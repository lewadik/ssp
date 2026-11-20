import os
import unittest

from ssipng.state import State
from ssipng.util import ServiceError


class TestState(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.path = os.path.join(self.tmp, "state.json")

    def tearDown(self):
        self._tmp.cleanup()

    def test_roundtrip(self):
        s = State(self.path)
        s.set(101, {"vmid": 101, "domain": "a.sslip.io"})
        s2 = State(self.path)
        self.assertEqual(s2.get(101), {"vmid": 101, "domain": "a.sslip.io"})
        self.assertEqual(list(s2.all()), ["101"])

    def test_remove(self):
        s = State(self.path)
        s.set(101, {"vmid": 101})
        s.remove(101)
        self.assertIsNone(State(self.path).get(101))

    def test_missing_file_is_empty(self):
        self.assertEqual(State(self.path).all(), {})

    def test_corrupt_file_recovered(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        s = State(self.path)
        self.assertEqual(s.all(), {})
        corrupt = [f for f in os.listdir(self.tmp) if ".corrupt-" in f]
        self.assertEqual(len(corrupt), 1)

    def test_find_by_domain(self):
        s = State(self.path)
        s.set(7, {"vmid": 7, "domain": "x.sslip.io"})
        self.assertEqual(s.find_by_domain("x.sslip.io"), "7")
        self.assertIsNone(s.find_by_domain("y.sslip.io"))

    def test_lock_creates_and_removes(self):
        s = State(self.path)
        with s.lock():
            self.assertTrue(os.path.exists(s._lock_path))
        self.assertFalse(os.path.exists(s._lock_path))

    def test_lock_blocks_on_fresh_lock(self):
        s = State(self.path)
        s._lock_path.write_text("", encoding="utf-8")
        with self.assertRaises(ServiceError):
            with s.lock(timeout=0.3):
                pass

    def test_stale_lock_is_stolen(self):
        import time
        s = State(self.path)
        s._lock_path.write_text("", encoding="utf-8")
        old = time.time() - 600
        os.utime(s._lock_path, (old, old))
        with s.lock():
            pass  # stale lock removed and acquired

    def test_lock_released_after_exception(self):
        s = State(self.path)
        try:
            with s.lock():
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        self.assertFalse(os.path.exists(s._lock_path))


if __name__ == "__main__":
    unittest.main()
