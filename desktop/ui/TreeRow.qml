import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts

Button {
    id: rowButton
    property bool branch: false
    property bool expanded: false
    property bool current: false
    property int count: 0
    signal disclose()
    width: parent.width; height: 28
    hoverEnabled: true
    Accessible.name: text
    Keys.onLeftPressed: event => {
        if (branch && expanded) disclose()
        else event.accepted = false
    }
    Keys.onRightPressed: event => {
        if (branch && !expanded) disclose()
        else event.accepted = false
    }
    Keys.onUpPressed: focusNextPrevChild(false)
    Keys.onDownPressed: focusNextPrevChild(true)
    function focusNextPrevChild(forward) {
        const next = nextItemInFocusChain(forward)
        if (next) next.forceActiveFocus(Qt.TabFocusReason)
    }
    background: Rectangle {
        color: rowButton.current ? Studio.selected : rowButton.hovered || rowButton.down ? Studio.wash : "transparent"
        radius: 6; border.width: rowButton.visualFocus ? 2 : 0; border.color: Studio.accent
        Behavior on color { ColorAnimation { duration: Studio.hover } }
    }
    contentItem: RowLayout {
        spacing: 5
        Item {
            Layout.preferredWidth: rowButton.branch ? 12 : 22
            Layout.preferredHeight: 20
            Glyph {
                anchors.centerIn: parent; width: 10; height: 10; visible: rowButton.branch
                name: "chevron"; ink: Studio.dim; rotation: rowButton.expanded ? 90 : 0
                Behavior on rotation { NumberAnimation { duration: Studio.hover; easing.type: Easing.OutCubic } }
            }
        }
        Glyph { Layout.preferredWidth: 15; Layout.preferredHeight: 15; name: rowButton.branch ? "folder" : "film"; ink: rowButton.current ? Studio.ink : Studio.muted }
        Mark { Layout.fillWidth: true; text: rowButton.text; font.pixelSize: 12; color: rowButton.current ? Studio.ink : Studio.muted }
        Mark { visible: rowButton.branch && rowButton.count > 0; text: rowButton.count; font.pixelSize: 10; color: Studio.dim; Layout.rightMargin: 4 }
    }
    leftPadding: 2; rightPadding: 4
    ToolTip.visible: hovered && contentItem.width < fontMetrics.advanceWidth(text) + 56
    ToolTip.text: text
    ToolTip.delay: 550
    FontMetrics { id: fontMetrics; font.pixelSize: 12 }
}
