import QtQuick
import QtQuick.Layouts
import QtQuick.Controls.Basic

Button {
    id: button
    property string title: ""
    property string detail: ""
    property string glyph: "film"
    property string openingGlyph: "play"
    property string cover: ""
    property string trailing: ""
    property bool multiline: false
    height: multiline ? 77 : 58; hoverEnabled: true
    SequentialAnimation {
        id: entry
        ParallelAnimation {
            NumberAnimation { target: button.contentItem; property: "opacity"; from: 0; to: 1; duration: Studio.hover }
            NumberAnimation { target: slide; property: "y"; from: Studio.reduce ? 0 : 3; to: 0; duration: Studio.panel; easing.type: Easing.OutCubic }
        }
    }
    Component.onCompleted: if (!Studio.reduce) entry.start()
    Accessible.name: [title, detail, trailing].filter(Boolean).join("，")
    leftPadding: 14; rightPadding: 14; topPadding: 4; bottomPadding: 4
    background: Rectangle {
        radius: 12; border.color: button.hovered || button.visualFocus ? Studio.strongLine : Studio.line
        color: button.down ? Studio.selected : button.hovered || button.visualFocus ? Studio.wash : Studio.sheet
        Behavior on color { ColorAnimation { duration: Studio.hover } }

    }
    contentItem: RowLayout {
        transform: Translate { id: slide }
        spacing: 12
        Item {
            Layout.preferredWidth: button.glyph === "folder" ? 26 : 64
            Layout.preferredHeight: button.glyph === "folder" ? 26 : 38
            Rectangle { anchors.fill: parent; radius: 3; visible: button.glyph !== "folder"; color: Studio.sidebar }
            Image { id: thumb; anchors.fill: parent; source: button.cover; asynchronous: true; fillMode: Image.PreserveAspectCrop; clip: true }
            Glyph { anchors.centerIn: parent; width: button.glyph === "folder" ? 21 : 18; height: width; visible: thumb.status !== Image.Ready; name: button.glyph; ink: Studio.dim }
        }
        ColumnLayout {
            Layout.fillWidth: true; spacing: 4
            Mark { Layout.fillWidth: true; text: button.title; font.weight: Font.Medium; maximumLineCount: button.multiline ? 2 : 1; wrapMode: button.multiline ? Text.Wrap : Text.NoWrap }
            Mark { Layout.fillWidth: true; text: button.detail; font.pixelSize: 11; secondary: true }
        }
        Mark { text: button.trailing; font.pixelSize: 11; secondary: true; visible: button.trailing.length > 0 }
        Glyph { Layout.preferredWidth: 14; Layout.preferredHeight: 14; name: button.openingGlyph; ink: Studio.muted; opacity: button.hovered || button.visualFocus ? 1 : .45; Behavior on opacity { NumberAnimation { duration: Studio.hover } } }
    }
}
