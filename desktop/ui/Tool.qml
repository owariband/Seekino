import QtQuick
import QtQuick.Controls.Basic

Button {
    id: button
    property string glyph: ""
    property bool trailingGlyph: false
    property string hint: ""
    property bool chosen: false
    property bool primary: false
    property bool outlined: false
    property real corner: primary || outlined ? height / 2 : 8
    property color fillColor: primary ? Studio.actionBg : (outlined ? Studio.sidebar : "transparent")
    property color contentColor: primary ? Studio.actionFg : (chosen ? Studio.actionBg : Studio.muted)
    property int symbolSize: 17
    property int textSize: Studio.labelSize
    hoverEnabled: true
    focusPolicy: Qt.StrongFocus
    implicitWidth: text.length ? contentItem.implicitWidth + (primary ? 34 : 22) : 30
    implicitHeight: primary ? 40 : 30
    padding: 0
    opacity: enabled ? 1 : .42
    Behavior on opacity { NumberAnimation { duration: Studio.quick } }
    Accessible.name: hint || text
    ToolTip.visible: hovered && hint.length > 0
    ToolTip.text: hint
    ToolTip.delay: 650
    contentItem: Item {
        implicitWidth: row.implicitWidth
        Row {
            id: row
            layoutDirection: button.trailingGlyph ? Qt.RightToLeft : Qt.LeftToRight
            anchors.centerIn: parent
            spacing: 7
            Glyph { width: button.symbolSize; height: width; anchors.verticalCenter: parent.verticalCenter; visible: button.glyph !== ""; name: button.glyph; ink: button.contentColor }
            Text { visible: text.length > 0; text: button.text; anchors.verticalCenter: parent.verticalCenter; color: button.contentColor; font.pixelSize: button.textSize; font.weight: button.primary ? Font.Medium : Font.Normal }
        }
    }
    background: Rectangle {
        radius: button.corner
        color: button.primary ? (button.down ? Qt.darker(button.fillColor,1.12) : button.hovered ? Qt.lighter(button.fillColor,1.06) : button.fillColor)
            : button.down ? Studio.selected : button.chosen ? Studio.selected : button.hovered ? Studio.surfaceMuted : button.fillColor
        border.width: button.visualFocus ? 2 : button.outlined ? 1 : 0
        border.color: button.visualFocus ? Studio.accent : Studio.line
        Behavior on color { ColorAnimation { duration: Studio.hover } }
        Behavior on border.color { ColorAnimation { duration: Studio.quick } }
    }
    scale: down && !Studio.reduce ? .96 : 1
    Behavior on scale { NumberAnimation { duration: Studio.quick; easing.type: Easing.OutCubic } }
}
