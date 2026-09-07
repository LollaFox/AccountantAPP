import CryptoKit
import Foundation
import Security

protocol SyncService {
    func redeemInvitation(_ draft: PendingPairing, settings: AppSettings) async throws
    func connect(settings: AppSettings) async throws -> VerifiedConnection
    func upload(receipt: PendingReceipt, imageData: Data, settings: AppSettings) async throws -> RemoteReceipt
    func receipt(id: UUID, settings: AppSettings) async throws -> RemoteReceipt
    func summary(month: String, settings: AppSettings) async throws -> MonthlySummary
    func addTransaction(_ transaction: ManualTransactionRequest, settings: AppSettings) async throws
    func disconnect() async
}

extension SyncService {
    func redeemInvitation(_ draft: PendingPairing, settings: AppSettings) async throws { throw SyncClient.SyncError.incompatibleServer }
}

final class PinnedSessionDelegate: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    private let expectedFingerprint: String
    private let lock = NSLock()
    private var rejectedTasks = Set<Int>()

    init(expectedFingerprint: String) {
        self.expectedFingerprint = PairingInput.normalizedFingerprint(expectedFingerprint)
    }

    func wasRejected(_ task: URLSessionTask) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        return rejectedTasks.remove(task.taskIdentifier) != nil
    }

    func accepts(certificateData: Data) -> Bool {
        let actual = SHA256.hash(data: certificateData).map { String(format: "%02X", $0) }.joined()
        return !expectedFingerprint.isEmpty && actual == expectedFingerprint
    }

    func urlSession(
        _ session: URLSession, task: URLSessionTask, didReceive challenge: URLAuthenticationChallenge,
        completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void
    ) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust else {
            completionHandler(.performDefaultHandling, nil)
            return
        }
        if let trust = challenge.protectionSpace.serverTrust,
           let chain = SecTrustCopyCertificateChain(trust) as? [SecCertificate], let certificate = chain.first,
           accepts(certificateData: SecCertificateCopyData(certificate) as Data) {
            completionHandler(.useCredential, URLCredential(trust: trust))
        } else {
            lock.lock()
            rejectedTasks.insert(task.taskIdentifier)
            lock.unlock()
            completionHandler(.cancelAuthenticationChallenge, nil)
        }
    }

    func urlSession(
        _ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void
    ) {
        // Never forward device credentials to a redirected origin.
        completionHandler(nil)
    }
}

actor SyncClient: SyncService {
    enum SyncError: LocalizedError, Equatable {
        case invalidServer, badResponse, notFound, deleted, unauthorized, forbidden, invalidEntry
        case unreachable, timedOut, offline, certificateRejected, tlsFailed, cancelled
        case incompatibleServer, serverChanged, conflict, rateLimited, serverUnavailable
        case server(Int)

        var errorDescription: String? {
            switch self {
            case .invalidServer: "配对信息无效，请检查电脑地址、设备密钥和证书指纹。"
            case .badResponse: "电脑返回了无法读取的数据，请更新电脑服务后重试。"
            case .invalidEntry: "此记录未被电脑接受，请检查内容和长度后修改重试。"
            case .notFound: "电脑上已没有这张小票"
            case .deleted: "这张小票已在电脑上删除"
            case .unauthorized: "设备密钥无效或已被撤销。请在电脑上为此设备创建新密钥。"
            case .forbidden: "此设备没有执行该操作的权限。"
            case .unreachable: "无法连接电脑。请确认服务正在运行、双方网络可互访，并使用配对页当前地址。检查电脑防火墙和 iPhone 的本地网络权限。"
            case .timedOut: "连接电脑超时。请检查当前地址和网络，稍后重试。"
            case .offline: "网络不可用，待发送数据已保留在本机。"
            case .certificateRejected: "证书不匹配，已拒绝连接。请核对当前电脑配对页的证书指纹。"
            case .tlsFailed: "无法建立安全连接。请确认使用的是电脑的 HTTPS 同步端口。"
            case .cancelled: "同步已暂停，待发送数据会在下次连接时重试。"
            case .incompatibleServer: "电脑服务版本不兼容，请先更新电脑端。"
            case .serverChanged: "此地址属于另一台服务器，已停止发送。请重新核对配对信息。"
            case .conflict: "请求编号与电脑上的记录冲突，请在电脑上核对。"
            case .rateLimited: "连接尝试过于频繁，请稍后重试。"
            case .serverUnavailable: "电脑服务暂时不可用，请稍后重试。"
            case .server(let status): "电脑请求失败（\(status)）"
            }
        }

        var retryable: Bool {
            switch self {
            case .unreachable, .timedOut, .offline, .serverUnavailable, .rateLimited: true
            default: false
            }
        }

        var canTryNextAddress: Bool {
            self == .unreachable || self == .timedOut || self == .offline
        }
    }

    private let configuration: URLSessionConfiguration
    private var session: URLSession?
    private var sessionDelegate: PinnedSessionDelegate?
    private var sessionFingerprint: String?
    private var preferredURL: String?
    private var preferredSettings: AppSettings?

    init(configuration: URLSessionConfiguration = .ephemeral) {
        self.configuration = configuration
    }

    func disconnect() {
        session?.invalidateAndCancel()
        session = nil
        sessionDelegate = nil
        sessionFingerprint = nil
        preferredURL = nil
        preferredSettings = nil
    }

    private func transport(settings: AppSettings) -> (URLSession, PinnedSessionDelegate) {
        let fingerprint = PairingInput.normalizedFingerprint(settings.certificateSHA256)
        if sessionFingerprint != fingerprint { disconnect() }
        if let session, let sessionDelegate { return (session, sessionDelegate) }
        let delegate = PinnedSessionDelegate(expectedFingerprint: fingerprint)
        let config = configuration.copy() as! URLSessionConfiguration
        config.waitsForConnectivity = false
        config.timeoutIntervalForRequest = 20
        config.timeoutIntervalForResource = 90
        config.httpShouldSetCookies = false
        config.urlCache = nil
        config.urlCredentialStorage = nil
        let created = URLSession(configuration: config, delegate: delegate, delegateQueue: nil)
        session = created
        sessionDelegate = delegate
        sessionFingerprint = fingerprint
        return (created, delegate)
    }

    private func perform<T: Decodable>(
        settings: AppSettings, path: String, method: String = "GET", body: Data? = nil,
        timeout: TimeInterval = 20, as type: T.Type
    ) async throws -> (T, String) {
        guard PairingInput.isReadyToSync(serverURL: settings.serverURL, syncToken: settings.syncToken,
                                        certificateSHA256: settings.certificateSHA256) else { throw SyncError.invalidServer }
        var bases = PairingInput.preferredSyncURLs(settings.serverURL)
        let preferred = preferredSettings == settings ? preferredURL : settings.lastSuccessfulURL
        if let preferred, let index = bases.firstIndex(of: preferred) {
            bases.remove(at: index)
            bases.insert(preferred, at: 0)
        }
        let (session, delegate) = transport(settings: settings)
        var lastError = SyncError.unreachable
        for base in bases {
            try Task.checkCancellation()
            guard let url = URL(string: base + path) else { throw SyncError.invalidServer }
            var request = URLRequest(url: url, timeoutInterval: timeout)
            request.httpMethod = method
            request.httpBody = body
            request.setValue(PairingInput.normalizedToken(settings.syncToken), forHTTPHeaderField: "X-Sync-Token")
            if let id = settings.serverID { request.setValue(id, forHTTPHeaderField: "X-Server-ID") }
            request.setValue("application/json", forHTTPHeaderField: "Accept")
            if body != nil { request.setValue("application/json", forHTTPHeaderField: "Content-Type") }
            do {
                let (data, response) = try await data(for: request, session: session, delegate: delegate)
                guard let http = response as? HTTPURLResponse else { throw SyncError.badResponse }
                switch http.statusCode {
                case 200..<300: break
                case 401: throw SyncError.unauthorized
                case 400, 413, 422: throw SyncError.invalidEntry
                case 403: throw SyncError.forbidden
                case 404: throw SyncError.notFound
                case 410: throw SyncError.deleted
                case 409:
                    let payload = try? JSONSerialization.jsonObject(with: data) as? [String: String]
                    throw payload?["code"] == "server_changed" ? SyncError.serverChanged : SyncError.conflict
                case 429: throw SyncError.rateLimited
                case 500..<600: throw SyncError.serverUnavailable
                case 300..<400: throw SyncError.badResponse
                default: throw SyncError.server(http.statusCode)
                }
                let decoder = JSONDecoder()
                decoder.keyDecodingStrategy = .convertFromSnakeCase
                guard let decoded = try? decoder.decode(type, from: data) else { throw SyncError.badResponse }
                preferredURL = base
                preferredSettings = settings
                return (decoded, base)
            } catch {
                let wrapped = NetworkSyncMessage.wrap(error)
                guard wrapped.canTryNextAddress else { throw wrapped }
                lastError = wrapped
            }
        }
        throw lastError
    }

    private func data(for request: URLRequest, session: URLSession, delegate: PinnedSessionDelegate) async throws -> (Data, URLResponse) {
        // An actual pin mismatch is distinguished from cancellation of a sync task.
        let holder = RequestTask()
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                let task = session.dataTask(with: request) { data, response, error in
                    if let task = holder.task, delegate.wasRejected(task) {
                        continuation.resume(throwing: SyncError.certificateRejected)
                    } else if let error {
                        continuation.resume(throwing: error)
                    } else if let data, let response {
                        continuation.resume(returning: (data, response))
                    } else {
                        continuation.resume(throwing: SyncError.badResponse)
                    }
                    holder.finish()
                }
                holder.start(task)
            }
        } onCancel: { holder.cancel() }
    }

    func connect(settings: AppSettings) async throws -> VerifiedConnection {
        do {
            let (info, base) = try await perform(settings: settings, path: "/api/v1/connection", timeout: 8, as: ServerConnection.self)
            guard info.service == "receipt-sync", info.protocolVersion == 2,
                  UUID(uuidString: info.serverId) != nil,
                  ["contributor", "viewer"].contains(info.device.role) else { throw SyncError.incompatibleServer }
            if let expected = settings.serverID, expected != info.serverId { throw SyncError.serverChanged }
            return VerifiedConnection(info: info, baseURL: base)
        } catch SyncError.notFound { throw SyncError.incompatibleServer }
    }

    func redeemInvitation(_ draft: PendingPairing, settings: AppSettings) async throws {
        let body = try JSONSerialization.data(withJSONObject: [
            "invitation_token": draft.invitation.invitationToken, "device_token": draft.deviceToken,
            "claim_id": draft.claimID, "server_id": draft.invitation.serverID,
        ])
        let (result, _) = try await perform(settings: settings, path: "/api/v1/pairing/redeem", method: "POST",
                                            body: body, as: PairingRedemption.self)
        guard result.serverId == draft.invitation.serverID, UUID(uuidString: result.deviceId) != nil else {
            throw SyncError.serverChanged
        }
    }

    func upload(receipt: PendingReceipt, imageData: Data, settings: AppSettings) async throws -> RemoteReceipt {
        let payload: [String: Any] = [
            "id": receipt.id.uuidString.lowercased(), "device_id": settings.deviceID,
            "captured_at": HongKongDate.isoString(from: receipt.capturedAt),
            "image_base64": imageData.base64EncodedString()
        ]
        let (remote, _) = try await perform(settings: settings, path: "/api/v1/receipts", method: "POST",
                                           body: JSONSerialization.data(withJSONObject: payload), timeout: 60, as: RemoteReceipt.self)
        guard remote.id.lowercased() == receipt.id.uuidString.lowercased(), LocalReceiptStatus(rawValue: remote.status) != nil else {
            throw SyncError.badResponse
        }
        return remote
    }

    func receipt(id: UUID, settings: AppSettings) async throws -> RemoteReceipt {
        let (remote, _) = try await perform(settings: settings, path: "/api/v1/receipts/\(id.uuidString.lowercased())", as: RemoteReceipt.self)
        guard remote.id.lowercased() == id.uuidString.lowercased(), LocalReceiptStatus(rawValue: remote.status) != nil else {
            throw SyncError.badResponse
        }
        return remote
    }

    func summary(month: String, settings: AppSettings) async throws -> MonthlySummary {
        guard month.range(of: #"^\d{4}-(0[1-9]|1[0-2])$"#, options: .regularExpression) != nil else { throw SyncError.badResponse }
        let (summary, _) = try await perform(settings: settings, path: "/api/v1/summary?month=\(month)", as: MonthlySummary.self)
        guard summary.month == month, summary.currency == "HKD" else { throw SyncError.badResponse }
        return summary
    }

    func addTransaction(_ transaction: ManualTransactionRequest, settings: AppSettings) async throws {
        let (remote, _) = try await perform(settings: settings, path: "/api/v1/transactions", method: "POST",
                                           body: JSONEncoder().encode(transaction), as: RemoteManualTransaction.self)
        guard remote.id == "manual-\(transaction.requestID)" else { throw SyncError.badResponse }
    }
}

private final class RequestTask: @unchecked Sendable {
    private let lock = NSLock()
    private var storedTask: URLSessionDataTask?
    private var cancelled = false
    var task: URLSessionDataTask? { lock.lock(); defer { lock.unlock() }; return storedTask }
    func start(_ task: URLSessionDataTask) {
        lock.lock()
        storedTask = task
        let cancel = cancelled
        lock.unlock()
        task.resume()
        if cancel { task.cancel() }
    }
    func cancel() {
        lock.lock()
        cancelled = true
        let task = storedTask
        lock.unlock()
        task?.cancel()
    }
    func finish() { lock.lock(); storedTask = nil; lock.unlock() }
}

enum NetworkSyncMessage {
    static func wrap(_ error: Error) -> SyncClient.SyncError {
        if let sync = error as? SyncClient.SyncError { return sync }
        if error is CancellationError { return .cancelled }
        let nsError = error as NSError
        if nsError.domain == NSURLErrorDomain { return from(URLError.Code(rawValue: nsError.code)) }
        return .badResponse
    }

    static func from(_ code: URLError.Code) -> SyncClient.SyncError {
        switch code {
        case .cancelled: .cancelled
        case .userCancelledAuthentication, .serverCertificateUntrusted, .serverCertificateHasBadDate,
             .serverCertificateNotYetValid, .serverCertificateHasUnknownRoot, .clientCertificateRejected: .certificateRejected
        case .secureConnectionFailed: .tlsFailed
        case .timedOut: .timedOut
        case .notConnectedToInternet: .offline
        case .cannotFindHost, .cannotConnectToHost, .dnsLookupFailed, .networkConnectionLost: .unreachable
        default: .badResponse
        }
    }

    static func display(_ error: Error) -> String {
        if (error as NSError).domain == NSURLErrorDomain || error is CancellationError {
            return wrap(error).errorDescription ?? "电脑同步失败"
        }
        return (error as? LocalizedError)?.errorDescription ?? wrap(error).errorDescription ?? "电脑同步失败"
    }
}

private struct RemoteManualTransaction: Decodable { let id: String }
private struct PairingRedemption: Decodable { let serverId: String; let deviceId: String }
