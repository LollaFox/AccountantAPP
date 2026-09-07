"""Exercise the management page with a disposable browser and virtual passkeys."""
import argparse
import os
import subprocess
from pathlib import Path

from test_multi_device import MultiDeviceTests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", required=True)
    parser.add_argument("--playwright", required=True, help="Installed Playwright package directory")
    args = parser.parse_args()
    fixture = MultiDeviceTests()
    fixture.setUpClass()
    try:
        fixture.setUp()
        fixture.review.pairing_scheme = "https"
        fixture.review.pairing_port = fixture.server.server_port
        fixture.review.certificate_sha256 = fixture.pin.hex().upper()
        environment = os.environ.copy()
        environment["RECEIPT_SYNC_PLAYWRIGHT"] = args.playwright
        result = subprocess.run([
            args.node, str(Path(__file__).with_name("test_web_security.cjs")),
            f"http://localhost:{fixture.review.server_port}",
        ], env=environment)
        if result.returncode == 0:
            if fixture.review.passkeys.count != 1 or not any(d["revoked_at"] for d in fixture.db.list_devices()):
                raise RuntimeError("Browser did not complete the enrollment and revocation assertions")
            print("Browser security verification passed; disposable passkeys and test data removed on exit.")
        return result.returncode
    finally:
        fixture.doCleanups()
        fixture.tearDownClass()


if __name__ == "__main__":
    raise SystemExit(main())
