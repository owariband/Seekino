import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts

Sheet {
    id: sheet; objectName: "taskInspector"
    width: Math.min(660, parent.width - 32)
    height: Math.min(610, parent.height - 32)
    property string taskID: ""
    property var detail: ({})
    property var consumption: ({})
    property bool showUsage: false
    property int segmentPage: 0
    readonly property int pageSize: 15
    readonly property var segments: consumption.segments || []
    property bool busy: false
    property string problem: ""
    property int revision: 0
    readonly property var progress: detail.facts || ({})
    readonly property var timing: detail.timing || ({})
    function openFor(task) {
        detail = task; taskID = task.id; consumption = ({}); showUsage = false; segmentPage = 0
        open(); reload()
    }
    function reload() {
        if (!visible) return
        busy = true; problem = ""
        workspace.loadTaskPanel(taskID, showUsage ? "taskUsage" : "taskDetail", ++revision)
    }
    function setSection(usage) {
        if (showUsage !== usage) { showUsage = usage; reload() }
    }
    function pair(a, b) { return a === undefined || b === undefined ? "—" : a + " / " + b }
    Timer {
        running: sheet.visible && !sheet.busy && !sheet.problem
        interval: sheet.showUsage ? 60000 : 8000
        repeat: true; onTriggered: sheet.reload()
    }
    Connections {
        target: workspace
        function onTaskPanelLoaded(id, section, generation, value, error) {
            if (!sheet.visible || id !== sheet.taskID || sheet.revision !== generation) return
            sheet.busy = false; sheet.problem = error
            if (!error) {
                if (section === "taskUsage") {
                    sheet.consumption = value
                    sheet.segmentPage = Math.max(0, Math.min(sheet.segmentPage, Math.ceil(sheet.segments.length / sheet.pageSize) - 1))
                }
                else sheet.detail = value
            }
        }
    }
    contentItem: ColumnLayout {
        spacing: 12
        RowLayout {
            Layout.fillWidth: true
            Mark { Layout.fillWidth: true; text: sheet.detail.title || qsTr("任务详情"); font.pixelSize: 20; font.weight: Font.DemiBold }
            Tool { glyph: "close"; hint: qsTr("关闭"); onClicked: sheet.close() }
        }
        RowLayout {
            Layout.fillWidth: true; spacing: 6
            Tool { text: qsTr("解析进度"); chosen: !sheet.showUsage; onClicked: sheet.setSection(false) }
            Tool { objectName: "taskUsageTab"; text: qsTr("模型消耗"); chosen: sheet.showUsage; onClicked: sheet.setSection(true) }
            Item { Layout.fillWidth: true }
            Mark { secondary: true; text: "v" + (sheet.detail.version || 1) }
        }
        ScrollView {
            id: scroll
            Layout.fillWidth: true; Layout.fillHeight: true; clip: true; contentWidth: availableWidth
            ColumnLayout {
                width: scroll.availableWidth; spacing: 10
                ColumnLayout {
                    visible: !sheet.showUsage; spacing: 10; Layout.fillWidth: true
                    Repeater {
                        model: [
                            [qsTr("固定切片"), sheet.detail.chunks === undefined ? "—" : String(sheet.detail.chunks)],
                            [qsTr("理解"), sheet.pair(sheet.progress.completedChunks, sheet.progress.totalChunks)],
                            ["Refine", sheet.pair(sheet.progress.completedRefines, sheet.progress.totalRefines)],
                            [qsTr("索引发布"), sheet.pair(sheet.progress.indexedChunks, sheet.progress.totalChunks)],
                            [qsTr("完成单元"), sheet.pair(sheet.progress.completedUnits, sheet.progress.totalUnits)],
                            [qsTr("有效解析时长"), sheet.timing.recorded ? Studio.time(sheet.timing.effectiveDurationMs, false) : "—"],
                            [qsTr("更新于"), sheet.detail.updatedAt ? new Date(sheet.detail.updatedAt).toLocaleString() : "—"]
                        ]
                        RowLayout {
                            required property var modelData
                            Layout.fillWidth: true
                            Mark { Layout.fillWidth: true; secondary: true; text: modelData[0] }
                            Mark { text: modelData[1] }
                        }
                    }
                    Text {
                        Layout.fillWidth: true; color: Studio.danger; visible: !!text
                        text: Studio.translate(sheet.detail.error || sheet.problem || "")
                        textFormat: Text.PlainText; wrapMode: Text.Wrap
                    }
                    Tool { text: qsTr("刷新"); enabled: !sheet.busy; onClicked: sheet.reload() }
                }
                UsageReport {
                    id: usage; visible: sheet.showUsage; Layout.fillWidth: true
                    taskScoped: true; overview: sheet.consumption; loading: sheet.busy; lookupError: sheet.problem
                    onRefreshRequested: sheet.reload()
                }
                ColumnLayout {
                    visible: sheet.showUsage && sheet.segments.length > 0
                    Layout.fillWidth: true; spacing: 8
                    RowLayout {
                        Layout.fillWidth: true
                        Mark { Layout.fillWidth: true; text: qsTr("分段用量"); font.weight: Font.DemiBold }
                        Mark { secondary: true; visible: sheet.segments.length > sheet.pageSize
                            text: (sheet.segmentPage + 1) + " / " + Math.ceil(sheet.segments.length / sheet.pageSize) }
                        Tool { visible: sheet.segments.length > sheet.pageSize; enabled: sheet.segmentPage > 0
                            glyph: "left"; hint: qsTr("上一页"); onClicked: --sheet.segmentPage }
                        Tool { visible: sheet.segments.length > sheet.pageSize; enabled: (sheet.segmentPage+1)*sheet.pageSize < sheet.segments.length
                            glyph: "chevron"; hint: qsTr("下一页"); onClicked: ++sheet.segmentPage }
                    }
                    Repeater {
                        model: sheet.segments.slice(sheet.segmentPage * sheet.pageSize, (sheet.segmentPage+1) * sheet.pageSize)
                        RowLayout {
                            required property var modelData
                            Layout.fillWidth: true
                            Mark {
                                Layout.fillWidth: true
                                text: modelData.sequence === null ? qsTr("未关联单个切片") : qsTr("第 %1 段").arg(modelData.sequence + 1)
                            }
                            Mark {
                                secondary: true
                                text: qsTr("%1 次 · %2 Token · %3").arg(usage.group(modelData.callCount))
                                    .arg(modelData.totalTokens === null ? qsTr("已知 %1").arg(usage.group(modelData.knownTokens)) : usage.group(modelData.totalTokens))
                                    .arg(usage.costLabel(modelData.cost))
                            }
                        }
                    }
                }
            }
        }
    }
}
