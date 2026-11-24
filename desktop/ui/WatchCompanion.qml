import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import QtCore
import QtQuick.Dialogs

Item {
    id: companion
    property bool libraryMode: false
    property var episodes: []
    property int currentEpisode: -1
    property bool showingEpisodes: false
    readonly property bool resizing: dockGrip.pressed
    signal episodeRequested(int index)
    signal closeRequested()
    property string videoTitle: ""
    property int positionMs: 0
    property bool opened: false
    property url pendingImage: ""
    FileDialog {
        id: pickImage; title: qsTr("添加参考图片")
        nameFilters: [qsTr("参考图片 (*.jpg *.jpeg *.png *.webp *.JPG *.JPEG *.PNG *.WEBP)")]
        onAccepted: { companion.pendingImage = selectedFile; companion.focusQuestion() }
    }
    Sheet {
        id: conversations; objectName: "watchThreadHistory"; width: Math.min(372,parent.width-24)
        contentItem: ColumnLayout {
            spacing: 10
            RowLayout {
                Layout.fillWidth: true
                Mark { text: companion.libraryMode ? qsTr("素材库对话") : qsTr("本片对话"); font.pixelSize: 17; font.weight: Font.DemiBold; Layout.fillWidth: true }
                Tool { text: qsTr("新对话"); glyph: "plus"; onClicked: {
                    workspace.selectWatchThread("",false); question.clear(); companion.pendingImage=""; conversations.close()
                } }
            }
            ListView {
                Layout.fillWidth: true; Layout.preferredHeight: Math.min(240, Math.max(44, contentHeight))
                clip: true; model: workspace.watchThreads; ScrollBar.vertical: ScrollBar {}
                delegate: RowLayout {
                    required property var modelData; width: ListView.view.width; height: 38
                    Button {
                        Layout.fillWidth: true; Layout.fillHeight: true
                        Accessible.name: modelData.title; hoverEnabled: true
                        contentItem: Mark { text: modelData.title; textFormat: Text.PlainText }
                        background: Rectangle { radius: 5; color: parent.hovered || workspace.watchThreadID === modelData.id ? Studio.selected : "transparent" }
                        onClicked: { workspace.selectWatchThread(modelData.id,false); question.clear(); companion.pendingImage=""; conversations.close() }
                    }
                    Tool { glyph: "close"; hint: qsTr("删除对话"); onClicked: {
                        removeThread.identifier = modelData.id; removeThread.open()
                    } }
                }
                Mark { anchors.centerIn: parent; visible: !workspace.watchThreads.length; text: qsTr("还没有对话"); secondary: true }
            }
            Tool { Layout.alignment: Qt.AlignRight; text: qsTr("完成"); onClicked: conversations.close() }
        }
    }
    Sheet {
        id: removeThread; property string identifier: ""
        contentItem: ColumnLayout {
            spacing: 12
            Mark { text: qsTr("删除这段对话和其中的所有消息？") }
            RowLayout { Layout.alignment: Qt.AlignRight
                Tool { text: qsTr("取消"); onClicked: removeThread.close() }
                Tool { text: qsTr("删除"); onClicked: { workspace.selectWatchThread(removeThread.identifier,true); removeThread.close(); conversations.close() } }
            }
        }
    }
    readonly property bool hasPanelFocus: opened && panel.activeFocus
    readonly property bool entryFocused: entry.activeFocus
    readonly property bool compact: width <= 800
    readonly property bool floating: !compact && !preferences.docked
    readonly property real reservedWidth: opened && !compact && !floating ? panel.width : 0
    readonly property real reservedHeight: opened && compact ? panel.height : 0
    signal settingsRequested()
    signal questionSent()
    signal seekRequested(int position)

    Settings {
        id: preferences
        category: "watch-companion"
        property bool docked: false
        property real dockWidth: 440
        property bool leftEdge: false
        property real entryBottom: 78
    }
    function clamp(value, min, max) { return Math.max(min, Math.min(value, Math.max(min, max))) }
    function focusQuestion() { if (opened) { showingEpisodes = false; question.forceActiveFocus(Qt.OtherFocusReason) } }
    function showPanel() {
        opened = true
        Qt.callLater(focusQuestion)
    }
    function dismiss() {
        if (libraryMode) { closeRequested(); return }
        opened = false
        entry.forceActiveFocus(Qt.OtherFocusReason)
    }
    function reset() {
        opened = false
        question.clear()
        showingEpisodes = false
        pendingImage = ""
        panel.customX = -1
        panel.customY = -1
    }
    function send() {
        const text = question.text.trim()
        if (!text || workspace.watchBusy) return
        if (!workspace.watchModelReady) { settingsRequested(); return }
        if (workspace.askWatch(text, positionMs, pendingImage)) {
            question.clear()
            pendingImage = ""
            questionSent()
        }
    }

    component MoveGrip: MouseArea {
        required property Item subject
        property point pressPoint
        property point origin
        property point proposed
        property bool moving: false
        signal relocated(real left, real top)
        signal tapped()
        hoverEnabled: true
        cursorShape: moving ? Qt.ClosedHandCursor : Qt.OpenHandCursor
        onPressed: mouse => {
            pressPoint = mapToItem(companion, mouse.x, mouse.y)
            origin = Qt.point(subject.x, subject.y)
            proposed = origin
        }
        onPositionChanged: mouse => {
            if (!pressed) return
            const current = mapToItem(companion, mouse.x, mouse.y)
            const dx = current.x - pressPoint.x, dy = current.y - pressPoint.y
            if (!moving && Math.hypot(dx, dy) < 5) return
            proposed = Qt.point(origin.x + dx, origin.y + dy)
            moving = true
        }
        onReleased: {
            if (moving) relocated(proposed.x, proposed.y)
            else if (containsMouse) tapped()
            moving = false
        }
        onCanceled: moving = false
    }

    AbstractButton {
        id: entry
        objectName: "kinoEntry"
        visible: !companion.opened && !companion.libraryMode
        width: 72; height: 56
        x: entryMove.moving ? companion.clamp(entryMove.proposed.x, 0, parent.width - width)
                            : preferences.leftEdge ? 12 : parent.width - width - 12
        y: entryMove.moving ? companion.clamp(entryMove.proposed.y, 4, parent.height - height - 62)
                            : companion.clamp(parent.height - height - preferences.entryBottom, 4, parent.height - height - 62)
        focusPolicy: Qt.StrongFocus
        hoverEnabled: true
        Accessible.name: qsTr("与 Kino 对话")
        ToolTip.visible: entryMove.containsMouse && !entryMove.moving
        ToolTip.text: qsTr("与 Kino 对话"); ToolTip.delay: 500
        onClicked: companion.showPanel()
        MoveGrip {
            id: entryMove; anchors.fill: parent; subject: entry; z: 2
            onTapped: companion.showPanel()
            onRelocated: (left, top) => {
                preferences.leftEdge = left + entry.width / 2 < companion.width / 2
                preferences.entryBottom = companion.clamp(companion.height - entry.height - top, 62, companion.height - entry.height - 4)
            }
        }
        background: Rectangle {
            radius: 12; color: entryMove.containsMouse ? "#8021322d" : "transparent"
            border.width: entry.visualFocus ? 2 : 0; border.color: Studio.accent
            Behavior on color { ColorAnimation { duration: Studio.hover } }
        }
        contentItem: Item {
            clip: true
            // The Web idle pose occupies the middle 18 x 15 pixels of the 64 x 28 Kino sprite.
            Kino {
                x: -49; y: -13; width: 160; height: 70
                enabled: false; Accessible.ignored: true
                mood: workspace.watchBusy ? "loading" : "idle"
            }
        }
    }

    FocusScope {
        id: panel
        objectName: "watchChatPanel"
        readonly property bool opened: companion.opened
        property real customX: -1
        property real customY: -1
        readonly property real edgeX: preferences.leftEdge ? 12 : companion.width - width - 12
        readonly property real edgeY: companion.clamp(companion.height - preferences.entryBottom - height, 8, companion.height - height - 8)
        x: companion.libraryMode || companion.compact ? 0 : !companion.floating ? companion.width - width
           : companion.clamp(panelMove.moving ? panelMove.proposed.x : customX < 0 ? edgeX : customX, 0, companion.width - width)
        y: companion.libraryMode ? 0 : companion.compact ? companion.height - height : !companion.floating ? 0
           : companion.clamp(panelMove.moving ? panelMove.proposed.y : customY < 0 ? edgeY : customY, 0, companion.height - height)
        readonly property real maxDockWidth: Math.min(850, Math.max(320, companion.width - 420))
        width: companion.libraryMode || companion.compact ? companion.width : companion.floating ?
            Math.min(398, Math.max(338, Math.round(companion.width * .38))) : companion.clamp(preferences.dockWidth, 320, maxDockWidth)
        height: companion.libraryMode ? companion.height : companion.compact ? Math.min(companion.height - 84, Math.max(236, Math.round(companion.height*.57)))
              : companion.floating ? Math.min(companion.height - 32, Math.max(330, Math.round(companion.height * .62))) : companion.height
        enabled: companion.opened; visible: opacity > 0
        property bool idle: false
        readonly property bool canFade: companion.opened && !companion.libraryMode && companion.floating &&
                                        !panelHover.hovered && !workspace.watchBusy && !conversations.opened && !pickImage.visible
        onCanFadeChanged: markActivity()
        function markActivity() { idle = false; fadeTimer.stop(); if (canFade) fadeTimer.start() }
        HoverHandler { id: panelHover }
        Timer { id: fadeTimer; interval: Math.max(1,Studio.preferences.kinoFadeSeconds)*1000; onTriggered: panel.idle = true }
        Connections { target: Studio.preferences; function onKinoFadeSecondsChanged() { panel.markActivity() } }
        opacity: !companion.opened ? 0 : idle ? .12 : 1
        Behavior on opacity { NumberAnimation { duration: panel.idle && !Studio.reduce ? 900 : Studio.hover } }
        Rectangle {
            anchors.fill: parent; radius: companion.floating ? 14 : 0
            color: Studio.sheet; border.color: Studio.line
            CardShadow { visible: companion.floating; radius: parent.radius }
        }
        // Consume pointer input on blank panel areas before it reaches the video.
        MouseArea { anchors.fill: parent; acceptedButtons: Qt.AllButtons }
        ColumnLayout {
            anchors.fill: parent; anchors.margins: 1; spacing: 0
            Item {
                Layout.fillWidth: true; Layout.preferredHeight: companion.compact ? 40 : 45
                MoveGrip {
                    id: panelMove; anchors.fill: parent; enabled: companion.floating; subject: panel
                    onRelocated: (left, top) => {
                        panel.customX = companion.clamp(left, 0, companion.width - panel.width)
                        panel.customY = companion.clamp(top, 0, companion.height - panel.height)
                    }
                }
                RowLayout {
                    anchors.fill: parent; anchors.leftMargin: 14; anchors.rightMargin: 7; spacing: 3
                    Image { source: "art/mascot-seek.png"; Layout.preferredWidth: 23; Layout.preferredHeight: 23; fillMode: Image.PreserveAspectFit }
                    Mark { text: companion.libraryMode ? qsTr("Kino 素材库问答") : qsTr("Kino 陪看"); font.pixelSize: 15; font.weight: Font.DemiBold; Layout.leftMargin: 3; Layout.fillWidth: true }
                    Tool { objectName: "watchHistory"; glyph: "library"; hint: qsTr("查看或新建对话")
                        enabled: !workspace.watchBusy; onClicked: conversations.open() }
                    Tool { objectName: "watchChatSettings"; glyph: "sliders"; hint: qsTr("问答模型设置"); onClicked: companion.settingsRequested() }
                    Tool {
                        objectName: "watchChatDock"; visible: !companion.libraryMode && !companion.compact
                        glyph: companion.floating ? "dockRight" : "floatWindow"
                        hint: companion.floating ? qsTr("停靠到侧栏") : qsTr("改为悬浮窗")
                        onClicked: preferences.docked = !preferences.docked
                    }
                    Tool { objectName: "watchChatClose"; glyph: "close"; hint: qsTr("关闭陪看 (Esc)"); onClicked: companion.dismiss() }
                }
            }
            Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Studio.line }
            RowLayout {
                visible: !companion.libraryMode
                Layout.fillWidth: true; Layout.leftMargin: 10; Layout.topMargin: 6; spacing: 3
                Tool { objectName: "watchChatTab"; text: qsTr("聊天"); chosen: !companion.showingEpisodes
                    onClicked: companion.focusQuestion() }
                Tool { objectName: "watchEpisodesTab"; text: qsTr("选集"); chosen: companion.showingEpisodes
                    onClicked: { companion.showingEpisodes = true; episodeList.forceActiveFocus(); panel.markActivity() } }
            }
            ListView {
                id: episodeList; objectName: "companionEpisodeList"; visible: companion.showingEpisodes
                Layout.fillWidth: true; Layout.fillHeight: true; Layout.margins: 10
                clip: true; spacing: 2; model: companion.episodes; currentIndex: companion.currentEpisode
                ScrollBar.vertical: ScrollBar { }
                delegate: ItemDelegate {
                    required property int index; required property var modelData
                    objectName: "companionEpisode" + index; width: ListView.view.width; height: 52; hoverEnabled: true
                    Accessible.name: modelData.title
                    onClicked: companion.episodeRequested(index)
                    background: Rectangle { radius: 7; color: index === companion.currentEpisode || parent.hovered ? Studio.selected : "transparent" }
                    contentItem: ColumnLayout {
                        spacing: 1
                        Mark { text: modelData.title; textFormat: Text.PlainText; Layout.fillWidth: true }
                        Mark { text: qsTr("第 %1 集").arg(modelData.episode) + " · " + Studio.time(modelData.durationMs,false)
                            secondary: true; font.pixelSize: 12 }
                    }
                }
                Keys.onReturnPressed: if (currentIndex >= 0) companion.episodeRequested(currentIndex)
            }
            ColumnLayout {
                Layout.fillWidth: true; Layout.fillHeight: true; spacing: 0; visible: !companion.showingEpisodes
            Item {
                Layout.fillWidth: true; Layout.fillHeight: true; Layout.minimumHeight: 15; clip: true
                ListView {
                    id: turns; objectName: "watchChatMessages"
                    anchors.fill: parent; anchors.margins: 12
                    spacing: 10; clip: true; boundsBehavior: Flickable.StopAtBounds
                    model: workspace.watchMessages
                    onCountChanged: Qt.callLater(() => positionViewAtEnd())
                    ScrollBar.vertical: ScrollBar { }
                    delegate: Item {
                        id: turn
                        required property var modelData
                        readonly property bool mine: modelData.role === "user"
                        width: ListView.view.width; height: message.y + message.implicitHeight + citations.implicitHeight + 7
                        Tool {
                            visible: !companion.libraryMode && !turn.modelData.failed
                            anchors.right: parent.right; anchors.top: parent.top
                            text: Studio.time(Number(turn.modelData.positionMs) || 0, false); textSize: 11
                            implicitHeight: 17; implicitWidth: contentItem.implicitWidth + 6
                            hint: turn.modelData.sourceRef ? qsTr("回到引用的片段") : qsTr("回到提问时的画面")
                            onClicked: {
                                if (turn.modelData.sourceRef) workspace.askAboutResult(turn.modelData.sourceRef, companion.videoTitle)
                                else companion.seekRequested(turn.modelData.positionMs)
                            }
                        }
                        Rectangle {
                            visible: turn.mine; anchors.right: parent.right
                            y: message.y - 7
                            width: Math.min(parent.width - 12, message.contentWidth + 20); height: message.implicitHeight + 14
                            radius: 10; color: Studio.selected
                        }
                        Column {
                            id: citations; y: message.y + message.implicitHeight + 3; width: parent.width; spacing: 2
                            Repeater {
                                model: companion.libraryMode && !turn.mine && !turn.modelData.failed ? (turn.modelData.sources || []) : []
                                Tool {
                                    required property var modelData; required property int index
                                    width: Math.min(parent.width, implicitWidth); clip: true; textSize: 12
                                    text: "[" + (index + 1) + "] " + modelData.title + "  ·  " + Studio.time(modelData.startMs,true) + " – " + Studio.time(modelData.endMs,true)
                                    hint: qsTr("回看这个参考片段"); onClicked: workspace.openResult(modelData.sourceRef,modelData.title)
                                }
                            }
                        }
                        Image {
                            id: attachmentPreview
                            source: turn.modelData.attachments?.[0]?.previewUrl || ""
                            visible: String(source).length > 0; anchors.right: parent.right
                            y: companion.libraryMode ? 8 : 25; width: Math.min(parent.width, 132); height: visible ? 85 : 0
                            fillMode: Image.PreserveAspectFit; horizontalAlignment: Image.AlignRight
                            sourceSize.width: 264; asynchronous: true
                            Accessible.name: qsTr("参考图片")
                        }
                        TextEdit {
                            id: message
                            x: turn.mine ? 14 : 0; y: attachmentPreview.y + attachmentPreview.height + (attachmentPreview.visible ? 5 : 0)
                            width: parent.width - (turn.mine ? 24 : 4)
                            readOnly: true; selectByMouse: true; activeFocusOnTab: false
                            text: turn.modelData.streaming ? workspace.watchStreamingText : turn.modelData.text || ""; textFormat: TextEdit.PlainText; wrapMode: TextEdit.Wrap
                            horizontalAlignment: turn.mine ? TextEdit.AlignRight : TextEdit.AlignLeft
                            font.pixelSize: 14; color: Studio.ink; selectionColor: Studio.selected; selectedTextColor: Studio.ink
                        }
                    }
                }
                Column {
                    visible: !turns.count && !workspace.watchBusy && !workspace.watchError
                    anchors.centerIn: parent
                    width: parent.width - 30; spacing: 5
                    Text {
                        width: parent.width; text: companion.libraryMode && workspace.watchModelReady ? qsTr("想从所选系列里找什么？") : workspace.watchModelReady ? qsTr("想了解画面里的哪一处？") : qsTr("连接视觉模型，问问当前画面。")
                        color: Studio.ink; font.pixelSize: 15; horizontalAlignment: Text.AlignHCenter; wrapMode: Text.Wrap
                    }
                    Tool {
                        visible: workspace.watchModelReady; anchors.horizontalCenter: parent.horizontalCenter
                        text: companion.libraryMode ? qsTr("找出有人在门口等待的片段") : qsTr("解释一下这帧画面"); textSize: 13; outlined: true
                        onClicked: { question.text = text; companion.focusQuestion() }
                    }
                    Tool {
                        objectName: "chatConfigureModel"; visible: !workspace.watchModelReady; anchors.horizontalCenter: parent.horizontalCenter
                        text: qsTr("配置问答模型…"); chosen: true; onClicked: companion.settingsRequested()
                    }
                }
            }
            Text {
                id: stateLabel; objectName: "watchChatError"
                Layout.fillWidth: true; Layout.leftMargin: 14; Layout.rightMargin: 14; Layout.bottomMargin: 5
                visible: !!text; text: Studio.translate(workspace.watchError) || (workspace.watchBusy ? (companion.libraryMode ? qsTr("Kino 正在查找片段…") : qsTr("Kino 正在看这一帧…")) : "")
                textFormat: Text.PlainText; font.pixelSize: 12; color: workspace.watchError ? Studio.danger : Studio.muted
                wrapMode: Text.Wrap; maximumLineCount: 3; elide: Text.ElideRight
                ToolTip.visible: errorHover.hovered && truncated; ToolTip.text: text
                HoverHandler { id: errorHover }
            }
            Tool {
                objectName: "watchChatRetry"; visible: !!workspace.watchError && !workspace.watchBusy && turns.count > 0
                Layout.leftMargin: 10; text: qsTr("重新提问"); textSize: 12
                onClicked: {
                    const last = workspace.watchMessages.slice().reverse().find(message => message.role === "user")
                    if (last && workspace.askWatch(last.text, last.positionMs, last.attachments?.[0]?.previewUrl || "")) companion.questionSent()
                }
            }
            Mark {
                Layout.fillWidth: true; Layout.leftMargin: 14; Layout.rightMargin: 14
                text: companion.libraryMode ? companion.videoTitle : workspace.playbackContext.sourceRef ? qsTr("已引用片段 ") + Studio.time(workspace.playbackContext.clipStartMs,true) + " – " + Studio.time(workspace.playbackContext.clipEndMs,true) :
                    companion.videoTitle + "  ·  " + Studio.time(companion.positionMs, false)
                textFormat: Text.PlainText; secondary: true; font.pixelSize: 12; font.features: ({ "tnum": 1 })
            }
            RowLayout {
                visible: String(companion.pendingImage).length > 0; Layout.fillWidth: true; Layout.leftMargin: 12; Layout.rightMargin: 12
                Image { source: companion.pendingImage; sourceSize.width: 32; Layout.preferredWidth: 26; Layout.preferredHeight: 24; fillMode: Image.PreserveAspectFit }
                Mark { Layout.fillWidth: true; text: decodeURIComponent(String(companion.pendingImage).split("/").pop()); textFormat: Text.PlainText; secondary: true }
                Tool { glyph: "close"; hint: qsTr("移除参考图片"); onClicked: companion.pendingImage = "" }
            }
            Rectangle {
                Layout.fillWidth: true; Layout.leftMargin: 10; Layout.rightMargin: 10
                Layout.topMargin: 5; Layout.bottomMargin: 10
                implicitHeight: Math.min(88, Math.max(42, question.contentHeight + 18))
                color: Studio.paper; radius: 10
                border.color: question.activeFocus ? Studio.strongLine : Studio.line
                Behavior on border.color { ColorAnimation { duration: Studio.hover } }
                Tool { id: addImage; objectName: "watchAttach"; glyph: "plus"; hint: qsTr("添加参考图片")
                    anchors.left: parent.left; anchors.leftMargin: 3; anchors.bottom: parent.bottom; anchors.bottomMargin: 5
                    enabled: !workspace.watchBusy; onClicked: pickImage.open() }
                ScrollView {
                    anchors.left: addImage.right; anchors.top: parent.top; anchors.bottom: parent.bottom; anchors.right: sendButton.left
                    anchors.margins: 4; contentWidth: availableWidth
                    ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                    TextArea {
                        id: question; objectName: "watchChatQuestion"
                        placeholderText: qsTr("问问 Kino…"); Accessible.name: qsTr("向 Kino 提问")
                        color: Studio.ink; placeholderTextColor: Studio.muted
                        font.pixelSize: 14; wrapMode: TextEdit.Wrap; selectByMouse: true
                        topPadding: 6; leftPadding: 6; bottomPadding: 6
                        background: null; focusPolicy: Qt.StrongFocus
                        onTextChanged: panel.markActivity()
                        Keys.onPressed: event => {
                            panel.markActivity()
                            if (event.key !== Qt.Key_Return && event.key !== Qt.Key_Enter) return
                            if (inputMethodComposing || (event.modifiers & Qt.ShiftModifier)) return
                            event.accepted = true
                            companion.send()
                        }
                    }
                }
                Tool {
                    id: sendButton; objectName: "watchChatSend"
                    anchors.right: parent.right; anchors.rightMargin: 5; anchors.bottom: parent.bottom; anchors.bottomMargin: 5
                    glyph: workspace.watchBusy ? "stop" : "up"; hint: workspace.watchBusy ? qsTr("停止生成") : qsTr("发送问题"); primary: true
                    implicitWidth: 31; implicitHeight: 31; corner: 9
                    enabled: workspace.watchBusy || !!question.text.trim()
                    onClicked: workspace.watchBusy ? workspace.stopWatch() : companion.send()
                }
            }
            }
        }
        MouseArea {
            id: dockGrip; objectName: "watchDockResize"
            visible: companion.opened && !companion.libraryMode && !companion.compact && !companion.floating
            anchors.left: parent.left; anchors.top: parent.top; anchors.bottom: parent.bottom; width: 8
            hoverEnabled: true; preventStealing: true; cursorShape: Qt.SizeHorCursor
            activeFocusOnTab: true; Accessible.role: Accessible.Splitter; Accessible.name: qsTr("调整陪看侧栏宽度")
            property real initialWidth
            property real pressX
            function resizeTo(value) { preferences.dockWidth = companion.clamp(value, 320, panel.maxDockWidth) }
            onPressed: mouse => { initialWidth = panel.width; pressX = mapToItem(companion,mouse.x,mouse.y).x }
            onPositionChanged: mouse => {
                if (pressed) resizeTo(initialWidth + pressX - mapToItem(companion,mouse.x,mouse.y).x)
            }
            Keys.onLeftPressed: resizeTo(panel.width + 24)
            Keys.onRightPressed: resizeTo(panel.width - 24)
            Rectangle { anchors.left: parent.left; height: parent.height; width: 2
                color: dockGrip.containsMouse || dockGrip.activeFocus ? Studio.accent : "transparent" }
        }
    }
}
