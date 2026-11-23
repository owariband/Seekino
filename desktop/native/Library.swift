import Foundation
import Security

struct DesktopError: LocalizedError {
    let message: String
    var errorDescription: String? { message }

    init(_ message: String) { self.message = message }
}

func bridgeError(_ message: UnsafeMutablePointer<CChar>?, fallback: String) -> DesktopError {
    guard let message else { return DesktopError(fallback) }
    defer { free(message) }
    return DesktopError(String(cString: message))
}

private struct Reply<Value: Decodable>: Decodable {
    let value: Value

    private enum Key: String, CodingKey { case value, error }

    init(from decoder: Decoder) throws {
        let fields = try decoder.container(keyedBy: Key.self)
        if let error = try fields.decodeIfPresent(String.self, forKey: .error) {
            throw DesktopError(error)
        }
        value = try fields.decode(Value.self, forKey: .value)
    }
}

final class LibraryClient {
    private let worker = DispatchQueue(label: "app.seekino.library", qos: .userInitiated)
    private let transfer = DispatchQueue(label: "app.seekino.import", qos: .userInitiated)

    func request<Value: Decodable>(_ action: String, _ args: [String: Any] = [:]) async throws -> Value {
        let request = try JSONSerialization.data(withJSONObject: ["action": action, "args": args])
        let queue = action == "import" ? transfer : worker
        return try await withCheckedThrowingContinuation { continuation in
            queue.async {
                continuation.resume(with: Result {
                    guard let resources = Bundle.main.resourceURL else {
                        throw DesktopError("应用包缺少运行资源")
                    }
                    var error: UnsafeMutablePointer<CChar>?
                    guard seekino_initialize(resources.path, &error) else {
                        throw bridgeError(error, fallback: "无法打开内置运行时")
                    }
                    let input = String(decoding: request, as: UTF8.self)
                    guard let response = seekino_call(input, &error) else {
                        throw bridgeError(error, fallback: "资料库没有返回结果")
                    }
                    defer { free(response) }
                    let data = Data(bytes: response, count: strlen(response))
                    return try JSONDecoder().decode(Reply<Value>.self, from: data).value
                })
            }
        }
    }

    func command(_ action: String, _ args: [String: Any] = [:]) async throws {
        let _: Bool? = try await request(action, args)
    }
}

struct SeriesItem: Codable, Identifiable, Equatable {
    let id: String
    let title: String
    let count: Int
    let ready: Int
}

struct VideoItem: Decodable, Identifiable, Equatable {
    let id: String
    let episode: Int
    let title: String
    let durationMs: Int64
    let status: String
    let indexStatus: String
    let progress: Double?
    let cover: String?

    var isReady: Bool { indexStatus == "READY" || indexStatus == "INDEXED" }
    var failed: Bool { status.hasSuffix("FAILED") || indexStatus.hasSuffix("FAILED") }
}

struct JobItem: Decodable, Identifiable, Equatable {
    let id: String
    let series: String
    let videoID: String
    let title: String
    let episode: Int
    let status: String
    let progress: Double?
    let elapsedMs: Int64
    let latest: Bool
    let paused: Bool
    let canPause: Bool
    let canRetry: Bool
    let uploadID: String?
    let version: Int
    let leaseVersion: Int
    let error: String?

    var active: Bool { ["PENDING", "PREPARING", "PARSING", "INDEXING"].contains(status) }
    var label: String {
        if paused { return "已暂停" }
        switch status {
        case "PENDING": return "等待解析"
        case "PREPARING": return "正在准备"
        case "PARSING": return "正在解析"
        case "INDEXING": return "正在建立索引"
        case "COMPLETE": return "可搜索"
        case "INDEXING_FAILED": return "索引失败"
        case "PARSING_FAILED": return "解析失败"
        default: return status
        }
    }
}

struct ImportResult: Decodable { let videoID: String }

struct SearchResponse: Decodable {
    let hits: [SearchHit]
    let videos: [VideoItem]
    let partial: Bool
}

struct SearchHit: Decodable, Identifiable, Equatable {
    let sourceRef: String
    let videoID: String
    let startMs: Int64
    let endMs: Int64
    let description: String

    var id: String { sourceRef }

    private enum CodingKeys: String, CodingKey {
        case sourceRef = "source_ref", videoID = "video_id", startMs = "start_ms", endMs = "end_ms", description
    }
}

struct MediaRange: Decodable, Equatable {
    let path: String
    let startMs: Int64
    let endMs: Int64
    let durationMs: Int64

    private enum CodingKeys: String, CodingKey {
        case path, startMs = "start_ms", endMs = "end_ms", durationMs = "duration_ms"
    }
}

enum ModelCredentials {
    static let names = ["ARK_API_KEY", "DEEPSEEK_API_KEY"]

    static func load() -> [String: String] {
        var credentials: [String: String] = [:]
        for name in names {
            var query = attributes(for: name)
            query[kSecReturnData as String] = true
            query[kSecMatchLimit as String] = kSecMatchLimitOne
            var record: CFTypeRef?
            let status = SecItemCopyMatching(query as CFDictionary, &record)
            if status == errSecSuccess, let data = record as? Data {
                credentials[name] = String(data: data, encoding: .utf8)
            }
        }
        return credentials
    }

    static func save(_ changes: [String: String]) throws {
        for (name, key) in changes {
            let query = attributes(for: name)
            let data = Data(key.utf8)
            let value = [kSecValueData as String: data]
            var status = SecItemUpdate(query as CFDictionary, value as CFDictionary)
            if status == errSecItemNotFound {
                status = SecItemAdd(query.merging(value) { _, updated in updated } as CFDictionary, nil)
            }
            guard status == errSecSuccess else {
                throw DesktopError(SecCopyErrorMessageString(status, nil) as String? ?? "无法保存模型凭据")
            }
        }
    }

    private static func attributes(for name: String) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: "app.seekino.desktop.models",
         kSecAttrAccount as String: name]
    }
}
