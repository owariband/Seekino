import QtQuick
import QtQuick.Controls.Basic

ComboBox {
    id: picker
    implicitHeight: 34
    implicitWidth: Math.max(86, contentItem.implicitWidth + leftPadding + rightPadding)
    leftPadding: 11
    rightPadding: 32
    hoverEnabled: true
    font.pixelSize: Studio.labelSize
    opacity: enabled ? 1 : 0.5
    contentItem: Text {
        text: picker.displayText
        font: picker.font
        color: Studio.ink
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
    }
    indicator: Glyph {
        width: 14; height: width
        x: picker.width - width - 11; anchors.verticalCenter: parent.verticalCenter
        name: "down"; ink: Studio.muted
    }
    background: Rectangle {
        radius: 8; color: picker.hovered ? Studio.wash : Studio.field
        border.color: picker.visualFocus || picker.popup.visible ? Studio.strongLine : Studio.line
        Behavior on color { ColorAnimation { duration: Studio.hover } }
    }
    delegate: ItemDelegate {
        required property int index
        width: picker.popup.width - 8; implicitHeight: 30
        highlighted: picker.highlightedIndex === index
        contentItem: Text {
            text: picker.textAt(index); color: Studio.ink; elide: Text.ElideRight
            font: picker.font; verticalAlignment: Text.AlignVCenter
        }
        background: Rectangle { radius: 5; color: parent.highlighted ? Studio.selected : "transparent" }
    }
    popup: Popup {
        y: picker.height + 3; width: picker.width; padding: 4
        implicitHeight: Math.min(236, list.contentHeight + 8)
        contentItem: ListView {
            id: list; clip: true; model: picker.popup.visible ? picker.delegateModel : null
            currentIndex: picker.highlightedIndex; ScrollBar.vertical: ScrollBar {}
        }
        background: Rectangle { radius: 9; color: Studio.sidebar; border.color: Studio.line; CardShadow { radius: 9 } }
        enter: Transition { NumberAnimation { property: "opacity"; from: 0; to: 1; duration: Studio.quick } }
        exit: Transition { NumberAnimation { property: "opacity"; from: 1; to: 0; duration: Studio.quick } }
    }
}
