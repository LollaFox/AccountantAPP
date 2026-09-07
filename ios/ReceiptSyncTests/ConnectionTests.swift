import CryptoKit
import CoreImage
import XCTest
@testable import ReceiptSync

@MainActor
final class ConnectionTests: XCTestCase {
    private let serverID = "29d7f431-6f43-4b74-8e6b-aefcf7dd0e28"
    private var roots: [URL] = []

    private func settings(_ id: String? = nil) -> AppSettings {
        AppSettings(serverURL: "https://192.0.2.10:8765", syncToken: "test-device-token",
                    certificateSHA256: String(repeating: "A", count: 64), deviceID: "test-phone", serverID: id ?? serverID,
                    role: "contributor")
    }

    private func directory(receipts: [PendingReceipt] = []) throws -> URL {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        roots.append(root)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        try encoder.encode(receipts).write(to: root.appendingPathComponent("queue.json"))
        for receipt in receipts where receipt.status != .confirmed {
            try Data("test-image".utf8).write(to: root.appendingPathComponent(receipt.imageFileName))
        }
        return root
    }

    override func tearDown() async throws {
        for root in roots { try? FileManager.default.removeItem(at: root) }
        roots = []
    }

    private func receipt(_ status: LocalReceiptStatus = .waiting, uploaded: Bool = false) -> PendingReceipt {
        let id = UUID()
        return PendingReceipt(id: id, capturedAt: Date(), imageFileName: "\(id).jpg", status: status,
                              serverID: serverID, uploaded: uploaded)
    }

    private func store(root: URL, service: FakeSyncService, configuration: AppSettings? = nil,
                       save: @escaping (AppSettings) throws -> Void = { _ in },
                       pending: PendingPairing? = nil,
                       savePending: @escaping (PendingPairing?) throws -> Void = { _ in }) -> ReceiptStore {
        ReceiptStore(settings: configuration ?? settings(), rootDirectory: root, client: service,
                     saveSettings: save, schedulesBackgroundRefresh: false,
                     pendingPairing: pending, savePendingPairing: savePending)
    }

    private func manual(_ blocked: String? = nil) -> PendingManual {
        PendingManual(transaction: ManualTransactionRequest(occurredAt: "2026-09-07T12:00:00+08:00", kind: "expense",
                      category: "测试", amountCents: 100, content: "临时记录", notes: ""),
                      serverID: serverID, blockedReason: blocked)
    }

    private func directory(manuals: [PendingManual]) throws -> URL {
        let root = try directory()
        try JSONEncoder().encode(manuals).write(to: root.appendingPathComponent("manual-queue.json"))
        return root
    }

    private func invitation(addresses: [String] = ["https://192.0.2.10:8765"], expires: Double? = nil,
                            server: String? = nil) -> PairingInvitation {
        PairingInvitation(version: 1, serverID: server ?? serverID, addresses: addresses,
                          certificateSHA256: String(repeating: "A", count: 64),
                          invitationToken: String(repeating: "B", count: 43),
                          expiresAt: expires ?? Date().timeIntervalSince1970 + 180)
    }

    private func invitationText(_ value: PairingInvitation) throws -> String {
        let encoded = try JSONEncoder().encode(value).base64EncodedString()
            .replacingOccurrences(of: "+", with: "-").replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
        return "receiptsync://pair?data=" + encoded
    }

    func testRejectedManualDoesNotBlockValidEntriesOrSummaryAndDoesNotAutoRetry() async throws {
        let first = manual(), second = manual()
        let service = FakeSyncService(serverID: serverID)
        service.manualErrors[first.id] = .invalidEntry
        let root = try directory(manuals: [first, second])
        let store = store(root: root, service: service)
        let success = await store.syncAll()
        XCTAssertTrue(success)
        XCTAssertEqual(service.manualIDs, [first.id, second.id])
        XCTAssertEqual(service.summaryCalls, 1)
        XCTAssertEqual(store.pendingManuals.first?.blockedReason, "invalid")
        let reloaded = self.store(root: root, service: service)
        await reloaded.syncAll()
        XCTAssertEqual(service.manualIDs.count, 2)
        XCTAssertEqual(service.summaryCalls, 2)
    }

    func testCorrectingRejectedManualPreservesIDAndDiscardIsLocalOnly() async throws {
        let invalid = manual("invalid"), conflict = manual("conflict")
        let service = FakeSyncService(serverID: serverID)
        let store = store(root: try directory(manuals: [invalid, conflict]), service: service)
        try await store.addManual(requestID: invalid.id, kind: "expense", date: Date(), category: "修正",
                                  amount: 2, content: "更正", notes: "")
        XCTAssertEqual(service.manualIDs, [invalid.id])
        do {
            try await store.addManual(requestID: conflict.id, kind: "expense", date: Date(), category: "修正",
                                      amount: 2, content: "更正", notes: "")
            XCTFail("Conflicting records must be checked on the Mac before editing")
        } catch {}
        try await store.discardRejectedManual(conflict.id)
        XCTAssertTrue(store.pendingManuals.isEmpty)
        XCTAssertEqual(service.manualIDs, [invalid.id])
    }

    func testAuthenticationFailureStillStopsWholeQueue() async throws {
        let first = manual(), second = manual()
        let service = FakeSyncService(serverID: serverID)
        service.manualErrors[first.id] = .unauthorized
        let store = store(root: try directory(manuals: [first, second]), service: service)
        let success = await store.syncAll()
        XCTAssertFalse(success)
        XCTAssertEqual(service.manualIDs, [first.id])
        XCTAssertEqual(service.summaryCalls, 0)
        XCTAssertEqual(store.pendingManuals.count, 2)
        XCTAssertNil(store.pendingManuals.first?.blockedReason)
    }

    func testClientValidationRejectsOversizedManualBeforeSaving() async throws {
        let service = FakeSyncService(serverID: serverID)
        let store = store(root: try directory(), service: service)
        do {
            try await store.addManual(kind: "expense", date: Date(), category: String(repeating: "字", count: 101),
                                      amount: 1, content: "", notes: "")
            XCTFail("Invalid data must not enter the queue")
        } catch {}
        XCTAssertTrue(store.pendingManuals.isEmpty)
        XCTAssertEqual(service.manualIDs.count, 0)
    }

    func testRejectedReceiptDoesNotBlockSummaryOrManual() async throws {
        let pending = receipt()
        let root = try directory(receipts: [pending])
        try JSONEncoder().encode([manual()]).write(to: root.appendingPathComponent("manual-queue.json"))
        let service = FakeSyncService(serverID: serverID)
        service.uploadError = .invalidEntry
        let store = store(root: root, service: service)
        await store.syncAll()
        XCTAssertEqual(store.receipts.first?.blockedReason, "invalid")
        XCTAssertTrue(store.pendingManuals.isEmpty)
        XCTAssertEqual(service.summaryCalls, 1)
        await store.syncAll()
        XCTAssertEqual(service.uploadCalls, 1)
        service.uploadError = nil
        try await store.retryReceipt(pending.id)
        XCTAssertEqual(service.uploadCalls, 2)
        XCTAssertNil(store.receipts.first?.blockedReason)
    }

    func testDiskFailurePreservesRejectedEntryDuringDiscard() async throws {
        let entry = manual("invalid")
        let root = try directory(manuals: [entry])
        let service = FakeSyncService(serverID: serverID)
        let store = store(root: root, service: service)
        // A directory at the destination reliably simulates an atomic-write failure.
        let target = root.appendingPathComponent("manual-queue.json")
        try FileManager.default.moveItem(at: target, to: root.appendingPathComponent("saved-manual-queue.json"))
        try FileManager.default.createDirectory(at: target, withIntermediateDirectories: false)
        do { try await store.discardRejectedManual(entry.id); XCTFail("write failure must be reported") }
        catch {}
        XCTAssertEqual(store.pendingManuals, [entry])
    }

    func testPairingDraftIsDurableBeforeNetworkAndResponseLossReusesKeyAfterRelaunch() async throws {
        let service = FakeSyncService(serverID: serverID)
        service.redemptionError = .unreachable
        var saved: PendingPairing?
        service.beforeRedemption = { draft in XCTAssertEqual(saved, draft) }
        let root = try directory()
        let store = store(root: root, service: service, savePending: { saved = $0 })
        let previous = store.settings
        do { try await store.pairWithInvitation(invitationText(invitation())); XCTFail("response lost") } catch {}
        XCTAssertEqual(store.settings, previous)
        XCTAssertNotNil(saved)
        let reloaded = self.store(root: root, service: service, pending: saved, savePending: { saved = $0 })
        service.redemptionError = nil
        try await reloaded.pairWithInvitation()
        XCTAssertEqual(service.redemptions.count, 2)
        XCTAssertEqual(service.redemptions.first, service.redemptions.last)
        XCTAssertEqual(reloaded.settings.syncToken, service.redemptions.first?.deviceToken)
        XCTAssertNil(saved)
    }

    func testPairingStorageFailureNeverSendsInvitation() async throws {
        let service = FakeSyncService(serverID: serverID)
        let store = store(root: try directory(), service: service,
                          savePending: { _ in throw KeychainStore.StorageError.unavailable })
        do { try await store.pairWithInvitation(invitationText(invitation())); XCTFail("cannot save key") } catch {}
        XCTAssertTrue(service.redemptions.isEmpty)
    }

    func testExpiredOrDifferentServerInvitationNeverSendsCredentials() async throws {
        let service = FakeSyncService(serverID: serverID)
        let store = store(root: try directory(), service: service)
        for value in [invitation(expires: 0), invitation(server: UUID().uuidString.lowercased())] {
            do { try await store.pairWithInvitation(invitationText(value)); XCTFail("invalid invitation") } catch {}
        }
        XCTAssertTrue(service.redemptions.isEmpty)
    }

    func testInvitationParserRejectsInsecureOrMalformedAddresses() throws {
        for addresses in [["http://192.0.2.10"], ["https://user:password@192.0.2.10"], [],
                          ["https://192.0.2.10/steal"], ["https://192.0.2.10 https://192.0.2.20"]] {
            XCTAssertThrowsError(try PairingInvitation.parse(invitationText(invitation(addresses: addresses))))
        }
        let text = try invitationText(invitation())
        XCTAssertThrowsError(try PairingInvitation.parse(text + "&data=duplicate"))
        XCTAssertEqual(try PairingInvitation.parse(text), invitation(expires: try PairingInvitation.parse(text).expiresAt))
    }

    func testPairingCleanupFailureDoesNotUndoCommittedConnection() async throws {
        let service = FakeSyncService(serverID: serverID)
        let store = store(root: try directory(), service: service, savePending: { value in
            if value == nil { throw KeychainStore.StorageError.unavailable }
        })
        try await store.pairWithInvitation(invitationText(invitation()))
        XCTAssertEqual(store.connectionState, .connected)
        XCTAssertEqual(store.settings.syncToken, service.redemptions.first?.deviceToken)
    }

    func testOfflineChecksConnectionOnceAndPreservesWholeQueue() async throws {
        let queued = [receipt(), receipt(), receipt()]
        let service = FakeSyncService(serverID: serverID)
        service.connectionError = .unreachable
        let store = store(root: try directory(receipts: queued), service: service)
        let success = await store.syncAll()
        XCTAssertFalse(success)
        XCTAssertEqual(service.connectionCalls, 1)
        XCTAssertEqual(service.uploadCalls, 0)
        XCTAssertEqual(store.receipts.map(\.status), [.waiting, .waiting, .waiting])
        XCTAssertTrue(store.connectionState.isFailure)
    }

    func testConfirmedReceiptSurvivesOfflineAndDeletedReceiptCleansUp() async throws {
        let confirmed = receipt(.confirmed, uploaded: true)
        let service = FakeSyncService(serverID: serverID)
        service.receiptError = .unreachable
        let store = store(root: try directory(receipts: [confirmed]), service: service)
        await store.syncAll()
        XCTAssertEqual(store.receipts.first?.status, .confirmed)
        service.receiptError = .notFound
        await store.syncAll()
        XCTAssertTrue(store.receipts.isEmpty)
        XCTAssertEqual(service.uploadCalls, 0)
    }

    func testInterruptedUploadIsLookedUpBeforeResending() async throws {
        let pending = receipt(.uploading)
        let service = FakeSyncService(serverID: serverID)
        service.receiptStatus = "processing"
        let store = store(root: try directory(receipts: [pending]), service: service)
        await store.syncAll()
        XCTAssertEqual(service.uploadCalls, 0)
        XCTAssertEqual(store.receipts.first?.status, .processing)
        XCTAssertEqual(store.receipts.first?.uploaded, true)
    }

    func testMissingUnacknowledgedUploadRetriesSameUUID() async throws {
        let pending = receipt(.uploading)
        let service = FakeSyncService(serverID: serverID)
        service.receiptError = .notFound
        let store = store(root: try directory(receipts: [pending]), service: service)
        await store.syncAll()
        XCTAssertEqual(service.uploadIDs, [pending.id])
        XCTAssertEqual(store.receipts.first?.status, .queued)
    }

    func testDeletedUploadResponseRemovesQueueAndImage() async throws {
        let pending = receipt()
        let service = FakeSyncService(serverID: serverID)
        service.uploadError = .deleted
        let root = try directory(receipts: [pending])
        let store = store(root: root, service: service)
        await store.syncAll()
        XCTAssertTrue(store.receipts.isEmpty)
        XCTAssertFalse(FileManager.default.fileExists(atPath: root.appendingPathComponent(pending.imageFileName).path))
    }

    func testDifferentServerDoesNotReceiveOldQueue() async throws {
        let anotherID = UUID().uuidString.lowercased()
        let service = FakeSyncService(serverID: anotherID)
        let store = store(root: try directory(receipts: [receipt()]), service: service, configuration: settings(anotherID))
        await store.syncAll()
        XCTAssertEqual(service.uploadCalls, 0)
        XCTAssertEqual(service.receiptCalls, 0)
        XCTAssertEqual(store.heldReceiptCount, 1)
    }

    func testFailedReplacementDoesNotOverwriteSavedSettings() async throws {
        let service = FakeSyncService(serverID: serverID)
        service.connectionError = .unauthorized
        var writes = 0
        let store = store(root: try directory(), service: service, save: { _ in writes += 1 })
        let previous = store.settings
        do {
            try await store.connectAndSave(serverURL: "https://192.0.2.20:8765", syncToken: "wrong-token", certificateSHA256: previous.certificateSHA256)
            XCTFail("invalid token must not be saved")
        } catch { XCTAssertEqual(error as? SyncClient.SyncError, .unauthorized) }
        XCTAssertEqual(store.settings, previous)
        XCTAssertEqual(writes, 0)
    }

    func testSuccessfulHandshakeCommitsIdentityAndAddressTogether() async throws {
        let service = FakeSyncService(serverID: serverID)
        var saved: AppSettings?
        let store = store(root: try directory(), service: service, save: { saved = $0 })
        try await store.connectAndSave(serverURL: "192.0.2.20", syncToken: "new-device-token", certificateSHA256: settings().certificateSHA256)
        XCTAssertEqual(saved?.serverID, serverID)
        XCTAssertEqual(saved?.lastSuccessfulURL, "https://192.0.2.20:8765")
        XCTAssertEqual(saved, store.settings)
    }

    func testKeychainFailurePreservesPreviousSettings() async throws {
        let service = FakeSyncService(serverID: serverID)
        let store = store(root: try directory(), service: service, save: { _ in throw KeychainStore.StorageError.unavailable })
        let previous = store.settings
        do {
            try await store.connectAndSave(serverURL: "192.0.2.20", syncToken: "new-device-token", certificateSHA256: previous.certificateSHA256)
            XCTFail("storage failure must be reported")
        } catch {}
        XCTAssertEqual(store.settings, previous)
    }

    func testForgetRetainsQueueBindingAcrossRelaunch() async throws {
        let pending = receipt()
        let root = try directory(receipts: [pending])
        let service = FakeSyncService(serverID: serverID)
        let store = store(root: root, service: service)
        try await store.forgetComputer()
        XCTAssertFalse(store.configured)
        XCTAssertEqual(store.receipts.count, 1)
        let reloaded = self.store(root: root, service: service, configuration: store.settings)
        XCTAssertEqual(reloaded.receipts.first?.serverID, serverID)
        XCTAssertTrue(FileManager.default.fileExists(atPath: root.appendingPathComponent(pending.imageFileName).path))
    }

    func testConcurrentSyncDoesNotUploadSameReceiptTwice() async throws {
        let service = FakeSyncService(serverID: serverID)
        service.uploadDelay = .milliseconds(30)
        let store = store(root: try directory(receipts: [receipt()]), service: service)
        async let first = store.syncAll()
        async let second = store.syncAll()
        _ = await (first, second)
        XCTAssertEqual(service.uploadCalls, 1)
        XCTAssertEqual(service.maxConcurrentUploads, 1)
    }

    func testCancelledSyncPreservesQueueWithoutCertificateError() async throws {
        let service = FakeSyncService(serverID: serverID)
        service.uploadDelay = .seconds(5)
        let store = store(root: try directory(receipts: [receipt()]), service: service)
        let task = Task { await store.syncAll() }
        while service.uploadCalls == 0 { await Task.yield() }
        task.cancel()
        let success = await task.value
        XCTAssertFalse(success)
        XCTAssertEqual(store.receipts.count, 1)
        XCTAssertEqual(store.connectionState, .idle)
        XCTAssertFalse(store.syncMessage.contains("证书"))
    }

    func testManualResponseLossRetainsDurableRequestIDForRetry() async throws {
        let service = FakeSyncService(serverID: serverID)
        service.manualError = .unreachable
        let root = try directory()
        let store = store(root: root, service: service)
        try await store.addManual(kind: "expense", date: Date(), category: "交通", amount: 2.5, content: "测试", notes: "")
        XCTAssertEqual(store.pendingManuals.count, 1)
        let requestID = store.pendingManuals[0].id
        let reloaded = self.store(root: root, service: service)
        service.manualError = nil
        await reloaded.syncAll()
        XCTAssertEqual(service.manualIDs, [requestID, requestID])
        XCTAssertTrue(reloaded.pendingManuals.isEmpty)
    }

    func testReadOnlyDeviceDoesNotSendQueuedWrites() async throws {
        let service = FakeSyncService(serverID: serverID)
        service.role = "viewer"
        let store = store(root: try directory(receipts: [receipt()]), service: service)
        await store.syncAll()
        XCTAssertFalse(store.canWrite)
        XCTAssertEqual(service.uploadCalls, 0)
        XCTAssertEqual(store.receipts.count, 1)
    }

    func testCorruptQueueIsNeverOverwritten() async throws {
        let root = try directory()
        let file = root.appendingPathComponent("queue.json")
        let damaged = Data("preserve this unreadable data".utf8)
        try damaged.write(to: file)
        let service = FakeSyncService(serverID: serverID)
        let store = store(root: root, service: service)
        await store.syncAll()
        XCTAssertEqual(try Data(contentsOf: file), damaged)
        XCTAssertEqual(service.connectionCalls, 0)
    }

    func testCertificatePinRequiresExactCertificateAndCancellationIsSeparate() {
        let certificate = Data("sample certificate bytes".utf8)
        let fingerprint = SHA256.hash(data: certificate).map { String(format: "%02X", $0) }.joined()
        let delegate = PinnedSessionDelegate(expectedFingerprint: fingerprint)
        XCTAssertTrue(delegate.accepts(certificateData: certificate))
        XCTAssertFalse(delegate.accepts(certificateData: Data("different certificate".utf8)))
        XCTAssertEqual(NetworkSyncMessage.wrap(CancellationError()), .cancelled)
        XCTAssertEqual(NetworkSyncMessage.wrap(URLError(.timedOut)), .timedOut)
        XCTAssertEqual(NetworkSyncMessage.wrap(URLError(.secureConnectionFailed)), .tlsFailed)
        XCTAssertEqual(NetworkSyncMessage.wrap(URLError(.serverCertificateUntrusted)), .certificateRejected)
    }

    func testTemporaryMacServerTLSAndSharedLedger() async throws {
        let path = ProcessInfo.processInfo.environment["RECEIPT_SYNC_TEST_FIXTURE"] ?? ""
        guard path.hasPrefix("/"), FileManager.default.fileExists(atPath: path) else {
            throw XCTSkip("Run ios/test_mac_connection.py for the disposable Mac TLS integration test")
        }
        let fixture = try JSONDecoder().decode(MacFixture.self, from: Data(contentsOf: URL(fileURLWithPath: path)))
        guard fixture.purpose == "temporary-mac-test", URL(string: fixture.url)?.host == "127.0.0.1" else {
            throw XCTSkip("Only a disposable loopback server is allowed")
        }
        let client = SyncClient()
        let secondClient = SyncClient()
        defer { Task { await client.disconnect(); await secondClient.disconnect() } }
        var first = AppSettings(serverURL: fixture.url, syncToken: fixture.token,
                                certificateSHA256: fixture.pin, deviceID: "temporary-simulator")
        let connected = try await client.connect(settings: first)
        XCTAssertEqual(connected.info.serverId, fixture.server_id)
        first.serverID = connected.info.serverId
        let repeatConnection = try await client.connect(settings: first)
        XCTAssertEqual(repeatConnection.info.device.id, connected.info.device.id)
        var second = first
        second.syncToken = fixture.second_token
        let secondConnection = try await secondClient.connect(settings: second)
        XCTAssertNotEqual(secondConnection.info.device.id, connected.info.device.id)
        let transaction = ManualTransactionRequest(occurredAt: "2026-09-06T12:00:00+08:00", kind: "expense",
                                                   category: "测试", amountCents: 125, content: "临时测试", notes: "")
        try await client.addTransaction(transaction, settings: first)
        try await client.addTransaction(transaction, settings: first)
        let summary = try await secondClient.summary(month: "2026-09", settings: second)
        XCTAssertEqual(summary.expenseCents, 125)
        let id = UUID()
        let queued = PendingReceipt(id: id, capturedAt: Date(), imageFileName: "temporary.jpg", status: .waiting)
        let image = Data([0xff, 0xd8, 0xff] + Array(repeating: UInt8(1), count: 100))
        let uploaded = try await client.upload(receipt: queued, imageData: image, settings: first)
        XCTAssertEqual(uploaded.status, "queued")
        _ = try await client.upload(receipt: queued, imageData: image, settings: first)
        let known = try await client.receipt(id: id, settings: first)
        XCTAssertEqual(known.id, id.uuidString.lowercased())
        var wrongServer = first
        wrongServer.serverID = UUID().uuidString.lowercased()
        do { _ = try await client.connect(settings: wrongServer); XCTFail("wrong server must be rejected") }
        catch { XCTAssertEqual(error as? SyncClient.SyncError, .serverChanged) }
        var revoked = first
        revoked.syncToken = fixture.revoked_token
        do { _ = try await client.connect(settings: revoked); XCTFail("revoked device must be rejected") }
        catch { XCTAssertEqual(error as? SyncClient.SyncError, .unauthorized) }
        var wrongPin = first
        wrongPin.certificateSHA256 = String(repeating: "0", count: 64)
        do { _ = try await client.connect(settings: wrongPin); XCTFail("wrong certificate must be rejected") }
        catch { XCTAssertEqual(error as? SyncClient.SyncError, .certificateRejected) }
        _ = try await client.connect(settings: first) // Recovers with the correct pin, no insecure fallback.

        // Decode the actual locally generated QR with Apple's barcode detector, then
        // redeem over pinned TLS twice to prove acknowledgement-loss idempotency.
        // Core Image's software QR detector also works on simulator hosts where
        // Vision's hardware inference context is unavailable. Camera UI remains a device check.
        let detector = try XCTUnwrap(CIDetector(ofType: CIDetectorTypeQRCode,
            context: CIContext(options: [.useSoftwareRenderer: true]),
            options: [CIDetectorAccuracy: CIDetectorAccuracyHigh]))
        let qrImage = try XCTUnwrap(CIImage(contentsOf: URL(fileURLWithPath: fixture.qr_path)))
        let decoded = try XCTUnwrap((detector.features(in: qrImage).first as? CIQRCodeFeature)?.messageString)
        let invitation = try PairingInvitation.parse(decoded)
        let draft = try PendingPairing.create(invitation)
        var qrSettings = first
        qrSettings.syncToken = draft.deviceToken
        try await client.redeemInvitation(draft, settings: qrSettings)
        try await client.redeemInvitation(draft, settings: qrSettings)
        let qrConnection = try await client.connect(settings: qrSettings)
        XCTAssertNotEqual(qrConnection.info.device.id, connected.info.device.id)
        let qrSummary = try await client.summary(month: "2026-09", settings: qrSettings)
        XCTAssertEqual(qrSummary.expenseCents, 125)
    }
}

private struct MacFixture: Decodable {
    let purpose, url, pin, token, second_token, revoked_token, server_id, qr_path: String
}

@MainActor
private final class FakeSyncService: SyncService {
    let serverID: String
    var role = "contributor"
    var connectionError: SyncClient.SyncError?
    var receiptError: SyncClient.SyncError?
    var uploadError: SyncClient.SyncError?
    var manualError: SyncClient.SyncError?
    var manualErrors: [String: SyncClient.SyncError] = [:]
    var summaryCalls = 0
    var redemptionError: SyncClient.SyncError?
    var redemptions: [PendingPairing] = []
    var beforeRedemption: ((PendingPairing) -> Void)?
    var receiptStatus = "queued"
    var connectionCalls = 0
    var receiptCalls = 0
    var uploadCalls = 0
    var uploadIDs: [UUID] = []
    var manualIDs: [String] = []
    var uploadDelay: Duration = .zero
    var activeUploads = 0
    var maxConcurrentUploads = 0
    init(serverID: String) { self.serverID = serverID }
    func connect(settings: AppSettings) async throws -> VerifiedConnection {
        connectionCalls += 1
        if let connectionError { throw connectionError }
        if let expected = settings.serverID, expected != serverID { throw SyncClient.SyncError.serverChanged }
        return VerifiedConnection(info: ServerConnection(service: "receipt-sync", protocolVersion: 2, serverId: serverID,
                                                        device: ConnectedDevice(id: "test", name: "Test", role: role)),
                                  baseURL: PairingInput.candidateURLs(settings.serverURL)[0])
    }
    func upload(receipt: PendingReceipt, imageData: Data, settings: AppSettings) async throws -> RemoteReceipt {
        uploadCalls += 1
        uploadIDs.append(receipt.id)
        activeUploads += 1
        maxConcurrentUploads = max(maxConcurrentUploads, activeUploads)
        defer { activeUploads -= 1 }
        try await Task.sleep(for: uploadDelay)
        if let uploadError { throw uploadError }
        return RemoteReceipt(id: receipt.id.uuidString.lowercased(), status: "queued", error: nil, transaction: nil)
    }
    func receipt(id: UUID, settings: AppSettings) async throws -> RemoteReceipt {
        receiptCalls += 1
        if let receiptError { throw receiptError }
        return RemoteReceipt(id: id.uuidString.lowercased(), status: receiptStatus, error: nil, transaction: nil)
    }
    func summary(month: String, settings: AppSettings) async throws -> MonthlySummary {
        summaryCalls += 1
        return MonthlySummary(month: month, currency: "HKD", incomeCents: 0, expenseCents: 0, balanceCents: 0,
                       incomeByCategory: [], expenseByCategory: [], pendingReceipts: 0, updatedAt: nil)
    }
    func addTransaction(_ transaction: ManualTransactionRequest, settings: AppSettings) async throws {
        manualIDs.append(transaction.requestID)
        if let error = manualErrors[transaction.requestID] { throw error }
        if let manualError { throw manualError }
    }
    func redeemInvitation(_ pending: PendingPairing, settings: AppSettings) async throws {
        beforeRedemption?(pending)
        redemptions.append(pending)
        if let redemptionError { throw redemptionError }
    }
    func disconnect() async {}
}
