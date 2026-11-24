import QtQuick
import QtQuick.Layouts
import QtQuick.Controls.Basic

Button {
    id: card
    property string title: ""
    signal ask()
    property string markedTitle: ""
    property string sourceLabel: ""
    property string filename: ""
    property string timeRange: ""
    property string preview: ""
    readonly property bool compact: width < 500
    readonly property int thumbWidth: compact ? 104 : 168
    implicitHeight: Math.max(compact ? 102 : 118, body.implicitHeight + 24)
    height: implicitHeight
    leftPadding: 16; rightPadding: 12; topPadding: 12; bottomPadding: 12
    hoverEnabled: true; activeFocusOnTab: true
    Accessible.name: [title, sourceLabel, timeRange, filename, qsTr("回看原片")].filter(Boolean).join("，")
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
        Column {
            id: body
            Layout.fillWidth: true; Layout.minimumWidth: 0; Layout.preferredWidth: 0
            spacing: 4
            Text {
                width: body.width; maximumLineCount: 2; wrapMode: Text.Wrap; elide: Text.ElideRight
                color: Studio.ink; font.pixelSize: 15
                lineHeightMode: Text.FixedHeight; lineHeight: 23
                textFormat: card.markedTitle ? Text.StyledText : Text.PlainText
                // The controller escaped all source text and inserted only <b> markers.
                text: card.markedTitle ? card.markedTitle.replace(/<b>/g, '<b><font color="' + (Studio.dark ? Studio.accent : Studio.actionBg) + '">').replace(/<\/b>/g,"</font></b>") : card.title
            }
            Row {
                spacing: 6
                Mark {
                    visible: !!text; text: card.sourceLabel || card.filename
                    width: Math.min(implicitWidth, Math.max(0, body.width - timestamp.implicitWidth - 20))
                    textFormat: Text.PlainText; secondary: true; font.pixelSize: 12
                }
                Mark { visible: !!(card.sourceLabel || card.filename) && !!card.timeRange; text: "·"; secondary: true; font.pixelSize: 12 }
                Mark {
                    id: timestamp
                    text: card.timeRange; secondary: true; font.pixelSize: 12; font.features: ({ "tnum": 1 })
                }
            }
            Tool {
                objectName: "askHit"
                text: qsTr("引用并问 Kino"); hint: qsTr("引用这个片段向 Kino 提问")
                textSize: 12; implicitHeight: 25; contentColor: Studio.accent
                onClicked: card.ask()
            }
            Mark {
                width: body.width; visible: !!card.sourceLabel && !!text && text !== card.sourceLabel
                textFormat: Text.PlainText; text: card.filename; color: Studio.dim; font.pixelSize: 12
            }
        }
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
                Mark { anchors.horizontalCenter: parent.horizontalCenter; text: thumb.status === Image.Loading ? qsTr("载入画面…") : qsTr("暂无预览"); secondary: true; font.pixelSize: 11 }
            }
            Rectangle {
                anchors.right: parent.right; anchors.bottom: parent.bottom; anchors.margins: 4
                height: 22; radius: height/2; color: "#d0192320"
                width: label.visible ? label.implicitWidth + 29 : height
                Behavior on width { NumberAnimation { duration: Studio.hover; easing.type: Easing.OutCubic } }
                clip: true
                Glyph { x: 5; anchors.verticalCenter: parent.verticalCenter; width: 12; height: 12; name: "play"; ink: "#f3f8f5" }
                Mark {
                    id: label; x: 22; anchors.verticalCenter: parent.verticalCenter
                    visible: card.hovered || card.visualFocus
                    text: qsTr("回看原片"); color: "#f3f8f5"; font.pixelSize: 11
                }
            }
        }
    }
    Keys.onReturnPressed: click()
    Keys.onEnterPressed: click()
}
