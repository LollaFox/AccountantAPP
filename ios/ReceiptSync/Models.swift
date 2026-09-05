import Foundation

enum PairingInput {
    static func normalizedURL(_ raw: String) -> String {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let first = trimmed.split(whereSeparator: { $0.isWhitespace }).first else { return "" }
        var value = String(first)
        if !value.contains("://") {
            value = "https://" + value
        }
        return value
    }

    static func normalizedToken(_ raw: String) -> String {
        raw.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    static func normalizedFingerprint(_ raw: String) -> String {
        let hex = raw.uppercased().filter(\.isHexDigit)
        return hex.count > 64 ? String(hex.suffix(64)) : hex
    }

    static func isReadyToSync(serverURL: String, syncToken: String, certificateSHA256: String) -> Bool {
        let url = normalizedURL(serverURL)
        let token = normalizedToken(syncToken)
        let fingerprint = normalizedFingerprint(certificateSHA256)
        guard let parsed = URL(string: url), parsed.scheme?.lowercased() == "https", parsed.host != nil else {
            return false
        }
        return !token.isEmpty && fingerprint.count == 64
    }

    static func rememberedDraft(
        serverURL: String,
        syncToken: String,
        certificateSHA256: String,
        storedURL: String,
        storedToken: String,
        storedCert: String
    ) -> (serverURL: String, syncToken: String, certificateSHA256: String) {
        let url = normalizedURL(serverURL)
        let token = normalizedToken(syncToken)
        let cert = normalizedFingerprint(certificateSHA256)
        return (
            serverURL: url.isEmpty || url == "https://" ? storedURL : url,
            syncToken: token.isEmpty ? storedToken : token,
            certificateSHA256: cert.isEmpty ? storedCert : cert
        )
    }

    static func validationMessage(serverURL: String, syncToken: String, certificateSHA256: String) -> String? {
        guard isReadyToSync(serverURL: serverURL, syncToken: syncToken, certificateSHA256: certificateSHA256) else {
            if !normalizedURL(serverURL).lowercased().hasPrefix("https://") {
                return "电脑地址必须以 https:// 开头"
            }
            if normalizedToken(syncToken).isEmpty {
                return "请填写同步密钥"
            }
            if normalizedFingerprint(certificateSHA256).count != 64 {
                return "证书指纹应为 64 位十六进制"
            }
            return "配对信息不完整，暂不同步"
        }
        return nil
    }
}

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
            certificateSHA256: KeychainStore.read(account: "certificateSHA256")
                ?? defaults.string(forKey: "certificateSHA256")
                ?? "",
            deviceID: deviceID
        )
    }

    func save() {
        UserDefaults.standard.set(serverURL, forKey: "serverURL")
        UserDefaults.standard.set(certificateSHA256, forKey: "certificateSHA256")
        UserDefaults.standard.set(deviceID, forKey: "deviceID")
        KeychainStore.write(syncToken, account: "syncToken")
        KeychainStore.write(certificateSHA256, account: "certificateSHA256")
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
