import AppKit
import AVFoundation
import CoreMedia
import SwiftUI

final class MediaPipeline {
    let display = AVSampleBufferDisplayLayer()
    private let audio = AVSampleBufferAudioRenderer()
    private let sync = AVSampleBufferRenderSynchronizer()
    private let worker = DispatchQueue(label: "app.seekino.playback", qos: .userInitiated)
    private var ticker: DispatchSourceTimer?
    private var decoder: OpaquePointer?
    private var next: (CMSampleBuffer, Int32)?
    private var end = false
    private var wantsPlayback = false
    private var duration: Int64 = 0
    private var needsPicture = true
    private var failed: ((DesktopError) -> Void)?

    init() {
        display.videoGravity = .resizeAspect
        sync.delaysRateChangeUntilHasSufficientMediaData = false
        sync.addRenderer(display)
        sync.addRenderer(audio)
        let ticker = DispatchSource.makeTimerSource(queue: worker)
        ticker.schedule(deadline: .now(), repeating: .milliseconds(16), leeway: .milliseconds(4))
        ticker.setEventHandler { [weak self] in self?.fill() }
        ticker.resume()
        self.ticker = ticker
    }

    deinit {
        ticker?.cancel()
        seekino_decoder_close(decoder)
    }

    var position: Int64 {
        let value = sync.currentTime().seconds
        return value.isFinite ? max(0, Int64(value * 1000)) : 0
    }

    var running: Bool { sync.rate > 0 }

    func open(_ media: MediaRange, onError: @escaping (DesktopError) -> Void) {
        worker.async {
            self.failed = onError
            self.wantsPlayback = true
            self.duration = media.durationMs
            self.flush()
            seekino_decoder_close(self.decoder)
            var error: UnsafeMutablePointer<CChar>?
            self.decoder = seekino_decoder_open(media.path, &error)
            guard self.decoder != nil else {
                self.fail(bridgeError(error, fallback: "无法读取原片"))
                return
            }
            self.seekTo(media.startMs)
        }
    }

    func seek(_ millis: Int64, play: Bool) {
        worker.async {
            self.wantsPlayback = play
            self.seekTo(millis)
        }
    }

    func setPlaying(_ value: Bool) {
        worker.async {
            self.wantsPlayback = value
            if !value {
                self.sync.rate = 0
            } else if self.position >= self.duration {
                self.seekTo(0)
            } else if !self.needsPicture {
                self.sync.rate = 1
            }
        }
    }

    func setVolume(_ value: Double) {
        worker.async { self.audio.volume = Float(value) }
    }

    func stop() {
        worker.async {
            self.wantsPlayback = false
            self.failed = nil
            self.flush()
            seekino_decoder_close(self.decoder)
            self.decoder = nil
        }
    }

    private func seekTo(_ millis: Int64) {
        guard let decoder else { return }
        let target = min(max(0, millis), max(0, duration - 1))
        flush()
        sync.setRate(0, time: CMTime(value: target, timescale: 1000))
        var error: UnsafeMutablePointer<CChar>?
        if !seekino_decoder_seek(decoder, target, &error) {
            fail(bridgeError(error, fallback: "无法跳转到原片时间"))
        }
    }

    private func flush() {
        sync.rate = 0
        display.sampleBufferRenderer.flush(removingDisplayedImage: true, completionHandler: nil)
        audio.flush()
        next = nil
        end = false
        needsPicture = true
    }

    private func fill() {
        guard let decoder else { return }
        if duration > 0 && position >= duration {
            sync.rate = 0
            wantsPlayback = false
            return
        }
        let ahead = sync.currentTime().seconds + 0.6
        for _ in 0..<30 {
            if next == nil && !end {
                var track: Int32 = 0
                var error: UnsafeMutablePointer<CChar>?
                if let sample = seekino_decoder_next(decoder, &track, &error) {
                    next = (sample, track)
                } else if error != nil {
                    fail(bridgeError(error, fallback: "无法解码原片"))
                    return
                } else {
                    end = true
                }
            }
            guard let (sample, track) = next else { break }
            if !needsPicture && sample.presentationTimeStamp.seconds > ahead { break }
            if track == 0 {
                let renderer = display.sampleBufferRenderer
                if renderer.requiresFlushToResumeDecoding {
                    fail(DesktopError("视频输出中断，请重新打开原片"))
                    return
                }
                guard renderer.isReadyForMoreMediaData else { break }
                if needsPicture {
                    seekino_display_immediately(sample)
                    needsPicture = false
                    if wantsPlayback { sync.rate = 1 }
                }
                renderer.enqueue(sample)
            } else {
                guard audio.isReadyForMoreMediaData else { break }
                audio.enqueue(sample)
            }
            next = nil
        }
        if end && needsPicture {
            fail(DesktopError("所选时间附近没有可显示的视频帧"))
        }
    }

    private func fail(_ error: DesktopError) {
        let callback = failed
        failed = nil
        sync.rate = 0
        wantsPlayback = false
        seekino_decoder_close(decoder)
        decoder = nil
        DispatchQueue.main.async { callback?(error) }
    }
}

@MainActor
final class PlayerModel: ObservableObject {
    let pipeline = MediaPipeline()
    @Published private(set) var item: MediaRange?
    @Published private(set) var title = ""
    @Published private(set) var hit = false
    @Published private(set) var error: String?
    private var opened = UUID()

    func open(_ media: MediaRange, title: String, hit: Bool) {
        let revision = UUID()
        opened = revision
        self.item = media
        self.title = title
        self.hit = hit
        error = nil
        pipeline.open(media) { [weak self] error in
            guard self?.opened == revision else { return }
            self?.error = error.message
        }
    }

    func close() {
        opened = UUID()
        item = nil
        pipeline.stop()
    }
}

struct VideoSurface: NSViewRepresentable {
    let display: AVSampleBufferDisplayLayer

    func makeNSView(context: Context) -> NSView {
        let view = NSView()
        view.wantsLayer = true
        view.layer = display
        return view
    }

    func updateNSView(_ view: NSView, context: Context) {}
}
