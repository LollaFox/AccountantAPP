import XCTest
@testable import ReceiptSync

final class HongKongDateTests: XCTestCase {
    func testMonthKeyUsesHongKongCalendarBoundary() {
        let lateAugustUTC = ISO8601DateFormatter().date(from: "2026-08-31T15:59:00Z")!
        let earlySeptemberUTC = ISO8601DateFormatter().date(from: "2026-08-31T16:30:00Z")!
        XCTAssertEqual(HongKongDate.monthKey(from: lateAugustUTC), "2026-08")
        XCTAssertEqual(HongKongDate.monthKey(from: earlySeptemberUTC), "2026-09")
    }

    func testIsoStringIncludesColonSeparatedOffset() {
        let date = ISO8601DateFormatter().date(from: "2026-09-04T04:00:00Z")!
        let encoded = HongKongDate.isoString(from: date)
        XCTAssertTrue(encoded.hasSuffix("+08:00"), encoded)
        XCTAssertEqual(encoded, "2026-09-04T12:00:00+08:00")
    }

    func testAddMonthsStaysOnHongKongCalendar() {
        let september = ISO8601DateFormatter().date(from: "2026-09-04T04:00:00Z")!
        XCTAssertEqual(HongKongDate.monthKey(from: HongKongDate.addMonths(-1, to: september)), "2026-08")
        XCTAssertEqual(HongKongDate.monthKey(from: HongKongDate.addMonths(1, to: september)), "2026-10")
    }

    func testCurrencyFormatsHKD() {
        XCTAssertEqual(CurrencyFormatter.string(cents: 0), "HK$0.00")
        XCTAssertEqual(CurrencyFormatter.string(cents: 250), "HK$2.50")
    }

    func testPairingFingerprintAcceptsPastedSeparators() {
        let hex = String(repeating: "AB", count: 32)
        let colonSeparated = stride(from: 0, to: hex.count, by: 2).map { offset in
            let start = hex.index(hex.startIndex, offsetBy: offset)
            return String(hex[start..<hex.index(start, offsetBy: 2)])
        }.joined(separator: ":")
        XCTAssertEqual(PairingInput.normalizedFingerprint(hex), hex)
        XCTAssertEqual(PairingInput.normalizedFingerprint(colonSeparated), hex)
        XCTAssertEqual(PairingInput.normalizedFingerprint("SHA-256\n" + hex), hex)
        XCTAssertTrue(PairingInput.isReadyToSync(
            serverURL: "https://192.0.2.10:8765",
            syncToken: "device-token",
            certificateSHA256: colonSeparated
        ))
    }

    func testPairingSyncRequiresValidTokenAndCertificate() {
        let hex = String(repeating: "A", count: 64)
        XCTAssertFalse(PairingInput.isReadyToSync(serverURL: "https://192.0.2.10:8765", syncToken: "", certificateSHA256: hex))
        XCTAssertFalse(PairingInput.isReadyToSync(serverURL: "https://192.0.2.10:8765", syncToken: "token", certificateSHA256: "short"))
        XCTAssertFalse(PairingInput.isReadyToSync(serverURL: "http://192.0.2.10:8765", syncToken: "token", certificateSHA256: hex))
        XCTAssertEqual(
            PairingInput.validationMessage(serverURL: "https://192.0.2.10:8765", syncToken: "", certificateSHA256: hex),
            "请填写同步密钥"
        )
        XCTAssertNil(PairingInput.validationMessage(
            serverURL: "192.0.2.10:8765",
            syncToken: "token",
            certificateSHA256: hex.lowercased()
        ))
    }

    func testPairingRejectsMalformedAddressesAndFingerprints() {
        for value in ["https://", "http://192.0.2.10", "https://user:password@example.test",
                      "https://example.test/api", "https://example.test?token=x", "https://example.test#path",
                      "https://example.test:0", "https://example.test:70000", "https://example.test http://example.test"] {
            XCTAssertTrue(PairingInput.candidateURLs(value).isEmpty, value)
        }
        let hex = String(repeating: "A", count: 64)
        XCTAssertEqual(PairingInput.normalizedFingerprint(hex + "A"), "")
        XCTAssertEqual(PairingInput.normalizedFingerprint("INVALID" + hex), "")
        XCTAssertEqual(PairingInput.normalizedFingerprint(hex + "!"), "")
        XCTAssertFalse(PairingInput.isReadyToSync(serverURL: "example.test", syncToken: "injected\r\nheader", certificateSHA256: hex))
        XCTAssertEqual(PairingInput.candidateURLs("192.0.2.10 HTTPS://192.0.2.10:8765/"), ["https://192.0.2.10:8765"])
    }

    func testFailedComputerConnectionDoesNotReadAsSynced() {
        XCTAssertEqual(
            NetworkSyncMessage.display(URLError(.cannotConnectToHost)),
            SyncClient.SyncError.unreachable.errorDescription
        )
        XCTAssertEqual(
            NetworkSyncMessage.display(URLError(.cancelled)),
            SyncClient.SyncError.cancelled.errorDescription
        )
        XCTAssertNotEqual(NetworkSyncMessage.display(URLError(.cannotConnectToHost)), "已同步")
    }

    func testHotspotAddressIsTriedBeforeCampusAddress() {
        let urls = PairingInput.preferredSyncURLs("https://10.71.6.34:8765 https://172.20.10.2:8765")
        XCTAssertEqual(urls.first, "https://172.20.10.2:8765")
        XCTAssertEqual(urls.last, "https://10.71.6.34:8765")
    }
}
