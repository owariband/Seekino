import QtQuick
import QtQuick.Controls.Basic

TextField {
    id: edit
    selectByMouse: true
    color: Studio.ink; placeholderTextColor: Studio.dim
    selectionColor: Studio.selected; selectedTextColor: Studio.ink
    font.pixelSize: 14
    leftPadding: 10; rightPadding: 10
    implicitHeight: 34
    background: Rectangle {
        radius: 10; color: Studio.field; border.width: 1
        border.color: edit.activeFocus ? Studio.strongLine : Studio.line
        Behavior on border.color { ColorAnimation { duration: Studio.hover } }
    }
}
