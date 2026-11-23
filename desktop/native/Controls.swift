import AppKit
import SwiftUI

struct TasksPane: View {
    @ObservedObject var model: Workspace
    @Environment(\.colorScheme) private var scheme
    @State private var history = false

    var body: some View {
        let theme = Celadon(scheme)
        let jobs = history ? model.jobs : model.jobs.filter(\.latest)
        VStack(alignment: .leading, spacing: 17) {
            HStack(alignment: .center) {
                VStack(alignment: .leading, spacing: 5) {
                    Text("解析任务").font(.system(size: 31, weight: .semibold)).tracking(-0.6)
                    Text("\(model.activeJobs) 项进行中 · \(jobs.count) 项任务")
                        .font(.system(size: 13)).foregroundStyle(theme.secondary)
                }
                Spacer()
                Picker("任务版本", selection: $history) {
                    Text("当前版本").tag(false)
                    Text("全部版本").tag(true)
                }
                .pickerStyle(.segmented).labelsHidden().frame(width: 160)
            }
            if jobs.isEmpty {
                EmptyPane(title: "暂无解析任务", detail: "导入视频后，可以在这里查看解析与索引进度。", icon: "list.bullet.rectangle")
                    .modifier(Surface())
            } else {
                LazyVStack(spacing: 9) {
                    ForEach(jobs) { job in
                        VStack(alignment: .leading, spacing: 10) {
                            HStack(spacing: 11) {
                                VStack(alignment: .leading, spacing: 5) {
                                    Text(job.title).font(.system(size: 14, weight: .semibold)).lineLimit(1)
                                    Text("\(job.series) · 第 \(job.episode) 集")
                                        .font(.system(size: 12)).foregroundStyle(theme.secondary).lineLimit(1)
                                }
                                Spacer(minLength: 4)
                                StatusTag(title: job.label, failure: job.error != nil && !job.active)
                                if model.workingJobs.contains(job.id) {
                                    ProgressView().controlSize(.small)
                                } else if job.canPause {
                                    Button(job.paused ? "继续" : "暂停") {
                                        Task { await model.pauseOrResume(job) }
                                    }.buttonStyle(ActionStyle())
                                } else if job.canRetry {
                                    Button("重试") { Task { await model.retry(job) } }
                                        .buttonStyle(ActionStyle())
                                }
                            }
                            if job.active || job.error != nil {
                                HStack {
                                    if let progress = job.progress {
                                        ProgressView(value: min(100, max(0, progress)), total: 100).controlSize(.small)
                                        Text("\(Int(progress.rounded()))%").monospacedDigit().font(.system(size: 11))
                                    } else if job.active && !job.paused {
                                        ProgressView().controlSize(.small)
                                        Text(job.label).font(.system(size: 11))
                                    }
                                    Spacer(minLength: 4)
                                    if job.elapsedMs > 0 {
                                        Text(timecode(job.elapsedMs)).monospacedDigit().font(.system(size: 11))
                                    }
                                }
                                .foregroundStyle(theme.secondary)
                            }
                            if let error = job.error {
                                Label(error, systemImage: "exclamationmark.circle")
                                    .font(.system(size: 12)).foregroundStyle(theme.danger)
                            }
                        }
                        .padding(14)
                        .modifier(Surface())
                    }
                }
            }
        }
    }
}

struct ModelSettings: View {
    @ObservedObject var model: Workspace
    @Environment(\.colorScheme) private var scheme
    @Environment(\.dismiss) private var dismiss
    @AppStorage("appearance") private var appearance = "system"
    @State private var ark = ""
    @State private var deepseek = ""
    @State private var error: String?
    @State private var saving = false

    var body: some View {
        let theme = Celadon(scheme)
        VStack(alignment: .leading, spacing: 18) {
            Text("设置").font(.system(size: 25, weight: .semibold))
            Picker("外观", selection: $appearance) {
                Text("跟随系统").tag("system")
                Text("浅色").tag("light")
                Text("深色").tag("dark")
            }.pickerStyle(.segmented)

            VStack(alignment: .leading, spacing: 11) {
                Text("模型连接").font(.system(size: 15, weight: .semibold))
                Text("新视频解析和描述搜索使用配置的模型服务。素材库保存在此 Mac 上。")
                    .font(.system(size: 12)).foregroundStyle(theme.secondary)
                keyField("火山方舟", name: "ARK_API_KEY", value: $ark)
                keyField("DeepSeek", name: "DEEPSEEK_API_KEY", value: $deepseek)
                Text("API Key 存在 macOS 钥匙串。留空保留现有凭据。")
                    .font(.system(size: 11)).foregroundStyle(theme.secondary)
            }
            .padding(13).modifier(Surface())

            VStack(alignment: .leading, spacing: 6) {
                Text("资料库位置").font(.system(size: 13, weight: .semibold))
                HStack {
                    Text(model.root.path).font(.system(size: 11))
                        .foregroundStyle(theme.secondary).textSelection(.enabled)
                    Spacer()
                    Button("在 Finder 中显示") {
                        NSWorkspace.shared.activateFileViewerSelecting([model.root])
                    }
                }
            }
            if let error {
                Text(error).font(.system(size: 12)).foregroundStyle(theme.danger).textSelection(.enabled)
            }
            HStack {
                Spacer()
                if saving { ProgressView().controlSize(.small) }
                Button("关闭") { dismiss() }.keyboardShortcut(.cancelAction).disabled(saving)
                Button("保存", action: save)
                    .buttonStyle(ActionStyle(kind: .primary)).keyboardShortcut(.defaultAction)
                    .disabled(saving || model.importStatus != nil)
            }
        }
        .foregroundStyle(theme.ink)
        .tint(theme.accent)
        .padding(24)
        .frame(width: 500)
        .background(theme.surface)
    }

    private func keyField(_ label: String, name: String, value: Binding<String>) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(label).font(.system(size: 12, weight: .medium))
                Spacer()
                if model.credentials[name] == true { StatusTag(title: "已配置") }
            }
            SecureField(name, text: value)
                .textFieldStyle(.roundedBorder)
                .accessibilityLabel("\(label) API Key")
        }
    }

    private func save() {
        let values = ["ARK_API_KEY": ark, "DEEPSEEK_API_KEY": deepseek]
            .mapValues { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.value.isEmpty }
        if values.isEmpty {
            dismiss()
            return
        }
        saving = true
        error = nil
        Task {
            defer { saving = false }
            do { try await model.saveCredentials(values) }
            catch { self.error = error.localizedDescription }
        }
    }
}

struct NewSeriesSheet: View {
    @ObservedObject var model: Workspace
    @Environment(\.dismiss) private var dismiss
    @State private var title = ""
    @State private var error: String?
    @State private var saving = false
    @FocusState private var focused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 15) {
            Text("新建系列").font(.system(size: 20, weight: .semibold))
            TextField("系列名称", text: $title).textFieldStyle(.roundedBorder).focused($focused)
            if let error { Text(error).font(.system(size: 12)).foregroundStyle(.red) }
            HStack {
                Spacer()
                if saving { ProgressView().controlSize(.small) }
                Button("取消") { dismiss() }.keyboardShortcut(.cancelAction).disabled(saving)
                Button("创建") {
                    saving = true
                    Task {
                        defer { saving = false }
                        do { try await model.addSeries(title); dismiss() }
                        catch { self.error = error.localizedDescription }
                    }
                }
                .buttonStyle(ActionStyle(kind: .primary)).keyboardShortcut(.defaultAction)
                .disabled(saving || title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }
        .padding(22)
        .frame(width: 380)
        .onAppear { focused = true }
    }
}

struct WatchPane: View {
    @ObservedObject var player: PlayerModel
    let onClose: () -> Void
    @Environment(\.colorScheme) private var scheme
    @State private var slider = 0.0
    @State private var scrubbing = false
    @State private var playing = false
    @State private var volume = 1.0

    var body: some View {
        let theme = Celadon(scheme)
        if let item = player.item {
            VStack(alignment: .leading, spacing: 13) {
                HStack(alignment: .center, spacing: 9) {
                    Image(systemName: "play.rectangle").foregroundStyle(theme.accent).accessibilityHidden(true)
                    Text(player.title).font(.system(size: 15, weight: .semibold)).lineLimit(1)
                    Spacer(minLength: 0)
                    Button(action: onClose) { Image(systemName: "xmark").frame(width: 25, height: 25) }
                        .buttonStyle(.plain).help("关闭播放器").accessibilityLabel("关闭播放器")
                }
                MediaStage(player: player, error: player.error)
                    .aspectRatio(16/10, contentMode: .fit)
                VStack(spacing: 7) {
                    Slider(value: Binding(
                        get: { slider },
                        set: { value in
                            slider = value
                            if !scrubbing { player.pipeline.seek(Int64(value), play: playing) }
                        }
                    ), in: 0...Double(max(1, item.durationMs)), onEditingChanged: { editing in
                        scrubbing = editing
                        if !editing { player.pipeline.seek(Int64(slider), play: playing) }
                    })
                    .controlSize(.small)
                    .accessibilityLabel("原片播放进度")
                    .accessibilityValue(timecode(Int64(slider)))

                    HStack(spacing: 11) {
                        Button {
                            playing.toggle()
                            player.pipeline.setPlaying(playing)
                        } label: {
                            Image(systemName: playing ? "pause.fill" : "play.fill")
                                .frame(width: 22, height: 22)
                        }
                        .buttonStyle(.plain)
                        .help(playing ? "暂停" : "播放")
                        .accessibilityLabel(playing ? "暂停" : "播放")
                        Text("\(timecode(Int64(slider))) / \(timecode(item.durationMs))")
                            .font(.system(size: 11)).monospacedDigit().foregroundStyle(theme.secondary)
                        Spacer(minLength: 0)
                        Image(systemName: volume > 0 ? "speaker.wave.2" : "speaker.slash")
                            .foregroundStyle(theme.secondary).accessibilityHidden(true)
                        Slider(value: $volume, in: 0...1).controlSize(.mini).frame(width: 65)
                            .accessibilityLabel("音量")
                            .onChange(of: volume) { _, value in player.pipeline.setVolume(value) }
                    }
                }
                .tint(theme.accent)
                if player.hit {
                    Rectangle().fill(theme.line).frame(height: 1)
                    HStack(spacing: 8) {
                        Image(systemName: "scope").foregroundStyle(theme.accent).accessibilityHidden(true)
                        Text("\(evidenceTime(item.startMs)) – \(evidenceTime(item.endMs))")
                            .font(.system(size: 12, weight: .semibold)).monospacedDigit()
                        Spacer(minLength: 0)
                        Button("回到命中片段") {
                            player.pipeline.seek(item.startMs, play: true)
                        }.buttonStyle(ActionStyle())
                    }
                }
                Spacer(minLength: 0)
            }
            .padding(17)
            .background(theme.surface)
            .onReceive(Timer.publish(every: 0.15, on: .main, in: .common).autoconnect()) { _ in
                if !scrubbing { slider = Double(min(item.durationMs, player.pipeline.position)) }
                playing = player.pipeline.running
            }
        }
    }
}

private struct MediaStage: View {
    @ObservedObject var player: PlayerModel
    let error: String?

    var body: some View {
        GeometryReader { geometry in
            ZStack {
                Color(red: 0.05, green: 0.08, blue: 0.07)
                VideoSurface(display: player.pipeline.display)
                    .frame(width: geometry.size.width, height: geometry.size.height)
                if let error {
                    VStack(spacing: 8) {
                        Image(systemName: "exclamationmark.circle")
                        Text(error).font(.system(size: 12)).multilineTextAlignment(.center)
                    }
                    .padding(14)
                    .foregroundStyle(.white)
                    .background(.black.opacity(0.76), in: RoundedRectangle(cornerRadius: 9))
                }
            }
        }
        .clipShape(RoundedRectangle(cornerRadius: 11))
        .accessibilityIdentifier("seekino.player.surface")
    }
}
