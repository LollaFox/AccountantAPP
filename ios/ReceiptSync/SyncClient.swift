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

        var errorDescription: String? {
            switch self {
            case .invalidServer: "电脑地址无效"
            case .badResponse: "电脑返回了无法读取的数据"
            case .notFound: "电脑上已没有这张小票"
            case .server(let message): message
            }
        }
    }

    private func request(
        settings: AppSettings,
        path: String,
        method: String = "GET",
        body: Data? = nil
    ) throws -> URLRequest {
        let base = PairingInput.normalizedURL(settings.serverURL).trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard PairingInput.isReadyToSync(
            serverURL: settings.serverURL,
            syncToken: settings.syncToken,
            certificateSHA256: settings.certificateSHA256
        ), let url = URL(string: base + path), url.scheme == "https" else {
            throw SyncError.invalidServer
        }
        var request = URLRequest(url: url, timeoutInterval: 45)
        request.httpMethod = method
        request.httpBody = body
        request.setValue(settings.syncToken, forHTTPHeaderField: "X-Sync-Token")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        return request
    }

    private func perform<T: Decodable>(_ request: URLRequest, settings: AppSettings, as type: T.Type) async throws -> T {
        let delegate = PinnedSessionDelegate(expectedFingerprint: settings.certificateSHA256)
        let session = URLSession(configuration: .ephemeral, delegate: delegate, delegateQueue: nil)
        defer { session.finishTasksAndInvalidate() }
        let (data, response) = try await session.data(for: request)
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
        return try await perform(try request(settings: settings, path: "/api/v1/receipts", method: "POST", body: body), settings: settings, as: RemoteReceipt.self)
    }

    func receipt(id: UUID, settings: AppSettings) async throws -> RemoteReceipt {
        try await perform(try request(settings: settings, path: "/api/v1/receipts/\(id.uuidString.lowercased())"), settings: settings, as: RemoteReceipt.self)
    }

    func summary(month: String, settings: AppSettings) async throws -> MonthlySummary {
        try await perform(try request(settings: settings, path: "/api/v1/summary?month=\(month)"), settings: settings, as: MonthlySummary.self)
    }

    func addTransaction(_ transaction: ManualTransactionRequest, settings: AppSettings) async throws {
        let encoder = JSONEncoder()
        let request = try request(settings: settings, path: "/api/v1/transactions", method: "POST", body: encoder.encode(transaction))
        let _: RemoteManualTransaction = try await perform(request, settings: settings, as: RemoteManualTransaction.self)
    }
}

private struct RemoteManualTransaction: Decodable {
    let id: String
}
