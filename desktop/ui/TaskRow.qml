import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts

Item {
    id: row
    required property var job
    property bool showVersion: false
    signal manageRequested(string action, var target)
    property bool hasHistory: false
    signal searchRequested()
    signal settingsRequested()
    signal inspectVideo(string id, string title)
    readonly property bool failed: ["PARSING_FAILED", "INDEXING_FAILED"].includes(job.status)
    readonly property bool done: job.status === "COMPLETE"
    readonly property bool queued: job.status === "PENDING"
    readonly property bool knownProgress: typeof job.progress === "number" && Number.isFinite(job.progress)
                                         && job.progress >= 0 && job.progress <= 100
    readonly property var phases: ({
        "PENDING": [qsTr("排队中"), qsTr("等待自动开始解析")],
        "PREPARING": [qsTr("准备中"), qsTr("准备视频和音轨")],
        "PARSING": [qsTr("解析中"), qsTr("解析画面与对白")],
        "INDEXING": [qsTr("建索引中"), qsTr("将片段写入检索索引")],
        "COMPLETE": [qsTr("已完成"), qsTr("片段已可搜索")],
        "PARSING_FAILED": [qsTr("解析失败"), qsTr("视频解析未完成")],
        "INDEXING_FAILED": [qsTr("索引失败"), qsTr("尚未发布检索索引")]
    })
    readonly property var phase: phases[job.status] || [qsTr("状态未知"), ""]
    readonly property string statusLabel: job.paused ? qsTr("已暂停") : phase[0]
    readonly property string explanation: job.paused ? qsTr("任务暂停在“%1”阶段").arg(phase[0])
        : (failed && job.error ? Studio.translate(job.error) : phase[1])
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
            text: [row.job.series, row.job.episode ? qsTr("第 %1 集").arg(row.job.episode) : "",
                   row.showVersion ? (row.job.latest ? qsTr("最新版本") : qsTr("历史版本")) + " v" + row.job.version : "",
                   row.job.elapsedMs > 0 ? qsTr("处理用时 %1").arg(Studio.time(row.job.elapsedMs, false)) : ""].filter(Boolean).join("  ·  ")
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
                Accessible.name: qsTr("任务总进度 %1").arg(text)
            }
            Tool {
                visible: row.job.canPause === true
                objectName: "jobPause_" + row.job.id
                text: row.job.paused ? qsTr("继续任务") : row.queued ? qsTr("暂缓开始") : qsTr("暂停任务")
                glyph: row.job.paused ? "play" : "pause"; symbolSize: 12; textSize: 12; corner: 5
                outlined: true; onClicked: workspace.pauseTask(row.job.id)
            }
            Tool {
                visible: row.done && row.job.latest === true
                objectName: "jobSearch_" + row.job.id; text: qsTr("搜索片段"); glyph: "search"; symbolSize: 12
                textSize: 12; corner: 5; onClicked: row.searchRequested()
            }
            Tool {
                id: more
                objectName: "jobActions_" + row.job.id; glyph: "more"; hint: qsTr("任务操作")
                onClicked: actions.open()
                Menu {
                    id: actions; y: more.height; width: 166
                    palette.text: Studio.ink; palette.windowText: Studio.ink
                    palette.highlight: Studio.selected; palette.highlightedText: Studio.ink
                    background: Rectangle { radius: 7; color: Studio.sheet; border.color: Studio.line }
                    MenuItem { objectName: "inspectTask_" + row.job.id; text: qsTr("查看任务详情…"); onTriggered: row.manageRequested("details", row.job) }
                    MenuSeparator {}
                    MenuItem { text: row.queued ? qsTr("开始解析…") : qsTr("整集重新解析…"); enabled: row.job.canReparse === true
                        onTriggered: row.manageRequested("reparse", row.job) }
                    MenuItem { text: qsTr("删除视频…"); enabled: row.job.canDelete === true
                        onTriggered: row.manageRequested("deleteImport", row.job) }
                    MenuItem { text: qsTr("删除此历史版本…"); enabled: row.job.canDeleteVersion === true
                        onTriggered: row.manageRequested("deleteVersion", row.job) }
                    MenuItem { text: qsTr("清理全部历史版本…"); enabled: row.hasHistory && row.job.latest === true
                        onTriggered: row.manageRequested("deleteHistory", row.job) }
                }
            }
        }
        RowLayout {
            visible: row.failed && (row.job.canRetry || row.job.errorAction)
            Layout.fillWidth: true; Layout.topMargin: 3; spacing: 5
            Tool {
                visible: row.job.errorAction === "settings" || row.job.errorAction === "source"
                text: row.job.errorAction === "settings" ? qsTr("配置模型凭据") : qsTr("检查原片")
                objectName: "jobInspect_" + row.job.id
                textSize: 12; corner: 5; outlined: true
                onClicked: row.job.errorAction === "settings" ? row.settingsRequested() : row.inspectVideo(row.job.videoID, row.job.title)
            }
            Tool {
                visible: row.job.canRetry === true
                objectName: "jobRetry_" + row.job.id; text: qsTr("重试"); textSize: 12; corner: 5
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
            color: Studio.muted; font.pixelSize: 12; text: visible ? qsTr("上次尝试 · %1").arg(Studio.translate(row.job.error)) : ""
        }
    }
    Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: Studio.line }
}
