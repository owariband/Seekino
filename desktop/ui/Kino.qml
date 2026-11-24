import QtQuick
import QtQuick.Controls.Basic

AbstractButton {
    id: kino
    property string mood: "idle"
    property bool poked: false
    readonly property string pose: poked ? "jump" : mood
    readonly property int frames: pose === "jump" ? 29 : pose === "loading" ? 12 : 36
    property int frame: 0
    implicitWidth: 192; implicitHeight: 84
    Accessible.name: qsTr("戳一戳 Kino")
    hoverEnabled: true
    onClicked: { if (!Studio.reduce) { poked = true; frame = 0 } }
    onPoseChanged: frame = 0
    onVisibleChanged: if (!visible) { poked = false; frame = 0 }
    Timer {
        interval: kino.poked ? 67 : kino.pose === "loading" ? 100 : 167
        running: kino.visible && kino.Window.window?.active !== false && !Studio.reduce
        repeat: true
        onTriggered: {
            kino.frame = (kino.frame + 1) % kino.frames
            if (kino.poked && kino.frame === 0) kino.poked = false
        }
    }
    background: Rectangle { radius: 12; color: "transparent"; border.width: kino.visualFocus ? 2 : 0; border.color: Studio.accent }
    contentItem: Item {
        // These exact poses are rasterized from web/src/shared/kino/engine.ts at its native pixel size.
        clip: true
        Image {
            source: "art/kino-" + kino.pose + ".png"; smooth: false; mipmap: false
            width: parent.width * kino.frames; height: parent.height
            x: -parent.width * (Studio.reduce ? 0 : kino.frame)
        }
    }
}
