import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from receipt_sync_server import Database, ReceiptWorker, coerce_lines, default_model_cache
import test_multi_device as fixtures


def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class ConnectionResilienceTests(fixtures.ServerFixture):
    def test_idle_tls_client_does_not_stall_other_devices(self):
        self.server.handshake_timeout = 0.3
        idle = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2)
        self.addCleanup(idle.close)
        self.assertEqual(self.request("GET", "/api/v1/health")[0], 200)
        idle.settimeout(2)
        self.assertEqual(idle.recv(1), b"")


    def test_connection_capacity_is_bounded_and_released(self):
        self.server.connection_slots = threading.BoundedSemaphore(2)
        self.server.handshake_timeout = 1
        idle = [socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2) for _ in range(2)]
        for peer in idle:
            self.addCleanup(peer.close)
        self.assertTrue(wait_until(lambda: self.server.connection_slots._value == 0))
        excess = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2)
        self.addCleanup(excess.close)
        self.assertEqual(excess.recv(1), b"")
        for peer in idle:
            peer.close()
        self.assertTrue(wait_until(lambda: self.server.connection_slots._value == 2))
        self.assertEqual(self.request("GET", "/api/v1/health")[0], 200)


    def test_incomplete_headers_have_an_absolute_deadline(self):
        self.review.request_deadline = 0.25
        peer = socket.create_connection(("127.0.0.1", self.review.server_port), timeout=2)
        self.addCleanup(peer.close)
        peer.sendall(b"GET /api/v1/health HTTP/1.1\r\nHost: localhost\r\nX-Slow: ")
        self.assertEqual(peer.recv(1), b"")
        self.assertEqual(self.request("GET", "/api/v1/summary", admin=True)[0], 200)


    def test_interrupted_upload_does_not_commit_and_can_retry(self):
        token = self.device()["sync_token"]
        body = self.receipt()
        conn = self.connection()
        self.addCleanup(conn.close)
        self.server.body_timeout = 0.1
        conn.putrequest("POST", "/api/v1/receipts")
        conn.putheader("X-Sync-Token", token)
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", "1000")
        conn.endheaders(b"{")
        response = conn.getresponse()
        self.assertEqual(response.status, 400)
        response.read()
        self.assertEqual(len(self.db.list_receipts()), 0)
        self.assertEqual(self.request("POST", "/api/v1/receipts", body, token)[0], 202)
        self.assertEqual(len(self.worker.jobs), 1)


class FakeEngine:
    def __init__(self):
        self.calls = []

    def recognize(self, path):
        self.calls.append(path)
        return coerce_lines(["Test shop", "TOTAL 10.00"]), {}


class WorkerRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="receipt-recovery-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.images = self.root / "images"
        self.images.mkdir()
        self.db = Database(self.root / "test.sqlite3", self.root / "exports")

    def record(self, status):
        receipt_id = str(uuid.uuid4())
        path = self.images / f"{receipt_id}.jpg"
        path.write_bytes(b"temporary test image")
        self.db.create_receipt(receipt_id, "test", "2026-09-07T12:00:00+08:00", str(path))
        self.db.set_receipt_status(receipt_id, status)
        return receipt_id

    def test_restart_recovers_queued_and_processing_but_never_confirmed(self):
        queued, processing = self.record("queued"), self.record("processing")
        untouched = [self.record(status) for status in ("review", "confirmed", "error")]
        engine = FakeEngine()
        restarted = Database(self.db.path, self.db.export_dir)
        worker = ReceiptWorker(restarted, self.images, default_model_cache(), engine=engine)
        self.addCleanup(worker.stop)
        self.assertTrue(wait_until(lambda: all(restarted.get_receipt(i)["status"] == "review" for i in (queued, processing))))
        self.assertEqual(len(engine.calls), 2)
        self.assertEqual([restarted.get_receipt(i)["status"] for i in untouched], ["review", "confirmed", "error"])

    def test_killed_worker_process_recovers_without_duplicating_ledger(self):
        receipt_id = self.record("queued")
        script = """
import sys
from pathlib import Path
from receipt_sync_server import Database, ReceiptWorker, default_model_cache
class InterruptedEngine:
    def recognize(self, path):
        (root / 'worker-ready').write_text('processing')
        sys.stdin.buffer.read(1)
root = Path(sys.argv[1])
db = Database(root / 'test.sqlite3', root / 'exports')
worker = ReceiptWorker(db, root / 'images', default_model_cache(), engine=InterruptedEngine())
worker.thread.join()
"""
        child = subprocess.Popen([sys.executable, "-c", script, str(self.root)], cwd=Path(__file__).parent,
                                 stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            self.assertTrue(wait_until(lambda: (self.root / "worker-ready").exists(), timeout=5), "temporary worker did not start")
            self.assertEqual(self.db.get_receipt(receipt_id)["status"], "processing")
        finally:
            child.kill()
            child.wait(timeout=5)
            child.stdin.close()
        engine = FakeEngine()
        restarted = ReceiptWorker(self.db, self.images, default_model_cache(), engine=engine)
        self.addCleanup(restarted.stop)
        self.assertTrue(wait_until(lambda: self.db.get_receipt(receipt_id)["status"] == "review"))
        self.assertEqual(len(engine.calls), 1)
        self.assertEqual(self.db.summary("2026-09")["expense_cents"], 0)

    def test_duplicate_submission_is_coalesced_and_deleted_job_is_not_revived(self):
        receipt_id = self.record("queued")
        with patch("threading.Thread.start"):
            worker = ReceiptWorker(self.db, self.images, default_model_cache(), engine=FakeEngine())
        worker.submit(receipt_id)
        worker.submit(receipt_id)
        self.assertEqual(worker.jobs.qsize(), 1)
        self.db.delete_receipt(receipt_id)
        worker.thread.start()
        self.addCleanup(worker.stop)
        self.assertTrue(wait_until(lambda: not worker.pending))
        self.assertEqual(worker.engine.calls, [])
        self.assertIsNone(self.db.get_receipt(receipt_id))

    def test_storage_failure_does_not_kill_shared_worker_or_lose_durable_job(self):
        first, second = self.record("queued"), self.record("queued")
        original = self.db.set_receipt_status
        def unavailable_for_first(receipt_id, *args, **kwargs):
            if receipt_id == first:
                raise sqlite3.OperationalError("temporary disk full")
            return original(receipt_id, *args, **kwargs)
        with patch.object(self.db, "set_receipt_status", side_effect=unavailable_for_first), patch("builtins.print"):
            worker = ReceiptWorker(self.db, self.images, default_model_cache(), engine=FakeEngine())
            try:
                self.assertTrue(wait_until(lambda: self.db.get_receipt(second)["status"] == "review"))
                self.assertTrue(worker.thread.is_alive())
                self.assertEqual(self.db.get_receipt(first)["status"], "queued")
            finally:
                worker.stop()
        recovered = ReceiptWorker(self.db, self.images, default_model_cache(), engine=FakeEngine())
        self.addCleanup(recovered.stop)
        self.assertTrue(wait_until(lambda: self.db.get_receipt(first)["status"] == "review"))

    def test_csv_disk_failure_keeps_previous_file_and_committed_id_is_retryable(self):
        entry = fixtures.ServerFixture.manual()
        self.db.add_transaction(entry)
        before = (self.db.export_dir / "transactions.csv").read_bytes()
        another = fixtures.ServerFixture.manual()
        with patch("receipt_sync_server.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.db.add_transaction(another)
        self.assertEqual((self.db.export_dir / "transactions.csv").read_bytes(), before)
        self.db.add_transaction(another)
        self.assertEqual(self.db.summary("2026-09")["expense_cents"], 500)
        self.db.export_csv()
        self.assertFalse(list(self.db.export_dir.glob("*.tmp")))
