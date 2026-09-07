import Foundation
import Security

enum PairingInput {
    static func candidateURLs(_ raw: String) -> [String] {
        let entries = raw.split(whereSeparator: \.isWhitespace)
        guard !entries.isEmpty, entries.count <= 6 else { return [] }
        var result: [String] = []
        for entry in entries {
            let value = entry.contains("://") ? String(entry) : "https://" + entry
            guard var parts = URLComponents(string: value), parts.scheme?.lowercased() == "https",
                  let host = parts.host, !host.isEmpty, !host.contains("%"),
                  parts.user == nil, parts.password == nil, parts.query == nil, parts.fragment == nil,
                  parts.path.isEmpty || parts.path == "/",
                  (1...65535).contains(parts.port ?? 8765) else { return [] }
            parts.scheme = "https"
            parts.host = host.lowercased()
            parts.port = parts.port ?? 8765
            parts.path = ""
            guard let url = parts.url?.absoluteString else { return [] }
            if !result.contains(url) { result.append(url) }
        }
        return result
    }

    static func preferredSyncURLs(_ raw: String) -> [String] {
        candidateURLs(raw).sorted { lhs, rhs in
            hotspotRank(lhs) < hotspotRank(rhs)
        }
    }

    static func normalizedURL(_ raw: String) -> String {
        preferredSyncURLs(raw).joined(separator: " ")
    }

    private static func hotspotRank(_ url: String) -> Int {
        let host = URL(string: url)?.host ?? ""
        if host.hasPrefix("172.20.10.") || host.hasPrefix("172.20.11.") {
            return 0
        }
        return 1
    }

    static func normalizedToken(_ raw: String) -> String {
        raw.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    static func normalizedFingerprint(_ raw: String) -> String {
        var value = raw.trimmingCharacters(in: .whitespacesAndNewlines).uppercased()
        for prefix in ["SHA256 FINGERPRINT=", "SHA-256", "SHA256"] where value.hasPrefix(prefix) {
            value.removeFirst(prefix.count)
            break
        }
        let hex = value.filter { !$0.isWhitespace && $0 != ":" }
        guard hex.count == 64, hex.utf8.allSatisfy({ (48...57).contains($0) || (65...70).contains($0) }) else { return "" }
        return hex
    }

    static func isReadyToSync(serverURL: String, syncToken: String, certificateSHA256: String) -> Bool {
        let token = normalizedToken(syncToken)
        let fingerprint = normalizedFingerprint(certificateSHA256)
        return !candidateURLs(serverURL).isEmpty && validToken(token) && fingerprint.count == 64
    }

    private static func validToken(_ token: String) -> Bool {
        !token.isEmpty && token.utf8.count <= 512 && token.utf8.allSatisfy { (33...126).contains($0) }
    }

    static func validationMessage(serverURL: String, syncToken: String, certificateSHA256: String) -> String? {
        guard isReadyToSync(serverURL: serverURL, syncToken: syncToken, certificateSHA256: certificateSHA256) else {
            if candidateURLs(serverURL).isEmpty {
                return "请填写完整 HTTPS 地址（端口默认 8765），不要包含网页路径。多个地址用空格分隔。"
            }
            if normalizedToken(syncToken).isEmpty {
                return "请填写同步密钥"
            }
            if !validToken(normalizedToken(syncToken)) { return "同步密钥格式无效，请重新复制该设备的密钥" }
            if normalizedFingerprint(certificateSHA256).count != 64 {
                return "证书指纹应为 64 位十六进制"
            }
            return "配对信息不完整，暂不同步"
        }
        return nil
    }
}

struct AppSettings: Codable, Equatable {
    var serverURL: String
    var syncToken: String
    var certificateSHA256: String
    var deviceID: String
    var serverID: String? = nil
    var lastSuccessfulURL: String? = nil
    var role: String? = nil

    static func load() -> AppSettings {
        let defaults = UserDefaults.standard
        if let json = KeychainStore.read(account: "pairing"), let data = json.data(using: .utf8),
           let saved = try? JSONDecoder().decode(AppSettings.self, from: data) { return saved }
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

    func save() throws {
        let data = try JSONEncoder().encode(self)
        // One Keychain update commits the address, identity and credentials together.
        try KeychainStore.write(String(decoding: data, as: UTF8.self), account: "pairing")
        UserDefaults.standard.removeObject(forKey: "serverURL")
        UserDefaults.standard.removeObject(forKey: "certificateSHA256")
        try? KeychainStore.write("", account: "syncToken")
        try? KeychainStore.write("", account: "certificateSHA256")
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
    var serverID: String? = nil
    var serverFingerprint: String? = nil
    var uploaded: Bool? = nil
    var blockedReason: String? = nil
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

struct ManualTransactionRequest: Codable, Equatable {
    var requestID: String = UUID().uuidString.lowercased()
    let occurredAt: String
    let kind: String
    let category: String
    let amountCents: Int
    let content: String
    let notes: String

    enum CodingKeys: String, CodingKey {
        case requestID = "request_id"
        case occurredAt = "occurred_at"
        case kind, category
        case amountCents = "amount_cents"
        case content, notes
    }
}

struct PendingManual: Codable, Identifiable, Equatable {
    var id: String { transaction.requestID }
    let transaction: ManualTransactionRequest
    let serverID: String
    var errorMessage: String? = nil
    var blockedReason: String? = nil
}

struct ServerConnection: Decodable, Equatable {
    let service: String
    let protocolVersion: Int
    let serverId: String
    let device: ConnectedDevice
}

struct ConnectedDevice: Decodable, Equatable {
    let id: String
    let name: String
    let role: String
}

struct VerifiedConnection: Equatable {
    let info: ServerConnection
    let baseURL: String
}

struct PairingInvitation: Codable, Equatable {
    let version: Int
    let serverID: String
    let addresses: [String]
    let certificateSHA256: String
    let invitationToken: String
    let expiresAt: Double

    enum CodingKeys: String, CodingKey {
        case version, addresses
        case serverID = "server_id", certificateSHA256 = "certificate_sha256"
        case invitationToken = "invitation_token", expiresAt = "expires_at"
    }

    static func parse(_ text: String) throws -> PairingInvitation {
        guard text.utf8.count <= 4096, let url = URLComponents(string: text.trimmingCharacters(in: .whitespacesAndNewlines)),
              url.scheme == "receiptsync", url.host == "pair", url.user == nil, url.password == nil,
              url.port == nil, url.path.isEmpty, url.fragment == nil, url.queryItems?.count == 1,
              url.queryItems?.first?.name == "data", let encoded = url.queryItems?.first?.value else {
            throw SyncClient.SyncError.invalidServer
        }
        let base64 = encoded.replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/")
        guard let data = Data(base64Encoded: base64.padding(toLength: ((base64.count + 3) / 4) * 4, withPad: "=", startingAt: 0)),
              let invitation = try? JSONDecoder().decode(Self.self, from: data), invitation.version == 1,
              UUID(uuidString: invitation.serverID) != nil,
              !invitation.addresses.isEmpty, invitation.addresses.count <= 6,
              invitation.addresses.allSatisfy({ PairingInput.candidateURLs($0).count == 1 && $0.hasPrefix("https://") }),
              PairingInput.normalizedFingerprint(invitation.certificateSHA256).count == 64,
              invitation.invitationToken.range(of: #"^[A-Za-z0-9_-]{43}$"#, options: .regularExpression) != nil,
              invitation.expiresAt.isFinite else { throw SyncClient.SyncError.invalidServer }
        return invitation
    }
}

struct PendingPairing: Codable, Equatable {
    let invitation: PairingInvitation
    let claimID: String
    let deviceToken: String

    static func create(_ invitation: PairingInvitation) throws -> PendingPairing {
        var bytes = [UInt8](repeating: 0, count: 32)
        guard SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes) == errSecSuccess else {
            throw KeychainStore.StorageError.unavailable
        }
        let token = Data(bytes).base64EncodedString().replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: "=", with: "")
        return PendingPairing(invitation: invitation, claimID: UUID().uuidString.lowercased(), deviceToken: token)
    }
    static func load() -> PendingPairing? {
        guard let json = KeychainStore.read(account: "pendingPairing"), let data = json.data(using: .utf8) else { return nil }
        return try? JSONDecoder().decode(Self.self, from: data)
    }
    static func save(_ pending: PendingPairing?) throws {
        try KeychainStore.write(try pending.map { String(decoding: try JSONEncoder().encode($0), as: UTF8.self) } ?? "", account: "pendingPairing")
    }
}

enum ConnectionState: Equatable {
    case unconfigured, idle, connecting, syncing, connected
    case failed(String)

    var isFailure: Bool {
        if case .failed = self { return true }
        return false
    }
}
