import QtQuick
import QtQuick.Controls.Basic

AbstractButton {
    id: brand
    Accessible.name: "Seekino 首页"
    implicitWidth: label.implicitWidth + 10; implicitHeight: 32
    FontLoader { id: inter; source: "art/inter-wordmark-750.ttf" }
    contentItem: Item {
        Text {
            id: label; anchors.centerIn: parent
            text: "Seekino"; color: Studio.wordmark; font.family: inter.name
            font.pixelSize: 22; font.letterSpacing: -1.1; font.weight: 750
            // The web wordmark gives the i dot its own color.
            Rectangle {
                x: metrics.advanceWidth("Seek") - 4.4 + 1.15
                y: 3; width: 3.5; height: 3.5; radius: 2
                color: Studio.dark ? "#ffffff" : "#74c5ba"
            }
        }
    }
    FontMetrics { id: metrics; font.family: inter.name; font.pixelSize: 22; font.weight: 750 }
    background: Rectangle { radius: 7; color: "transparent"; border.width: brand.visualFocus ? 2 : 0; border.color: Studio.accent }
}
