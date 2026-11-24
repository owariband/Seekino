import QtQuick
import QtQuick.Layouts
import QtQuick.Controls.Basic

Item {
    id: searchPane
    property var scopes: []
    function toggleScope(id) {
        if (!id) scopes = []
        else scopes = scopes.includes(id) ? scopes.filter(selected => selected !== id) : scopes.concat([id])
    }
    property bool keywords: true
    readonly property bool draft: !workspace.lastQuery && !workspace.searching
    readonly property var examples: [qsTr("下雨天有人在门口等待的画面"),qsTr("乐队第一次站上舞台演出"),qsTr("两个人在天台吵架的那一段")]
    signal openHit(string ref, string title)
    signal importRequested()
    signal settingsRequested()
    signal jobsRequested()
    signal watchSettingsRequested()
    Sheet {
        id: libraryChat; padding: 0; width: Math.min(600,parent.width - 32); height: Math.min(570,parent.height - 36)
        onOpened: { workspace.openLibraryConversation(searchPane.scopes); chatLoader.item.showPanel() }
        onClosed: workspace.leaveLibraryConversation()
        contentItem: Loader {
            id: chatLoader; active: libraryChat.visible
            sourceComponent: WatchCompanion {
                libraryMode: true; videoTitle: scopeButton.text
                onCloseRequested: libraryChat.close()
                onSettingsRequested: searchPane.watchSettingsRequested()
            }
        }
    }
    onEnabledChanged: if (!enabled) libraryChat.close()
    function focusQuery() { query.forceActiveFocus(); query.selectAll() }
    function run() {
        if (query.text.trim().length && workspace.ready && !workspace.searching)
            workspace.searchScopes(query.text.trim(), keywords, scopes)
    }
    Rectangle { anchors.fill: parent; color: Studio.paper }
    Flickable {
        id: viewport
        anchors.fill: parent; clip: true; boundsBehavior: Flickable.StopAtBounds
        contentWidth: width
        contentHeight: searchPane.draft ? Math.max(height, content.y+content.height+16) : height
        interactive: searchPane.draft && contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: viewport.interactive ? ScrollBar.AsNeeded : ScrollBar.AlwaysOff }
        ColumnLayout {
        id: content
        width: Math.min(viewport.width - (viewport.width < 580 ? 32 : 48), 760)
        x: (viewport.width-width)/2
        y: searchPane.draft ? Math.max(16,(viewport.height-height)/2) : 16
        height: searchPane.draft ? implicitHeight : viewport.height - 2*y
        spacing: 8
        ColumnLayout {
            visible: searchPane.draft; Layout.fillWidth: true; spacing: 13
            Kino { Layout.alignment: Qt.AlignHCenter; Layout.preferredWidth: 192; Layout.preferredHeight: 84; mood: "idle"; dozeWhenIdle: true }
            Text {
                Layout.fillWidth: true; color: Studio.muted; horizontalAlignment: Text.AlignHCenter; wrapMode: Text.Wrap
                text: qsTr("描述记得的画面、台词或人物关系，一起把那一刻找回来。"); font.pixelSize: 15
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
                Mark { Layout.fillWidth: true; font.pixelSize: Studio.pageTitleSize; font.weight: Font.DemiBold; text: workspace.searching ? qsTr("正在搜索…") : "“" + workspace.lastQuery + "”" }
                Mark { visible: !workspace.searching && !workspace.searchIndexUnavailable; text: qsTr("%1 个片段").arg(workspace.hits.length); secondary: true; font.pixelSize: 12 }
                Rectangle {
                    visible: workspace.partial && !workspace.searching
                    Layout.fillWidth: true; implicitHeight: coverage.implicitHeight + 8
                    radius: 7; color: Studio.wash; border.color: Studio.line
                    RowLayout {
                        id: coverage
                        anchors.left: parent.left; anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
                        anchors.leftMargin: 12; anchors.rightMargin: 6; spacing: 6
                        Text {
                            Layout.fillWidth: true; wrapMode: Text.Wrap
                            text: workspace.searchIndexUnavailable ? qsTr("所选范围暂无可检索的索引") : qsTr("本次搜索覆盖不完整")
                            font.pixelSize: 13; color: Studio.ink
                        }
                        Tool {
                            id: coverageButton; objectName: "searchCoverageJobs"; text: qsTr("查看解析任务"); textSize: 12; corner: 5
                            contentColor: Studio.actionBg
                            Keys.onReturnPressed: searchPane.jobsRequested()
                            Keys.onEnterPressed: searchPane.jobsRequested()
                            onClicked: searchPane.jobsRequested()
                        }
                    }
                }
                Item {
                    Layout.fillWidth: true; Layout.fillHeight: true
                    ColumnLayout {
                        visible: !workspace.hits.length; anchors.centerIn: parent; spacing: 8
                        Kino { Layout.alignment: Qt.AlignHCenter; Layout.preferredWidth: 76; Layout.preferredHeight: 56; sceneWidth: 38; ground: false; interactive: false; mood: workspace.searching ? "run" : "think" }
                        Mark { Layout.alignment: Qt.AlignHCenter; text: workspace.searching ? qsTr("正在查找片段…") : workspace.searchIndexUnavailable ? qsTr("索引发布后，可再次搜索。") : workspace.partial ? qsTr("当前可检索范围内暂无命中。")
                              : workspace.series.length ? qsTr("没有找到片段，换个关键词再试试。") : qsTr("资料库里还没有视频。"); color: Studio.muted }
                    }
                    ListView {
                        anchors.fill: parent; clip: true; boundsBehavior: Flickable.StopAtBounds; spacing: 8
                        model: workspace.hits; ScrollBar.vertical: ScrollBar {}
                        onModelChanged: positionViewAtBeginning()
                        delegate: DocumentRow {
                            required property var modelData
                            objectName: "searchHit_" + modelData.sourceRef
                            width: ListView.view.width; title: modelData.description || modelData.title
                            markedTitle: modelData.highlighted
                            sourceLabel: [modelData.seriesTitle, modelData.episode ? qsTr("第 %1 集").arg(modelData.episode) : ""].filter(Boolean).join(" · ")
                            filename: modelData.filename
                            timeRange: Studio.time(modelData.startMs, true) + " – " + Studio.time(modelData.endMs, true)
                            onAsk: workspace.askAboutResult(modelData.sourceRef, modelData.title)
                            preview: modelData.cover || ""; onClicked: searchPane.openHit(modelData.sourceRef,modelData.title)
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
                placeholderText: searchPane.keywords ? qsTr("输入一句台词，或切换到描述搜索…") : qsTr("描述一个画面，找回那一刻…")
                Accessible.name: qsTr("搜索内容"); font.pixelSize: 14
                KeyNavigation.priority: KeyNavigation.BeforeItem
                KeyNavigation.tab: coverageButton.visible ? coverageButton : null
                Keys.onReturnPressed: event => { if (!(event.modifiers & Qt.ShiftModifier)) searchPane.run(); else event.accepted=false }
                Keys.onEnterPressed: event => { if (!(event.modifiers & Qt.ShiftModifier)) searchPane.run(); else event.accepted=false }
            }
            RowLayout {
                id: controls; anchors.bottom: parent.bottom; anchors.left: parent.left; anchors.right: parent.right
                anchors.leftMargin: 10; anchors.rightMargin: 8; anchors.bottomMargin: 8; spacing: 6
                Tool { glyph: "plus"; hint: qsTr("导入视频"); onClicked: searchPane.importRequested() }
                Tool { text: searchPane.keywords ? qsTr("关键词") : qsTr("描述搜索"); glyph: "down"; hint: qsTr("切换关键词与描述搜索"); symbolSize: 11; textSize: 12; onClicked: searchPane.keywords=!searchPane.keywords }
                Item { Layout.fillWidth: true }
                Tool { objectName: "submitSearch"; primary: true; corner: 10; implicitWidth: 34; implicitHeight: 34; glyph: "up"; hint: qsTr("搜索"); enabled: query.text.trim().length > 0 && !workspace.searching; onClicked: searchPane.run() }
            }
        }
        RowLayout {
            Layout.fillWidth: true; spacing: 6
            Tool {
                id: scopeButton; text: searchPane.scopes.length > 1 ? qsTr("已选 ") + searchPane.scopes.length + qsTr(" 个系列") :
                    workspace.series.find(item => item.id === searchPane.scopes[0])?.title || qsTr("全部视频系列")
                glyph: "down"; outlined: true; hint: qsTr("选择搜索范围"); textSize: 11; symbolSize: 11
                Layout.maximumWidth: 240; clip: true; onClicked: scopeMenu.open()
                Popup {
                    id: scopeMenu; x: 0; y: -height-4
                    width: 210; padding: 5; implicitHeight: Math.min(200,(workspace.series.length+1)*29+10)
                    modal: true; dim: false; focus: true
                    background: Rectangle { color: Studio.sheet; border.color: Studio.line; radius: 12 }
                    contentItem: ListView {
                        clip: true; model: [{id:"",title:qsTr("全部视频系列")}].concat(workspace.series); ScrollBar.vertical: ScrollBar {}
                        delegate: TreeRow {
                            required property var modelData; width: ListView.view.width
                            text: modelData.title; current: modelData.id ? searchPane.scopes.includes(modelData.id) : !searchPane.scopes.length; branch: true
                            onClicked: searchPane.toggleScope(modelData.id)
                        }
                    }
                }
            }
            Item { Layout.fillWidth: true }
            Tool { objectName: "libraryConversation"; text: qsTr("问 Kino"); glyph: "chat"; textSize: 12; outlined: true
                enabled: workspace.series.length > 0; onClicked: libraryChat.open() }
            Tool { text: qsTr("配置模型"); glyph: "sliders"; textSize: 12; symbolSize: 14; onClicked: searchPane.settingsRequested() }
        }
        }
    }
}
