"""Run native iOS transport tests against a disposable Mac server on loopback.

No OCR, real ledger, stored pairing, signing account or personal passkey is used.
Only a temporary fixture path (never its credentials) is passed to xcodebuild.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import segno
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pc"))
from test_multi_device import MultiDeviceTests


def main():
    fixture = MultiDeviceTests()
    fixture.setUpClass()
    try:
        fixture.setUp()
        first = fixture.db.create_device("Temporary simulator")
        second = fixture.db.create_device("Temporary second device")
        revoked = fixture.db.create_device("Temporary revoked device")
        fixture.db.revoke_device(revoked["id"])
        invitation = fixture.server.pairing.create("Temporary QR phone", "contributor")
        payload = {"version": 1, "server_id": fixture.db.server_id,
                   "addresses": [f"https://127.0.0.1:{fixture.server.server_port}"],
                   "certificate_sha256": fixture.pin.hex().upper(),
                   "invitation_token": invitation["invitation_token"], "expires_at": invitation["expires_at"]}
        uri = "receiptsync://pair?data=" + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        qr_path = fixture.root / "temporary-invitation.png"
        segno.make(uri, error="m").save(str(qr_path), scale=6)
        qr_path.chmod(0o600)
        path = fixture.root / "ios-test-fixture.json"
        path.write_text(json.dumps({
            "purpose": "temporary-mac-test", "url": f"https://127.0.0.1:{fixture.server.server_port}",
            "pin": fixture.pin.hex().upper(), "server_id": fixture.db.server_id,
            "token": first["sync_token"], "second_token": second["sync_token"],
            "revoked_token": revoked["sync_token"],
            "qr_path": str(qr_path),
        }))
        path.chmod(0o600)
        environment = os.environ.copy()
        environment["TEST_RUNNER_RECEIPT_SYNC_TEST_FIXTURE"] = str(path)
        result = subprocess.run([
            "xcodebuild", "test", "-quiet", "-project", str(ROOT / "ios/ReceiptSync.xcodeproj"),
            "-scheme", "ReceiptSync", "-configuration", "Debug", "-destination",
            "platform=iOS Simulator,name=iPhone 17", "-derivedDataPath", str(ROOT / "ios/build"),
            "CODE_SIGNING_ALLOWED=NO",
            "-only-testing:ReceiptSyncTests/ConnectionTests/testTemporaryMacServerTLSAndSharedLedger",
        ], cwd=ROOT, env=environment)
        if result.returncode == 0:
            # Also prove that the test actually reached the server, not merely skipped.
            if fixture.db.summary("2026-09")["expense_cents"] != 125 or len(fixture.worker.jobs) != 1:
                raise RuntimeError("Native transport test did not complete its temporary-server assertions")
            if len([d for d in fixture.db.list_devices() if d["name"] == "Temporary QR phone"]) != 1:
                raise RuntimeError("Native QR redemption did not authorize exactly one device")
            print("Native Mac/iOS TLS integration passed; temporary credentials and data will be removed.")
        return result.returncode
    finally:
        fixture.doCleanups()
        fixture.tearDownClass()


if __name__ == "__main__":
    raise SystemExit(main())
