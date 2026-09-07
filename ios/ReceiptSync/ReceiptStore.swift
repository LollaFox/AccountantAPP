import Combine
import Foundation
import UIKit

@MainActor
final class ReceiptStore: ObservableObject {
    static let shared: ReceiptStore = {
        #if DEBUG
        if ProcessInfo.processInfo.arguments.contains("-ui-testing")
            || ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] != nil {
            return ReceiptStore(settings: AppSettings(serverURL: "", syncToken: "", certificateSHA256: "", deviceID: "ui-test"),
                                rootDirectory: FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString),
                                saveSettings: { _ in }, schedulesBackgroundRefresh: false, pendingPairing: nil, savePendingPairing: { _ in })
        }
        #endif
        return ReceiptStore()
    }()

    @Published private(set) var settings: AppSettings
    @Published private(set) var receipts: [PendingReceipt] = []
    @Published private(set) var pendingManuals: [PendingManual] = []
    @Published private(set) var summary = MonthlySummary.empty
    @Published private(set) var selectedMonth = Date()
    @Published private(set) var syncMessage = ""
    @Published private(set) var connectionState: ConnectionState = .unconfigured
    @Published private(set) var testingConnection = false
    @Published private(set) var pendingPairing: PendingPairing?
    private let savePendingPairing: (PendingPairing?) throws -> Void

    private let client: any SyncService
    private let saveSettings: (AppSettings) throws -> Void
    private let schedulesBackgroundRefresh: Bool
    private let encoder = JSONEncoder()
    private let decoder = JSONDecoder()
    private let rootDirectory: URL
    private let queueFile: URL
    private let manualQueueFile: URL
    private var loadError: Error?
    private var generation = 0
    private var syncTask: Task<Bool, Never>?
    private var syncRunID: UUID?
    private var resyncRequested = false

    enum StoreError: LocalizedError {
        case missingImage, unavailableStorage, pairingRequired
        var errorDescription: String? {
            switch self {
            case .missingImage: "本机找不到小票图片，请核对电脑上的小票状态。"
            case .unavailableStorage: "无法读取或保存本机队列。已有数据已保留，请检查设备储存空间。"
            case .pairingRequired: "请先测试并保存电脑连接，再新增手工收支。"
            }
        }
    }

    init(settings: AppSettings = AppSettings.load(), rootDirectory: URL? = nil,
         client: any SyncService = SyncClient(), saveSettings: @escaping (AppSettings) throws -> Void = { try $0.save() },
         schedulesBackgroundRefresh: Bool = true, pendingPairing: PendingPairing? = PendingPairing.load(),
         savePendingPairing: @escaping (PendingPairing?) throws -> Void = PendingPairing.save) {
        self.settings = settings
        self.pendingPairing = pendingPairing
        self.savePendingPairing = savePendingPairing
        self.client = client
        self.saveSettings = saveSettings
        self.schedulesBackgroundRefresh = schedulesBackgroundRefresh
        let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        self.rootDirectory = rootDirectory ?? support.appendingPathComponent("ReceiptSync", isDirectory: true)
        queueFile = self.rootDirectory.appendingPathComponent("queue.json")
        manualQueueFile = self.rootDirectory.appendingPathComponent("manual-queue.json")
        encoder.dateEncodingStrategy = .iso8601
        decoder.dateDecodingStrategy = .iso8601
        do {
            try FileManager.default.createDirectory(at: self.rootDirectory, withIntermediateDirectories: true)
            if FileManager.default.fileExists(atPath: queueFile.path) {
                receipts = try decoder.decode([PendingReceipt].self, from: Data(contentsOf: queueFile))
            }
            if FileManager.default.fileExists(atPath: manualQueueFile.path) {
                pendingManuals = try decoder.decode([PendingManual].self, from: Data(contentsOf: manualQueueFile))
            }
            // Bind legacy queue entries to the existing pin before users can edit or forget pairing.
            for index in receipts.indices {
                if receipts[index].serverID == nil, receipts[index].serverFingerprint == nil,
                   !settings.certificateSHA256.isEmpty {
                    receipts[index].serverID = settings.serverID
                    receipts[index].serverFingerprint = PairingInput.normalizedFingerprint(settings.certificateSHA256)
                }
                if receipts[index].uploaded == nil,
                   [.queued, .processing, .review, .confirmed].contains(receipts[index].status) {
                    receipts[index].uploaded = true
                }
            }
        } catch { loadError = error }
        connectionState = configured ? .idle : .unconfigured
    }

    var configured: Bool {
        PairingInput.isReadyToSync(serverURL: settings.serverURL, syncToken: settings.syncToken,
                                  certificateSHA256: settings.certificateSHA256)
    }

    var canWrite: Bool { settings.role != "viewer" }
    var selectedMonthKey: String { HongKongDate.monthKey(from: selectedMonth) }
    var heldReceiptCount: Int { receipts.filter { !belongsToCurrentServer($0) }.count }
    var heldManualCount: Int { pendingManuals.filter { $0.serverID != settings.serverID }.count }

    private func belongsToCurrentServer(_ receipt: PendingReceipt) -> Bool {
        if let id = receipt.serverID { return id == settings.serverID }
        if let pin = receipt.serverFingerprint { return pin == PairingInput.normalizedFingerprint(settings.certificateSHA256) }
        return true
    }

    private func stopSync() async {
        generation += 1
        syncTask?.cancel()
        syncTask = nil
        syncRunID = nil
        resyncRequested = false
        await client.disconnect()
    }

    func connectAndSave(serverURL: String, syncToken: String, certificateSHA256: String) async throws {
        guard !testingConnection else { return }
        if let message = PairingInput.validationMessage(serverURL: serverURL, syncToken: syncToken,
                                                       certificateSHA256: certificateSHA256) {
            throw PairingValidationError(message: message)
        }
        testingConnection = true
        defer { testingConnection = false }
        await stopSync()
        let previous = settings
        connectionState = .connecting
        syncMessage = "正在验证电脑连接"
        var candidate = AppSettings(serverURL: PairingInput.normalizedURL(serverURL),
                                    syncToken: PairingInput.normalizedToken(syncToken),
                                    certificateSHA256: PairingInput.normalizedFingerprint(certificateSHA256),
                                    deviceID: settings.deviceID, serverID: settings.serverID)
        do {
            let connection = try await client.connect(settings: candidate)
            try Task.checkCancellation()
            candidate.serverID = connection.info.serverId
            candidate.lastSuccessfulURL = connection.baseURL
            candidate.role = connection.info.device.role
            try persist() // Save legacy queue bindings before replacing credentials.
            try saveSettings(candidate)
            settings = candidate
            summary = .empty
            connectionState = .connected
            syncMessage = "已连接电脑"
        } catch {
            settings = previous
            connectionState = configured ? .idle : .unconfigured
            syncMessage = configured ? "原有连接设置已保留" : "请先设置电脑地址"
            await client.disconnect()
            throw error
        }
    }

    func forgetComputer() async throws {
        guard !testingConnection else { return }
        testingConnection = true
        defer { testingConnection = false }
        await stopSync()
        try persist()
        let empty = AppSettings(serverURL: "", syncToken: "", certificateSHA256: "", deviceID: settings.deviceID)
        try saveSettings(empty)
        settings = empty
        summary = .empty
        connectionState = .unconfigured
        syncMessage = "已忘记电脑，本机队列已保留"
    }

    func pairWithInvitation(_ text: String? = nil) async throws {
        guard !testingConnection else { return }
        let draft: PendingPairing
        if let text {
            let invitation = try PairingInvitation.parse(text)
            if let expected = settings.serverID, expected != invitation.serverID { throw SyncClient.SyncError.serverChanged }
            if let pendingPairing, pendingPairing.invitation == invitation { draft = pendingPairing }
            else {
                guard invitation.expiresAt > Date().timeIntervalSince1970 else {
                    throw PairingValidationError(message: "邀请已过期，请在电脑生成新的二维码。")
                }
                draft = try PendingPairing.create(invitation)
            }
        } else {
            guard let pendingPairing else { throw SyncClient.SyncError.invalidServer }
            draft = pendingPairing
        }
        if let expected = settings.serverID, expected != draft.invitation.serverID { throw SyncClient.SyncError.serverChanged }
        testingConnection = true
        defer { testingConnection = false }
        await stopSync()
        // Durable before any network request: response loss must reuse this exact claim and key.
        try savePendingPairing(draft)
        pendingPairing = draft
        var candidate = AppSettings(serverURL: draft.invitation.addresses.joined(separator: " "), syncToken: draft.deviceToken,
                                    certificateSHA256: draft.invitation.certificateSHA256, deviceID: settings.deviceID,
                                    serverID: draft.invitation.serverID)
        do {
            try await client.redeemInvitation(draft, settings: candidate)
            let connection = try await client.connect(settings: candidate)
            try Task.checkCancellation()
            candidate.lastSuccessfulURL = connection.baseURL
            candidate.role = connection.info.device.role
            try persist()
            try saveSettings(candidate)
            settings = candidate
            summary = .empty
            // Credentials are already durably committed. Cleanup failure must not report
            // a failed pairing or replace this now-working connection.
            do { try savePendingPairing(nil); pendingPairing = nil }
            catch { pendingPairing = draft }
            connectionState = .connected
            syncMessage = "已安全配对电脑"
        } catch {
            await client.disconnect()
            connectionState = configured ? .idle : .unconfigured
            throw error
        }
    }

    func moveMonth(_ amount: Int) {
        selectedMonth = HongKongDate.addMonths(amount, to: selectedMonth)
        summary = .empty
        Task { await syncAll() }
    }

    func enqueue(image: UIImage) async {
        guard canWrite else { setFailure(SyncClient.SyncError.forbidden); return }
        guard let data = image.jpegData(compressionQuality: 0.82) else { return }
        let id = UUID()
        let fileName = "\(id.uuidString.lowercased()).jpg"
        do {
            guard loadError == nil else { throw StoreError.unavailableStorage }
            guard data.count <= 12 * 1024 * 1024 else {
                throw PairingValidationError(message: "小票图片超过 12 MB，请重新拍摄。")
            }
            try data.write(to: imageURL(fileName), options: .atomic)
            receipts.insert(PendingReceipt(id: id, capturedAt: Date(), imageFileName: fileName, status: .waiting,
                                           serverID: settings.serverID,
                                           serverFingerprint: configured ? settings.certificateSHA256 : nil, uploaded: false), at: 0)
            do { try persist() } catch { receipts.removeAll { $0.id == id }; throw error }
            await syncAll()
        } catch { setFailure(error) }
    }

    @discardableResult
    func syncAll() async -> Bool {
        guard !testingConnection else { return false }
        guard configured else {
            connectionState = .unconfigured
            syncMessage = "请先设置电脑地址"
            return false
        }
        if let task = syncTask {
            resyncRequested = true
            return await waitForSync(task)
        }
        let runID = UUID()
        let version = generation
        syncRunID = runID
        let task = Task { @MainActor in
            var succeeded = false
            repeat {
                self.resyncRequested = false
                succeeded = await self.syncPass(generation: version)
            } while succeeded && self.resyncRequested && !Task.isCancelled && self.generation == version
            return succeeded
        }
        syncTask = task
        let result = await waitForSync(task)
        if syncRunID == runID {
            syncTask = nil
            syncRunID = nil
        }
        return result
    }

    private func waitForSync(_ task: Task<Bool, Never>) async -> Bool {
        await withTaskCancellationHandler { await task.value } onCancel: { task.cancel() }
    }

    private func checkCurrent(_ version: Int) throws {
        try Task.checkCancellation()
        guard version == generation else { throw CancellationError() }
    }

    private func syncPass(generation version: Int) async -> Bool {
        do {
            try checkCurrent(version)
            guard loadError == nil else { throw StoreError.unavailableStorage }
            connectionState = .connecting
            syncMessage = "正在连接电脑"
            let connection = try await client.connect(settings: settings)
            try checkCurrent(version)
            var verified = settings
            verified.serverID = connection.info.serverId
            verified.lastSuccessfulURL = connection.baseURL
            verified.role = connection.info.device.role
            if verified != settings { try saveSettings(verified); settings = verified }
            connectionState = .syncing
            syncMessage = "正在同步"
            if canWrite {
                for id in receipts.filter({ belongsToCurrentServer($0) && $0.blockedReason == nil }).map(\.id) {
                    try checkCurrent(version)
                    try await syncReceipt(id: id, settings: verified, generation: version)
                }
                for manual in pendingManuals.filter({ $0.serverID == verified.serverID && $0.blockedReason == nil }) {
                    try checkCurrent(version)
                    do {
                        try await client.addTransaction(manual.transaction, settings: verified)
                        try checkCurrent(version)
                        let previous = pendingManuals
                        pendingManuals.removeAll { $0.id == manual.id }
                        do { try persist() } catch { pendingManuals = previous; throw error }
                    } catch {
                        try checkCurrent(version)
                        if let index = pendingManuals.firstIndex(where: { $0.id == manual.id }) {
                            pendingManuals[index].errorMessage = NetworkSyncMessage.display(error)
                            pendingManuals[index].blockedReason = Self.recordFailure(error)
                            try persist()
                        }
                        if Self.recordFailure(error) != nil { continue }
                        throw error
                    }
                }
            }
            let month = selectedMonthKey
            let loaded = try await client.summary(month: month, settings: verified)
            try checkCurrent(version)
            if month == selectedMonthKey { summary = loaded }
            else { resyncRequested = true }
            connectionState = .connected
            let held = heldReceiptCount + heldManualCount
            if held > 0 { syncMessage = "已同步；\(held) 条其他服务器的本机记录已保留" }
            else if canWrite, pendingManuals.contains(where: { $0.serverID == verified.serverID && $0.blockedReason != nil }) {
                syncMessage = "汇总已更新；有收支记录需要修改或在电脑核对"
            } else if canWrite, let message = receipts.first(where: { $0.errorMessage != nil })?.errorMessage {
                syncMessage = "已连接；\(message)"
            } else { syncMessage = canWrite ? "已同步" : "已同步（只读汇总）" }
            if schedulesBackgroundRefresh { AppDelegate.scheduleBackgroundRefresh() }
            return true
        } catch {
            guard version == generation else { return false }
            if NetworkSyncMessage.wrap(error) == .cancelled {
                connectionState = .idle
                syncMessage = SyncClient.SyncError.cancelled.localizedDescription
            } else { setFailure(error) }
            return false
        }
    }

    private func syncReceipt(id: UUID, settings verified: AppSettings, generation version: Int) async throws {
        guard let index = receipts.firstIndex(where: { $0.id == id }) else { return }
        receipts[index].serverID = verified.serverID
        receipts[index].serverFingerprint = verified.certificateSHA256
        let current = receipts[index]
        try persist()
        do {
            let remote: RemoteReceipt
            if current.status == .waiting && current.uploaded == false {
                remote = try await upload(current, settings: verified)
            } else {
                do {
                    remote = try await client.receipt(id: id, settings: verified)
                } catch SyncClient.SyncError.notFound {
                    try checkCurrent(version)
                    if current.uploaded == true {
                        try removeReceipt(id)
                        return
                    }
                    remote = try await upload(current, settings: verified)
                }
            }
            try checkCurrent(version)
            guard let refreshed = receipts.firstIndex(where: { $0.id == id }) else { return }
            receipts[refreshed].uploaded = true
            receipts[refreshed].status = LocalReceiptStatus(rawValue: remote.status) ?? .queued
            receipts[refreshed].merchant = remote.transaction?.merchant
            receipts[refreshed].amountCents = remote.transaction?.amountCents
            receipts[refreshed].errorMessage = remote.error
            try persist()
            if remote.status == "confirmed" { try? FileManager.default.removeItem(at: imageURL(current.imageFileName)) }
        } catch {
            try checkCurrent(version)
            if NetworkSyncMessage.wrap(error) == .deleted { try removeReceipt(id); return }
            guard let refreshed = receipts.firstIndex(where: { $0.id == id }) else { return }
            // Network failure must not turn a confirmed receipt into a fresh upload.
            receipts[refreshed].status = current.status
            receipts[refreshed].errorMessage = NetworkSyncMessage.display(error)
            receipts[refreshed].blockedReason = Self.recordFailure(error)
            try persist()
            if let storageError = error as? StoreError, case .missingImage = storageError { return }
            if Self.recordFailure(error) != nil { return }
            throw error
        }
    }

    private func upload(_ receipt: PendingReceipt, settings: AppSettings) async throws -> RemoteReceipt {
        let data: Data
        do { data = try Data(contentsOf: imageURL(receipt.imageFileName)) }
        catch { throw StoreError.missingImage }
        if let index = receipts.firstIndex(where: { $0.id == receipt.id }) {
            receipts[index].status = .uploading
            try persist()
        }
        return try await client.upload(receipt: receipt, imageData: data, settings: settings)
    }

    private func removeReceipt(_ id: UUID) throws {
        guard let receipt = receipts.first(where: { $0.id == id }) else { return }
        let previous = receipts
        receipts.removeAll { $0.id == id }
        do { try persist() } catch { receipts = previous; throw error }
        try? FileManager.default.removeItem(at: imageURL(receipt.imageFileName))
    }

    func refreshSummary() async { await syncAll() }

    private static func recordFailure(_ error: Error) -> String? {
        switch error as? SyncClient.SyncError {
        case .invalidEntry: "invalid"
        case .conflict: "conflict"
        default: nil
        }
    }

    func discardRejectedManual(_ id: String) async throws {
        guard let entry = pendingManuals.first(where: { $0.id == id }), entry.blockedReason != nil else { return }
        await stopSync()
        let previous = pendingManuals
        pendingManuals.removeAll { $0.id == id }
        do { try persist() } catch { pendingManuals = previous; throw error }
    }

    func retryReceipt(_ id: UUID) async throws {
        await stopSync()
        guard let index = receipts.firstIndex(where: { $0.id == id }) else { return }
        let previous = receipts
        receipts[index].blockedReason = nil
        receipts[index].errorMessage = nil
        do { try persist() } catch { receipts = previous; throw error }
        await syncAll()
    }

    func addManual(requestID: String = UUID().uuidString.lowercased(), kind: String, date: Date, category: String,
                   amount: Decimal, content: String, notes: String) async throws {
        guard configured, let serverID = settings.serverID else { throw StoreError.pairingRequired }
        guard canWrite else { throw SyncClient.SyncError.forbidden }
        guard loadError == nil else { throw StoreError.unavailableStorage }
        let cleanCategory = category.trimmingCharacters(in: .whitespacesAndNewlines)
        let cleanContent = content.trimmingCharacters(in: .whitespacesAndNewlines)
        let cleanNotes = notes.trimmingCharacters(in: .whitespacesAndNewlines)
        guard ["income", "expense"].contains(kind), UUID(uuidString: requestID) != nil,
              !cleanCategory.isEmpty, cleanCategory.unicodeScalars.count <= 100,
              cleanContent.unicodeScalars.count <= 500, cleanNotes.unicodeScalars.count <= 2000 else {
            throw PairingValidationError(message: "请填写有效大类（最多 100 字）；说明最多 500 字，备注最多 2000 字。")
        }
        let rounding = NSDecimalNumberHandler(roundingMode: .plain, scale: 0, raiseOnExactness: false,
                                              raiseOnOverflow: false, raiseOnUnderflow: false, raiseOnDivideByZero: false)
        let rounded = NSDecimalNumber(decimal: amount * 100).rounding(accordingToBehavior: rounding)
        guard rounded != .notANumber, rounded.compare(0) == .orderedDescending,
              rounded.compare(NSDecimalNumber(value: 1_000_000_000_000)) != .orderedDescending else {
            throw PairingValidationError(message: "金额必须至少为 HK$0.01，且在支持范围内。")
        }
        let request = ManualTransactionRequest(requestID: requestID, occurredAt: HongKongDate.isoString(from: date),
                                               kind: kind, category: cleanCategory, amountCents: rounded.intValue,
                                               content: cleanContent.isEmpty ? cleanCategory : cleanContent, notes: cleanNotes)
        if pendingManuals.contains(where: { $0.id == requestID }) {
            await stopSync()
        }
        let previous = pendingManuals
        if let index = pendingManuals.firstIndex(where: { $0.id == requestID }) {
            guard pendingManuals[index].serverID == serverID, pendingManuals[index].blockedReason == "invalid" else {
                throw PairingValidationError(message: "此记录可能已发送；请先在电脑核对，不要修改后重复提交。")
            }
            pendingManuals[index] = PendingManual(transaction: request, serverID: serverID)
        } else {
            pendingManuals.append(PendingManual(transaction: request, serverID: serverID))
        }
        do { try persist() } catch { pendingManuals = previous; throw error }
        selectedMonth = date
        summary = .empty
        // Once durable, the entry is accepted locally; offline failure is shown in the queue.
        await syncAll()
    }

    private func imageURL(_ name: String) throws -> URL {
        let url = rootDirectory.appendingPathComponent(name).standardizedFileURL.resolvingSymlinksInPath()
        guard url.deletingLastPathComponent() == rootDirectory.standardizedFileURL.resolvingSymlinksInPath() else {
            throw StoreError.missingImage
        }
        return url
    }

    private func persist() throws {
        guard loadError == nil else { throw StoreError.unavailableStorage }
        do {
            try encoder.encode(receipts).write(to: queueFile, options: .atomic)
            try encoder.encode(pendingManuals).write(to: manualQueueFile, options: .atomic)
        } catch { throw StoreError.unavailableStorage }
    }

    private func setFailure(_ error: Error) {
        syncMessage = NetworkSyncMessage.display(error)
        connectionState = .failed(syncMessage)
    }
}

struct PairingValidationError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}
