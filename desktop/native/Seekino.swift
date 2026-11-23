import AppKit
import SwiftUI

@MainActor
final class AppLifecycle: NSObject, NSApplicationDelegate {
    weak var workspace: Workspace?
    private var terminating = false

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let workspace else { return .terminateNow }
        guard !terminating else { return .terminateLater }
        terminating = true
        Task {
            await workspace.shutdown()
            sender.reply(toApplicationShouldTerminate: true)
        }
        return .terminateLater
    }
}

struct SeekinoWindow: App {
    @NSApplicationDelegateAdaptor(AppLifecycle.self) private var lifecycle
    @StateObject private var workspace = Workspace(root: SeekinoMain.libraryURL)
    @AppStorage("appearance") private var appearance = "system"

    var body: some Scene {
        Window("Seekino", id: "seekino-workspace") {
            WorkspaceView(model: workspace)
                .preferredColorScheme(appearance == "system" ? nil : appearance == "dark" ? .dark : .light)
                .onAppear { lifecycle.workspace = workspace }
        }
        .defaultSize(width: 1220, height: 766)
        .commands {
            CommandGroup(replacing: .newItem) {
                Button("导入视频…") { workspace.choosingFiles = true }
                    .keyboardShortcut("o").disabled(!workspace.opened || workspace.importStatus != nil)
                Button("新建系列…") { workspace.showingNewSeries = true }
                    .keyboardShortcut("n").disabled(!workspace.opened)
            }
            CommandGroup(replacing: .appSettings) {
                Button("设置…") { workspace.showingSettings = true }.keyboardShortcut(",")
            }
            CommandMenu("查找") {
                Button("搜索视频") { workspace.focusSearch() }.keyboardShortcut("k")
                Button("刷新资料库") { Task { await workspace.refresh() } }.keyboardShortcut("r")
            }
        }
    }
}

@main
struct SeekinoMain {
    static var libraryURL: URL {
        let args = CommandLine.arguments
        if let index = args.firstIndex(of: "--library"), index + 1 < args.count {
            return URL(fileURLWithPath: args[index + 1], isDirectory: true)
        }
        return URL.applicationSupportDirectory.appending(path: "Seekino", directoryHint: .isDirectory)
    }

    @MainActor
    static func main() {
        let args = CommandLine.arguments
        if args.count == 4 && args[1] == "--verify-library" {
            Task { @MainActor in
                do {
                    try await DesktopVerification.run(root: args[2], query: args[3])
                    exit(EXIT_SUCCESS)
                } catch {
                    fputs("\(error.localizedDescription)\n", stderr)
                    exit(EXIT_FAILURE)
                }
            }
            RunLoop.main.run()
        } else {
            SeekinoWindow.main()
        }
    }
}
