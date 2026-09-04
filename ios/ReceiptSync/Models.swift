import Foundation

struct AppSettings: Equatable {
    var serverURL: String
    var syncToken: String
    var certificateSHA256: String
    var deviceID: String

    static func load() -> AppSettings {
        let defaults = UserDefaults.standard
        var deviceID = defaults.string(forKey: "deviceID") ?? ""
        if deviceID.isEmpty {
            deviceID = "iphone-\(UUID().uuidString.lowercased())"
            defaults.set(deviceID, forKey: "deviceID")
        }
        return AppSettings(
            serverURL: defaults.string(forKey: "serverURL") ?? "",
            syncToken: KeychainStore.read(account: "syncToken") ?? "",
            certificateSHA256: defaults.string(forKey: "certificateSHA256") ?? "",
            deviceID: deviceID
        )
    }

    func save() {
        UserDefaults.standard.set(serverURL, forKey: "serverURL")
        UserDefaults.standard.set(certificateSHA256, forKey: "certificateSHA256")
        UserDefaults.standard.set(deviceID, forKey: "deviceID")
        KeychainStore.write(syncToken, account: "syncToken")
    }
}

enum LocalReceiptStatus: String, Codable {
    case waiting
    case uploading
    case queued
    case processing
    case review
    case confirmed
    case error

    var label: String {
        switch self {
        case .waiting: "待同步"
        case .uploading: "上传中"
        case .queued: "等待识别"
        case .processing: "识别中"
        case .review: "待电脑审核"
        case .confirmed: "已计入汇总"
        case .error: "同步失败"
        }
    }
}

struct PendingReceipt: Identifiable, Codable, Equatable {
    let id: UUID
    let capturedAt: Date
    let imageFileName: String
    var status: LocalReceiptStatus
    var merchant: String?
    var amountCents: Int?
    var errorMessage: String?
}

struct CategoryTotal: Codable, Identifiable, Equatable {
    var id: String { "\(kind)-\(category)" }
    let kind: String
    let category: String
    let amountCents: Int
    let transactionCount: Int
}

struct MonthlySummary: Codable, Equatable {
    let month: String
    let currency: String
    let incomeCents: Int
    let expenseCents: Int
    let balanceCents: Int
    let incomeByCategory: [CategoryTotal]
    let expenseByCategory: [CategoryTotal]
    let pendingReceipts: Int
    let updatedAt: String?

    static let empty = MonthlySummary(
        month: "", currency: "HKD", incomeCents: 0, expenseCents: 0, balanceCents: 0,
        incomeByCategory: [], expenseByCategory: [], pendingReceipts: 0, updatedAt: nil
    )
}

struct RemoteTransaction: Codable {
    let merchant: String?
    let amountCents: Int?
}

struct RemoteReceipt: Codable {
    let id: String
    let status: String
    let error: String?
    let transaction: RemoteTransaction?
}

struct ManualTransactionRequest: Encodable {
    let occurredAt: String
    let kind: String
    let category: String
    let amountCents: Int
    let content: String
    let notes: String

    enum CodingKeys: String, CodingKey {
        case occurredAt = "occurred_at"
        case kind, category
        case amountCents = "amount_cents"
        case content, notes
    }
}
