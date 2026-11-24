import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import QtQuick.Effects
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
    readonly property bool compact: (Window.window?.width ?? width) <= 800
    readonly property bool floating: !compact && !Studio.watchPanel.docked
    readonly property real reservedWidth: opened && !compact && !floating ? panel.width : 0
    readonly property real reservedHeight: opened && compact ? panel.height : 0
    signal settingsRequested()
    signal questionSent()
    signal seekRequested(int position)

    function clamp(value, min, max) { return Math.max(min, Math.min(value, Math.max(min, max))) }
    function focusQuestion() { if (opened) { showingEpisodes = false; question.forceActiveFocus(Qt.OtherFocusReason) } }
    function showPanel() {
        opened = true
        Qt.callLater(focusQuestion)
    }
    function dismiss() {
        watchOptions.close()
        if (libraryMode) { closeRequested(); return }
        opened = false
        entry.forceActiveFocus(Qt.OtherFocusReason)
    }
    function reset() {
        watchOptions.close()
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

    component PanelTab: Tool {
        id: tab
        Accessible.role: Accessible.PageTab
        Accessible.selected: chosen
        implicitWidth: contentItem.implicitWidth + 2; implicitHeight: companion.compact ? 40 : 44
        symbolSize: 15; textSize: 13
        background: Item {
            Rectangle {
                anchors.fill: parent; anchors.topMargin: 5; anchors.bottomMargin: 5; radius: 5
                color: tab.down ? Studio.selected : tab.hovered ? Studio.surfaceMuted : "transparent"
                border.width: tab.visualFocus ? 2 : 0; border.color: Studio.accent
                Behavior on color { ColorAnimation { duration: Studio.hover } }
            }
            Rectangle {
                anchors.bottom: parent.bottom; width: parent.width; height: 2; radius: 1
                color: Studio.actionBg; opacity: tab.chosen ? 1 : 0
                Behavior on opacity { NumberAnimation { duration: Studio.quick } }
            }
        }
    }

    AbstractButton {
        id: entry
        objectName: "kinoEntry"
        readonly property int gutter: companion.width <= 580 ? 16 : 24
        readonly property real dockX: Studio.watchPanel.leftEdge ? gutter : companion.width - width - gutter
        readonly property real dockBottom: companion.clamp(Studio.watchPanel.entryBottom, 16, companion.height - height - 8)
        readonly property real dockY: companion.height - height - dockBottom
        readonly property bool emphasized: entryMove.containsMouse || activeFocus
        enabled: !companion.opened && !companion.libraryMode
        visible: !companion.libraryMode && opacity > 0
        width: 64; height: 64
        x: entryMove.moving ? entryMove.proposed.x : dockX
        y: entryMove.moving ? entryMove.proposed.y : dockY
        Behavior on x { enabled: !entryMove.moving && entry.visible && !Studio.reduce; NumberAnimation {
            duration: 320; easing.type: Easing.BezierSpline; easing.bezierCurve: [.34, 1.4, .64, 1, 1, 1]
        } }
        Behavior on y { enabled: !entryMove.moving && entry.visible && !Studio.reduce; NumberAnimation {
            duration: 320; easing.type: Easing.BezierSpline; easing.bezierCurve: [.34, 1.4, .64, 1, 1, 1]
        } }
        opacity: companion.opened ? 0 : 1
        scale: companion.opened ? .9 : 1
        Behavior on opacity { NumberAnimation { duration: Studio.hover } }
        Behavior on scale { NumberAnimation { duration: Studio.hover } }
        focusPolicy: Qt.StrongFocus
        Accessible.name: qsTr("与 Kino 对话")
        ToolTip.visible: entry.enabled && entryMove.containsMouse && !entryMove.pressed
        ToolTip.text: qsTr("与 Kino 对话"); ToolTip.delay: 500
        onClicked: companion.showPanel()
        MouseArea {
            id: entryMove; objectName: "watchEntryDrag"
            anchors.fill: parent; z: 2; preventStealing: true; hoverEnabled: true
            acceptedButtons: Qt.LeftButton
            cursorShape: moving ? Qt.ClosedHandCursor : Qt.OpenHandCursor
            property point pressPoint
            property point origin
            property point proposed
            property real turnX
            property bool facingLeft
            property bool moving: false
            onPressed: mouse => {
                pressPoint = mapToItem(companion, mouse.x, mouse.y)
                origin = Qt.point(entry.x, entry.y)
                turnX = pressPoint.x
                facingLeft = !Studio.watchPanel.leftEdge
            }
            onPositionChanged: mouse => {
                if (!pressed) return
                const point = mapToItem(companion, mouse.x, mouse.y)
                const dx = point.x - pressPoint.x, dy = point.y - pressPoint.y
                if (!moving && Math.hypot(dx, dy) < 4) return
                // Keep facing the same way through tiny hand tremors at each turn.
                if (Math.abs(point.x - turnX) >= 6) facingLeft = point.x < turnX
                if (facingLeft === (point.x < turnX)) turnX = point.x
                proposed = Qt.point(companion.clamp(origin.x + dx, 0, companion.width - entry.width),
                                    companion.clamp(origin.y + dy, 0, companion.height - entry.height))
                moving = true
            }
            onReleased: mouse => {
                if (!moving) {
                    if (mouse.x >= 0 && mouse.x < width && mouse.y >= 0 && mouse.y < height) companion.showPanel()
                    return
                }
                const point = mapToItem(companion, mouse.x, mouse.y)
                const left = origin.x + point.x - pressPoint.x
                const top = origin.y + point.y - pressPoint.y
                Studio.watchPanel.leftEdge = left + entry.width / 2 < companion.width / 2
                Studio.watchPanel.entryBottom = Math.round(companion.clamp(companion.height - top - entry.height,
                                                                      16, companion.height - entry.height - 8))
                moving = false
                mascot.playReaction("jump")
            }
            onCanceled: moving = false
        }
        background: Rectangle {
            radius: 8; color: "transparent"
            border.width: entry.visualFocus ? 2 : 0; border.color: Studio.accent
        }
        contentItem: Item {
            Kino {
                id: mascot
                objectName: "watchEntrySprite"
                anchors.centerIn: parent
                anchors.horizontalCenterOffset: -3
                anchors.verticalCenterOffset: -10 + (entry.emphasized && !entryMove.moving ? -2 : 0)
                Behavior on anchors.verticalCenterOffset { enabled: !entryMove.moving; NumberAnimation { duration: Studio.hover } }
                sceneWidth: 38; width: sceneWidth * 3; height: 84
                layer.enabled: GraphicsInfo.api !== GraphicsInfo.Software
                layer.effect: MultiEffect {
                    shadowEnabled: true; shadowBlur: .18; shadowVerticalOffset: 3; shadowOpacity: .35
                }
                interactive: false; ground: false; dozeWhenIdle: true
                mood: entryMove.moving ? (entryMove.facingLeft ? "dashLeft" : "dashRight")
                      : workspace.watchBusy ? "loading" : entry.emphasized ? "look" : "idle"
            }
        }
    }

    FocusScope {
        id: panel
        objectName: "watchChatPanel"
        readonly property bool opened: companion.opened
        property real customX: -1
        property real customY: -1
        readonly property real edgeX: Studio.watchPanel.leftEdge ? 24 : companion.width - width - 24
        readonly property real edgeY: companion.clamp(companion.height - entry.dockBottom - height, 24, companion.height - height - 16)
        x: companion.libraryMode || companion.compact ? 0 : !companion.floating ? companion.width - width
           : companion.clamp(customX < 0 ? edgeX : customX, 8, companion.width - width - 8)
        y: companion.libraryMode ? 0 : companion.compact ? companion.height - height : !companion.floating ? 0
           : companion.clamp(customY < 0 ? edgeY : customY, 8, companion.height - height - 8)
        readonly property real maxDockWidth: Math.min(850, Math.max(320, companion.width - 420))
        width: companion.libraryMode || companion.compact ? companion.width : companion.floating ?
            companion.clamp(Math.round(companion.width * .25), 320, 400) : companion.clamp(Studio.watchPanel.dockWidth, 320, maxDockWidth)
        height: companion.libraryMode ? companion.height : companion.compact ? Math.min(companion.height - 84, Math.max(236, Math.round(companion.height*.57)))
              : companion.floating ? Math.min(640, companion.height - 40, Math.round(companion.height * .62)) : companion.height
        enabled: companion.opened; visible: opacity > 0
        readonly property bool animateFloat: companion.floating && !companion.libraryMode
        transformOrigin: Studio.watchPanel.leftEdge ? Item.BottomLeft : Item.BottomRight
        scale: animateFloat && !companion.opened ? .97 : 1
        transform: Translate { y: panel.animateFloat && !companion.opened ? 8 : 0
            Behavior on y { NumberAnimation { duration: Studio.hover } } }
        Behavior on scale { NumberAnimation { duration: Studio.hover } }
        function resetPosition() { customX = -1; customY = -1 }
        Connections {
            target: companion
            function onWidthChanged() { panel.resetPosition() }
            function onHeightChanged() { panel.resetPosition() }
            function onFloatingChanged() { panel.resetPosition() }
        }
        property bool idle: false
        readonly property bool canFade: companion.opened && !companion.libraryMode && companion.floating &&
                                        Studio.preferences.kinoFadeEnabled && !panelHover.hovered && !workspace.watchBusy &&
                                        !watchOptions.visible && !conversations.opened && !removeThread.opened && !pickImage.visible &&
                                        !(question.activeFocus && (question.inputMethodComposing || question.text.length > 0))
        onCanFadeChanged: markActivity()
        function markActivity() { idle = false; fadeTimer.stop(); if (canFade) fadeTimer.start() }
        HoverHandler { id: panelHover }
        Keys.onPressed: event => { markActivity(); event.accepted = false }
        Connections {
            target: panel.Window.window
            function onActiveFocusItemChanged() { if (panel.activeFocus) panel.markActivity() }
        }
        Timer { id: fadeTimer; interval: Math.max(1,Studio.preferences.kinoFadeSeconds)*1000; onTriggered: panel.idle = true }
        Connections { target: Studio.preferences; function onKinoFadeSecondsChanged() { panel.markActivity() } }
        opacity: !companion.opened ? 0 : idle ? .12 : 1
        Behavior on opacity {
            id: fadeTransition
            NumberAnimation { duration: fadeTransition.targetValue === .12 && !Studio.reduce ? 900 : Studio.hover }
        }
        Rectangle {
            anchors.fill: parent; radius: companion.floating ? 14 : 0
            color: Studio.sheet; border.color: Studio.line
            CardShadow { visible: companion.floating; radius: parent.radius }
        }
        Popup {
            id: watchOptions; objectName: "watchOptionsPopup"; parent: optionsButton
            x: optionsButton.width - width; y: optionsButton.height + 4
            width: Math.min(296, panel.width - 16); margins: 8; padding: 14
            focus: true; closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
            onOpened: settingsControls.focusMode()
            onClosed: if (companion.opened) optionsButton.forceActiveFocus(Qt.PopupFocusReason)
            contentItem: ColumnLayout {
                spacing: 8
                WatchPreferences { id: settingsControls; compact: companion.compact; Layout.fillWidth: true }
                Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Studio.line }
                Tool {
                    objectName: "watchChatSettings"; text: qsTr("问答模型设置"); glyph: "sliders"
                    onClicked: { watchOptions.close(); companion.settingsRequested() }
                }
            }
            background: Rectangle { radius: 10; color: Studio.sheet; border.color: Studio.line; CardShadow { radius: 10 } }
        }
        // Consume pointer input on blank panel areas before it reaches the video.
        MouseArea { anchors.fill: parent; acceptedButtons: Qt.AllButtons }
        ColumnLayout {
            anchors.fill: parent; anchors.margins: 1; spacing: 0
            Item {
                Layout.fillWidth: true; Layout.preferredHeight: companion.compact ? 40 : 44
                Row {
                    id: tabs
                    anchors.left: parent.left; anchors.leftMargin: 10; anchors.verticalCenter: parent.verticalCenter
                    spacing: 8
                    PanelTab {
                        objectName: "watchChatTab"; visible: !companion.libraryMode
                        glyph: "chat"; text: qsTr("聊天"); chosen: !companion.showingEpisodes
                        onClicked: companion.focusQuestion()
                    }
                    PanelTab {
                        objectName: "watchEpisodesTab"; visible: !companion.libraryMode
                        glyph: "film"; text: qsTr("选集"); chosen: companion.showingEpisodes
                        onClicked: { companion.showingEpisodes = true; episodeList.forceActiveFocus(); panel.markActivity() }
                    }
                }
                Mark {
                    visible: companion.libraryMode
                    anchors.left: tabs.left; anchors.right: actions.left; anchors.verticalCenter: parent.verticalCenter
                    text: qsTr("Kino 素材库问答"); font.pixelSize: 15; font.weight: Font.DemiBold
                }
                MouseArea {
                    id: panelMove; objectName: "watchPanelMove"
                    anchors.left: tabs.right; anchors.right: actions.left
                    anchors.top: parent.top; anchors.bottom: parent.bottom
                    enabled: companion.floating && !companion.libraryMode
                    preventStealing: true; hoverEnabled: true
                    cursorShape: pressed ? Qt.ClosedHandCursor : Qt.OpenHandCursor
                    ToolTip.visible: containsMouse && !pressed && !watchOptions.visible
                    ToolTip.delay: 650; ToolTip.text: qsTr("拖动标题栏移动浮窗")
                    property point offset
                    onPressed: mouse => {
                        const point = mapToItem(companion, mouse.x, mouse.y)
                        offset = Qt.point(point.x - panel.x, point.y - panel.y)
                        panel.markActivity()
                    }
                    onPositionChanged: mouse => {
                        if (!pressed) return
                        const point = mapToItem(companion, mouse.x, mouse.y)
                        panel.customX = companion.clamp(point.x - offset.x, 8, companion.width - panel.width - 8)
                        panel.customY = companion.clamp(point.y - offset.y, 8, companion.height - panel.height - 8)
                    }
                }
                Row {
                    id: actions
                    anchors.right: parent.right; anchors.rightMargin: 6; anchors.verticalCenter: parent.verticalCenter
                    Tool { implicitWidth: 27; objectName: "watchHistory"; glyph: "history"; hint: qsTr("查看或新建对话")
                        enabled: !workspace.watchBusy; onClicked: conversations.open() }
                    Tool { implicitWidth: 27; objectName: "libraryChatSettings"; visible: companion.libraryMode; glyph: "sliders"
                        hint: qsTr("问答模型设置"); onClicked: companion.settingsRequested() }
                    Tool { implicitWidth: 27;
                        id: optionsButton; objectName: "watchOptionsButton"; visible: !companion.libraryMode
                        glyph: "sliders"; hint: qsTr("观看设置"); chosen: watchOptions.visible
                        onClicked: watchOptions.visible ? watchOptions.close() : watchOptions.open()
                    }
                    Tool { implicitWidth: 27;
                        objectName: "watchChatDock"; visible: !companion.libraryMode && !companion.compact
                        glyph: companion.floating ? "dockRight" : "floatWindow"
                        hint: companion.floating ? qsTr("停靠到侧栏") : qsTr("改为悬浮窗")
                        onClicked: Studio.watchPanel.docked = !Studio.watchPanel.docked
                    }
                    Tool { implicitWidth: 27; objectName: "watchChatClose"; glyph: "close"; hint: qsTr("关闭陪看 (Esc)"); onClicked: companion.dismiss() }
                }
            }
            Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Studio.line }
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
                    // The QVariantList is replaced when the first delta arrives and when history is
                    // committed. Keep one actual actor outside the delegates across those replacements.
                    currentIndex: {
                        const rows = workspace.watchMessages
                        for (let i = rows.length - 1; i >= 0; --i)
                            if (rows[i].role === "assistant") return i
                        return -1
                    }
                    highlightFollowsCurrentItem: false
                    property var departure: null
                    property int arrivalIndex: -1
                    function arrive() {
                        if (arrivalIndex < 0 || currentIndex !== arrivalIndex || !currentItem || !replyKino.visible) return
                        arrivalIndex = -1
                        if (departure) replyKino.arriveFrom(departure, contentItem)
                        departure = null
                    }
                    onCurrentItemChanged: Qt.callLater(arrive)
                    Connections {
                        target: workspace
                        function onWatchTurnStarted() {
                            if (!companion.opened || !turns.visible) return
                            const previous = replyKino.visible ? replyKino : welcomeKino
                            turns.departure = previous.visible ? previous.capturePerch(turns.contentItem) : null
                            turns.arrivalIndex = workspace.watchMessages.length + 1
                        }
                        function onWatchThreadIDChanged() {
                            // The first live reply gets a persistent thread ID at commit time.
                            if (!workspace.watchPhase) {
                                turns.arrivalIndex = -1
                                turns.departure = null
                                replyKino.resetPose()
                            }
                        }
                        function onWatchTurnFinished(failed) {
                            if (replyKino.visible) replyKino.playReaction(failed ? "oops" : "happy")
                        }
                    }
                    Kino {
                        id: replyKino; objectName: "watchReplyKino"
                        parent: turns.contentItem
                        visible: companion.opened && !!turns.currentItem && turns.currentIndex >= 0
                        interactive: false; ground: false; z: 3
                        x: 0; y: turns.currentItem?.y ?? 0
                        width: turns.width; height: 56
                        sceneWidth: Math.max(38, Math.floor(turns.width / 2))
                        readonly property var phaseMoods: ({"QUEUED":"wander", "RETRIEVING":"run", "EVALUATING":"think", "RESPONDING":"loading"})
                        mood: phaseMoods[workspace.watchPhase] || "sleep"
                        onVisibleChanged: if (visible) Qt.callLater(turns.arrive)
                    }
                    onCountChanged: Qt.callLater(() => positionViewAtEnd())
                    ScrollBar.vertical: ScrollBar { }
                    delegate: Item {
                        id: turn
                        required property var modelData
                        required property int index
                        readonly property int perchHeight: index === turns.currentIndex && !mine ? replyKino.height : 0
                        readonly property bool mine: modelData.role === "user"
                        width: ListView.view.width; height: message.y + message.implicitHeight + citations.implicitHeight + 7
                        Tool {
                            visible: !companion.libraryMode && !turn.modelData.failed
                            anchors.right: parent.right; anchors.top: parent.top; anchors.topMargin: turn.perchHeight
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
                            y: turn.perchHeight + (companion.libraryMode ? 8 : 25); width: Math.min(parent.width, 132); height: visible ? 85 : 0
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
                    Kino {
                        id: welcomeKino; objectName: "watchWelcomeKino"
                        anchors.horizontalCenter: parent.horizontalCenter
                        sceneWidth: 64; width: 192; height: 84
                        interactive: true; dozeWhenIdle: true
                    }
                    Text {
                        visible: workspace.watchModelReady
                        width: parent.width; text: companion.libraryMode ? qsTr("想从所选系列里找什么？") : qsTr("想了解画面里的哪一处？")
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
            RowLayout {
                id: statusRow
                readonly property bool restoring: workspace.watchBusy && !workspace.watchPhase
                readonly property bool standaloneError: !!workspace.watchError &&
                    !workspace.watchMessages.some(turn => turn.failed && turn.text === workspace.watchError)
                visible: stateLabel.text.length > 0
                Layout.fillWidth: true; Layout.leftMargin: 14; Layout.rightMargin: 14; Layout.bottomMargin: 5
                Loader {
                    objectName: "watchStatusKino"
                    active: companion.opened && (statusRow.standaloneError || (statusRow.restoring && turns.currentIndex < 0))
                    visible: active
                    Layout.preferredWidth: 76; Layout.preferredHeight: 56
                    sourceComponent: Kino {
                        sceneWidth: 38; interactive: false; ground: false
                        mood: statusRow.standaloneError ? "oops" : "loading"
                        opacity: arrival.running ? arrival.value : 1
                        SequentialAnimation {
                            id: arrival
                            property real value: 0
                            running: statusRow.restoring && !Studio.reduce
                            PauseAnimation { duration: 300 }
                            NumberAnimation { target: arrival; property: "value"; from: 0; to: 1; duration: 200; easing.type: Easing.OutQuad }
                        }
                    }
                }
                Text {
                    id: stateLabel; objectName: "watchChatError"
                    Layout.fillWidth: true
                    text: Studio.translate(workspace.watchError) || (statusRow.restoring ? qsTr("正在恢复会话…") :
                        workspace.watchBusy ? (companion.libraryMode ? qsTr("Kino 正在查找片段…") : qsTr("Kino 正在看这一帧…")) : "")
                    textFormat: Text.PlainText; font.pixelSize: 12; color: workspace.watchError ? Studio.danger : Studio.muted
                    wrapMode: Text.Wrap; maximumLineCount: 3; elide: Text.ElideRight
                    ToolTip.visible: errorHover.hovered && truncated; ToolTip.text: text
                    HoverHandler { id: errorHover }
                }
            }
            Tool {
                objectName: "watchChatRetry"; visible: !!workspace.watchError && !workspace.watchBusy && turns.count > 0
                Layout.leftMargin: 10; text: qsTr("重新提问"); textSize: 12
                onClicked: {
                    const last = workspace.watchMessages.slice().reverse().find(message => message.role === "user")
                    if (last && workspace.askWatch(last.text, last.positionMs, last.attachments?.[0]?.previewUrl || "")) companion.questionSent()
                }
            }
            Item {
                id: frameContext
                Layout.fillWidth: true; Layout.leftMargin: 14; Layout.rightMargin: 14
                implicitHeight: Math.max(sourceLabel.implicitHeight, frameTime.implicitHeight)
                readonly property var episode: companion.episodes[companion.currentEpisode]
                readonly property string frameSource: companion.Window.window?.visibility === Window.FullScreen
                    ? companion.videoTitle : episode ? qsTr("第 %1 集").arg(episode.episode) : ""
                Mark {
                    id: sourceLabel
                    text: companion.libraryMode ? companion.videoTitle : workspace.playbackContext.sourceRef
                        ? qsTr("已引用片段 ") : frameContext.frameSource
                    visible: text.length > 0
                    width: Math.min(implicitWidth, Math.max(0, frameContext.width - (frameTime.visible ? frameTime.implicitWidth + 4 : 0)))
                    Accessible.name: text
                    textFormat: Text.PlainText; secondary: true; font.pixelSize: 12
                }
                Mark {
                    id: frameTime; objectName: "watchFrameTime"; visible: !companion.libraryMode
                    x: sourceLabel.visible ? sourceLabel.width + 4 : 0
                    text: workspace.playbackContext.sourceRef
                        ? Studio.time(workspace.playbackContext.clipStartMs,true) + " – " + Studio.time(workspace.playbackContext.clipEndMs,true)
                        : (sourceLabel.visible ? "· " : "") + Studio.time(companion.positionMs, false)
                    Accessible.name: text
                    textFormat: Text.PlainText; secondary: true; font.pixelSize: 12; font.features: ({ "tnum": 1 })
                }
                ToolTip.visible: sourceHover.hovered && !companion.libraryMode && companion.videoTitle.length > 0
                ToolTip.text: companion.videoTitle; ToolTip.delay: 650
                HoverHandler { id: sourceHover }
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
            function resizeTo(value) { Studio.watchPanel.dockWidth = companion.clamp(value, 320, panel.maxDockWidth) }
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
