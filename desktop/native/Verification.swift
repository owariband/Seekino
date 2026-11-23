import Foundation
import CoreMedia

enum DesktopVerification {
    @MainActor
    static func run(root: String, query: String) async throws {
        let client = LibraryClient()
        let _: [String: Bool] = try await client.request("open", ["root": root])
        do {
            let series: [SeriesItem] = try await client.request("seriesList")
            let jobs: [JobItem] = try await client.request("tasks")
            let result: SearchResponse = try await client.request("search", [
                "query": query, "seriesIDs": series.map(\.id), "keywords": true,
            ])
            guard let hit = result.hits.first, let video = result.videos.first(where: { $0.id == hit.videoID }),
                  jobs.contains(where: { $0.videoID == hit.videoID && $0.latest && $0.status == "COMPLETE" }) else {
                throw DesktopError("验收库缺少搜索结果或已完成的任务")
            }
            let videos: [VideoItem] = try await client.request("seriesDetail", ["id": series[0].id])
            guard !videos.isEmpty else { throw DesktopError("无法显示系列视频") }
            let range: MediaRange = try await client.request("openResult", ["sourceRef": hit.sourceRef])
            guard range.durationMs >= range.endMs, range.startMs == hit.startMs, range.endMs == hit.endMs else {
                throw DesktopError("命中时间与原片证据不一致")
            }
            let full: MediaRange = try await client.request("openVideo", ["videoID": video.id])
            guard full.path == range.path, full.startMs == 0, full.endMs == range.durationMs else {
                throw DesktopError("原片与命中证据的路径或时长不一致")
            }
            try decode(range)
            let pipeline = MediaPipeline()
            pipeline.setVolume(0)
            var failure: DesktopError?
            pipeline.open(range) { failure = $0 }
            let deadline = Date().addingTimeInterval(5)
            while pipeline.position < range.startMs + 150 && failure == nil && Date() < deadline {
                try await Task.sleep(for: .milliseconds(50))
            }
            if let failure { throw failure }
            guard pipeline.position >= range.startMs + 150 else {
                throw DesktopError("连续播放时钟没有推进")
            }
            let played = pipeline.position
            let target = max(0, range.startMs - 100)
            pipeline.seek(target, play: false)
            try await Task.sleep(for: .milliseconds(300))
            guard abs(pipeline.position - target) <= 10, !pipeline.running else {
                throw DesktopError("播放器暂停跳转后的位置不正确")
            }
            pipeline.stop()
            try await client.command("close")
            print("desktop verification pid=\(ProcessInfo.processInfo.processIdentifier), hit=[\(range.startMs),\(range.endMs)), played=\(played)ms, pausedSeek=\(target)ms")
        } catch {
            try? await client.command("close")
            throw error
        }
    }

    private static func decode(_ media: MediaRange) throws {
        var error: UnsafeMutablePointer<CChar>?
        guard let decoder = seekino_decoder_open(media.path, &error) else {
            throw bridgeError(error, fallback: "无法打开验收视频")
        }
        defer { seekino_decoder_close(decoder) }
        guard seekino_decoder_seek(decoder, media.startMs, &error) else {
            throw bridgeError(error, fallback: "无法跳转到命中位置")
        }
        var videoMs: Int64?
        var audioMs: Int64?
        while videoMs == nil || audioMs == nil {
            var track: Int32 = 0
            guard let sample = seekino_decoder_next(decoder, &track, &error) else {
                throw bridgeError(error, fallback: "验收视频缺少连续视频帧或音轨")
            }
            let millis = Int64((sample.presentationTimeStamp.seconds * 1000).rounded())
            if track == 0 && videoMs == nil { videoMs = millis }
            if track == 1 && audioMs == nil { audioMs = millis }
        }
        guard let videoMs, let audioMs, videoMs >= media.startMs, audioMs >= media.startMs,
              videoMs < media.endMs, audioMs < media.endMs else {
            throw DesktopError("解码出的起始画面或音轨落在命中区间外")
        }
        print("first samples video=\(videoMs)ms audio=\(audioMs)ms")
    }
}
