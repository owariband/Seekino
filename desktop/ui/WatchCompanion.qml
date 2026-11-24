import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import QtCore

Item {
    id: companion
    property string videoTitle: ""
    property int positionMs: 0
    property bool opened: false
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
        property bool leftEdge: false
        property real entryBottom: 78
    }
    function clamp(value, min, max) { return Math.max(min, Math.min(value, Math.max(min, max))) }
    function focusQuestion() { if (opened) question.forceActiveFocus(Qt.OtherFocusReason) }
    function showPanel() {
        opened = true
        Qt.callLater(focusQuestion)
    }
    function dismiss() {
        opened = false
        entry.forceActiveFocus(Qt.OtherFocusReason)
    }
    function reset() {
        opened = false
        question.clear()
        panel.customX = -1
        panel.customY = -1
    }
    function send() {
        const text = question.text.trim()
        if (!text || workspace.watchBusy) return
        if (!workspace.watchModelReady) { settingsRequested(); return }
        if (workspace.askWatch(text, positionMs)) {
            question.clear()
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
        visible: !companion.opened
        width: 72; height: 56
        x: entryMove.moving ? companion.clamp(entryMove.proposed.x, 0, parent.width - width)
                            : preferences.leftEdge ? 12 : parent.width - width - 12
        y: entryMove.moving ? companion.clamp(entryMove.proposed.y, 4, parent.height - height - 62)
                            : companion.clamp(parent.height - height - preferences.entryBottom, 4, parent.height - height - 62)
        focusPolicy: Qt.StrongFocus
        hoverEnabled: true
        Accessible.name: "与 Kino 对话"
        ToolTip.visible: entryMove.containsMouse && !entryMove.moving
        ToolTip.text: "与 Kino 对话"; ToolTip.delay: 500
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
        x: companion.compact ? 0 : !companion.floating ? companion.width - width
           : companion.clamp(panelMove.moving ? panelMove.proposed.x : customX < 0 ? edgeX : customX, 0, companion.width - width)
        y: companion.compact ? companion.height - height : !companion.floating ? 0
           : companion.clamp(panelMove.moving ? panelMove.proposed.y : customY < 0 ? edgeY : customY, 0, companion.height - height)
        width: companion.compact ? companion.width : Math.min(398, Math.max(338, Math.round(companion.width * .38)))
        height: companion.compact ? Math.min(companion.height - 84, Math.max(236, Math.round(companion.height*.57)))
              : companion.floating ? Math.min(companion.height - 32, Math.max(330, Math.round(companion.height * .62))) : companion.height
        enabled: companion.opened; visible: opacity > 0
        opacity: companion.opened ? 1 : 0
        Behavior on opacity { NumberAnimation { duration: Studio.hover } }
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
                    Mark { text: "Kino 陪看"; font.pixelSize: 15; font.weight: Font.DemiBold; Layout.leftMargin: 3; Layout.fillWidth: true }
                    Tool { objectName: "watchChatSettings"; glyph: "sliders"; hint: "问答模型设置"; onClicked: companion.settingsRequested() }
                    Tool {
                        objectName: "watchChatDock"; visible: !companion.compact
                        glyph: companion.floating ? "dockRight" : "floatWindow"
                        hint: companion.floating ? "停靠到侧栏" : "改为悬浮窗"
                        onClicked: preferences.docked = !preferences.docked
                    }
                    Tool { objectName: "watchChatClose"; glyph: "close"; hint: "关闭陪看 (Esc)"; onClicked: companion.dismiss() }
                }
            }
            Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Studio.line }
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
                        width: ListView.view.width; height: message.implicitHeight + (mine ? 32 : 4)
                        Tool {
                            visible: turn.mine
                            anchors.right: parent.right; anchors.top: parent.top
                            text: Studio.time(Number(turn.modelData.positionMs) || 0, false); textSize: 11
                            implicitHeight: 17; implicitWidth: contentItem.implicitWidth + 6
                            hint: "回到提问时的画面"; onClicked: companion.seekRequested(turn.modelData.positionMs)
                        }
                        Rectangle {
                            visible: turn.mine; anchors.right: parent.right
                            anchors.bottom: parent.bottom
                            width: Math.min(parent.width - 12, message.contentWidth + 20); height: message.implicitHeight + 14
                            radius: 10; color: Studio.selected
                        }
                        TextEdit {
                            id: message
                            x: turn.mine ? 14 : 0; y: turn.mine ? 25 : 2
                            width: parent.width - (turn.mine ? 24 : 4)
                            readOnly: true; selectByMouse: true; activeFocusOnTab: false
                            text: turn.modelData.text || ""; textFormat: TextEdit.PlainText; wrapMode: TextEdit.Wrap
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
                        width: parent.width; text: workspace.watchModelReady ? "想了解画面里的哪一处？" : "连接视觉模型，问问当前画面。"
                        color: Studio.ink; font.pixelSize: 15; horizontalAlignment: Text.AlignHCenter; wrapMode: Text.Wrap
                    }
                    Tool {
                        visible: workspace.watchModelReady; anchors.horizontalCenter: parent.horizontalCenter
                        text: "解释一下这帧画面"; textSize: 13; outlined: true
                        onClicked: { question.text = text; companion.focusQuestion() }
                    }
                    Tool {
                        objectName: "chatConfigureModel"; visible: !workspace.watchModelReady; anchors.horizontalCenter: parent.horizontalCenter
                        text: "配置问答模型…"; chosen: true; onClicked: companion.settingsRequested()
                    }
                }
            }
            Text {
                id: stateLabel; objectName: "watchChatError"
                Layout.fillWidth: true; Layout.leftMargin: 14; Layout.rightMargin: 14; Layout.bottomMargin: 5
                visible: !!text; text: workspace.watchError || (workspace.watchBusy ? "Kino 正在看这一帧…" : "")
                textFormat: Text.PlainText; font.pixelSize: 12; color: workspace.watchError ? Studio.danger : Studio.muted
                wrapMode: Text.Wrap; maximumLineCount: 3; elide: Text.ElideRight
                ToolTip.visible: errorHover.hovered && truncated; ToolTip.text: text
                HoverHandler { id: errorHover }
            }
            Tool {
                objectName: "watchChatRetry"; visible: !!workspace.watchError && !workspace.watchBusy && turns.count > 0
                Layout.leftMargin: 10; text: "重新提问"; textSize: 12
                onClicked: {
                    const last = workspace.watchMessages[turns.count - 1]
                    if (last.role === "user" && workspace.askWatch(last.text, last.positionMs)) companion.questionSent()
                }
            }
            Mark {
                Layout.fillWidth: true; Layout.leftMargin: 14; Layout.rightMargin: 14
                text: companion.videoTitle + "  ·  " + Studio.time(companion.positionMs, false)
                textFormat: Text.PlainText; secondary: true; font.pixelSize: 12; font.features: ({ "tnum": 1 })
            }
            Rectangle {
                Layout.fillWidth: true; Layout.leftMargin: 10; Layout.rightMargin: 10
                Layout.topMargin: 5; Layout.bottomMargin: 10
                implicitHeight: Math.min(88, Math.max(42, question.contentHeight + 18))
                color: Studio.paper; radius: 10
                border.color: question.activeFocus ? Studio.strongLine : Studio.line
                Behavior on border.color { ColorAnimation { duration: Studio.hover } }
                ScrollView {
                    anchors.left: parent.left; anchors.top: parent.top; anchors.bottom: parent.bottom; anchors.right: sendButton.left
                    anchors.margins: 4; contentWidth: availableWidth
                    ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                    TextArea {
                        id: question; objectName: "watchChatQuestion"
                        placeholderText: "问问 Kino…"; Accessible.name: "向 Kino 提问"
                        color: Studio.ink; placeholderTextColor: Studio.muted
                        font.pixelSize: 14; wrapMode: TextEdit.Wrap; selectByMouse: true
                        topPadding: 6; leftPadding: 6; bottomPadding: 6
                        background: null; focusPolicy: Qt.StrongFocus
                        Keys.onPressed: event => {
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
                    glyph: "up"; hint: "发送问题"; primary: true
                    implicitWidth: 31; implicitHeight: 31; corner: 9
                    enabled: !!question.text.trim() && !workspace.watchBusy
                    onClicked: companion.send()
                }
            }
        }
    }
}
