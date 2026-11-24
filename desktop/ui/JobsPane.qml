import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts

Item {
    id: pane
    signal searchRequested()
    signal settingsRequested()
    signal inspectVideo(string id, string title)
    property string filter: "all"
    property bool history: false
    property var followed: ({})
    property bool manualRefresh: false
    readonly property var tasks: history ? workspace.jobs : workspace.jobs.filter(task => task.latest)
    readonly property var filters: [
        {key: "all", label: qsTr("全部")}, {key: "queued", label: qsTr("排队中")},
        {key: "running", label: qsTr("处理中")}, {key: "attention", label: qsTr("需处理")},
        {key: "done", label: qsTr("已完成")}
    ]
    readonly property var byId: {
        const found = {}
        for (const job of tasks) found[job.id] = job
        return found
    }

    function group(task) {
        if (task.paused || ["PARSING_FAILED", "INDEXING_FAILED"].includes(task.status)) return "attention"
        if (task.status === "PENDING") return "queued"
        if (["PREPARING", "PARSING", "INDEXING"].includes(task.status)) return "running"
        if (task.status === "COMPLETE") return "done"
        return "other"
    }
    function matches(task, key) { return key === "all" || group(task) === key }
    function count(key) { return tasks.filter(task => matches(task, key)).length }
    function resetView() {
        followed = {}
        syncRows()
        if (list) list.positionViewAtBeginning()
    }
    function syncRows() {
        if (!enabled || !rows) return
        const active = [], changed = []
        const tracking = filter === "queued" || filter === "running" || filter === "attention"
        for (const job of tasks) {
            if (matches(job, filter)) {
                if (tracking) followed[job.id] = true
                active.push({ jobId: job.id, sectionName: "" })
            } else if (tracking && followed[job.id]) {
                changed.push({ jobId: job.id, sectionName: qsTr("本次查看期间已更新") })
            }
        }
        // Keeping delegates for unchanged tasks preserves keyboard focus during polling.
        const next = active.concat(changed)
        const wanted = new Set(next.map(row => row.jobId))
        for (let i = rows.count - 1; i >= 0; --i)
            if (!wanted.has(rows.get(i).jobId)) rows.remove(i)
        for (let i = 0; i < next.length; ++i) {
            if (i >= rows.count || rows.get(i).jobId !== next[i].jobId) {
                let found = i + 1
                while (found < rows.count && rows.get(found).jobId !== next[i].jobId) ++found
                if (found < rows.count) rows.move(found, i, 1)
                else rows.insert(i, next[i])
            }
            if (rows.get(i).sectionName !== next[i].sectionName)
                rows.setProperty(i, "sectionName", next[i].sectionName)
        }
    }
    onTasksChanged: syncRows()
    onFilterChanged: resetView()
    onHistoryChanged: resetView()
    onEnabledChanged: { if (enabled) resetView() }
    Component.onCompleted: syncRows()
    Connections {
        target: workspace
        function onRefreshingChanged() { if (!workspace.refreshing) pane.manualRefresh = false }
    }
    ListModel { id: rows }
    TaskInspector { id: inspector }
    readonly property var historicalVideos: {
        const ids = {}
        for (const job of workspace.jobs) if (!job.latest) ids[job.videoID] = true
        return ids
    }
    Sheet {
        id: confirmJob
        objectName: "confirmJobAction"
        property var target: ({})
        property string action: ""
        readonly property string actionTitle: action === "reparse" ? (target.status === "PENDING" ? qsTr("开始解析") : qsTr("整集重新解析"))
             : action === "deleteImport" ? qsTr("删除视频") : action === "deleteVersion" ? qsTr("删除此历史版本") : qsTr("清理全部历史版本")
        contentItem: ColumnLayout {
            spacing: 14
            Mark { text: confirmJob.actionTitle; font.pixelSize: 18; font.weight: Font.DemiBold }
            Text { Layout.fillWidth: true; wrapMode: Text.Wrap; textFormat: Text.PlainText; color: Studio.ink
                text: (confirmJob.target.title || "") + " · v" + (confirmJob.target.version || 1) }
            Text { Layout.fillWidth: true; wrapMode: Text.Wrap; color: Studio.muted; font.pixelSize: 13
                text: confirmJob.action === "deleteImport" ? qsTr("将从本资料库移除导入的视频、解析记录和检索索引。")
                    : confirmJob.action === "reparse" ? qsTr("将按当前解析配置运行，可能产生模型调用费用。")
                    : qsTr("将清除所选的历史解析记录和对应索引；保留原片与当前版本。")}
            RowLayout {
                Layout.alignment: Qt.AlignRight
                Tool { text: qsTr("取消"); onClicked: confirmJob.close() }
                Tool { objectName: "confirmJobManage"; text: confirmJob.actionTitle; primary: true
                    onClicked: { workspace.manageTask(confirmJob.target.id, confirmJob.action); confirmJob.close() } }
            }
        }
    }
    ColumnLayout {
        anchors.fill: parent
        anchors.leftMargin: pane.width < 580 ? 16 : 24
        anchors.rightMargin: anchors.leftMargin
        anchors.topMargin: pane.height < 480 ? 16 : 24
        spacing: 0
        RowLayout {
            Layout.fillWidth: true
            Mark { Layout.fillWidth: true; text: qsTr("解析任务"); font.pixelSize: Studio.pageTitleSize; font.weight: Font.DemiBold }
            Tool {
                objectName: "refreshJobs"; Layout.preferredWidth: 72
                text: pane.manualRefresh ? qsTr("刷新中…") : qsTr("刷新")
                hint: qsTr("重新读取本机任务状态"); enabled: !pane.manualRefresh
                onClicked: { pane.manualRefresh = true; workspace.refresh() }
            }
        }
        RowLayout {
            Layout.fillWidth: true; Layout.topMargin: 14; spacing: 5
            Mark { text: qsTr("显示范围"); secondary: true; font.pixelSize: 12 }
            Tool {
                objectName: "jobVersionScope"; text: pane.history ? qsTr("全部解析版本") : qsTr("每集最新版本")
                glyph: "down"; symbolSize: 11; textSize: 12
                hint: qsTr("选择任务的解析版本范围"); onClicked: versions.open()
                Menu {
                    id: versions
                    y: parent.height; width: 164
                    palette.text: Studio.ink; palette.windowText: Studio.ink
                    palette.highlight: Studio.selected; palette.highlightedText: Studio.ink
                    background: Rectangle { radius: 7; color: Studio.sheet; border.color: Studio.line }
                    MenuItem {
                        text: qsTr("每集最新版本"); checkable: true; checked: !pane.history
                        onTriggered: pane.history = false
                    }
                    MenuItem {
                        text: qsTr("全部解析版本"); checkable: true; checked: pane.history
                        onTriggered: pane.history = true
                    }
                }
            }
        }
        RowLayout {
            id: tabs; Layout.topMargin: 8; Layout.fillWidth: true; spacing: 0
            Repeater {
                model: pane.filters
                Tool {
                    required property var modelData
                    readonly property bool selected: pane.filter === modelData.key
                    objectName: "taskFilter_" + modelData.key
                    text: modelData.label + " " + pane.count(modelData.key)
                    textSize: 12; corner: 5; Layout.preferredHeight: 34
                    Layout.preferredWidth: Math.min(implicitWidth, tabs.width / pane.filters.length)
                    contentColor: selected ? Studio.ink : Studio.muted
                    Accessible.role: Accessible.PageTab
                    Accessible.selected: selected
                    onClicked: pane.filter = modelData.key
                    Rectangle {
                        anchors.horizontalCenter: parent.horizontalCenter; anchors.bottom: parent.bottom
                        width: parent.width - 14; height: 2; radius: 1
                        color: selected ? Studio.actionBg : "transparent"
                        Behavior on color { ColorAnimation { duration: Studio.hover } }
                    }
                }
            }
            Item { Layout.fillWidth: true }
        }
        Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Studio.line }
        Item {
            Layout.fillWidth: true; Layout.fillHeight: true
            EmptyView {
                anchors.centerIn: parent; width: Math.min(parent.width - 20, 450); visible: rows.count === 0
                glyph: "activity"
                title: pane.tasks.length ? qsTr("没有符合筛选条件的任务") : qsTr("暂无解析任务")
                message: pane.tasks.length ? "" : qsTr("导入视频后，可在这里查看解析和索引进度。")
            }
            ListView {
                id: list; objectName: "taskList"; anchors.fill: parent; clip: true
                model: rows; boundsBehavior: Flickable.StopAtBounds; ScrollBar.vertical: ScrollBar {}
                section.property: "sectionName"
                section.delegate: Item {
                    required property string section
                    width: ListView.view.width; height: section ? 32 : 0
                    Mark { anchors.left: parent.left; anchors.bottom: parent.bottom; text: section; secondary: true; font.pixelSize: 12 }
                }
                delegate: TaskRow {
                    required property string jobId
                    width: ListView.view.width
                    job: pane.byId[jobId] || ({})
                    showVersion: pane.history
                    hasHistory: !!pane.historicalVideos[job.videoID]
                    onManageRequested: (action,target) => {
                        if (action === "details") { inspector.openFor(target); return }
                        confirmJob.target = target; confirmJob.action = action; confirmJob.open()
                    }
                    onSearchRequested: pane.searchRequested()
                    onSettingsRequested: pane.settingsRequested()
                    onInspectVideo: (id,title) => pane.inspectVideo(id,title)
                }
            }
        }
    }
}
