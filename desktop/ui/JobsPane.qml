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
        {key: "all", label: "全部"}, {key: "queued", label: "排队中"},
        {key: "running", label: "处理中"}, {key: "attention", label: "需处理"},
        {key: "done", label: "已完成"}
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
                changed.push({ jobId: job.id, sectionName: "本次查看期间已更新" })
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
    ColumnLayout {
        anchors.fill: parent
        anchors.leftMargin: pane.width < 580 ? 16 : 24
        anchors.rightMargin: anchors.leftMargin
        anchors.topMargin: pane.height < 480 ? 16 : 24
        spacing: 0
        RowLayout {
            Layout.fillWidth: true
            Mark { Layout.fillWidth: true; text: "解析任务"; font.pixelSize: Studio.pageTitleSize; font.weight: Font.DemiBold }
            Tool {
                objectName: "refreshJobs"; Layout.preferredWidth: 72
                text: pane.manualRefresh ? "刷新中…" : "刷新"
                hint: "重新读取本机任务状态"; enabled: !pane.manualRefresh
                onClicked: { pane.manualRefresh = true; workspace.refresh() }
            }
        }
        RowLayout {
            Layout.fillWidth: true; Layout.topMargin: 14; spacing: 5
            Mark { text: "显示范围"; secondary: true; font.pixelSize: 12 }
            Tool {
                objectName: "jobVersionScope"; text: pane.history ? "全部解析版本" : "每集最新版本"
                glyph: "down"; symbolSize: 11; textSize: 12
                hint: "选择任务的解析版本范围"; onClicked: versions.open()
                Menu {
                    id: versions
                    y: parent.height; width: 164
                    palette.text: Studio.ink; palette.windowText: Studio.ink
                    palette.highlight: Studio.selected; palette.highlightedText: Studio.ink
                    background: Rectangle { radius: 7; color: Studio.sheet; border.color: Studio.line }
                    MenuItem {
                        text: "每集最新版本"; checkable: true; checked: !pane.history
                        onTriggered: pane.history = false
                    }
                    MenuItem {
                        text: "全部解析版本"; checkable: true; checked: pane.history
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
                title: pane.tasks.length ? "没有符合筛选条件的任务" : "暂无解析任务"
                message: pane.tasks.length ? "" : "导入视频后，可在这里查看解析和索引进度。"
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
                    onSearchRequested: pane.searchRequested()
                    onSettingsRequested: pane.settingsRequested()
                    onInspectVideo: (id,title) => pane.inspectVideo(id,title)
                }
            }
        }
    }
}
