import QtQuick
import QtQuick.Layouts
import QtQuick.Controls.Basic

Button {
    id: card
    property string title: ""
    property string markedTitle: ""
    property string sourceLabel: ""
    property string filename: ""
    property string timeRange: ""
    property string preview: ""
    readonly property bool compact: width < 500
    readonly property int thumbWidth: compact ? 104 : 168
    implicitHeight: Math.max(compact ? 102 : 118, body.implicitHeight + 22)
    height: implicitHeight
    leftPadding: 10; rightPadding: compact ? 10 : 16; topPadding: 10; bottomPadding: 10
    hoverEnabled: true; activeFocusOnTab: true
    Accessible.name: [title, timeRange, sourceLabel, filename, "回看原片"].join("，")
    SequentialAnimation {
        id: entry
        ParallelAnimation {
            NumberAnimation { target: card.contentItem; property: "opacity"; from: 0; to: 1; duration: Studio.hover }
            NumberAnimation { target: slide; property: "y"; from: 4; to: 0; duration: Studio.panel; easing.type: Easing.OutCubic }
        }
    }
    Component.onCompleted: if (!Studio.reduce) entry.start()
    background: Rectangle {
        radius: 14; border.width: card.visualFocus ? 2 : 1
        border.color: card.visualFocus ? Studio.accent : card.hovered ? Studio.strongLine : Studio.line
        color: card.down ? Studio.selected : card.hovered || card.visualFocus ? Studio.wash : Studio.sheet
        Behavior on color { ColorAnimation { duration: Studio.hover } }
        Behavior on border.color { ColorAnimation { duration: Studio.hover } }
    }
    contentItem: RowLayout {
        transform: Translate { id: slide }
        spacing: card.compact ? 10 : 14
        Rectangle {
            Layout.preferredWidth: card.thumbWidth; Layout.preferredHeight: Math.round(card.thumbWidth*9/16)
            radius: 8; color: Studio.surfaceMuted; clip: true
            Image {
                id: thumb; objectName: "hitThumbnail"
                anchors.fill: parent; sourceSize: Qt.size(card.thumbWidth*2,Math.round(card.thumbWidth*9/16)*2)
                source: card.preview; asynchronous: true; cache: false; fillMode: Image.PreserveAspectFit
            }
            Column {
                visible: thumb.status !== Image.Ready; anchors.centerIn: parent; spacing: 4
                Glyph { anchors.horizontalCenter: parent.horizontalCenter; width: 20; height: 20; name: "film"; ink: Studio.muted }
                Mark { anchors.horizontalCenter: parent.horizontalCenter; text: thumb.status === Image.Loading ? "载入画面…" : "暂无预览"; secondary: true; font.pixelSize: 11 }
            }
        }
        ColumnLayout {
            id: body; Layout.fillWidth: true; spacing: 5
            Mark {
                Layout.fillWidth: true; color: Studio.dark ? Studio.accent : Studio.actionBg
                font.pixelSize: 12; font.weight: Font.DemiBold; text: card.timeRange
            }
            Text {
                Layout.fillWidth: true; maximumLineCount: 2; wrapMode: Text.Wrap; elide: Text.ElideRight
                color: Studio.ink; font.pixelSize: card.compact ? 14 : 15; font.weight: Font.Medium
                textFormat: Text.StyledText
                // The controller escaped all source text and inserted only <b> markers.
                text: card.markedTitle.replace(/<b>/g, '<b><font color="' + (Studio.dark ? Studio.accent : Studio.actionBg) + '">').replace(/<\/b>/g,"</font></b>")
            }
            Mark { Layout.fillWidth: true; visible: !!card.sourceLabel; textFormat: Text.PlainText; text: card.sourceLabel; secondary: true; font.pixelSize: 12 }
            RowLayout {
                Layout.fillWidth: true; visible: !!card.filename; spacing: 5
                Glyph { Layout.preferredWidth: 12; Layout.preferredHeight: 12; name: "film"; ink: Studio.muted }
                Mark { Layout.fillWidth: true; textFormat: Text.PlainText; text: card.filename; secondary: true; font.pixelSize: 12 }
            }
        }
        Row {
            spacing: 5
            Glyph { anchors.verticalCenter: parent.verticalCenter; width: 15; height: 15; name: "play"; ink: Studio.dark ? Studio.accent : Studio.actionBg }
            Mark { visible: !card.compact; text: "回看原片"; font.pixelSize: 12; color: Studio.dark ? Studio.accent : Studio.actionBg }
        }
    }
    Keys.onReturnPressed: click()
    Keys.onEnterPressed: click()
}
