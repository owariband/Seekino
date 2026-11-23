import QtQuick
import QtQuick.Layouts

ColumnLayout {
    id: empty
    property string title: ""
    property string message: ""
    property string glyph: "film"
    property string action: ""
    property bool firstImport: false
    property bool needsModel: false
    signal activated()
    signal settingsRequested()
    spacing: 0
    Kino {
        visible: empty.firstImport
        Layout.alignment: Qt.AlignHCenter
        Layout.preferredWidth: 192; Layout.preferredHeight: 84
        focusPolicy: Qt.ClickFocus
    }
    Image {
        visible: !empty.firstImport
        Layout.alignment: Qt.AlignHCenter
        Layout.preferredWidth: 72; Layout.preferredHeight: 72
        source: "art/mascot-" + (empty.glyph === "library" ? "seek" : "puzzled") + ".png"
        sourceSize: Qt.size(144,144); fillMode: Image.PreserveAspectFit
        transformOrigin: Item.Bottom; rotation: -4
        SequentialAnimation on rotation {
            running: empty.visible && !empty.firstImport && !Studio.reduce && empty.Window.window?.active !== false
            loops: Animation.Infinite
            NumberAnimation { to: -7; duration: 1600; easing.type: Easing.InOutSine }
            NumberAnimation { to: -4; duration: 1600; easing.type: Easing.InOutSine }
        }
    }
    Text {
        Layout.fillWidth: true; Layout.topMargin: 12; text: empty.title; color: Studio.ink
        font.pixelSize: empty.firstImport ? 27 : 22; font.weight: Font.DemiBold
        horizontalAlignment: Text.AlignHCenter; wrapMode: Text.Wrap
    }
    Text {
        Layout.fillWidth: true; Layout.topMargin: 8; text: empty.message; visible: text.length > 0
        color: Studio.muted; font.pixelSize: 14; lineHeight: 1.45
        horizontalAlignment: Text.AlignHCenter; wrapMode: Text.Wrap
    }
    Tool {
        objectName: empty.firstImport ? "emptyImport" : "emptyAction"
        Layout.alignment: Qt.AlignHCenter; Layout.topMargin: empty.firstImport ? 22 : 20
        visible: empty.action.length > 0; glyph: "import"; text: empty.action
        primary: true; onClicked: empty.activated()
    }
    Tool {
        Layout.topMargin: 6; Layout.alignment: Qt.AlignHCenter
        visible: empty.firstImport && empty.needsModel
        text: "配置解析模型…"; textSize: 12
        onClicked: empty.settingsRequested()
    }
}
