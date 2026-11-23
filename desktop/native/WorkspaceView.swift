import AppKit
import SwiftUI

struct WorkspaceView: View {
    @ObservedObject var model: Workspace
    @ObservedObject private var player: PlayerModel
    @Environment(\.colorScheme) private var scheme
    @FocusState private var searchFocused: Bool
    @State private var dragging = false

    init(model: Workspace) {
        self.model = model
        self.player = model.player
    }

    private var theme: Celadon { Celadon(scheme) }

    var body: some View {
        HStack(spacing: 0) {
            sidebar.frame(width: 206)
            Rectangle().fill(theme.line).frame(width: 1)
            VStack(spacing: 0) {
                toolbar
                Rectangle().fill(theme.line).frame(height: 1)
                if model.opened {
                    HSplitView {
                        workspace.frame(minWidth: 386, maxWidth: .infinity, maxHeight: .infinity)
                        if player.item != nil {
                            WatchPane(player: player, onClose: model.closePlayer)
                                .frame(minWidth: 360, idealWidth: 487, maxHeight: .infinity)
                        }
                    }
                } else {
                    VStack(spacing: 15) {
                        if let error = model.openingError {
                            EmptyPane(title: "无法打开资料库", detail: error, icon: "externaldrive.badge.exclamationmark")
                            Button("重新打开") { Task { await model.start() } }.buttonStyle(ActionStyle(kind: .primary))
                        } else {
                            ProgressView("正在打开资料库…").tint(theme.accent)
                        }
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                }
            }
        }
        .background(theme.canvas)
        .foregroundStyle(theme.ink)
        .tint(theme.accent)
        .frame(minWidth: 1000, minHeight: 660)
        .task {
            await model.start()
            await model.watchTasks()
        }
        .fileImporter(isPresented: $model.choosingFiles, allowedContentTypes: supportedVideoTypes,
                      allowsMultipleSelection: true) { result in
            switch result {
            case .success(let files): Task { await model.importFiles(files) }
            case .failure(let error): model.notice = error.localizedDescription
            }
        }
        .sheet(isPresented: $model.showingSettings, onDismiss: model.clearDeferredImport) {
            ModelSettings(model: model)
        }
        .sheet(isPresented: $model.showingNewSeries) {
            NewSeriesSheet(model: model)
        }
        .alert("Seekino", isPresented: Binding(
            get: { model.notice != nil },
            set: { if !$0 { model.notice = nil } }
        )) {
            Button("好") { model.notice = nil }
        } message: {
            Text(model.notice ?? "")
        }
        .dropDestination(for: URL.self, action: { files, _ in
            guard let local = checkedVideos(files), model.opened, model.importStatus == nil else { return false }
            Task { await model.importFiles(local) }
            return true
        }, isTargeted: { dragging = $0 })
        .overlay {
            if dragging && model.opened {
                RoundedRectangle(cornerRadius: 14)
                    .strokeBorder(theme.accent, style: StrokeStyle(lineWidth: 2, dash: [8, 4]))
                    .padding(8)
                    .allowsHitTesting(false)
            }
        }
        .onChange(of: model.searchFocus) { _, _ in searchFocused = true }
    }

    private var sidebar: some View {
        VStack(alignment: .leading, spacing: 21) {
            HStack(spacing: 9) {
                if let url = Bundle.main.url(forResource: "SeekinoLogo", withExtension: "png"),
                   let image = NSImage(contentsOf: url) {
                    Image(nsImage: image).resizable().scaledToFit().frame(width: 32, height: 32)
                        .accessibilityHidden(true)
                }
                Text("Seekino").font(.system(size: 23, weight: .semibold)).tracking(-0.9)
            }
            .padding(.horizontal, 11)
            .padding(.top, 12)

            VStack(spacing: 4) {
                ForEach(WorkspacePage.allCases) { page in
                    sideButton(page.title, icon: page.icon, selected: model.page == page,
                               badge: page == .tasks && model.activeJobs > 0 ? "\(model.activeJobs)" : nil) {
                        model.page = page
                    }
                }
            }

            VStack(spacing: 6) {
                HStack {
                    Text("系列").font(.system(size: 12, weight: .semibold)).foregroundStyle(theme.secondary)
                    Spacer()
                    Button {
                        model.showingNewSeries = true
                    } label: {
                        Image(systemName: "plus").frame(width: 22, height: 22)
                    }
                    .buttonStyle(.plain)
                    .help("新建系列")
                    .accessibilityLabel("新建系列")
                    .disabled(!model.opened)
                }
                .padding(.horizontal, 10)
                ScrollView {
                    LazyVStack(spacing: 3) {
                        ForEach(model.series) { series in
                            sideButton(series.title, icon: "folder", selected: model.page == .library && model.selectedSeries == series.id,
                                       badge: series.count > 0 ? "\(series.count)" : nil) {
                                model.select(series)
                            }
                        }
                    }
                }
            }

            Spacer(minLength: 0)
            VStack(spacing: 10) {
                Rectangle().fill(theme.line).frame(height: 1)
                sideButton("设置", icon: "gearshape", selected: false, badge: nil) {
                    model.showingSettings = true
                }
            }
        }
        .padding(.horizontal, 12)
        .padding(.bottom, 15)
        .frame(maxHeight: .infinity, alignment: .top)
        .background(theme.surface)
    }

    private func sideButton(_ title: String, icon: String, selected: Bool, badge: String?,
                            action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 9) {
                Image(systemName: icon).font(.system(size: 13, weight: .medium)).frame(width: 18)
                    .foregroundStyle(selected ? theme.accent : theme.secondary)
                Text(title).font(.system(size: 13, weight: selected ? .semibold : .medium)).lineLimit(1)
                Spacer(minLength: 0)
                if let badge {
                    Text(badge).font(.system(size: 11, weight: .medium)).monospacedDigit()
                        .foregroundStyle(theme.secondary)
                }
            }
            .padding(.horizontal, 9)
            .frame(height: 34)
            .background(selected ? theme.wash : .clear, in: RoundedRectangle(cornerRadius: 8))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(selected ? .isSelected : [])
    }

    private var toolbar: some View {
        HStack(spacing: 11) {
            Text(model.page.title)
                .font(.system(size: 14, weight: .semibold))
            Spacer()
            if let status = model.importStatus {
                if let progress = model.importProgress {
                    ProgressView(value: progress).controlSize(.small).frame(width: 72)
                    Text("\(Int(progress * 100))%")
                        .monospacedDigit().font(.system(size: 11)).foregroundStyle(theme.secondary)
                } else {
                    ProgressView().controlSize(.small)
                }
                Text(status).lineLimit(1).font(.system(size: 12)).foregroundStyle(theme.secondary)
            } else if model.opened && !model.modelReady {
                Button {
                    model.showingSettings = true
                } label: {
                    Label("配置模型", systemImage: "key.horizontal")
                }
                .buttonStyle(ActionStyle())
                .help("新视频解析和描述搜索使用已配置的模型服务")
            }
            Button {
                model.choosingFiles = true
            } label: {
                Label("导入视频", systemImage: "plus")
            }
            .buttonStyle(ActionStyle(kind: .primary))
            .disabled(!model.opened || model.importStatus != nil)
            .help("导入视频（⌘O）")
        }
        .padding(.horizontal, 22)
        .frame(height: 57)
        .background(theme.canvas)
    }

    private var workspace: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                switch model.page {
                case .search: searchPane
                case .library: libraryPane
                case .tasks: TasksPane(model: model)
                }
            }
            .frame(maxWidth: 986, alignment: .leading)
            .padding(.horizontal, 28)
            .padding(.top, 27)
            .padding(.bottom, 32)
            .frame(maxWidth: .infinity)
        }
        .background(alignment: .topTrailing) {
            RadialGradient(colors: [theme.accent.opacity(0.08), .clear],
                           center: .topTrailing, startRadius: 0, endRadius: 380)
                .padding(.top, -10)
        }
    }

    private var searchPane: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text("搜索视频").font(.system(size: 31, weight: .semibold)).tracking(-0.8)

            VStack(alignment: .leading, spacing: 12) {
                HStack(spacing: 10) {
                    Image(systemName: "magnifyingglass").font(.system(size: 17, weight: .medium))
                        .foregroundStyle(theme.accent)
                    TextField("输入一句台词，或描述想找的画面", text: $model.query)
                        .font(.system(size: 16))
                        .textFieldStyle(.plain)
                        .focused($searchFocused)
                        .accessibilityIdentifier("seekino.search.query")
                        .onSubmit { runSearch() }
                    if !model.query.isEmpty {
                        Button { model.query = ""; searchFocused = true } label: {
                            Image(systemName: "xmark.circle.fill").foregroundStyle(theme.secondary)
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel("清空关键词")
                    }
                    Button("搜索", action: runSearch)
                        .buttonStyle(ActionStyle(kind: .primary))
                        .disabled(model.searching || model.query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                                  || model.series.isEmpty)
                        .accessibilityIdentifier("seekino.search.run")
                }

                HStack(spacing: 11) {
                    Picker("搜索类型", selection: $model.searchMode) {
                        ForEach(SearchMode.allCases) { Text($0.rawValue).tag($0) }
                    }
                    .labelsHidden()
                    .pickerStyle(.segmented)
                    .frame(width: 163)

                    Picker("搜索范围", selection: $model.searchScope) {
                        Text("全部系列").tag(String?.none)
                        ForEach(model.series) { Text($0.title).tag(Optional($0.id)) }
                    }
                    .labelsHidden()
                    .frame(maxWidth: 168)

                    Spacer(minLength: 0)
                    if model.searching { ProgressView().controlSize(.small) }
                }
            }
            .padding(14)
            .modifier(Surface())

            if model.series.isEmpty {
                VStack(spacing: 0) {
                    EmptyPane(title: "素材库还是空的", detail: "导入视频并完成解析，即可搜索台词和画面。", icon: "film.stack")
                    Button {
                        model.choosingFiles = true
                    } label: {
                        Label("导入第一部视频", systemImage: "square.and.arrow.down")
                    }
                    .buttonStyle(ActionStyle(kind: .primary))
                    .padding(.bottom, 24)
                }
                .modifier(Surface())
            } else if model.searching {
                ProgressView("正在查找片段…")
                    .padding(29).frame(maxWidth: .infinity)
            } else if let result = model.searchResult {
                HStack(alignment: .firstTextBaseline) {
                    Text("“\(model.lastQuery)”").font(.system(size: 16, weight: .semibold))
                    Spacer()
                    Text("\(result.hits.count) 个片段").monospacedDigit().font(.system(size: 12))
                        .foregroundStyle(theme.secondary)
                }
                if result.partial {
                    Label("部分视频的索引还未就绪，结果可能不完整。", systemImage: "info.circle")
                        .font(.system(size: 12)).foregroundStyle(theme.secondary)
                }
                if result.hits.isEmpty {
                    EmptyPane(title: "没有找到片段", detail: "换一个关键词、描述或搜索范围。", icon: "magnifyingglass")
                        .modifier(Surface())
                } else {
                    LazyVStack(spacing: 8) {
                        ForEach(result.hits) { hit in
                            HitRow(hit: hit, video: model.video(hit)) {
                                Task { await model.play(hit) }
                            }
                        }
                    }
                }
            } else if model.series.allSatisfy({ $0.ready == 0 }) {
                EmptyPane(title: "索引完成后即可搜索", detail: "在解析任务中查看当前阶段和失败原因。", icon: "clock")
                    .modifier(Surface())
            } else if !model.videos.isEmpty {
                HStack {
                    Text(model.currentSeries?.title ?? "资料库").font(.system(size: 16, weight: .semibold))
                    Spacer()
                    Button("打开资料库") { model.page = .library }.buttonStyle(ActionStyle())
                }
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 170, maximum: 265), spacing: 13)], spacing: 13) {
                    ForEach(model.videos.suffix(4).reversed()) { video in
                        VideoCard(video: video) { Task { await model.play(video) } }
                    }
                }
            }
        }
    }

    private var libraryPane: some View {
        VStack(alignment: .leading, spacing: 17) {
            HStack(alignment: .center) {
                VStack(alignment: .leading, spacing: 5) {
                    Text(model.currentSeries?.title ?? "资料库").font(.system(size: 31, weight: .semibold))
                        .tracking(-0.6)
                    if let series = model.currentSeries {
                        Text("\(series.count) 部视频 · \(series.ready) 部可搜索")
                            .font(.system(size: 13)).foregroundStyle(theme.secondary)
                    }
                }
                Spacer()
                Button {
                    model.showingNewSeries = true
                } label: {
                    Label("新建系列", systemImage: "folder.badge.plus")
                }
                .buttonStyle(ActionStyle())
            }
            if model.videos.isEmpty {
                VStack(spacing: 0) {
                    EmptyPane(title: "添加视频", detail: "选择或拖入 MP4、MOV、AVI 文件。", icon: "tray.and.arrow.down")
                    Button("选取视频…") { model.choosingFiles = true }
                        .buttonStyle(ActionStyle(kind: .primary))
                        .padding(.bottom, 24)
                }
                .modifier(Surface())
            } else {
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 177, maximum: 365), spacing: 14)], spacing: 14) {
                    ForEach(model.videos) { video in
                        VideoCard(video: video) { Task { await model.play(video) } }
                    }
                }
            }
        }
    }

    private func runSearch() { Task { await model.search() } }

    private func checkedVideos(_ files: [URL]) -> [URL]? {
        guard !files.isEmpty && files.allSatisfy({
            $0.isFileURL && ["mp4", "mov", "avi"].contains($0.pathExtension.lowercased())
        }) else { return nil }
        return files
    }
}

private struct HitRow: View {
    @Environment(\.colorScheme) private var scheme
    @State private var hover = false
    let hit: SearchHit
    let video: VideoItem?
    let play: () -> Void

    var body: some View {
        let theme = Celadon(scheme)
        Button(action: play) {
            HStack(spacing: 13) {
                VideoCover(path: video?.cover).frame(width: 88, height: 53)
                VStack(alignment: .leading, spacing: 5) {
                    HStack(spacing: 7) {
                        Text("\(evidenceTime(hit.startMs)) – \(evidenceTime(hit.endMs))")
                            .font(.system(size: 11, weight: .semibold)).monospacedDigit()
                            .foregroundStyle(theme.accent)
                        if let video {
                            Text(video.title).lineLimit(1).font(.system(size: 11)).foregroundStyle(theme.secondary)
                        }
                    }
                    Text(hit.description).font(.system(size: 14, weight: .medium)).lineLimit(3)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                Image(systemName: "play.fill")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(theme.accent)
                    .frame(width: 35, height: 35)
                    .background(theme.wash, in: Circle())
                    .accessibilityHidden(true)
            }
            .padding(10)
            .background(hover ? theme.wash : theme.paper, in: RoundedRectangle(cornerRadius: 12))
            .overlay { RoundedRectangle(cornerRadius: 12).strokeBorder(theme.line) }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help("从 \(evidenceTime(hit.startMs)) 播放原片")
        .onHover { hover = $0 }
        .accessibilityIdentifier("seekino.search.result")
    }
}

private struct VideoCard: View {
    @Environment(\.colorScheme) private var scheme
    @State private var hover = false
    let video: VideoItem
    let play: () -> Void

    var body: some View {
        let theme = Celadon(scheme)
        Button(action: play) {
            VStack(alignment: .leading, spacing: 10) {
                VideoCover(path: video.cover)
                    .aspectRatio(16/9, contentMode: .fit)
                    .overlay(alignment: .bottomTrailing) {
                        Text(timecode(video.durationMs)).font(.system(size: 11, weight: .medium))
                            .monospacedDigit().foregroundStyle(.white)
                            .padding(.horizontal, 5).padding(.vertical, 3)
                            .background(.black.opacity(0.7), in: RoundedRectangle(cornerRadius: 4))
                            .padding(6)
                    }
                    .overlay {
                        if hover {
                            Image(systemName: "play.fill").foregroundStyle(.white)
                                .frame(width: 35, height: 35).background(.black.opacity(0.65), in: Circle())
                        }
                    }
                Text(video.title).font(.system(size: 14, weight: .semibold))
                    .lineLimit(1).frame(maxWidth: .infinity, alignment: .leading)
                HStack {
                    Text("第 \(video.episode) 集").font(.system(size: 11)).foregroundStyle(theme.secondary)
                    Spacer(minLength: 4)
                    StatusTag(title: video.isReady ? "可搜索" : video.failed ? "处理失败" : "建库中", failure: video.failed)
                }
                if !video.isReady && !video.failed, let progress = video.progress {
                    ProgressView(value: max(0, min(100, progress)), total: 100).controlSize(.small)
                }
            }
            .padding(10)
            .modifier(Surface())
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hover = $0 }
        .help("播放 \(video.title)")
    }
}
