import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts

AbstractButton {
    id: card
    property string title: ""
    property string cover: ""
    property string detail: ""
    property string badge: ""
    property bool episode: false
    hoverEnabled: true; Accessible.name: [title,detail].join("，")
    contentItem: ColumnLayout {
        spacing: 5
        Item {
            id: art
            Layout.fillWidth: true; Layout.preferredHeight: width*(card.episode ? 9/16 : 10/16)
            transform: Translate { y: card.hovered && !Studio.reduce ? -3 : 0; Behavior on y { NumberAnimation { duration: Studio.hover; easing.type: Easing.OutCubic } } }
            Rectangle { anchors.fill: parent; radius: card.episode ? 4 : 14; color: Studio.surfaceMuted; border.color: card.visualFocus ? Studio.accent : Studio.line }
            CardShadow { radius: card.episode ? 4 : 14; opacity: card.hovered ? 1 : 0; Behavior on opacity { NumberAnimation { duration: Studio.hover } } }
            Glyph { anchors.centerIn: parent; width: 30; height: 30; name: "film"; ink: Studio.actionBg }
            Image { anchors.fill: parent; source: card.cover; asynchronous: true; fillMode: Image.PreserveAspectCrop; clip: true }
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
        Mark { Layout.fillWidth: true; Layout.topMargin: 3; text: card.title; color: card.hovered ? Studio.actionBg : Studio.ink; font.pixelSize: 15; font.weight: Font.DemiBold }
        Mark { Layout.fillWidth: true; text: card.detail; color: Studio.muted; font.pixelSize: 12 }
    }
}
