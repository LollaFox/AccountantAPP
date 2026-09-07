# Connection and passkey verification — 2026-09-07

Scope: this Mac hosts one shared ledger; multiple independently authorized devices use its restricted HTTPS service. Passkeys protect local administration and device authorization. They do not replace background-sync credentials or encrypt the database on disk.

## Results

| Check | Result | Coverage |
| --- | --- | --- |
| Python regression suite | 77 passed, no skips | Existing ledger/parser/installer regressions plus real WebAuthn signatures, origin/RP/challenge/UV, replay, expiry, enrollment races, backup/revocation, single-use fresh authorization, restricted recovery sessions, atomic recovery-code consumption, remote API boundaries, single-winner QR claims, expiration/cancellation/restart retries, idle TLS peers, bounded connections, absolute deadlines, partial bodies, process-kill OCR recovery and disk failures |
| iOS simulator suite | 44 passed, 2 intentional skips, 0 failures (46 total) | Existing queue/date/settings/UI tests plus rejected-item isolation and correction, stable IDs, local-only discard, disk-write rollback, pairing-before-network persistence, lost-response recovery, malformed/expired/wrong-server invitations, and explicit QR confirmation |
| Native iOS → temporary Mac HTTPS server | 1 passed, no skips | Real URLSession certificate pinning, distinct devices sharing one ledger, idempotent manual entries/uploads, wrong server/revoked credential/wrong certificate rejection; Apple Core Image decodes a generated QR image, then native iOS claims it over pinned TLS twice and the server verifies exactly one new device |
| Headless Chromium management UI | 10 groups passed | Canonical localhost/secure context; browser WebAuthn enrollment; independent locked tabs/no browser-storage sessions; one-time manual credential display/revocation; fresh-passkey QR generation/rendering/close cancellation; last-key deletion refusal and recovery-code clearing; lost-key replacement and code replay rejection; lost-key revocation; logout/login; no exposure after locking an in-flight request |
| Static checks | Passed | Python compilation, macOS shell syntax, management JavaScript syntax and `git diff --check` |

The two skips in the full simulator suite are intentional: the disposable-server test runs separately through its fixture harness, and the optional physical/hotspot UI test requires explicit opt-in. The disposable-server harness fails if the test silently skips: it also checks the temporary ledger, OCR job count and QR-authorized device count.

Xcode prints debugger-version diagnostics during UI launches. No Swift compiler errors occurred. Check actual test counts in the `.xcresult` summary; a successful filtered command alone can mean no matching tests ran.

Final counts were read from Xcode's reports: full suite `2026.09.07_14-49-53-+0800` (44 passed / 2 skipped / 0 failed), separate native integration `2026.09.07_14-42-54-+0800` (1 passed / 0 skipped / 0 failed). Reports live under gitignored `ios/build/Logs/Test` and may be pruned by later Xcode runs. A filtered UI command matched no tests; it was not counted as verification, and the complete suite was rerun successfully afterward.

## Corrections made during verification

- Xcode did not forward the temporary fixture through the scheme's build-setting environment entry. The harness now uses Xcode's documented `TEST_RUNNER_` environment prefix. It fails if the test silently skips or never reaches the server.
- The browser test now waits for the dialog's asynchronous `close` event before checking credential removal. No production security bypass was added for testing.
- Added reusable browser verification scripts; browser tests use software authenticators, never a personal passkey or Touch ID.
- Moved TLS handshakes out of the accept loop and bounded the workers. An idle raw TCP peer no longer prevents another device from establishing TLS. Request deadlines also stop byte-drip clients.
- Rehydrate queued/processing OCR jobs from SQLite after restart. A child process is deliberately killed while recognizing a temporary image; restart returns the receipt to review without entering it in the ledger. A simulated SQLite/full-disk failure no longer kills the shared worker.
- Failed CSV replacement preserves the previous complete file, while retrying a committed transaction does not create a duplicate. CSV files are individually atomic; the pair is not one cross-file transaction.
- Permanently rejected queue entries no longer block valid entries or the latest summary. Corrections reuse IDs only for definitively rejected entries; conflicts require checking the Mac. Authentication, certificate and storage failures still stop syncing.
- Added local Passkey management, fresh verification for sensitive security actions and a single-use offline recovery code. Recovery is limited to replacement-key enrollment and does not erase the ledger or authorize a device by itself.
- Added three-minute QR invitations. Phones persist a newly generated device secret before sending the claim; the server atomically binds one claimant and permits bounded retries of the same proof. Revoked devices never revive through replay.
- Browser checks now wait for asynchronous close handlers before testing cleared secrets and explicitly open the recovery disclosure. A controlled last-key deletion error now explains why the last key must be preserved.
- Vision hardware inference is unavailable on this simulator. The static QR integration uses Apple's Core Image software QR decoder; the physical camera implementation still uses VisionKit and needs device testing.
- Updated the settings UI test to scroll fields clear of the navigation bar/keyboard after adding the QR section; added explicit confirmation/invalid-invitation UI coverage.

## Repeat the checks

From the repository root, with the existing Mac Python environment:

```bash
pc/.venv/bin/python -m unittest discover -s pc -p 'test_*.py' -v
pc/.venv/bin/python ios/test_mac_connection.py
xcodebuild test -quiet -project ios/ReceiptSync.xcodeproj -scheme ReceiptSync \
  -configuration Debug -destination 'platform=iOS Simulator,name=iPhone 17' \
  -derivedDataPath ios/build CODE_SIGNING_ALLOWED=NO
```

The browser verification additionally needs Node, the Playwright package and its headless Chromium browser. Pass the installed executable/package paths:

```text
pc/.venv/bin/python pc/verify_web_security.py --node <node-executable> --playwright <playwright-package-directory>
```

Loopback listeners, the simulator, browser launch and normal Xcode/Python caches may require execution outside a restricted sandbox. No real-service credentials should be supplied to these scripts.

## Isolation and remaining manual checks

- Test databases, TLS certificates, credentials, images and virtual passkeys used disposable temporary directories; the test servers/browsers were stopped and their temporary data removed on exit.
- The real Mac ledger, receipt images and personal passkeys were not used or modified. No OCR model download or third-party receipt processing was performed.
- A reusable headless Chromium/FFmpeg test runtime was installed in the user's Playwright cache; it does not use an existing personal browser profile.
- Still required: real Mac Touch ID/platform-passkey enrollment (including Safari behavior), physical-iPhone Keychain and local-network permissions, camera upload, complete OCR/review/confirm/delete flow, and iOS's opportunity-based background refresh.
- Also verify physical camera QR scanning, cancelling the system Passkey prompt and recovery with the user's chosen backup authenticator. No real credential was enrolled, revoked or recovered by these tests.
- This round ran on macOS, not Windows. The shared Windows server and PowerShell entry point need their Windows regression run before claiming Windows acceptance.
- These tests are regression evidence, not an independent security audit. Passkeys do not protect against a compromised Mac account or an attacker who already controls the server's files/process.
