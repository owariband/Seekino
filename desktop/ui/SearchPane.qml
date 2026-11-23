import QtQuick
import QtQuick.Layouts
import QtQuick.Controls.Basic

Item {
    id: searchPane
    property string scope: ""
    property bool keywords: true
    readonly property bool draft: !workspace.lastQuery && !workspace.searching
    readonly property var examples: ["下雨天有人在门口等待的画面","乐队第一次站上舞台演出","两个人在天台吵架的那一段"]
    signal openHit(string ref, string title)
    signal importRequested()
    signal settingsRequested()
    function focusQuery() { query.forceActiveFocus(); query.selectAll() }
    function run() {
        if (query.text.trim().length && workspace.ready && !workspace.searching)
            workspace.search(query.text.trim(), keywords, scope)
    }
    Flickable {
        id: viewport
        anchors.fill: parent; clip: true; boundsBehavior: Flickable.StopAtBounds
        contentWidth: width
        contentHeight: searchPane.draft ? Math.max(height, content.y+content.height+16) : height
        interactive: searchPane.draft && contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: viewport.interactive ? ScrollBar.AsNeeded : ScrollBar.AlwaysOff }
        ColumnLayout {
        id: content
        width: Math.min(viewport.width - Math.max(32,Math.min(84,viewport.width*.10)),760)
        x: (viewport.width-width)/2
        y: searchPane.draft ? Math.max(15,(viewport.height-height)/2) : 14
        height: searchPane.draft ? implicitHeight : viewport.height-32
        spacing: 8
        ColumnLayout {
            visible: searchPane.draft; Layout.fillWidth: true; spacing: 13
            Kino { Layout.alignment: Qt.AlignHCenter; Layout.preferredWidth: 192; Layout.preferredHeight: 84; mood: "idle" }
            Text {
                Layout.fillWidth: true; color: Studio.muted; horizontalAlignment: Text.AlignHCenter; wrapMode: Text.Wrap
                text: "描述记得的画面、台词或人物关系，一起把那一刻找回来。"; font.pixelSize: 15
            }
            Column {
                id: prompts
                Layout.fillWidth: true; spacing: 8
                // Each wrapped row is centered, as with flex-wrap and justify-content on Web.
                Repeater {
                    model: parent.width >= 370 ? [searchPane.examples.slice(0,2),searchPane.examples.slice(2)] : searchPane.examples.map(text => [text])
                    Row {
                        required property var modelData
                        anchors.horizontalCenter: parent.horizontalCenter; spacing: 8
                        Repeater {
                            model: parent.modelData
                            Tool {
                                required property string modelData
                                text: modelData; textSize: 13; outlined: true; fillColor: Studio.sheet; height: 34
                                onClicked: { query.text=modelData; searchPane.keywords=false; query.forceActiveFocus() }
                            }
                        }
                    }
                }
            }
        }
        Item {
            Layout.fillWidth: true; Layout.topMargin: searchPane.draft ? 10 : 0
            Layout.fillHeight: true; visible: !searchPane.draft
            ColumnLayout {
                anchors.fill: parent; spacing: 10
                Mark { Layout.fillWidth: true; font.pixelSize: 18; font.weight: Font.DemiBold; text: workspace.searching ? "正在搜索…" : "“" + workspace.lastQuery + "”" }
                Mark { text: workspace.searching ? "" : workspace.hits.length + " 个片段" + (workspace.partial ? "   ·   部分视频的索引仍在构建中" : ""); secondary: true; font.pixelSize: 12 }
                Item {
                    Layout.fillWidth: true; Layout.fillHeight: true
                    ColumnLayout {
                        visible: !workspace.hits.length; anchors.centerIn: parent; spacing: 8
                        Kino { Layout.alignment: Qt.AlignHCenter; Layout.preferredWidth: 128; Layout.preferredHeight: 56; mood: workspace.searching ? "loading" : "think" }
                        Mark { Layout.alignment: Qt.AlignHCenter; text: workspace.searching ? "正在查找片段…" : "没有找到片段，换个关键词再试试。"; color: Studio.muted }
                    }
                    ListView {
                        anchors.fill: parent; clip: true; boundsBehavior: Flickable.StopAtBounds; spacing: 8
                        model: workspace.hits; ScrollBar.vertical: ScrollBar {}
                        onModelChanged: positionViewAtBeginning()
                        delegate: DocumentRow {
                            required property var modelData
                            objectName: "searchHit_" + modelData.sourceRef
                            width: ListView.view.width; multiline: true; title: modelData.description || modelData.title
                            detail: Studio.time(modelData.startMs, true) + " – " + Studio.time(modelData.endMs, true) + "     " + modelData.title
                            cover: modelData.cover || ""; onClicked: searchPane.openHit(modelData.sourceRef,modelData.title)
                        }
                    }
                }
            }
        }
        Rectangle {
            id: composer
            Layout.fillWidth: true; Layout.topMargin: 8; Layout.preferredHeight: searchPane.draft ? 116 : 88
            radius: 18; color: Studio.sheet; border.color: query.activeFocus ? Studio.strongLine : Studio.line
            Behavior on border.color { ColorAnimation { duration: Studio.hover } }
            Behavior on Layout.preferredHeight { NumberAnimation { duration: Studio.panel; easing.type: Easing.OutCubic } }
            CardShadow { radius: 18 }
            TextArea {
                id: query; objectName: "queryInput"
                anchors.top: parent.top; anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: controls.top
                leftPadding: 16; rightPadding: 16; topPadding: 13; bottomPadding: 0
                color: Studio.ink; placeholderTextColor: Studio.dim; background: Item {}
                selectionColor: Studio.selected; selectedTextColor: Studio.ink
                selectByMouse: true; wrapMode: TextEdit.Wrap
                placeholderText: searchPane.keywords ? "输入一句台词，或切换到描述搜索…" : "描述一个画面，找回那一刻…"
                Accessible.name: "搜索内容"; font.pixelSize: 14
                Keys.onReturnPressed: event => { if (!(event.modifiers & Qt.ShiftModifier)) searchPane.run(); else event.accepted=false }
                Keys.onEnterPressed: event => { if (!(event.modifiers & Qt.ShiftModifier)) searchPane.run(); else event.accepted=false }
            }
            RowLayout {
                id: controls; anchors.bottom: parent.bottom; anchors.left: parent.left; anchors.right: parent.right
                anchors.leftMargin: 10; anchors.rightMargin: 8; anchors.bottomMargin: 8; spacing: 6
                Tool { glyph: "plus"; hint: "导入视频"; onClicked: searchPane.importRequested() }
                Tool { text: searchPane.keywords ? "关键词" : "描述搜索"; glyph: "down"; hint: "切换关键词与描述搜索"; symbolSize: 11; textSize: 12; onClicked: searchPane.keywords=!searchPane.keywords }
                Item { Layout.fillWidth: true }
                Tool { objectName: "submitSearch"; primary: true; corner: 10; implicitWidth: 34; implicitHeight: 34; glyph: "up"; hint: "搜索"; enabled: query.text.trim().length > 0 && !workspace.searching; onClicked: searchPane.run() }
            }
        }
        RowLayout {
            Layout.fillWidth: true; spacing: 6
            Tool {
                id: scopeButton; text: workspace.series.find(item => item.id === searchPane.scope)?.title || "全部视频系列"
                glyph: "down"; outlined: true; hint: "选择搜索范围"; textSize: 11; symbolSize: 11
                Layout.maximumWidth: 240; clip: true; onClicked: scopeMenu.open()
                Popup {
                    id: scopeMenu; x: 0; y: -height-4
                    width: 210; padding: 5; implicitHeight: Math.min(200,(workspace.series.length+1)*29+10)
                    modal: true; dim: false; focus: true
                    background: Rectangle { color: Studio.sheet; border.color: Studio.line; radius: 12 }
                    contentItem: ListView {
                        clip: true; model: [{id:"",title:"全部视频系列"}].concat(workspace.series); ScrollBar.vertical: ScrollBar {}
                        delegate: TreeRow {
                            required property var modelData; width: ListView.view.width
                            text: modelData.title; current: modelData.id === searchPane.scope; branch: true
                            onClicked: { searchPane.scope=modelData.id; scopeMenu.close() }
                        }
                    }
                }
            }
            Item { Layout.fillWidth: true }
            Tool { text: "配置模型"; glyph: "sliders"; textSize: 12; symbolSize: 14; onClicked: searchPane.settingsRequested() }
        }
        }
    }
}
