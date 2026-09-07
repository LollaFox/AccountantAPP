import secrets
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from receipt_pairing import DevicePairing
import test_multi_device as fixtures


class InvitationTests(fixtures.ServerFixture):
    def invitation(self, role="contributor"):
        return self.server.pairing.create("Temporary phone", role)

    def claim(self, invitation, token=None, claim_id=None):
        return {"invitation_token": invitation["invitation_token"], "device_token": token or secrets.token_urlsafe(32),
                "claim_id": claim_id or str(uuid.uuid4()), "server_id": self.db.server_id}

    def test_invitation_redeems_over_tls_then_device_can_sync(self):
        body = self.claim(self.invitation())
        status, result = self.request("POST", "/api/v1/pairing/redeem", body)
        self.assertEqual(status, 200)
        self.assertEqual(result["server_id"], self.db.server_id)
        self.assertNotIn("sync_token", result)
        self.assertEqual(self.request("GET", "/api/v1/connection", token=body["device_token"])[0], 200)

    def test_claim_response_loss_retries_same_key_even_after_restart(self):
        body = self.claim(self.invitation())
        first = self.request("POST", "/api/v1/pairing/redeem", body)[1]
        restarted = DevicePairing(self.db)
        replay = restarted.redeem(body["invitation_token"], body["device_token"], body["claim_id"], body["server_id"])
        self.assertEqual(first, replay)
        self.assertEqual(len(self.db.list_devices()), 2)  # Legacy + exactly one new device.

    def test_simultaneous_claims_authorize_only_one_device(self):
        invitation = self.invitation()
        claims = [self.claim(invitation) for _ in range(6)]
        with ThreadPoolExecutor(max_workers=6) as pool:
            statuses = list(pool.map(lambda body: self.request("POST", "/api/v1/pairing/redeem", body)[0], claims))
        self.assertEqual(statuses.count(200), 1)
        self.assertEqual(statuses.count(400), 5)

    def test_expired_cancelled_and_wrong_server_invitations_fail_closed(self):
        invitation = self.invitation()
        body = self.claim(invitation)
        with patch("receipt_pairing.time.time", return_value=time.time() + 181):
            self.assertEqual(self.request("POST", "/api/v1/pairing/redeem", body)[0], 400)
        self.server.pairing.cancel(invitation["id"])
        self.assertEqual(self.request("POST", "/api/v1/pairing/redeem", body)[0], 400)
        body = self.claim(self.invitation())
        body["server_id"] = str(uuid.uuid4())
        self.assertEqual(self.request("POST", "/api/v1/pairing/redeem", body)[0], 400)

    def test_redeemed_invitation_never_revives_a_revoked_device(self):
        body = self.claim(self.invitation())
        first = self.request("POST", "/api/v1/pairing/redeem", body)[1]
        self.db.revoke_device(first["device_id"])
        self.assertEqual(self.request("POST", "/api/v1/pairing/redeem", body)[0], 400)

    def test_invitation_creation_requires_fresh_passkey_and_remains_local(self):
        self.review.pairing_scheme = "https"
        self.review.certificate_sha256 = "A" * 64  # Deliberately non-live test data.
        with patch("receipt_sync_server.local_addresses", return_value=["192.0.2.10"]):
            status, response = self.request("POST", "/api/v1/pairing/invitations", {"name": "Test"}, admin=True)
            self.assertEqual(status, 201)
            self.assertTrue(response["qr"].startswith("data:image/svg+xml"))
            self.assertNotIn("device_token", response["invitation"])
            self.assertEqual(self.request("POST", "/api/v1/pairing/invitations", {"name": "Test"}, admin=True)[0], 400)
        token = self.device()["sync_token"]
        self.assertEqual(self.request("POST", "/api/v1/pairing/invitations", {"name": "Forbidden"}, token)[0], 403)
        self.assertEqual(self.request("GET", "/api/v1/passkeys", token=token)[0], 403)
        self.assertEqual(self.request("POST", "/api/v1/pairing/redeem", self.claim(self.invitation()), admin=True)[0], 403)

    def test_invitation_viewer_permission_is_not_chosen_by_claiming_phone(self):
        body = self.claim(self.invitation("viewer"))
        body["role"] = "contributor"
        self.assertEqual(self.request("POST", "/api/v1/pairing/redeem", body)[0], 200)
        self.assertEqual(self.request("POST", "/api/v1/transactions", self.manual(), body["device_token"])[0], 403)
