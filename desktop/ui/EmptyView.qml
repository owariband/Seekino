import QtQuick
import QtQuick.Layouts

ColumnLayout {
    id: empty
    property string title: ""
    property string message: ""
    property string glyph: "film"
    property string action: ""
    signal activated()
    spacing: 0
    Image {
        Layout.alignment: Qt.AlignHCenter; Layout.preferredWidth: 72; Layout.preferredHeight: 72
        source: "art/mascot-" + (empty.glyph === "library" ? "seek" : "puzzled") + ".png"
        sourceSize: Qt.size(144,144); fillMode: Image.PreserveAspectFit
        transformOrigin: Item.Bottom; rotation: -4
        SequentialAnimation on rotation {
            running: empty.visible && !Studio.reduce && empty.Window.window?.active !== false
            loops: Animation.Infinite
            NumberAnimation { to: -7; duration: 1600; easing.type: Easing.InOutSine }
            NumberAnimation { to: -4; duration: 1600; easing.type: Easing.InOutSine }
        }
    }
    Text { Layout.fillWidth: true; Layout.topMargin: 12; text: empty.title; color: Studio.ink; font.pixelSize: 22; font.weight: Font.DemiBold; horizontalAlignment: Text.AlignHCenter; wrapMode: Text.Wrap }
    Text { Layout.fillWidth: true; Layout.topMargin: 8; text: empty.message; visible: text.length > 0; color: Studio.muted; font.pixelSize: 14; horizontalAlignment: Text.AlignHCenter; wrapMode: Text.Wrap }
    Tool { objectName: "emptyAction"; Layout.alignment: Qt.AlignHCenter; Layout.topMargin: 20; visible: empty.action.length > 0; glyph: "import"; text: empty.action; primary: true; onClicked: empty.activated() }
}
