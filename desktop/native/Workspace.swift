import AppKit
import SwiftUI
import UniformTypeIdentifiers

enum WorkspacePage: String, CaseIterable, Identifiable {
    case search, library, tasks
    var id: Self { self }
    var title: String {
        switch self {
        case .search: "搜索"
        case .library: "资料库"
        case .tasks: "解析任务"
        }
    }
    var icon: String {
        switch self {
        case .search: "magnifyingglass"
        case .library: "rectangle.stack"
        case .tasks: "list.bullet.rectangle"
        }
    }
}

enum SearchMode: String, CaseIterable, Identifiable {
    case keyword = "关键词"
    case semantic = "描述搜索"
    var id: Self { self }
}

@MainActor
final class Workspace: ObservableObject {
    let client = LibraryClient()
    let player = PlayerModel()
    let root: URL

    @Published var page: WorkspacePage = .search
    @Published var query = ""
    @Published var searchMode: SearchMode = .keyword
    @Published var searchScope: String?
    @Published var selectedSeries: String?
    @Published var showingSettings = false
    @Published var showingNewSeries = false
    @Published var choosingFiles = false
    @Published var searchFocus = 0

    @Published private(set) var credentials: [String: Bool] = [:]
    @Published private(set) var series: [SeriesItem] = []
    @Published private(set) var videos: [VideoItem] = []
    @Published private(set) var jobs: [JobItem] = []
    @Published private(set) var searchResult: SearchResponse?
    @Published private(set) var lastQuery = ""
    @Published private(set) var searching = false
    @Published private(set) var importStatus: String?
    @Published private(set) var importProgress: Double?
    @Published private(set) var starting = false
    @Published private(set) var opened = false
    @Published private(set) var openingError: String?
    @Published private(set) var workingJobs: Set<String> = []
    @Published var notice: String?

    private var knownVideos: [String: VideoItem] = [:]
    private var playbackRequest = UUID()
    private var searchRequest = UUID()
    private var closing = false
    private var deferredImport: [URL]?

    init(root: URL) { self.root = root }

    var modelReady: Bool { ModelCredentials.names.allSatisfy { credentials[$0] == true } }
    var activeJobs: Int { jobs.filter { $0.latest && $0.active && !$0.paused }.count }
    var currentSeries: SeriesItem? { series.first { $0.id == selectedSeries } }

    func start() async {
        guard !opened, !starting, !closing else { return }
        starting = true
        openingError = nil
        defer { starting = false }
        do {
            let keys = await Task.detached { ModelCredentials.load() }.value
            credentials = try await client.request("open", ["root": root.path, "keys": keys])
            guard !closing else { return }
            opened = true
            if modelReady { searchMode = .semantic }
            try await refreshLibrary()
            try await refreshTasks()
        } catch {
            if opened { notice = error.localizedDescription }
            else { openingError = error.localizedDescription }
        }
    }

    func watchTasks() async {
        while !Task.isCancelled && !closing {
            try? await Task.sleep(for: .seconds(1.5))
            guard !Task.isCancelled, opened, !closing, importStatus == nil, activeJobs > 0 else { continue }
            do {
                let previous = jobs
                try await refreshTasks()
                if previous != jobs {
                    try await refreshLibrary()
                }
            } catch is CancellationError {
                return
            } catch {
                notice = error.localizedDescription
            }
        }
    }

    func focusSearch() {
        page = .search
        searchFocus += 1
    }

    func select(_ item: SeriesItem) {
        selectedSeries = item.id
        page = .library
        videos = []
        Task {
            do { try await refreshVideos() }
            catch { notice = error.localizedDescription }
        }
    }

    func addSeries(_ title: String) async throws {
        let name = title.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty else { throw DesktopError("请输入系列名称") }
        let item: SeriesItem = try await client.request("createSeries", ["name": name])
        selectedSeries = item.id
        try await refreshLibrary()
        page = .library
    }

    func importFiles(_ files: [URL]) async {
        guard opened, !closing, importStatus == nil, !files.isEmpty else { return }
        guard modelReady else {
            deferredImport = files
            showingSettings = true
            return
        }
        page = .library
        importStatus = "正在准备导入…"
        defer {
            importStatus = nil
            importProgress = nil
        }
        do {
            if selectedSeries == nil { try await addSeries("我的素材") }
            guard let selectedSeries else { return }
            var errors: [String] = []
            for (index, file) in files.enumerated() {
                if closing { break }
                importStatus = files.count == 1 ? "正在导入 \(file.lastPathComponent)"
                    : "正在导入 \(index + 1)/\(files.count) · \(file.lastPathComponent)"
                let scoped = file.startAccessingSecurityScopedResource()
                defer { if scoped { file.stopAccessingSecurityScopedResource() } }
                let polling = Task {
                    while !Task.isCancelled, !closing {
                        try? await Task.sleep(for: .milliseconds(250))
                        let progress: Double? = try? await client.request("importProgress")
                        if !Task.isCancelled, importProgress != progress { importProgress = progress }
                    }
                }
                defer {
                    polling.cancel()
                    importProgress = nil
                }
                do {
                    let _: ImportResult = try await client.request("import", ["path": file.path, "seriesID": selectedSeries])
                } catch {
                    errors.append("\(file.lastPathComponent) · \(error.localizedDescription)")
                }
            }
            try await refreshLibrary()
            try await refreshTasks()
            if !errors.isEmpty { notice = errors.joined(separator: "\n") }
        } catch {
            notice = error.localizedDescription
        }
    }

    func saveCredentials(_ changes: [String: String]) async throws {
        let keys = try await Task.detached {
            try ModelCredentials.save(changes)
            return ModelCredentials.load()
        }.value
        credentials = try await client.request("open", ["root": root.path, "keys": keys])
        opened = true
        openingError = nil
        if modelReady { searchMode = .semantic }
        showingSettings = false
        if let files = deferredImport {
            deferredImport = nil
            await importFiles(files)
        } else {
            try await refreshLibrary()
        }
    }

    func clearDeferredImport() { deferredImport = nil }

    func search() async {
        let value = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard opened, !closing, !searching, !value.isEmpty, !series.isEmpty else { return }
        if searchMode == .semantic && !modelReady {
            showingSettings = true
            return
        }
        let revision = UUID()
        searchRequest = revision
        searching = true
        lastQuery = value
        searchResult = nil
        let ids = searchScope.map { [$0] } ?? series.map(\.id)
        defer { if searchRequest == revision { searching = false } }
        do {
            let result: SearchResponse = try await client.request("search", [
                "query": value, "seriesIDs": ids, "keywords": searchMode == .keyword,
            ])
            guard searchRequest == revision else { return }
            for video in result.videos { knownVideos[video.id] = video }
            searchResult = result
        } catch {
            if searchRequest == revision { notice = error.localizedDescription }
        }
    }

    func video(_ hit: SearchHit) -> VideoItem? { knownVideos[hit.videoID] }

    func play(_ hit: SearchHit) async {
        await play("openResult", ["sourceRef": hit.sourceRef], title: video(hit)?.title ?? "原片", hit: true)
    }

    func play(_ video: VideoItem) async {
        await play("openVideo", ["videoID": video.id], title: video.title, hit: false)
    }

    func closePlayer() {
        playbackRequest = UUID()
        player.close()
    }

    func pauseOrResume(_ job: JobItem) async {
        await updateTask(job, action: "pause", values: ["id": job.id, "paused": !job.paused])
    }

    func retry(_ job: JobItem) async {
        guard let id = job.uploadID else { return }
        if !modelReady {
            showingSettings = true
            return
        }
        await updateTask(job, action: "retry", values: ["uploadID": id])
    }

    func refresh() async {
        do {
            try await refreshLibrary()
            try await refreshTasks()
        } catch {
            notice = error.localizedDescription
        }
    }

    func shutdown() async {
        closing = true
        closePlayer()
        guard opened || starting else { return }
        do { try await client.command("close") }
        catch { NSLog("Seekino could not close its library during termination") }
        opened = false
    }

    private func play(_ action: String, _ args: [String: Any], title: String, hit: Bool) async {
        let revision = UUID()
        playbackRequest = revision
        do {
            let range: MediaRange = try await client.request(action, args)
            guard revision == playbackRequest, !closing else { return }
            player.open(range, title: title, hit: hit)
        } catch {
            if revision == playbackRequest { notice = error.localizedDescription }
        }
    }

    private func refreshLibrary() async throws {
        var list: [SeriesItem] = []
        repeat {
            let batch: [SeriesItem] = try await client.request("seriesList", ["offset": list.count])
            list += batch
            if batch.count < 100 { break }
        } while !closing
        if series != list { series = list }
        if let selectedSeries, !list.contains(where: { $0.id == selectedSeries }) { self.selectedSeries = nil }
        if selectedSeries == nil { selectedSeries = list.first?.id }
        try await refreshVideos()
    }

    private func refreshVideos() async throws {
        guard let id = selectedSeries else { return }
        let items: [VideoItem] = try await client.request("seriesDetail", ["id": id])
        for item in items { knownVideos[item.id] = item }
        if selectedSeries == id && videos != items { videos = items }
    }

    private func refreshTasks() async throws {
        var list: [JobItem] = []
        repeat {
            let batch: [JobItem] = try await client.request("tasks", ["offset": list.count])
            list += batch
            if batch.count < 100 { break }
        } while !closing
        if jobs != list { jobs = list }
    }

    private func updateTask(_ job: JobItem, action: String, values: [String: Any]) async {
        guard !workingJobs.contains(job.id) else { return }
        workingJobs.insert(job.id)
        defer { workingJobs.remove(job.id) }
        var args = values
        args["version"] = job.version
        args["leaseVersion"] = job.leaseVersion
        do { try await client.command(action, args) }
        catch { notice = error.localizedDescription }
        await refresh()
    }
}

let supportedVideoTypes = ["mp4", "mov", "avi"].compactMap { UTType(filenameExtension: $0) }

func timecode(_ milliseconds: Int64) -> String {
    let total = max(0, milliseconds) / 1000
    let hours = total / 3600
    let minutes = (total % 3600) / 60
    let seconds = total % 60
    return hours > 0 ? String(format: "%lld:%02lld:%02lld", hours, minutes, seconds)
        : String(format: "%02lld:%02lld", minutes, seconds)
}

func evidenceTime(_ milliseconds: Int64) -> String {
    let whole = timecode(milliseconds)
    let rest = max(0, milliseconds) % 1000
    return rest == 0 ? whole : String(format: "%@.%03lld", whole, rest)
}
