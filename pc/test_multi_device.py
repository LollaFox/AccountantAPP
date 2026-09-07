from __future__ import annotations

import base64
import csv
import hashlib
import http.client
import json
import secrets
import ssl
import subprocess
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from receipt_sync_server import AppConfig, Database, Handler, ReceiptHTTPServer, default_model_cache


class QuietHandler(Handler):
    def log_message(self, *args):
        pass


class RecordingWorker:
    def __init__(self):
        self.jobs = []
        self.lock = threading.Lock()

    def submit(self, receipt_id):
        with self.lock:
            self.jobs.append(receipt_id)


class ServerFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tls_temp = tempfile.TemporaryDirectory(prefix="receipt-sync-test-tls-")
        cls.tls_root = Path(cls.tls_temp.name)
        subprocess.run(["bash", str(Path(__file__).parent / "generate_certificate.sh"), str(cls.tls_root)],
                       check=True, capture_output=True)
        cls.cert = cls.tls_root / "receipt-sync-cert.pem"
        cls.key = cls.tls_root / "receipt-sync-key.pem"
        cls.pin = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cls.cert.read_text())).digest()

    @classmethod
    def tearDownClass(cls):
        cls.tls_temp.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="receipt-sync-devices-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.legacy = secrets.token_urlsafe(24)
        self.config = AppConfig(self.root, "127.0.0.1", 0, self.legacy, default_model_cache())
        self.worker = RecordingWorker()
        self.server = ReceiptHTTPServer(("127.0.0.1", 0), QuietHandler, self.config, worker=self.worker)
        self.db = self.server.database
        self.server.allow_admin = False
        self.server.scheme = self.server.pairing_scheme = "https"
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.minimum_version = ssl.TLSVersion.TLSv1_2
        tls.load_cert_chain(self.cert, self.key)
        self.server.configure_tls(tls)
        self.review = ReceiptHTTPServer(("127.0.0.1", 0), QuietHandler, self.config,
                                        database=self.db, worker=self.worker)
        # This suite tests API authorization; cryptographic ceremonies are tested in test_passkeys.py.
        with self.review.passkeys.lock:
            self.admin_session = self.review.passkeys._new_session()["admin_session"]
        self.threads = []
        for server in (self.server, self.review):
            thread = threading.Thread(target=lambda s=server: s.serve_forever(poll_interval=0.02), daemon=True)
            thread.start()
            self.threads.append(thread)
        self.addCleanup(self.stop_servers)

    def stop_servers(self):
        for server in (self.server, self.review):
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join(timeout=2)

    def connection(self):
        # A real pinned TLS client against a temporary loopback server, never user data.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        connection = http.client.HTTPSConnection("127.0.0.1", self.server.server_port, context=context, timeout=5)
        connection.connect()
        self.assertTrue(secrets.compare_digest(hashlib.sha256(connection.sock.getpeercert(binary_form=True)).digest(), self.pin))
        return connection

    def request(self, method, path, body=None, token=None, *, admin=False, headers=None, connection=None):
        owned = connection is None
        conn = connection or (http.client.HTTPConnection("127.0.0.1", self.review.server_port, timeout=5) if admin else self.connection())
        request_headers = dict(headers or {})
        if admin:
            request_headers.setdefault("X-Admin-Session", self.admin_session)
        if token is not None:
            request_headers["X-Sync-Token"] = token
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        try:
            conn.request(method, path, json.dumps(body) if body is not None else None, request_headers)
            response = conn.getresponse()
            data = json.loads(response.read())
            return response.status, data
        finally:
            if owned:
                conn.close()

    def device(self, name="Phone", role="contributor"):
        with self.review.passkeys.lock:
            self.admin_session = self.review.passkeys._new_session()["admin_session"]
        status, device = self.request("POST", "/api/v1/devices", {"name": name, "role": role}, admin=True)
        self.assertEqual(status, 201)
        return device

    @staticmethod
    def manual(request_id=None, amount=250):
        return {"request_id": request_id or str(uuid.uuid4()), "occurred_at": "2026-09-04T12:00:00+08:00",
                "kind": "expense", "category": "交通", "amount_cents": amount, "content": "测试"}

    @staticmethod
    def receipt(receipt_id=None):
        return {"id": receipt_id or str(uuid.uuid4()), "device_id": "untrusted-device-name",
                "captured_at": "2026-09-04T12:00:00+08:00",
                "image_base64": base64.b64encode(b"\xff\xd8\xff" + b"test-image" * 10).decode()}

class MultiDeviceTests(ServerFixture):
    def test_two_devices_share_server_and_ledger_but_have_distinct_credentials(self):
        first, second = self.device("Phone"), self.device("Tablet")
        self.assertTrue(first["sync_token"] != second["sync_token"])
        for device in (first, second):
            status, handshake = self.request("GET", "/api/v1/connection", token=device["sync_token"])
            self.assertEqual(status, 200)
            self.assertEqual(handshake["server_id"], self.db.server_id)
            self.assertEqual(handshake["device"]["id"], device["id"])
            self.assertEqual(handshake["protocol_version"], 2)
            self.assertNotIn("sync_token", handshake)
        self.assertEqual(self.request("POST", "/api/v1/transactions", self.manual(), first["sync_token"])[0], 201)
        status, summary = self.request("GET", "/api/v1/summary?month=2026-09", token=second["sync_token"])
        self.assertEqual(status, 200)
        self.assertEqual(summary["expense_cents"], 250)

    def test_revocation_is_individual_and_applies_on_reused_connections(self):
        first, second = self.device(), self.device()
        conn = self.connection()
        self.addCleanup(conn.close)
        self.assertEqual(self.request("GET", "/api/v1/connection", token=first["sync_token"], connection=conn)[0], 200)
        self.assertEqual(self.request("DELETE", "/api/v1/devices/" + first["id"], admin=True)[0], 200)
        self.assertEqual(self.request("GET", "/api/v1/connection", token=first["sync_token"], connection=conn)[0], 401)
        self.assertEqual(self.request("GET", "/api/v1/connection", token=second["sync_token"])[0], 200)
        restarted = Database(self.db.path, self.db.export_dir)
        self.assertIsNone(restarted.authenticate_device(first["sync_token"]))
        self.assertEqual(restarted.server_id, self.db.server_id)

    def test_migration_preserves_existing_data_and_legacy_revocation_survives_restart(self):
        self.db.add_transaction(self.manual())
        self.assertIsNotNone(self.db.authenticate_device(self.legacy))
        self.db.revoke_device("legacy")
        restarted = Database(self.db.path, self.db.export_dir)
        restarted.import_legacy_device(self.legacy)
        self.assertIsNone(restarted.authenticate_device(self.legacy))
        self.assertEqual(restarted.summary("2026-09")["expense_cents"], 250)

    def test_only_hashes_are_stored_and_tokens_are_not_listed(self):
        device = self.device()
        status, listing = self.request("GET", "/api/v1/devices", admin=True)
        self.assertEqual(status, 200)
        self.assertNotIn(device["sync_token"], json.dumps(listing))
        self.assertNotIn("token_hash", json.dumps(listing))
        with self.db.session() as db:
            row = db.execute("SELECT token_hash FROM devices WHERE id=?", (device["id"],)).fetchone()
        self.assertTrue(row[0] != device["sync_token"])

    def test_anonymous_health_is_minimal_and_handshake_requires_token_even_on_loopback(self):
        status, health = self.request("GET", "/api/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(set(health), {"status", "service", "time"})
        self.assertEqual(self.request("GET", "/api/v1/connection")[0], 401)
        self.assertEqual(self.request("GET", "/api/v1/connection", admin=True)[0], 401)
        self.assertEqual(self.request("GET", "/")[0], 404)

    def test_phone_token_has_only_required_endpoints_and_never_becomes_local_admin(self):
        token = self.device()["sync_token"]
        receipt_id = str(uuid.uuid4())
        for method, path in [
            ("GET", "/api/v1/devices"), ("POST", "/api/v1/devices"),
            ("DELETE", "/api/v1/devices/legacy"), ("GET", "/api/v1/pairing"),
            ("GET", "/api/v1/transactions"), ("GET", "/api/v1/receipts"),
            ("POST", f"/api/v1/receipts/{receipt_id}/confirm"),
            ("DELETE", f"/api/v1/receipts/{receipt_id}"),
            ("GET", f"/api/v1/receipts/{receipt_id}?raw=0&raw=1"),
        ]:
            with self.subTest(method=method, path=path):
                self.assertEqual(self.request(method, path, {} if method == "POST" else None, token)[0], 403)
                self.assertEqual(self.request(method, path, {} if method == "POST" else None, token, admin=True)[0], 403)
        self.assertEqual(self.request("GET", "/", admin=True, headers={"Host": "untrusted.invalid"})[0], 404)

    def test_viewer_can_read_summary_but_cannot_upload_or_write(self):
        token = self.device(role="viewer")["sync_token"]
        self.assertEqual(self.request("GET", "/api/v1/summary", token=token)[0], 200)
        self.assertEqual(self.request("GET", "/api/v1/connection", token=token)[0], 200)
        self.assertEqual(self.request("POST", "/api/v1/receipts", self.receipt(), token)[0], 403)
        self.assertEqual(self.request("POST", "/api/v1/transactions", self.manual(), token)[0], 403)
        self.assertEqual(self.request("GET", "/api/v1/receipts/" + str(uuid.uuid4()), token=token)[0], 403)

    def test_wrong_server_identity_rejects_writes(self):
        token = self.device()["sync_token"]
        status, result = self.request("POST", "/api/v1/transactions", self.manual(), token,
                                      headers={"X-Server-ID": str(uuid.uuid4())})
        self.assertEqual(status, 409)
        self.assertEqual(result["code"], "server_changed")
        self.assertEqual(self.db.summary("2026-09")["expense_cents"], 0)

    def test_manual_retry_is_idempotent_and_changed_payload_is_rejected(self):
        token = self.device()["sync_token"]
        body = self.manual()
        first = self.request("POST", "/api/v1/transactions", body, token)
        second = self.request("POST", "/api/v1/transactions", body, token)
        self.assertEqual(first, second)
        self.assertEqual(self.db.summary("2026-09")["expense_cents"], 250)
        changed = {**body, "amount_cents": 900}
        self.assertEqual(self.request("POST", "/api/v1/transactions", changed, token)[0], 409)
        self.db.delete_manual_transaction(first[1]["id"])
        self.assertEqual(self.request("POST", "/api/v1/transactions", body, token), first)
        self.assertEqual(self.db.summary("2026-09")["expense_cents"], 0)

    def test_simultaneous_device_submissions_have_consistent_summary_and_export(self):
        tokens = [self.device(str(i))["sync_token"] for i in range(3)]
        bodies = [self.manual() for _ in range(12)]
        def send(index):
            return self.request("POST", "/api/v1/transactions", bodies[index % 12], tokens[index % 3])[0]
        with ThreadPoolExecutor(max_workers=6) as pool:
            statuses = list(pool.map(send, range(24)))
        self.assertEqual(statuses, [201] * 24)
        self.assertEqual(self.db.summary("2026-09")["expense_cents"], 12 * 250)
        with (self.db.export_dir / "transactions.csv").open(encoding="utf-8-sig") as handle:
            self.assertEqual(len(list(csv.DictReader(handle))), 12)

    def test_duplicate_receipts_queue_once_and_deleted_receipts_cannot_revive(self):
        device = self.device()
        body = self.receipt()
        def upload(_):
            return self.request("POST", "/api/v1/receipts", body, device["sync_token"])[0]
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(list(pool.map(upload, range(4))), [202] * 4)
        self.assertEqual(self.worker.jobs, [body["id"]])
        receipt = self.db.get_receipt(body["id"])
        self.assertEqual(receipt["device_id"], device["id"])
        status, remote = self.request("GET", "/api/v1/receipts/" + body["id"], token=device["sync_token"])
        self.assertEqual(status, 200)
        self.assertEqual(set(remote), {"id", "status", "error", "transaction"})
        self.assertEqual(self.db.summary("2026-09")["expense_cents"], 0)
        self.db.delete_receipt(body["id"])
        self.assertEqual(upload(0), 410)
        self.assertEqual(self.request("GET", "/api/v1/receipts/" + body["id"], token=device["sync_token"])[0], 404)
        self.assertFalse(Path(receipt["image_path"]).exists())
        self.assertEqual(self.worker.jobs, [body["id"]])

    def test_reused_receipt_id_cannot_overwrite_image(self):
        token = self.device()["sync_token"]
        body = self.receipt()
        self.assertEqual(self.request("POST", "/api/v1/receipts", body, token)[0], 202)
        altered = {**body, "image_base64": base64.b64encode(b"\xff\xd8\xff" + b"different" * 10).decode()}
        self.assertEqual(self.request("POST", "/api/v1/receipts", altered, token)[0], 409)
        image = Path(self.db.get_receipt(body["id"])["image_path"]).read_bytes()
        self.assertEqual(image, base64.b64decode(body["image_base64"]))


if __name__ == "__main__":
    unittest.main()
