import QtQuick
import QtQuick.Layouts
import QtQuick.Controls.Basic

Item {
    id: tasksPane
    property bool history: false
    property string selectedFilter: ""
    readonly property var tasks: history ? workspace.jobs : workspace.jobs.filter(task => task.latest)
    function matches(task, group) {
        if (group === "active") return ["PENDING","PREPARING","PARSING","INDEXING"].includes(task.status)
        if (group === "failed") return ["PARSING_FAILED","INDEXING_FAILED"].includes(task.status)
        if (group === "done") return task.status === "COMPLETE"
        return true
    }
    readonly property string filter: selectedFilter || (tasks.some(task => matches(task,"active")) ? "active" : "all")
    readonly property var filtered: tasks.filter(task => matches(task,filter))
    ColumnLayout {
        anchors.top: parent.top; anchors.bottom: parent.bottom; anchors.topMargin: 25; anchors.bottomMargin: 14
        anchors.horizontalCenter: parent.horizontalCenter; width: Math.min(parent.width-44,1250); spacing: 0
        RowLayout {
            Layout.fillWidth: true
            Mark { Layout.fillWidth: true; text: "解析任务"; font.pixelSize: 26; font.weight: Font.Bold }
            Tool { text: "同步状态"; outlined: true; onClicked: workspace.refresh() }
            Tool { text: tasksPane.history ? "全部版本" : "当前版本"; outlined: true; glyph: "down"; symbolSize: 11; textSize: 12; hint: "切换当前与全部任务版本"; onClicked: tasksPane.history = !tasksPane.history }
        }
        Row {
            Layout.topMargin: 28; spacing: 3
            Repeater {
                model: [{key:"active",name:"进行中"},{key:"failed",name:"需处理"},{key:"done",name:"已完成"},{key:"all",name:"全部"}]
                Tool {
                    required property var modelData
                    text: modelData.name + "  " + tasksPane.tasks.filter(task=>tasksPane.matches(task,modelData.key)).length
                    textSize: 11; corner: height/2; chosen: tasksPane.filter === modelData.key
                    onClicked: tasksPane.selectedFilter=modelData.key
                }
            }
        }
        Item {
            Layout.fillHeight: true; Layout.fillWidth: true; Layout.topMargin: 14
            EmptyView {
                anchors.centerIn: parent; width: Math.min(parent.width,450); visible: tasksPane.filtered.length === 0
                title: tasksPane.tasks.length ? "没有符合筛选条件的任务" : "暂无解析任务"
                message: tasksPane.tasks.length ? "" : "导入后可在这里查看进度、暂停任务或重试。"
            }
            ListView {
                anchors.fill: parent; clip: true; boundsBehavior: Flickable.StopAtBounds; spacing: 14
                model: tasksPane.filtered; ScrollBar.vertical: ScrollBar {}
                delegate: Rectangle {
                    required property var modelData
                    width: ListView.view.width; height: modelData.error ? 112 : 84
                    radius: 14; color: Studio.sheet; border.color: Studio.line
                    RowLayout {
                        anchors.fill: parent; anchors.leftMargin: 16; anchors.rightMargin: 16; spacing: 12
                        Rectangle {
                            Layout.preferredWidth: 42; Layout.preferredHeight: 42; radius: 21; color: "transparent"; border.color: Studio.strongLine
                            Mark { anchors.centerIn: parent; text: String(modelData.episode).padStart(2,"0"); font.pixelSize: 12; font.weight: Font.DemiBold; color: Studio.actionBg }
                        }
                        ColumnLayout {
                            spacing: 5; Layout.fillWidth: true
                            Mark { Layout.fillWidth: true; text: modelData.title; font.weight: Font.DemiBold; font.pixelSize: 15 }
                            Mark { Layout.fillWidth: true; text: modelData.series + "  ·  " + modelData.label + (modelData.elapsedMs > 0 ? "   " + Studio.time(modelData.elapsedMs,false) : ""); color: Studio.actionBg; font.pixelSize: 12 }
                            Rectangle {
                                Layout.fillWidth: true; Layout.preferredHeight: 3; radius: 2; color: Studio.surfaceMuted
                                visible: modelData.canPause && modelData.progress !== null && modelData.progress !== undefined
                                Rectangle { width: parent.width*Math.min(1,Math.max(0,modelData.progress/100)); height: parent.height; radius: 2; color: Studio.accent; Behavior on width { NumberAnimation { duration: Studio.panel } } }
                            }
                            Mark { visible: !!modelData.error; Layout.fillWidth: true; text: modelData.error || ""; color: Studio.danger; font.pixelSize: 12 }
                        }
                        Tool { visible: modelData.canPause; outlined: true; text: modelData.paused ? "继续" : "暂停"; textSize: 12; onClicked: workspace.pauseTask(modelData.id) }
                        Tool { visible: modelData.canRetry; outlined: true; text: "重试"; textSize: 12; onClicked: workspace.retryTask(modelData.id) }
                    }
                }
            }
        }
    }
}
