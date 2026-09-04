import Combine
import Foundation
import UIKit

@MainActor
final class ReceiptStore: ObservableObject {
    static let shared = ReceiptStore()

    @Published private(set) var settings = AppSettings.load()
    @Published private(set) var receipts: [PendingReceipt] = []
    @Published private(set) var summary = MonthlySummary.empty
    @Published var selectedMonth = Date()
    @Published private(set) var syncMessage = ""

    private let client = SyncClient()
    private let encoder: JSONEncoder
    private let decoder: JSONDecoder
    private let rootDirectory: URL
    private let queueFile: URL

    private init() {
        encoder = JSONEncoder()
        decoder = JSONDecoder()
        encoder.dateEncodingStrategy = .iso8601
        decoder.dateDecodingStrategy = .iso8601
        let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        rootDirectory = support.appendingPathComponent("ReceiptSync", isDirectory: true)
        queueFile = rootDirectory.appendingPathComponent("queue.json")
        try? FileManager.default.createDirectory(at: rootDirectory, withIntermediateDirectories: true)
        if let data = try? Data(contentsOf: queueFile), let saved = try? decoder.decode([PendingReceipt].self, from: data) {
            receipts = saved
        }
    }

    var configured: Bool {
        settings.serverURL.lowercased().hasPrefix("https://")
            && !settings.syncToken.isEmpty
            && settings.certificateSHA256.replacingOccurrences(of: ":", with: "").count == 64
    }

    var selectedMonthKey: String {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM"
        return formatter.string(from: selectedMonth)
    }

    func updateSettings(serverURL: String, syncToken: String, certificateSHA256: String) {
        settings.serverURL = serverURL.trimmingCharacters(in: .whitespacesAndNewlines)
        settings.syncToken = syncToken.trimmingCharacters(in: .whitespacesAndNewlines)
        settings.certificateSHA256 = certificateSHA256
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .replacingOccurrences(of: ":", with: "")
            .uppercased()
        settings.save()
        Task { await syncAll() }
    }

    func moveMonth(_ amount: Int) {
        selectedMonth = Calendar.current.date(byAdding: .month, value: amount, to: selectedMonth) ?? selectedMonth
        Task { await refreshSummary() }
    }

    func enqueue(image: UIImage) async {
        guard let data = image.jpegData(compressionQuality: 0.82) else { return }
        let id = UUID()
        let fileName = "\(id.uuidString.lowercased()).jpg"
        do {
            try data.write(to: rootDirectory.appendingPathComponent(fileName), options: .atomic)
            receipts.insert(PendingReceipt(id: id, capturedAt: Date(), imageFileName: fileName, status: .waiting), at: 0)
            persist()
            await syncAll()
        } catch {
            syncMessage = error.localizedDescription
        }
    }

    func syncAll() async {
        guard configured else {
            syncMessage = "请先设置电脑地址"
            return
        }
        syncMessage = "正在同步"
        for id in receipts.map(\.id) {
            await syncReceipt(id: id)
        }
        await refreshSummary()
        syncMessage = "已同步"
        AppDelegate.scheduleBackgroundRefresh()
    }

    private func syncReceipt(id: UUID) async {
        guard let index = receipts.firstIndex(where: { $0.id == id }) else { return }
        let current = receipts[index]
        do {
            let remote: RemoteReceipt
            if current.status == .waiting || current.status == .error {
                receipts[index].status = .uploading
                receipts[index].errorMessage = nil
                persist()
                let imageURL = rootDirectory.appendingPathComponent(current.imageFileName)
                let data = try Data(contentsOf: imageURL)
                remote = try await client.upload(receipt: current, imageData: data, settings: settings)
            } else {
                remote = try await client.receipt(id: id, settings: settings)
            }
            guard let refreshedIndex = receipts.firstIndex(where: { $0.id == id }) else { return }
            receipts[refreshedIndex].status = LocalReceiptStatus(rawValue: remote.status) ?? .queued
            receipts[refreshedIndex].merchant = remote.transaction?.merchant
            receipts[refreshedIndex].amountCents = remote.transaction?.amountCents
            receipts[refreshedIndex].errorMessage = remote.error
            if remote.status == "confirmed" {
                try? FileManager.default.removeItem(at: rootDirectory.appendingPathComponent(current.imageFileName))
            }
            persist()
        } catch SyncClient.SyncError.notFound {
            guard let refreshedIndex = receipts.firstIndex(where: { $0.id == id }) else { return }
            let imageURL = rootDirectory.appendingPathComponent(receipts[refreshedIndex].imageFileName)
            receipts.remove(at: refreshedIndex)
            try? FileManager.default.removeItem(at: imageURL)
            persist()
        } catch {
            guard let refreshedIndex = receipts.firstIndex(where: { $0.id == id }) else { return }
            receipts[refreshedIndex].status = .error
            receipts[refreshedIndex].errorMessage = error.localizedDescription
            persist()
        }
    }

    func refreshSummary() async {
        guard configured else { return }
        do {
            summary = try await client.summary(month: selectedMonthKey, settings: settings)
        } catch {
            syncMessage = error.localizedDescription
        }
    }

    func addManual(kind: String, date: Date, category: String, amount: Decimal, content: String, notes: String) async throws {
        let formatter = ISO8601DateFormatter()
        let rounding = NSDecimalNumberHandler(
            roundingMode: .plain, scale: 0, raiseOnExactness: false,
            raiseOnOverflow: false, raiseOnUnderflow: false, raiseOnDivideByZero: false
        )
        let cents = NSDecimalNumber(decimal: amount * 100).rounding(accordingToBehavior: rounding).intValue
        let request = ManualTransactionRequest(
            occurredAt: formatter.string(from: date), kind: kind, category: category,
            amountCents: cents, content: content.isEmpty ? category : content, notes: notes
        )
        try await client.addTransaction(request, settings: settings)
        selectedMonth = date
        await refreshSummary()
    }

    private func persist() {
        guard let data = try? encoder.encode(receipts) else { return }
        try? data.write(to: queueFile, options: .atomic)
    }
}
