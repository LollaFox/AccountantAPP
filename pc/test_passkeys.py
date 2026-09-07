"""Real WebAuthn signatures, without Touch ID prompts or any user credentials."""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import secrets
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import receipt_passkeys
from receipt_passkeys import AdminPasskeys, PasskeyError
from receipt_sync_server import AppConfig, Database, Handler, ReceiptHTTPServer, default_model_cache

if receipt_passkeys.AVAILABLE:
    import cbor2
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec


def encoded(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


class TestAuthenticator:
    """Ephemeral ES256 authenticator: production only receives the public key."""
    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = secrets.token_bytes(32)

    def credential(self, offered, *, registration=False, origin="http://localhost:8764",
                   rp="localhost", flags=None, counter=1, client_overrides=None, user_handle=None):
        client = {"type": "webauthn.create" if registration else "webauthn.get",
                  "challenge": offered["options"]["challenge"], "origin": origin, "crossOrigin": False}
        client.update(client_overrides or {})
        client_data = json.dumps(client).encode()
        if flags is None:
            flags = 0x45 if registration else 0x05  # UP + UV (+ AT for registration)
        auth_data = hashlib.sha256(rp.encode()).digest() + bytes([flags]) + counter.to_bytes(4, "big")
        response = {"clientDataJSON": encoded(client_data)}
        if registration:
            numbers = self.key.public_key().public_numbers()
            cose = {1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, "big"), -3: numbers.y.to_bytes(32, "big")}
            auth_data += bytes(16) + len(self.credential_id).to_bytes(2, "big") + self.credential_id + cbor2.dumps(cose)
            response["attestationObject"] = encoded(cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data}))
            response["transports"] = ["internal"]
        else:
            response["authenticatorData"] = encoded(auth_data)
            response["signature"] = encoded(self.key.sign(auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256())))
            if user_handle is not None:
                response["userHandle"] = encoded(user_handle)
        return {"id": encoded(self.credential_id), "rawId": encoded(self.credential_id), "type": "public-key", "response": response}


@unittest.skipUnless(receipt_passkeys.AVAILABLE, "install pc/requirements-server.txt to test real WebAuthn")
class PasskeyTests(unittest.TestCase):
    origin = "http://localhost:8764"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="receipt-sync-passkey-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.db = Database(root / "test.sqlite3", root / "exports")
        self.manager = AdminPasskeys(self.db)
        self.authenticator = TestAuthenticator()

    def register(self, authenticator=None, session=""):
        offered = self.manager.registration_options(self.origin, session)
        credential = (authenticator or self.authenticator).credential(offered, registration=True)
        result = self.manager.verify("register", offered["challenge_id"], credential, self.origin, session)
        return result["admin_session"]

    def test_registration_login_logout_and_public_key_only(self):
        session = self.register()
        self.assertTrue(self.manager.authenticated(session))
        self.manager.logout(session)
        self.assertFalse(self.manager.authenticated(session))
        offered = self.manager.authentication_options(self.origin)
        credential = self.authenticator.credential(offered, counter=2, user_handle=uuid.UUID(self.db.server_id).bytes)
        result = self.manager.verify("authenticate", offered["challenge_id"], credential, self.origin)
        self.assertTrue(self.manager.authenticated(result["admin_session"]))
        with self.db.session() as db:
            row = db.execute("SELECT * FROM admin_passkeys").fetchone()
        self.assertEqual(set(row.keys()), {"credential_id", "public_key", "sign_count", "created_at", "name", "revoked_at"})
        self.assertEqual(row["sign_count"], 2)
        self.assertEqual(cbor2.loads(row["public_key"])[3], -7)
        self.assertNotIn(session, self.manager.sessions)

    def test_registration_rejects_wrong_origin_rp_challenge_type_or_missing_verification(self):
        for invalid in [
            {"origin": "https://attacker.invalid"}, {"rp": "attacker.invalid"},
            {"client_overrides": {"challenge": encoded(b"wrong-challenge")}},
            {"client_overrides": {"type": "webauthn.get"}}, {"flags": 0x41}, {"flags": 0x44},
            {"client_overrides": {"crossOrigin": True}}, {"client_overrides": {"topOrigin": self.origin}},
        ]:
            with self.subTest(invalid=invalid):
                offered = self.manager.registration_options(self.origin)
                credential = self.authenticator.credential(offered, registration=True, **invalid)
                with self.assertRaises(PasskeyError):
                    self.manager.verify("register", offered["challenge_id"], credential, self.origin)
                self.assertEqual(self.manager.count, 0)
                self.assertFalse(self.manager.sessions)

    def test_assertion_rejects_invalid_signature_origin_rp_counter_and_user_handle(self):
        self.register()
        for invalid in [
            {"origin": "https://attacker.invalid"}, {"rp": "attacker.invalid"}, {"flags": 0x01},
            {"flags": 0x04}, {"counter": 1}, {"user_handle": uuid.uuid4().bytes},
            {"client_overrides": {"challenge": encoded(b"wrong")}},
            {"client_overrides": {"crossOrigin": True}},
        ]:
            with self.subTest(invalid=invalid):
                offered = self.manager.authentication_options(self.origin)
                options = {"counter": 2, **invalid}
                credential = self.authenticator.credential(offered, **options)
                with self.assertRaises(PasskeyError):
                    self.manager.verify("authenticate", offered["challenge_id"], credential, self.origin)
        offered = self.manager.authentication_options(self.origin)
        credential = self.authenticator.credential(offered, counter=2)
        credential["response"]["signature"] = encoded(b"not-a-signature")
        with self.assertRaises(PasskeyError):
            self.manager.verify("authenticate", offered["challenge_id"], credential, self.origin)

    def test_challenge_is_single_use_and_cannot_cross_ceremonies(self):
        offered = self.manager.registration_options(self.origin)
        credential = self.authenticator.credential(offered, registration=True)
        self.manager.verify("register", offered["challenge_id"], credential, self.origin)
        with self.assertRaises(PasskeyError):
            self.manager.verify("register", offered["challenge_id"], credential, self.origin)
        offered = self.manager.authentication_options(self.origin)
        credential = self.authenticator.credential(offered, counter=2)
        with self.assertRaises(PasskeyError):
            self.manager.verify("register", offered["challenge_id"], credential, self.origin)
        with self.assertRaises(PasskeyError):
            self.manager.verify("authenticate", offered["challenge_id"], credential, self.origin)

    def test_initial_two_tab_race_cannot_enroll_a_second_owner(self):
        offered = self.manager.registration_options(self.origin)
        self.register()
        credential = TestAuthenticator().credential(offered, registration=True)
        with self.assertRaises(PasskeyError):
            self.manager.verify("register", offered["challenge_id"], credential, self.origin)
        self.assertEqual(self.manager.count, 1)

    def test_backup_key_requires_same_live_admin_session(self):
        session = self.register()
        with self.assertRaises(PasskeyError):
            self.manager.registration_options(self.origin)
        offered = self.manager.registration_options(self.origin, session)
        self.assertEqual(len(offered["options"]["excludeCredentials"]), 1)
        self.manager.logout(session)
        with self.assertRaises(PasskeyError):
            self.manager.verify("register", offered["challenge_id"], TestAuthenticator().credential(offered, registration=True), self.origin, session)
        offered = self.manager.authentication_options(self.origin)
        session = self.manager.verify("authenticate", offered["challenge_id"], self.authenticator.credential(offered, counter=2), self.origin)["admin_session"]
        self.register(TestAuthenticator(), session)
        self.assertEqual(self.manager.count, 2)

    def test_expiry_and_restart_clear_sessions_but_keep_public_credentials(self):
        session = self.register()
        offered = self.manager.authentication_options(self.origin)
        credential = self.authenticator.credential(offered, counter=2)
        with patch("receipt_passkeys.time.monotonic", return_value=10**12):
            self.assertFalse(self.manager.authenticated(session))
            with self.assertRaises(PasskeyError):
                self.manager.verify("authenticate", offered["challenge_id"], credential, self.origin)
        restarted = AdminPasskeys(self.db)
        self.assertEqual(restarted.count, 1)
        self.assertFalse(restarted.authenticated(session))

    def test_missing_dependency_fails_closed(self):
        with patch("receipt_passkeys.AVAILABLE", False):
            with self.assertRaises(PasskeyError):
                self.manager.registration_options(self.origin)

    def test_fresh_authorization_is_short_lived_and_single_use(self):
        session = self.register()
        self.manager.require_fresh(session)
        with self.assertRaises(PasskeyError):
            self.manager.require_fresh(session)
        with self.manager.lock:
            session = self.manager._new_session()["admin_session"]
            self.manager.sessions[self.manager._digest(session)]["verified_at"] -= 61
        with self.assertRaises(PasskeyError):
            self.manager.require_fresh(session)

    def test_revocation_invalidates_key_sessions_and_refuses_last_key(self):
        first_session = self.register()
        second_authenticator = TestAuthenticator()
        second_session = self.register(second_authenticator, first_session)
        keys = self.manager.list_credentials(second_session)
        first_key = next(key for key in keys if not key["current"])
        self.manager.revoke(first_key["id"], second_session)
        self.assertFalse(self.manager.authenticated(first_session))
        self.assertTrue(self.manager.authenticated(second_session))
        offered = self.manager.authentication_options(self.origin)
        with self.assertRaises(PasskeyError):
            self.manager.verify("authenticate", offered["challenge_id"], self.authenticator.credential(offered, counter=2), self.origin)
        offered = self.manager.authentication_options(self.origin)
        second_session = self.manager.verify("authenticate", offered["challenge_id"], second_authenticator.credential(offered, counter=2), self.origin)["admin_session"]
        active = next(key for key in self.manager.list_credentials(second_session) if not key["revoked_at"])
        with self.assertRaises(PasskeyError):
            self.manager.revoke(active["id"], second_session)
        self.assertEqual(self.manager.count, 1)

    def test_recovery_code_only_enrolls_a_replacement_and_is_consumed_atomically(self):
        session = self.register()
        code = self.manager.rotate_recovery(session)["recovery_code"]
        with self.db.session() as db:
            stored = db.execute("SELECT token_hash FROM admin_recovery").fetchone()[0]
        self.assertTrue(stored != code.encode())
        recovered = self.manager.recover(code)["admin_session"]
        self.assertFalse(self.manager.authenticated(recovered))
        self.assertFalse(self.manager.authenticated(session))
        with self.assertRaises(PasskeyError):
            self.manager.require_fresh(recovered)
        # Cancelling registration does not consume the only recovery code.
        abandoned = self.manager.registration_options(self.origin, recovered)
        recovered = self.manager.recover(code)["admin_session"]
        replacement = TestAuthenticator()
        with self.assertRaises(PasskeyError):
            self.manager.verify("register", abandoned["challenge_id"], replacement.credential(abandoned, registration=True), self.origin)
        new_session = self.register(replacement, recovered)
        self.assertTrue(self.manager.authenticated(new_session))
        with self.assertRaises(PasskeyError):
            self.manager.recover(code)
        self.assertEqual(self.manager.count, 2)

    def test_rotating_recovery_invalidates_old_code_without_changing_ledger(self):
        self.db.add_transaction({"occurred_at": "2026-09-07T12:00:00+08:00", "kind": "income", "category": "测试", "amount_cents": 100})
        session = self.register()
        old_code = self.manager.rotate_recovery(session)["recovery_code"]
        offered = self.manager.authentication_options(self.origin)
        session = self.manager.verify("authenticate", offered["challenge_id"], self.authenticator.credential(offered, counter=2), self.origin)["admin_session"]
        new_code = self.manager.rotate_recovery(session)["recovery_code"]
        with self.assertRaises(PasskeyError):
            self.manager.recover(old_code)
        self.manager.recover(new_code)
        self.assertEqual(self.db.summary("2026-09")["income_cents"], 100)


class QuietHandler(Handler):
    def log_message(self, *args):
        pass


@unittest.skipUnless(receipt_passkeys.AVAILABLE, "install pc/requirements-server.txt to test WebAuthn HTTP")
class PasskeyHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="receipt-sync-passkey-http-")
        self.addCleanup(self.temp.cleanup)
        config = AppConfig(Path(self.temp.name), "127.0.0.1", 0, secrets.token_urlsafe(32), default_model_cache())
        self.server = ReceiptHTTPServer(("127.0.0.1", 0), QuietHandler, config, worker=object())
        self.origin = f"http://localhost:{self.server.server_port}"
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            conn.request(method, path, json.dumps(body) if body is not None else None,
                         {"Host": self.origin.split("//")[1], "Origin": self.origin,
                          "Content-Type": "application/json", **(headers or {})})
            response = conn.getresponse()
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
        finally:
            conn.close()

    def test_passkey_is_required_for_management_then_unlocks_and_locks(self):
        for method, path in [("GET", "/api/v1/pairing"), ("GET", "/api/v1/summary"),
                             ("GET", "/api/v1/transactions"), ("GET", "/api/v1/receipts"),
                             ("POST", "/api/v1/devices"), ("DELETE", "/api/v1/devices/legacy")]:
            status, body = self.request(method, path, {} if method == "POST" else None)
            self.assertEqual(status, 401)
            self.assertEqual(body["code"], "passkey_required")
        self.assertFalse(self.request("GET", "/api/v1/auth/status")[1]["registered"])
        status, offered = self.request("POST", "/api/v1/auth/register/options", {})
        self.assertEqual(status, 200)
        credential = TestAuthenticator().credential(offered, registration=True, origin=self.origin)
        status, result = self.request("POST", "/api/v1/auth/register/verify", {"challenge_id": offered["challenge_id"], "credential": credential})
        self.assertEqual(status, 200)
        headers = {"X-Admin-Session": result["admin_session"]}
        self.assertEqual(self.request("GET", "/api/v1/devices", headers=headers)[0], 200)
        self.assertEqual(self.request("GET", "/api/v1/devices", headers={**headers, "Origin": "https://attacker.invalid"})[0], 403)
        self.assertEqual(self.request("POST", "/api/v1/auth/logout", {}, headers)[0], 200)
        self.assertEqual(self.request("GET", "/api/v1/devices", headers=headers)[0], 401)

    def test_remote_token_or_forged_origin_cannot_enroll(self):
        for headers in [
            {"Origin": "https://attacker.invalid"}, {"Origin": "null"}, {"Origin": ""},
            {"Host": "attacker.invalid"}, {"X-Sync-Token": self.server.config.sync_token},
        ]:
            with self.subTest(header_names=list(headers)):
                self.assertEqual(self.request("POST", "/api/v1/auth/register/options", {}, headers)[0], 403)
        self.server.allow_admin = False
        self.assertEqual(self.request("POST", "/api/v1/auth/register/options", {})[0], 403)
        self.assertEqual(self.request("GET", "/api/v1/auth/status")[0], 404)
        self.assertEqual(self.server.passkeys.count, 0)

    def test_recovery_session_cannot_read_ledger_or_authorize_devices(self):
        manager = self.server.passkeys
        offered = manager.registration_options(self.origin)
        result = manager.verify("register", offered["challenge_id"],
                                TestAuthenticator().credential(offered, registration=True, origin=self.origin), self.origin)
        code = manager.rotate_recovery(result["admin_session"])["recovery_code"]
        status, recovered = self.request("POST", "/api/v1/auth/recover", {"code": code})
        self.assertEqual(status, 200)
        headers = {"X-Admin-Session": recovered["admin_session"]}
        for method, path in [("GET", "/api/v1/summary"), ("GET", "/api/v1/passkeys"),
                             ("GET", "/api/v1/devices"), ("POST", "/api/v1/devices"),
                             ("POST", "/api/v1/pairing/invitations"), ("DELETE", "/api/v1/devices/legacy")]:
            self.assertEqual(self.request(method, path, {} if method == "POST" else None, headers)[0], 401)
        self.assertEqual(self.request("POST", "/api/v1/auth/register/options", {}, headers)[0], 200)

    def test_all_security_mutations_reject_remote_tokens_and_cross_origin(self):
        for path in ("/api/v1/auth/recover", "/api/v1/auth/recovery/rotate", "/api/v1/auth/passkeys/revoke",
                     "/api/v1/auth/register/options", "/api/v1/auth/register/verify"):
            for headers in ({"Origin": "https://attacker.invalid"}, {"X-Sync-Token": self.server.config.sync_token}):
                self.assertEqual(self.request("POST", path, {}, headers)[0], 403)


if __name__ == "__main__":
    unittest.main()
