import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts

FocusScope {
    id: finder
    objectName: "episodeFind"
    visible: false
    width: Math.min(parent.width - 24, 450)
    height: Math.min(parent.height - 28, body.implicitHeight + 24)
    readonly property var result: workspace.episodeFind || ({})
    readonly property var matches: result.matches || []
    property int selected: -1
    property bool smart: false
    signal seekRequested(int position)
    signal dismissed()
    function openFind() {
        visible = true
        input.forceActiveFocus()
        input.selectAll()
    }
    function dismiss() {
        visible = false
        smart = false
        pending.stop()
        input.clear()
        workspace.closeEpisodeFind()
        dismissed()
    }
    function choose(index) {
        if (index < 0 || index >= matches.length) return
        selected = index
        list.positionViewAtIndex(index, ListView.Contain)
        seekRequested(matches[index].positionMs)
    }
    function step(delta) {
        if (!matches.length) return
        choose(selected < 0 ? (delta > 0 ? 0 : matches.length - 1)
                             : (selected + delta + matches.length) % matches.length)
    }
    function search() {
        selected = -1
        workspace.findEpisode(input.text.trim(), Studio.preferences.episodeVisualFind, smart)
    }
    // Consume pointer events on the panel before they reach the video.
    Rectangle {
        anchors.fill: parent; radius: 12; color: Studio.sidebar; border.color: Studio.line
        MouseArea { anchors.fill: parent }
    }
    Timer { id: pending; interval: finder.smart ? 400 : 230; onTriggered: if (finder.visible) finder.search() }
    ColumnLayout {
        id: body; anchors.fill: parent; anchors.margins: 12; spacing: 6
        RowLayout {
            Layout.fillWidth: true; spacing: 2
            Entry {
                id: input; objectName: "episodeFindInput"; Layout.fillWidth: true
                placeholderText: qsTr("在本集查找台词、画面文字或画面")
                Accessible.name: qsTr("在本集查找")
                maximumLength: 500
                onTextChanged: {
                    if (!finder.visible) return
                    workspace.findEpisode("", false, false)
                    if (text.trim()) pending.restart()
                }
                Keys.onPressed: event => {
                    if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
                        if (pending.running) { pending.stop(); finder.search() }
                        else finder.step(event.modifiers & Qt.ShiftModifier ? -1 : 1)
                        event.accepted = true
                    } else if (event.key === Qt.Key_Down || event.key === Qt.Key_Up) {
                        finder.step(event.key === Qt.Key_Down ? 1 : -1); event.accepted = true
                    }
                }
            }
            Tool { glyph: "left"; hint: qsTr("上一个匹配"); enabled: !!finder.matches.length; onClicked: finder.step(-1) }
            Tool { glyph: "chevron"; hint: qsTr("下一个匹配"); enabled: !!finder.matches.length; onClicked: finder.step(1) }
            Tool { glyph: "close"; hint: qsTr("关闭本集查找"); onClicked: finder.dismiss() }
        }
        RowLayout {
            Layout.fillWidth: true
            Tool {
                text: qsTr("包括画面"); chosen: Studio.preferences.episodeVisualFind; hint: qsTr("包括画面和相关画面")
                onClicked: { Studio.preferences.episodeVisualFind = !Studio.preferences.episodeVisualFind; finder.search() }
            }
            Tool {
                objectName: "episodeSmartFind"; text: qsTr("智能搜索"); chosen: finder.smart; hint: qsTr("用 Kino 模型纠正搜索词")
                onClicked: { finder.smart = !finder.smart; finder.search(); input.forceActiveFocus() }
            }
            Tool {
                text: qsTr("重试"); visible: !!finder.result.error || !!finder.result.missing || !!finder.result.relatedUnavailable || !!finder.result.interpretUnavailable
                onClicked: { workspace.closeEpisodeFind(); finder.search() }
            }
            Mark { Layout.fillWidth: true; horizontalAlignment: Text.AlignRight; secondary: true; font.pixelSize: 12
                text: finder.result.error || (finder.result.loading ? qsTr("查找中…") : finder.matches.length ?
                    (finder.selected >= 0 ? (finder.selected+1) + " / " : "") + finder.matches.length + qsTr(" 处匹配") :
                    finder.result.missing ? qsTr("本集解析结果尚未就绪") : input.text.trim() && !pending.running ? qsTr("没有匹配的结果") : "") }
        }
        Mark {
            Layout.fillWidth: true; visible: !!finder.result.interpreting || !!finder.result.interpretUnavailable || !!finder.result.expansions
            text: finder.result.interpreting ? qsTr("正在理解搜索词…") : finder.result.interpretUnavailable ?
                qsTr("智能搜索暂不可用，已按原词查找") : qsTr("纠正 · ") + (finder.result.expansions || []).join("、")
            secondary: true; font.pixelSize: 12
        }
        ListView {
            id: list; objectName: "episodeFindMatches"; Layout.fillWidth: true
            visible: count > 0
            Layout.preferredHeight: Math.min(200, count * 38, Math.max(38, finder.parent.height - 200))
            clip: true; boundsBehavior: Flickable.StopAtBounds; model: finder.matches
            ScrollBar.vertical: ScrollBar {}
            delegate: ItemDelegate {
                required property var modelData; required property int index
                objectName: "episodeMatch" + index; width: ListView.view.width; height: 38; hoverEnabled: true
                Accessible.name: timeLabel.text + " " + modelData.text
                onClicked: finder.choose(index)
                background: Rectangle { radius: 5; color: finder.selected === index || parent.hovered ? Studio.selected : "transparent" }
                contentItem: RowLayout {
                    spacing: 8
                    Mark { id: timeLabel; text: Studio.time(modelData.positionMs,false); color: Studio.accent; font.pixelSize: 12 }
                    Mark { text: ({"dialogue":qsTr("台词"),"screen_text":qsTr("画面文字"),"visual":qsTr("画面"),"related":qsTr("相关画面")})[modelData.source]; secondary: true; font.pixelSize: 12 }
                    Mark { text: modelData.text; textFormat: Text.PlainText; Layout.fillWidth: true }
                }
            }
        }
        Mark { visible: finder.result.relatedUnavailable === true; font.pixelSize: 12; secondary: true
            text: qsTr("相关画面检索暂不可用") }
    }
}
