import QtQuick
import QtQuick.Controls.Basic

AbstractButton {
    id: brand
    Accessible.name: "Seekino 首页"
    implicitWidth: 96; implicitHeight: 28
    leftPadding: 8; rightPadding: 8; topPadding: 4; bottomPadding: 4
    hoverEnabled: true
    background: Rectangle {
        color: "white"
        radius: 7
        border.width: brand.visualFocus ? 2 : 1
        border.color: brand.visualFocus ? Studio.accent : (brand.hovered ? Studio.strongLine : Studio.line)
        Behavior on border.color { ColorAnimation { duration: Studio.hover } }
    }
    contentItem: Image {
        // The Finder/Dock and Windows icons show this same master in full.
        source: "art/seekino-wordmark.png"
        sourceClipRect: Qt.rect(74, 412, 876, 200)
        fillMode: Image.PreserveAspectFit
        mipmap: true
    }
    scale: down && !Studio.reduce ? 0.97 : 1
    Behavior on scale { NumberAnimation { duration: Studio.quick; easing.type: Easing.OutCubic } }
}
