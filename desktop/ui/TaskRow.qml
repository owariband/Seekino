import QtQuick
import QtQuick.Layouts

Item {
    id: row
    required property var job
    property bool showVersion: false
    signal searchRequested()
    signal settingsRequested()
    signal inspectVideo(string id, string title)
    readonly property bool failed: ["PARSING_FAILED", "INDEXING_FAILED"].includes(job.status)
    readonly property bool done: job.status === "COMPLETE"
    readonly property bool queued: job.status === "PENDING"
    readonly property bool knownProgress: typeof job.progress === "number" && Number.isFinite(job.progress)
                                         && job.progress >= 0 && job.progress <= 100
    readonly property var phases: ({
        "PENDING": ["排队中", "等待自动开始解析"],
        "PREPARING": ["准备中", "准备视频和音轨"],
        "PARSING": ["解析中", "解析画面与对白"],
        "INDEXING": ["建索引中", "将片段写入检索索引"],
        "COMPLETE": ["已完成", "片段已可搜索"],
        "PARSING_FAILED": ["解析失败", "视频解析未完成"],
        "INDEXING_FAILED": ["索引失败", "尚未发布检索索引"]
    })
    readonly property var phase: phases[job.status] || ["状态未知", ""]
    readonly property string statusLabel: job.paused ? "已暂停" : phase[0]
    readonly property string explanation: job.paused ? "任务暂停在“" + phase[0] + "”阶段"
        : (failed && job.error ? job.error : phase[1])
    readonly property color tone: job.paused ? Studio.mix("#835d17", "#e8c180")
        : failed ? Studio.danger : done ? Studio.actionBg : queued ? Studio.muted : Studio.mix("#35659b", "#9bc0ef")
    height: content.implicitHeight + 29
    ColumnLayout {
        id: content
        anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top
        anchors.topMargin: 14; anchors.rightMargin: 8; spacing: 3
        RowLayout {
            Layout.fillWidth: true; spacing: 12
            Mark {
                Layout.fillWidth: true; text: row.job.title || ""; font.pixelSize: 15; font.weight: Font.DemiBold
            }
            Rectangle {
                implicitWidth: status.implicitWidth + 16; implicitHeight: 24; radius: 5
                color: Qt.rgba(row.tone.r, row.tone.g, row.tone.b, Studio.dark ? .14 : .10)
                Mark {
                    id: status; objectName: "jobStatus_" + row.job.id
                    anchors.centerIn: parent; text: row.statusLabel
                    font.pixelSize: 12; font.weight: Font.DemiBold; color: row.tone
                }
                Accessible.role: Accessible.StaticText; Accessible.name: row.statusLabel
            }
        }
        Mark {
            Layout.fillWidth: true; secondary: true; font.pixelSize: 12
            text: [row.job.series, row.job.episode ? "第 " + row.job.episode + " 集" : "",
                   row.showVersion ? (row.job.latest ? "最新版本" : "历史版本") + " v" + row.job.version : "",
                   row.job.elapsedMs > 0 ? "处理用时 " + Studio.time(row.job.elapsedMs, false) : ""].filter(Boolean).join("  ·  ")
        }
        RowLayout {
            Layout.fillWidth: true; Layout.topMargin: 4; spacing: 8
            Text {
                objectName: "jobStage_" + row.job.id
                Layout.fillWidth: true; wrapMode: Text.Wrap
                color: Studio.ink; font.pixelSize: 13; text: row.explanation
            }
            Mark {
                objectName: "jobPercent_" + row.job.id
                visible: row.knownProgress && !row.failed; secondary: true; font.pixelSize: 12
                text: row.knownProgress ? Number(row.job.progress.toFixed(1)) + "%" : ""
                Accessible.name: "任务总进度 " + text
            }
            Tool {
                visible: row.job.canPause === true
                objectName: "jobPause_" + row.job.id
                text: row.job.paused ? "继续任务" : row.queued ? "暂缓开始" : "暂停任务"
                glyph: row.job.paused ? "play" : "pause"; symbolSize: 12; textSize: 12; corner: 5
                outlined: true; onClicked: workspace.pauseTask(row.job.id)
            }
            Tool {
                visible: row.done && row.job.latest === true
                objectName: "jobSearch_" + row.job.id; text: "搜索片段"; glyph: "search"; symbolSize: 12
                textSize: 12; corner: 5; onClicked: row.searchRequested()
            }
        }
        RowLayout {
            visible: row.failed && (row.job.canRetry || row.job.errorAction)
            Layout.fillWidth: true; Layout.topMargin: 3; spacing: 5
            Tool {
                visible: row.job.errorAction === "settings" || row.job.errorAction === "source"
                text: row.job.errorAction === "settings" ? "配置模型凭据" : "检查原片"
                objectName: "jobInspect_" + row.job.id
                textSize: 12; corner: 5; outlined: true
                onClicked: row.job.errorAction === "settings" ? row.settingsRequested() : row.inspectVideo(row.job.videoID, row.job.title)
            }
            Tool {
                visible: row.job.canRetry === true
                objectName: "jobRetry_" + row.job.id; text: "重试"; textSize: 12; corner: 5
                outlined: !row.job.errorAction; onClicked: workspace.retryTask(row.job.id)
            }
            Item { Layout.fillWidth: true }
        }
        Rectangle {
            visible: row.knownProgress && !row.failed && !row.done
            Layout.fillWidth: true; Layout.preferredHeight: 3; Layout.topMargin: 3
            radius: 1.5; color: Studio.surfaceMuted
            Rectangle {
                width: parent.width * (row.knownProgress ? row.job.progress / 100 : 0)
                height: parent.height; radius: parent.radius; color: row.tone
                Behavior on width { NumberAnimation { duration: Studio.panel; easing.type: Easing.OutCubic } }
            }
        }
        Mark {
            visible: !!row.job.error && !row.failed; Layout.fillWidth: true
            color: Studio.muted; font.pixelSize: 12; text: visible ? "上次尝试 · " + row.job.error : ""
        }
    }
    Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: Studio.line }
}
