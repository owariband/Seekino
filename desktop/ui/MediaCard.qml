import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts

AbstractButton {
    id: card
    property string title: ""
    property string cover: ""
    property string coverFallback: ""
    property bool compactShelf: false
    property string detail: ""
    property string badge: ""
    property bool episode: false
    implicitHeight: Math.ceil(width * (episode ? 9/16 : 10/16)) + (episode ? 55 : 86)
    padding: 0; focusPolicy: Qt.StrongFocus
    hoverEnabled: true; Accessible.name: [title,detail].join("，")
    contentItem: ColumnLayout {
        spacing: card.episode ? 5 : 0
        Item {
            id: art
            Layout.fillWidth: true; Layout.preferredHeight: Math.round(card.width*(card.episode ? 9/16 : 10/16))
            transform: Translate { y: card.down && !Studio.reduce ? 1 : card.hovered && !Studio.reduce ? -2 : 0; Behavior on y { NumberAnimation { duration: Studio.hover; easing.type: Easing.OutCubic } } }
            Rectangle { anchors.fill: parent; radius: card.episode ? 4 : 14; color: Studio.surfaceMuted; border.color: card.visualFocus ? Studio.accent : Studio.line }
            CardShadow { radius: card.episode ? 4 : 14; opacity: card.hovered ? 1 : 0; Behavior on opacity { NumberAnimation { duration: Studio.hover } } }
            Glyph { anchors.centerIn: parent; width: 30; height: 30; name: "film"; ink: Studio.actionBg; visible: card.episode }
            Column { visible: !card.episode; anchors.centerIn: parent; width: parent.width - 32; spacing: 15
                Glyph { anchors.horizontalCenter: parent.horizontalCenter; width: 30; height: 30; name: "film"; ink: Studio.actionBg }
                Mark { width: parent.width; text: card.title; textFormat: Text.PlainText; color: Studio.actionBg; horizontalAlignment: Text.AlignHCenter; wrapMode: Text.Wrap; maximumLineCount: 3; font.pixelSize: 18; font.weight: Font.DemiBold }
            }
            Image {
                anchors.fill: parent; visible: coverImage.status !== Image.Ready; source: coverImage.status === Image.Error ? card.coverFallback : ""
                asynchronous: true; mipmap: true; fillMode: Image.PreserveAspectCrop; clip: true
            }
            Image {
                id: coverImage; anchors.fill: parent; source: card.cover; asynchronous: true; mipmap: true
                fillMode: Image.PreserveAspectCrop; clip: true
                opacity: status === Image.Ready ? 1 : 0
                Behavior on opacity { NumberAnimation { duration: Studio.hover } }
            }
            Rectangle { anchors.fill: parent; color: "#29000000"; visible: !card.episode && Studio.dark && coverImage.status === Image.Ready }
            Canvas {
                anchors.fill: parent
                property color paper: Studio.paper
                property color outline: card.visualFocus ? Studio.accent : Studio.line
                onPaperChanged: requestPaint()
                onOutlineChanged: requestPaint()
                onPaint: {
                    const ctx=getContext("2d"), r=card.episode ? 4 : 14
                    ctx.reset()
                    // Clip the four outside corners without a GPU-only mask on software renderers.
                    for (let i=0;i<4;i++) {
                        ctx.save(); ctx.translate(i===1 || i===2 ? width : 0,i>=2 ? height : 0)
                        ctx.rotate(i*Math.PI/2); ctx.beginPath()
                        ctx.moveTo(0,0); ctx.lineTo(r,0); ctx.arc(r,r,r,-Math.PI/2,-Math.PI,true)
                        ctx.closePath(); ctx.fillStyle=paper; ctx.fill(); ctx.restore()
                    }
                    const w=card.visualFocus ? 2 : 1
                    ctx.beginPath(); ctx.roundedRect(w/2,w/2,width-w,height-w,r,r)
                    ctx.lineWidth=w; ctx.strokeStyle=outline; ctx.stroke()
                }
            }
            Rectangle {
                anchors.centerIn: parent; width: 42; height: 42; radius: 21
                visible: card.episode; color: "#a6090c0b"; border.color: "#80ffffff"
                opacity: card.hovered || card.visualFocus ? 1 : 0; Behavior on opacity { NumberAnimation { duration: Studio.hover } }
                Glyph { anchors.centerIn: parent; width: 18; height: 18; name: "play"; ink: "#ffffff" }
            }
            Rectangle {
                anchors.bottom: parent.bottom; anchors.right: parent.right; anchors.margins: 7
                visible: card.badge.length > 0; width: duration.implicitWidth+10; height: 19; radius: 4; color: "#c7090c0b"
                Mark { id: duration; anchors.centerIn: parent; text: card.badge; font.pixelSize: 11; color: "#f3f8f5" }
            }
        }
        Mark { Layout.fillWidth: true; Layout.topMargin: card.episode ? 3 : 14; text: card.title; color: card.hovered || card.visualFocus ? Studio.actionBg : Studio.ink; textFormat: Text.PlainText; font.pixelSize: card.episode ? 15 : card.compactShelf ? 14 : 16; font.weight: Font.DemiBold; wrapMode: Text.Wrap; maximumLineCount: card.episode ? 1 : 2 }
        Mark { Layout.fillWidth: true; Layout.topMargin: card.episode ? 0 : 7; textFormat: Text.PlainText; text: card.detail; color: Studio.muted; font.pixelSize: 12 }
        Item { visible: !card.episode; Layout.fillHeight: true }
    }
}
