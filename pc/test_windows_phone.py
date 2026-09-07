from __future__ import annotations

import base64
import hashlib
import json
import secrets
import ssl
import sys
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[1]
PC = Path(__file__).resolve().parent
for path in (ROOT, PC):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from windows.phone_sync import (
    AppSettings,
    CertificateRejected,
    HongKongDate,
    MemorySecretStore,
    PairingInput,
    PairingInvitation,
    PendingReceipt,
    PhoneStore,
    PinnedHTTPSConnection,
    SyncClient,
    SyncError,
    certificate_sha256,
    default_phone_data_dir,
    format_hkd,
    new_device_token,
    sniff_image_suffix,
)
import test_multi_device as fixtures


JPEG_BYTES = b"\xff\xd8\xff" + b"x" * 40
SERVER_ID = "2bfc30f1-f7cb-40a3-80db-9417f11b4072"
CERT = "A" * 64


def invitation_uri(
    server_id: str = SERVER_ID,
    addresses: list[str] | None = None,
    token: str | None = None,
    expires_at: float | None = None,
    fingerprint: str = CERT,
) -> str:
    payload = {
        "version": 1,
        "server_id": server_id,
        "addresses": ["https://172.20.10.2:8765"] if addresses is None else addresses,
        "certificate_sha256": fingerprint,
        "invitation_token": token or secrets.token_urlsafe(32),
        "expires_at": expires_at if expires_at is not None else time.time() + 120,
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    return "receiptsync://pair?data=" + encoded


def connection_body(server_id: str = SERVER_ID, role: str = "contributor") -> bytes:
    return json.dumps(
        {
            "service": "receipt-sync",
            "protocol_version": 2,
            "server_id": server_id,
            "device": {"id": "dev-1", "name": "Windows", "role": role},
        }
    ).encode()


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.role = "contributor"
        self.server_id = SERVER_ID
        self.device_id = str(uuid.uuid4())
        self.base_url = "https://172.20.10.2:8765"
        self.remote = {"status": "review", "error": None, "transaction": None, "id": ""}
        self.summary_payload = {
            "month": "2026-09",
            "currency": "HKD",
            "income_cents": 100,
            "expense_cents": 40,
            "balance_cents": 60,
            "income_by_category": [],
            "expense_by_category": [],
            "pending_receipts": 1,
            "updated_at": None,
        }
        self.upload_error: Exception | None = None
        self.receipt_error: Exception | None = None
        self.manual_errors: dict[str, Exception] = {}
        self.transactions: list[dict[str, object]] = []
        self.redeemed: list[str] = []

    def redeem_invitation(self, draft, settings):  # type: ignore[no-untyped-def]
        self.redeemed.append(draft.device_token)
        self.calls.append(("redeem", draft.claim_id))
        return {"device_id": self.device_id, "server_id": draft.invitation.server_id}

    def connect(self, settings):  # type: ignore[no-untyped-def]
        from windows.phone_sync import VerifiedConnection

        self.calls.append(("connect", settings.sync_token))
        return VerifiedConnection(self.server_id, self.device_id, self.role, self.base_url)

    def upload(self, receipt, image_data, settings):  # type: ignore[no-untyped-def]
        self.calls.append(("upload", receipt.id))
        if self.upload_error:
            raise self.upload_error
        payload = dict(self.remote)
        payload["id"] = receipt.id
        return payload

    def receipt(self, receipt_id: str, settings):  # type: ignore[no-untyped-def]
        self.calls.append(("receipt", receipt_id))
        if self.receipt_error:
            raise self.receipt_error
        payload = dict(self.remote)
        payload["id"] = receipt_id
        return payload

    def summary(self, month: str, settings):  # type: ignore[no-untyped-def]
        from windows.phone_sync import MonthlySummary

        self.calls.append(("summary", month))
        payload = dict(self.summary_payload)
        payload["month"] = month
        return MonthlySummary.from_payload(payload)

    def add_transaction(self, payload, settings):  # type: ignore[no-untyped-def]
        request_id = str(payload["request_id"])
        self.calls.append(("manual", request_id))
        if request_id in self.manual_errors:
            raise self.manual_errors[request_id]
        self.transactions.append(payload)
        return {"id": f"manual-{request_id}"}


class HongKongDateTests(unittest.TestCase):
    def test_month_key_uses_hong_kong_calendar_boundary(self) -> None:
        self.assertEqual(HongKongDate.month_key(datetime(2026, 8, 31, 15, 59, tzinfo=timezone.utc)), "2026-08")
        self.assertEqual(HongKongDate.month_key(datetime(2026, 8, 31, 16, 30, tzinfo=timezone.utc)), "2026-09")

    def test_iso_string_includes_colon_separated_offset(self) -> None:
        self.assertEqual(
            HongKongDate.iso_string(datetime(2026, 9, 4, 4, 0, tzinfo=timezone.utc)),
            "2026-09-04T12:00:00+08:00",
        )

    def test_currency_formats_hkd(self) -> None:
        self.assertEqual(format_hkd(0), "HK$0.00")
        self.assertEqual(format_hkd(250), "HK$2.50")


class PairingInputTests(unittest.TestCase):
    def test_invitation_round_trip(self) -> None:
        token = secrets.token_urlsafe(32)
        uri = invitation_uri(token=token)
        parsed = PairingInvitation.parse(uri)
        self.assertEqual(parsed.server_id, SERVER_ID)
        self.assertEqual(parsed.invitation_token, token)
        self.assertEqual(parsed.addresses, ["https://172.20.10.2:8765"])

    def test_invitation_rejects_insecure_or_malformed_addresses(self) -> None:
        for addresses in (
            ["http://192.0.2.10:8765"],
            ["https://user:password@192.0.2.10:8765"],
            [],
            ["https://192.0.2.10:8765/admin"],
            ["https://192.0.2.10:8765 https://192.0.2.20:8765"],
        ):
            with self.assertRaises(SyncError):
                PairingInvitation.parse(invitation_uri(addresses=addresses))

    def test_http_and_query_urls_are_rejected(self) -> None:
        self.assertEqual(PairingInput.candidate_urls("http://192.0.2.10:8765"), [])
        self.assertEqual(PairingInput.candidate_urls("https://192.0.2.10:8765/admin"), [])
        self.assertFalse(PairingInput.is_ready_to_sync("https://192.0.2.10:8765", "", CERT))

    def test_hotspot_address_is_tried_before_campus_address(self) -> None:
        urls = PairingInput.preferred_sync_urls("https://10.71.6.34:8765 https://172.20.10.2:8765")
        self.assertEqual(urls[0], "https://172.20.10.2:8765")

    def test_device_token_matches_server_length(self) -> None:
        self.assertRegex(new_device_token(), r"^[A-Za-z0-9_-]{43}$")


class PhoneStoreTests(unittest.TestCase):
    def _store(self, client: FakeClient | None = None, secrets: MemorySecretStore | None = None) -> tuple[PhoneStore, Path, FakeClient]:
        temp = Path(tempfile.mkdtemp())
        fake = client or FakeClient()
        store = PhoneStore(data_dir=temp, secrets=secrets or MemorySecretStore(), client=fake)
        return store, temp, fake

    def _queue(self, store: PhoneStore, status: str = "waiting", uploaded: bool = False, server_id: str = SERVER_ID) -> PendingReceipt:
        receipt_id = str(uuid.uuid4())
        file_name = f"{receipt_id}.jpg"
        (store.data_dir / file_name).write_bytes(JPEG_BYTES)
        receipt = PendingReceipt(
            id=receipt_id,
            captured_at="2026-09-07T12:00:00+08:00",
            image_file_name=file_name,
            status=status,
            server_id=server_id,
            uploaded=uploaded,
        )
        store.receipts.append(receipt)
        store._persist_queue()
        return receipt

    def test_default_data_dir_is_not_the_computer_ledger(self) -> None:
        path = default_phone_data_dir()
        self.assertTrue(str(path).endswith("ReceiptSyncPhone"))
        self.assertNotEqual(path.name, "ReceiptSync")

    def test_invitation_is_persisted_before_network(self) -> None:
        store, _temp, fake = self._store()

        def redeem(draft, settings):  # type: ignore[no-untyped-def]
            self.assertIsNotNone(store.secrets.read("pendingPairing"))
            return FakeClient.redeem_invitation(fake, draft, settings)

        fake.redeem_invitation = redeem  # type: ignore[method-assign]
        store.pair_with_invitation(invitation_uri())
        self.assertEqual(store.settings.server_id, SERVER_ID)
        self.assertEqual(store.settings.role, "contributor")
        self.assertIsNone(store.secrets.read("pendingPairing"))
        saved = json.loads(store.secrets.read("pairing") or "{}")
        self.assertIn("sync_token", saved)
        public = json.loads((store.data_dir / "settings.json").read_text(encoding="utf-8"))
        self.assertNotIn("sync_token", public)
        self.assertNotIn("certificate_sha256", public)

    def test_pairing_storage_failure_never_sends_invitation(self) -> None:
        class FailPending(MemorySecretStore):
            def write(self, account: str, value: str) -> None:
                if account == "pendingPairing" and value:
                    raise OSError("cannot save pairing")
                super().write(account, value)

        fake = FakeClient()
        store, _temp, _fake = self._store(fake, FailPending())
        with self.assertRaises(OSError):
            store.pair_with_invitation(invitation_uri())
        self.assertEqual(fake.redeemed, [])

    def test_expired_or_different_server_invitation_never_sends_credentials(self) -> None:
        store, _temp, fake = self._store()
        store.pair_with_invitation(invitation_uri())
        fake.redeemed.clear()
        with self.assertRaises(SyncError):
            store.pair_with_invitation(invitation_uri(expires_at=0))
        with self.assertRaises(SyncError):
            store.pair_with_invitation(invitation_uri(server_id=str(uuid.uuid4())))
        self.assertEqual(fake.redeemed, [])

    def test_lost_redeem_response_reuses_the_same_device_token(self) -> None:
        store, _temp, fake = self._store()
        uri = invitation_uri()

        def redeem(draft, settings):  # type: ignore[no-untyped-def]
            FakeClient.redeem_invitation(fake, draft, settings)
            if len(fake.redeemed) == 1:
                raise SyncError(SyncError.unreachable, retry_next=True)
            return {"device_id": fake.device_id, "server_id": draft.invitation.server_id}

        fake.redeem_invitation = redeem  # type: ignore[method-assign]
        with self.assertRaises(SyncError):
            store.pair_with_invitation(uri)
        self.assertIsNotNone(store.pending_pairing)
        store.pair_with_invitation(uri)
        self.assertEqual(fake.redeemed[0], fake.redeemed[1])
        self.assertIsNone(store.pending_pairing)

    def test_failed_handshake_does_not_overwrite_saved_settings(self) -> None:
        store, _temp, fake = self._store()
        store.pair_with_invitation(invitation_uri())
        previous = store.settings.sync_token
        fake.role = "viewer"

        def connect(settings):  # type: ignore[no-untyped-def]
            raise SyncError(SyncError.unauthorized)

        fake.connect = connect  # type: ignore[method-assign]
        with self.assertRaises(SyncError):
            store.test_and_save(store.settings.server_url, "wrong-device-token-value-xxxxxxxxxxxx", store.settings.certificate_sha256)
        self.assertEqual(store.settings.sync_token, previous)
        self.assertEqual(store.settings.role, "contributor")

    def test_viewer_cannot_upload_or_add_manual(self) -> None:
        fake = FakeClient()
        fake.role = "viewer"
        store, _temp, _fake = self._store(fake)
        store.pair_with_invitation(invitation_uri())
        self.assertFalse(store.can_write)
        with self.assertRaises(SyncError):
            store.enqueue_image(JPEG_BYTES)
        with self.assertRaises(SyncError):
            store.add_manual("income", datetime.now(timezone.utc), "工资", Decimal("1"), "", "")

    def test_oversized_manual_is_rejected_before_queueing(self) -> None:
        store, _temp, fake = self._store()
        store.pair_with_invitation(invitation_uri())
        fake.calls.clear()
        with self.assertRaises(SyncError):
            store.add_manual("expense", datetime.now(timezone.utc), "字" * 101, Decimal("1"), "", "")
        self.assertEqual(store.manuals, [])
        self.assertFalse(any(kind == "manual" for kind, _name in fake.calls))

    def test_rejected_manual_does_not_block_valid_entries_or_summary(self) -> None:
        store, _temp, fake = self._store()
        store.pair_with_invitation(invitation_uri())
        first_id = str(uuid.uuid4())
        second_id = str(uuid.uuid4())
        fake.manual_errors[first_id] = SyncError(SyncError.invalid_entry)
        store.add_manual("expense", datetime.now(timezone.utc), "无效", Decimal("1"), "", "", request_id=first_id)
        store.add_manual("expense", datetime.now(timezone.utc), "有效", Decimal("2"), "", "", request_id=second_id)
        self.assertEqual(store.manuals[0].blocked_reason, "invalid")
        self.assertEqual(len(fake.transactions), 1)
        self.assertTrue(any(kind == "summary" for kind, _name in fake.calls))
        fake.calls.clear()
        store.sync_all()
        self.assertEqual([kind for kind, _name in fake.calls if kind == "manual"], [])

    def test_correcting_invalid_manual_keeps_request_id(self) -> None:
        store, _temp, fake = self._store()
        store.pair_with_invitation(invitation_uri())
        request_id = str(uuid.uuid4())
        fake.manual_errors[request_id] = SyncError(SyncError.invalid_entry)
        store.add_manual("expense", datetime.now(timezone.utc), "错误", Decimal("1"), "", "", request_id=request_id)
        del fake.manual_errors[request_id]
        store.add_manual("expense", datetime.now(timezone.utc), "修正", Decimal("2"), "更正", "", request_id=request_id)
        self.assertEqual(fake.transactions[-1]["request_id"], request_id)
        self.assertEqual(fake.transactions[-1]["category"], "修正")
        self.assertEqual(store.manuals, [])

    def test_conflict_manual_cannot_be_blindly_rewritten(self) -> None:
        store, _temp, fake = self._store()
        store.pair_with_invitation(invitation_uri())
        request_id = str(uuid.uuid4())
        fake.manual_errors[request_id] = SyncError(SyncError.conflict)
        store.add_manual("expense", datetime.now(timezone.utc), "冲突", Decimal("1"), "", "", request_id=request_id)
        with self.assertRaises(SyncError):
            store.add_manual("expense", datetime.now(timezone.utc), "修正", Decimal("2"), "", "", request_id=request_id)
        store.discard_rejected_manual(request_id)
        self.assertEqual(store.manuals, [])

    def test_deleted_receipt_410_clears_local_queue(self) -> None:
        store, temp, fake = self._store()
        store.pair_with_invitation(invitation_uri())
        fake.upload_error = SyncError(SyncError.deleted)
        store.enqueue_image(JPEG_BYTES)
        self.assertEqual(store.receipts, [])
        self.assertEqual(list(temp.glob("*.jpg")), [])

    def test_missing_unacknowledged_upload_retries_same_uuid(self) -> None:
        store, _temp, fake = self._store()
        store.pair_with_invitation(invitation_uri())
        receipt = self._queue(store, status="uploading", uploaded=False)
        fake.receipt_error = SyncError(SyncError.not_found)
        fake.calls.clear()
        store.sync_all()
        self.assertIn(("receipt", receipt.id), fake.calls)
        self.assertIn(("upload", receipt.id), fake.calls)
        self.assertEqual(store.receipts[0].id, receipt.id)

    def test_uploaded_receipt_404_clears_queue(self) -> None:
        store, temp, fake = self._store()
        store.pair_with_invitation(invitation_uri())
        receipt = self._queue(store, status="review", uploaded=True)
        fake.receipt_error = SyncError(SyncError.not_found)
        store.sync_all()
        self.assertEqual(store.receipts, [])
        self.assertFalse((temp / receipt.image_file_name).exists())

    def test_old_server_queue_is_not_uploaded(self) -> None:
        store, _temp, fake = self._store()
        store.pair_with_invitation(invitation_uri())
        self._queue(store, server_id=str(uuid.uuid4()))
        fake.calls.clear()
        store.sync_all()
        self.assertFalse(any(kind == "upload" for kind, _name in fake.calls))
        self.assertEqual(store.held_receipt_count, 1)

    def test_unauthorized_upload_stops_the_rest_of_the_queue(self) -> None:
        store, _temp, fake = self._store()
        store.pair_with_invitation(invitation_uri())
        first = self._queue(store)
        self._queue(store)
        fake.upload_error = SyncError(SyncError.unauthorized)
        fake.calls.clear()
        with self.assertRaises(SyncError):
            store.sync_all()
        self.assertEqual([name for kind, name in fake.calls if kind == "upload"], [first.id])
        self.assertFalse(any(kind == "summary" for kind, _name in fake.calls))

    def test_manual_entry_uses_request_id_and_hong_kong_offset(self) -> None:
        store, _temp, fake = self._store()
        store.pair_with_invitation(invitation_uri())
        when = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
        store.add_manual("income", when, "工资", Decimal("12.35"), "", "")
        self.assertEqual(fake.transactions[0]["occurred_at"], "2026-09-07T20:00:00+08:00")
        self.assertEqual(fake.transactions[0]["amount_cents"], 1235)
        self.assertRegex(str(fake.transactions[0]["request_id"]), r"^[0-9a-f-]{36}$")

    def test_forget_computer_keeps_queue(self) -> None:
        store, _temp, fake = self._store()
        store.pair_with_invitation(invitation_uri())
        fake.remote = {"status": "review", "error": None, "transaction": None, "id": "x"}
        store.enqueue_image(JPEG_BYTES)
        self.assertTrue(store.receipts)
        store.forget_computer()
        self.assertFalse(store.settings.configured)
        self.assertTrue(store.receipts)

    def test_jpeg_and_png_magic_bytes_are_required(self) -> None:
        self.assertEqual(sniff_image_suffix(JPEG_BYTES), ".jpg")
        with self.assertRaises(ValueError):
            sniff_image_suffix(b"not-an-image" + b"x" * 40)


class SyncClientTests(unittest.TestCase):
    def test_client_sends_connection_handshake_and_server_id(self) -> None:
        requests: list[Request] = []

        def transport(request: Request, fingerprint: str) -> tuple[int, bytes]:
            requests.append(request)
            self.assertNotIn("origin", {key.lower() for key, _ in request.header_items()})
            if request.full_url.endswith("/api/v1/connection"):
                return 200, connection_body()
            raise AssertionError(request.full_url)

        settings = AppSettings(
            server_url="https://10.71.6.34:8765 https://172.20.10.2:8765",
            sync_token=secrets.token_urlsafe(32),
            certificate_sha256=CERT,
            device_id="windows-test",
            server_id=SERVER_ID,
        )
        client = SyncClient(transport=transport)
        connected = client.connect(settings)
        self.assertEqual(connected.server_id, SERVER_ID)
        self.assertEqual(requests[0].full_url, "https://172.20.10.2:8765/api/v1/connection")
        headers = {key.lower(): value for key, value in requests[0].header_items()}
        self.assertEqual(headers.get("x-server-id"), SERVER_ID)
        self.assertTrue(headers.get("x-sync-token"))

        with self.assertRaises(SyncError):
            client._json(settings, "GET", "/api/v1/pairing")
        with self.assertRaises(SyncError):
            client._json(settings, "POST", "/api/v1/pairing/invitations")
        with self.assertRaises(SyncError):
            client._json(settings, "GET", "/api/v1/receipts/abc?raw=1")

    def test_redeem_does_not_send_origin(self) -> None:
        from windows.phone_sync import PendingPairing

        requests: list[Request] = []

        def transport(request: Request, fingerprint: str) -> tuple[int, bytes]:
            requests.append(request)
            headers = {key.lower() for key, _value in request.header_items()}
            self.assertNotIn("origin", headers)
            if request.full_url.endswith("/api/v1/pairing/redeem"):
                body = json.loads(request.data)
                self.assertEqual(set(body), {"invitation_token", "device_token", "claim_id", "server_id"})
                return 200, json.dumps({"device_id": str(uuid.uuid4()), "server_id": SERVER_ID}).encode()
            raise AssertionError(request.full_url)

        invitation = PairingInvitation.parse(invitation_uri())
        draft = PendingPairing.create(invitation)
        settings = AppSettings(
            server_url=" ".join(invitation.addresses),
            sync_token=draft.device_token,
            certificate_sha256=invitation.certificate_sha256,
            device_id="windows-test",
            server_id=invitation.server_id,
        )
        SyncClient(transport=transport).redeem_invitation(draft, settings)
        self.assertTrue(requests)

    def test_certificate_mismatch_does_not_try_the_next_address(self) -> None:
        seen: list[str] = []

        def transport(request: Request, fingerprint: str) -> tuple[int, bytes]:
            seen.append(request.full_url)
            raise SyncError(SyncError.certificate_rejected)

        settings = AppSettings(
            server_url="https://10.71.6.34:8765 https://192.0.2.10:8765",
            sync_token=secrets.token_urlsafe(32),
            certificate_sha256=CERT,
            device_id="windows-test",
        )
        with self.assertRaises(SyncError):
            SyncClient(transport=transport).connect(settings)
        self.assertEqual(len(seen), 1)

    def test_unreachable_address_can_try_the_next_one(self) -> None:
        seen: list[str] = []

        def transport(request: Request, fingerprint: str) -> tuple[int, bytes]:
            seen.append(request.full_url)
            if "10.71.6.34" in request.full_url:
                raise SyncError(SyncError.unreachable, retry_next=True)
            return 200, connection_body()

        settings = AppSettings(
            server_url="https://10.71.6.34:8765 https://192.0.2.10:8765",
            sync_token=secrets.token_urlsafe(32),
            certificate_sha256=CERT,
            device_id="windows-test",
        )
        connected = SyncClient(transport=transport).connect(settings)
        self.assertEqual(connected.base_url, "https://192.0.2.10:8765")
        self.assertEqual(len(seen), 2)

    def test_rate_limit_does_not_try_the_next_address(self) -> None:
        seen: list[str] = []

        def transport(request: Request, fingerprint: str) -> tuple[int, bytes]:
            seen.append(request.full_url)
            return 429, b"{}"

        settings = AppSettings(
            server_url="https://10.71.6.34:8765 https://192.0.2.10:8765",
            sync_token=secrets.token_urlsafe(32),
            certificate_sha256=CERT,
            device_id="windows-test",
        )
        with self.assertRaises(SyncError):
            SyncClient(transport=transport).connect(settings)
        self.assertEqual(len(seen), 1)

    def test_certificate_mismatch_is_rejected(self) -> None:
        der = b"certificate-bytes"
        conn = PinnedHTTPSConnection("192.0.2.10", certificate_sha256(b"other"))
        conn.port = 8765
        conn.timeout = 1
        fake_ssl = MagicMock()
        fake_ssl.getpeercert.return_value = der
        with patch("windows.phone_sync.socket.create_connection", return_value=MagicMock()), patch.object(
            ssl.SSLContext, "wrap_socket", return_value=fake_ssl
        ):
            with self.assertRaises(CertificateRejected):
                conn.connect()

    def test_wrong_protocol_version_is_incompatible(self) -> None:
        def transport(request: Request, fingerprint: str) -> tuple[int, bytes]:
            payload = json.loads(connection_body())
            payload["protocol_version"] = 1
            return 200, json.dumps(payload).encode()

        settings = AppSettings(
            server_url="https://192.0.2.10:8765",
            sync_token=secrets.token_urlsafe(32),
            certificate_sha256=CERT,
            device_id="windows-test",
        )
        with self.assertRaisesRegex(SyncError, "不兼容"):
            SyncClient(transport=transport).connect(settings)


class LiveWindowsPhoneTests(fixtures.ServerFixture):
    def live_fingerprint(self) -> str:
        return hashlib.sha256(ssl.PEM_cert_to_DER_cert(self.cert.read_text())).hexdigest().upper()

    def live_invitation_uri(self, role: str = "contributor") -> str:
        invitation = self.server.pairing.create("Windows phone", role)
        return invitation_uri(
            server_id=self.db.server_id,
            addresses=[f"https://127.0.0.1:{self.server.server_port}"],
            token=invitation["invitation_token"],
            expires_at=invitation["expires_at"],
            fingerprint=self.live_fingerprint(),
        )

    def live_store(self) -> PhoneStore:
        return PhoneStore(data_dir=Path(tempfile.mkdtemp()), secrets=MemorySecretStore(), client=SyncClient())

    def test_live_redeem_pins_certificate_and_syncs_ledger(self) -> None:
        store = self.live_store()
        store.pair_with_invitation(self.live_invitation_uri())
        self.assertEqual(store.settings.server_id, self.db.server_id)
        self.assertEqual(store.settings.role, "contributor")
        self.assertEqual(store.settings.certificate_sha256, self.live_fingerprint())
        when = datetime(2026, 9, 7, 4, 0, tzinfo=timezone.utc)
        store.add_manual("income", when, "工资", Decimal("12.35"), "九月工资", "")
        self.assertEqual(self.db.summary("2026-09")["income_cents"], 1235)
        store.selected_month = when
        store.sync_all()
        self.assertEqual(store.summary.income_cents, 1235)
        self.assertEqual(store.sync_message, "已同步")

    def test_live_wrong_certificate_is_rejected(self) -> None:
        store = self.live_store()
        uri = invitation_uri(
            server_id=self.db.server_id,
            addresses=[f"https://127.0.0.1:{self.server.server_port}"],
            token=self.server.pairing.create("Windows phone", "contributor")["invitation_token"],
            fingerprint="B" * 64,
        )
        before = len(self.db.list_devices())
        with self.assertRaises(SyncError) as raised:
            store.pair_with_invitation(uri)
        self.assertEqual(str(raised.exception), SyncError.certificate_rejected)
        self.assertEqual(len(self.db.list_devices()), before)
        self.assertFalse(store.settings.configured)

    def test_live_viewer_cannot_write_and_contributor_upload_is_cleared_after_delete(self) -> None:
        viewer = self.live_store()
        viewer.pair_with_invitation(self.live_invitation_uri("viewer"))
        self.assertEqual(viewer.sync_message, "已同步（只读汇总）")
        with self.assertRaises(SyncError):
            viewer.add_manual("expense", datetime.now(timezone.utc), "交通", Decimal("1"), "", "")
        with self.assertRaises(SyncError):
            viewer.enqueue_image(JPEG_BYTES)

        phone = self.live_store()
        phone.pair_with_invitation(self.live_invitation_uri())
        receipt = phone.enqueue_image(JPEG_BYTES)
        self.assertEqual(self.db.get_receipt(receipt.id)["status"], "queued")
        self.db.delete_receipt(receipt.id)
        phone.sync_all()
        self.assertEqual(phone.receipts, [])
        self.assertEqual(self.request("POST", "/api/v1/receipts", fixtures.ServerFixture.receipt(receipt.id), phone.settings.sync_token)[0], 410)

    def test_live_manual_retry_is_idempotent(self) -> None:
        store = self.live_store()
        store.pair_with_invitation(self.live_invitation_uri())
        request_id = str(uuid.uuid4())
        when = datetime(2026, 9, 7, 4, 0, tzinfo=timezone.utc)
        store.add_manual("expense", when, "交通", Decimal("2.50"), "巴士", "", request_id=request_id)
        self.assertEqual(self.db.summary("2026-09")["expense_cents"], 250)
        store.manuals = []
        from windows.phone_sync import PendingManual

        store.manuals = [
            PendingManual(
                request_id=request_id,
                occurred_at="2026-09-07T12:00:00+08:00",
                kind="expense",
                category="交通",
                amount_cents=250,
                content="巴士",
                notes="",
                server_id=store.settings.server_id or "",
            )
        ]
        store.sync_all()
        self.assertEqual(self.db.summary("2026-09")["expense_cents"], 250)


if __name__ == "__main__":
    unittest.main()
