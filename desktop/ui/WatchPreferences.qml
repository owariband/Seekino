import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts

ColumnLayout {
    id: controls
    property bool compact: false
    spacing: 7
    function focusMode() { (compact ? fadeToggle : Studio.watchPanel.docked ? docked : floating).forceActiveFocus(Qt.PopupFocusReason) }

    component ModeButton: Tool {
        Layout.fillWidth: true; implicitHeight: 31; textSize: 13; corner: 6
        Accessible.role: Accessible.RadioButton; Accessible.checked: chosen
        fillColor: chosen ? Studio.selected : "transparent"
        contentColor: chosen ? Studio.actionBg : Studio.muted
        Keys.onLeftPressed: { floating.click(); floating.forceActiveFocus() }
        Keys.onRightPressed: { docked.click(); docked.forceActiveFocus() }
    }
    component Toggle: CheckBox {
        id: toggle
        implicitHeight: 32; leftPadding: 0; rightPadding: 3; spacing: 9
        indicator: Rectangle {
            x: 0; y: (toggle.height - height) / 2; width: 29; height: 17; radius: height / 2
            color: toggle.checked ? Studio.actionBg : Studio.surfaceMuted
            border.width: toggle.visualFocus ? 2 : 1; border.color: toggle.visualFocus ? Studio.accent : Studio.line
            Behavior on color { ColorAnimation { duration: Studio.quick } }
            Rectangle {
                y: 3; x: toggle.checked ? 15 : 3; width: 11; height: 11; radius: 6
                color: toggle.checked ? Studio.actionFg : Studio.muted
                Behavior on x { NumberAnimation { duration: Studio.quick; easing.type: Easing.OutCubic } }
            }
        }
        contentItem: Mark {
            leftPadding: toggle.indicator.width + toggle.spacing
            text: toggle.text; font.pixelSize: 13
        }
    }
    Mark { text: qsTr("对话面板"); font.weight: Font.Medium }
    Rectangle {
        Layout.preferredWidth: 220; Layout.maximumWidth: controls.width
        implicitHeight: 37; color: Studio.wash; border.color: Studio.line; radius: 8
        RowLayout {
            anchors.fill: parent; anchors.margins: 3; spacing: 2
            ModeButton {
                id: floating; objectName: "watchChooseFloating"; glyph: "floatWindow"
                text: qsTr("悬浮窗"); chosen: !Studio.watchPanel.docked; enabled: !controls.compact
                onClicked: Studio.watchPanel.docked = false
            }
            ModeButton {
                id: docked; objectName: "watchChooseDocked"; glyph: "dockRight"
                text: qsTr("右侧栏"); chosen: Studio.watchPanel.docked; enabled: !controls.compact
                onClicked: Studio.watchPanel.docked = true
            }
        }
    }
    Mark {
        visible: controls.compact; Layout.fillWidth: true
        text: qsTr("窄窗口中对话显示在播放器下方"); secondary: true
        wrapMode: Text.Wrap; maximumLineCount: 2; font.pixelSize: 12
    }
    RowLayout {
        Layout.fillWidth: true; spacing: 5
        Toggle {
            id: fadeToggle; objectName: "watchAutoFade"
            text: qsTr("浮窗自动淡化"); palette.windowText: Studio.ink
            checked: Studio.preferences.kinoFadeEnabled
            onToggled: Studio.preferences.kinoFadeEnabled = checked
        }
        Item { Layout.fillWidth: true }
        Entry {
            id: delay; objectName: "watchFadeSeconds"; Layout.preferredWidth: 53; enabled: fadeToggle.checked
            Accessible.name: qsTr("自动淡化时间（秒）")
            horizontalAlignment: Text.AlignHCenter
            validator: DoubleValidator { bottom: 1; top: 2147483; notation: DoubleValidator.StandardNotation; locale: Qt.locale().name }
            inputMethodHints: Qt.ImhFormattedNumbersOnly
            function display() { text = Number(Studio.preferences.kinoFadeSeconds).toLocaleString(Qt.locale(), "g", 12) }
            function save() {
                if (acceptableInput) Studio.preferences.kinoFadeSeconds = Number.fromLocaleString(Qt.locale(), text)
                display()
            }
            Component.onCompleted: display()
            onEditingFinished: save()
            // A value that failed the validator does not emit editingFinished on focus loss.
            onActiveFocusChanged: if (!activeFocus && !acceptableInput) display()
            Connections { target: Studio.preferences; function onKinoFadeSecondsChanged() { delay.display() } }
        }
        Mark { text: qsTr("秒"); secondary: true }
    }
    Toggle {
        objectName: "watchPauseOnSend"
        text: qsTr("发问时暂停，返回播放时继续"); palette.windowText: Studio.ink
        checked: Studio.preferences.pauseOnSend; onToggled: Studio.preferences.pauseOnSend = checked
    }
}
