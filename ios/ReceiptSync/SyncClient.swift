import CryptoKit
import Foundation
import Security

private final class PinnedSessionDelegate: NSObject, URLSessionDelegate {
    private let expectedFingerprint: String

    init(expectedFingerprint: String) {
        self.expectedFingerprint = expectedFingerprint
            .replacingOccurrences(of: ":", with: "")
            .replacingOccurrences(of: " ", with: "")
            .uppercased()
    }

    func urlSession(
        _ session: URLSession,
        didReceive challenge: URLAuthenticationChallenge,
        completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void
    ) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              let trust = challenge.protectionSpace.serverTrust,
              let chain = SecTrustCopyCertificateChain(trust) as? [SecCertificate],
              let certificate = chain.first else {
            completionHandler(.performDefaultHandling, nil)
            return
        }
        let certificateData = SecCertificateCopyData(certificate) as Data
        let fingerprint = SHA256.hash(data: certificateData).map { String(format: "%02X", $0) }.joined()
        if fingerprint == expectedFingerprint {
            completionHandler(.useCredential, URLCredential(trust: trust))
        } else {
            completionHandler(.cancelAuthenticationChallenge, nil)
        }
    }
}

actor SyncClient {
    enum SyncError: LocalizedError {
        case invalidServer
        case badResponse
        case notFound
        case server(String)
        case unreachable
        case certificateRejected

        var errorDescription: String? {
            switch self {
            case .invalidServer: "电脑地址无效"
            case .badResponse: "电脑返回了无法读取的数据"
            case .notFound: "电脑上已没有这张小票"
            case .server(let message): message
            case .unreachable: "无法连接电脑。热点时请填写配对页里 172.20.10.x 的地址，并在 Windows 防火墙放行 TCP 8765。"
            case .certificateRejected: "证书不匹配。请从当前电脑的配对页重新复制证书指纹。"
            }
        }
    }

    private func request(
        settings: AppSettings,
        path: String,
        method: String = "GET",
        body: Data? = nil,
        base: String
    ) throws -> URLRequest {
        let trimmed = base.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard PairingInput.isReadyToSync(
            serverURL: settings.serverURL,
            syncToken: settings.syncToken,
            certificateSHA256: settings.certificateSHA256
        ), let url = URL(string: trimmed + path), url.scheme == "https" else {
            throw SyncError.invalidServer
        }
        var request = URLRequest(url: url, timeoutInterval: 12)
        request.httpMethod = method
        request.httpBody = body
        request.setValue(settings.syncToken, forHTTPHeaderField: "X-Sync-Token")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        return request
    }

    private func perform<T: Decodable>(
        settings: AppSettings,
        path: String,
        method: String = "GET",
        body: Data? = nil,
        as type: T.Type
    ) async throws -> T {
        let bases = PairingInput.preferredSyncURLs(settings.serverURL)
        guard !bases.isEmpty else { throw SyncError.invalidServer }
        var lastError: Error = SyncError.invalidServer
        for base in bases {
            do {
                return try await performOnce(try request(settings: settings, path: path, method: method, body: body, base: base), settings: settings, as: type)
            } catch {
                let wrapped = NetworkSyncMessage.wrap(error)
                if case .unreachable = wrapped {
                    lastError = wrapped
                    continue
                }
                throw wrapped
            }
        }
        throw lastError
    }

    private func performOnce<T: Decodable>(_ request: URLRequest, settings: AppSettings, as type: T.Type) async throws -> T {
        let delegate = PinnedSessionDelegate(expectedFingerprint: settings.certificateSHA256)
        let session = URLSession(configuration: .ephemeral, delegate: delegate, delegateQueue: nil)
        defer { session.finishTasksAndInvalidate() }
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            throw NetworkSyncMessage.wrap(error)
        }
        guard let http = response as? HTTPURLResponse else { throw SyncError.badResponse }
        if http.statusCode == 404 { throw SyncError.notFound }
        guard (200..<300).contains(http.statusCode) else {
            let message = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["error"] as? String
            throw SyncError.server(message ?? "电脑同步失败（\(http.statusCode)）")
        }
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(type, from: data)
    }

    func upload(receipt: PendingReceipt, imageData: Data, settings: AppSettings) async throws -> RemoteReceipt {
        let payload: [String: Any] = [
            "id": receipt.id.uuidString.lowercased(),
            "device_id": settings.deviceID,
            "captured_at": HongKongDate.isoString(from: receipt.capturedAt),
            "image_base64": imageData.base64EncodedString()
        ]
        let body = try JSONSerialization.data(withJSONObject: payload)
        return try await perform(settings: settings, path: "/api/v1/receipts", method: "POST", body: body, as: RemoteReceipt.self)
    }

    func receipt(id: UUID, settings: AppSettings) async throws -> RemoteReceipt {
        try await perform(settings: settings, path: "/api/v1/receipts/\(id.uuidString.lowercased())", as: RemoteReceipt.self)
    }

    func summary(month: String, settings: AppSettings) async throws -> MonthlySummary {
        try await perform(settings: settings, path: "/api/v1/summary?month=\(month)", as: MonthlySummary.self)
    }

    func addTransaction(_ transaction: ManualTransactionRequest, settings: AppSettings) async throws {
        let encoder = JSONEncoder()
        let _: RemoteManualTransaction = try await perform(
            settings: settings,
            path: "/api/v1/transactions",
            method: "POST",
            body: encoder.encode(transaction),
            as: RemoteManualTransaction.self
        )
    }
}

enum NetworkSyncMessage {
    static func wrap(_ error: Error) -> SyncClient.SyncError {
        if let sync = error as? SyncClient.SyncError {
            return sync
        }
        if let urlError = error as? URLError {
            return from(urlError.code)
        }
        let nsError = error as NSError
        if nsError.domain == NSURLErrorDomain {
            return from(URLError.Code(rawValue: nsError.code))
        }
        return .unreachable
    }

    static func from(_ code: URLError.Code) -> SyncClient.SyncError {
        switch code {
        case .cancelled, .userCancelledAuthentication, .secureConnectionFailed,
             .serverCertificateUntrusted, .serverCertificateHasBadDate,
             .serverCertificateNotYetValid, .serverCertificateHasUnknownRoot,
             .clientCertificateRejected:
            return .certificateRejected
        default:
            return .unreachable
        }
    }

    static func display(_ error: Error) -> String {
        wrap(error).errorDescription ?? "电脑同步失败"
    }
}

private struct RemoteManualTransaction: Decodable {
    let id: String
}
