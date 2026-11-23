import QtQuick
import QtQuick.Layouts
import QtQuick.Controls.Basic
import Seekino.Media 1.0

FocusScope {
    id: player
    property string title: ""
    property bool hit: false
    property int startMs: 0
    property int endMs: 0
    property bool cinema: false
    property int previousVisibility: Window.Windowed
    property bool controlsAwake: true
    readonly property color timelineColor: "#89d6c8"

    function open(path, start, end, duration, caption, isHit) {
        title = caption; startMs = start; endMs = end; hit = isHit
        surface.open(path, start, duration)
        reveal()
        Qt.callLater(() => { if (player.enabled) player.forceActiveFocus() })
    }
    function close() {
        if (cinema) toggleCinema()
        hideControls.stop()
        surface.close()
    }
    Component.onDestruction: if (surface) surface.close()
    function reveal() { controlsAwake = true; hideControls.restart() }
    function playPause() { reveal(); surface.setPlaying(!surface.playing) }
    function skip(ms) { reveal(); surface.seek(Math.max(0, Math.min(surface.durationMs, surface.positionMs + ms))) }
    function toggleMute() {
        reveal()
        if (surface.muted || surface.volume === 0) {
            if (surface.volume === 0) surface.volume = .5
            surface.muted = false
        } else surface.muted = true
    }
    function toggleCinema() {
        const win = player.Window.window
        if (!win) return
        if (!cinema) {
            previousVisibility = win.visibility; cinema = true
            win.showFullScreen()
        } else {
            cinema = false
            if (previousVisibility === Window.Maximized) win.showMaximized()
            else if (previousVisibility !== Window.FullScreen) win.showNormal()
        }
        reveal()
        player.forceActiveFocus()
    }
    Connections {
        target: player.Window.window
        function onVisibilityChanged(visibility) {
            if (player.cinema && visibility !== Window.FullScreen) player.cinema = false
        }
    }
    Shortcut { sequence: "Escape"; enabled: player.cinema; onActivated: player.toggleCinema() }
    Keys.onPressed: event => {
        if (event.modifiers !== Qt.NoModifier) return
        switch (event.key) {
        case Qt.Key_Left: skip(-10000); break
        case Qt.Key_Right: skip(10000); break
        case Qt.Key_K: playPause(); break
        case Qt.Key_M: toggleMute(); break
        case Qt.Key_F: if (!event.isAutoRepeat) toggleCinema(); break
        default: return
        }
        event.accepted = true
    }
    Timer {
        id: hideControls; interval: 5000
        onTriggered: {
            if (seek.pressed || gain.pressed || controlsPointer.hovered) restart()
            else player.controlsAwake = false
        }
    }

    component PlayerButton: Button {
        id: key
        property string symbol: ""
        property string hint: ""
        property bool circle: false
        property int symbolSize: 18
        implicitWidth: circle ? 64 : text ? 46 : 34
        implicitHeight: circle ? 64 : 34
        padding: 0
        hoverEnabled: true
        Accessible.name: hint
        ToolTip.visible: hovered && hint.length > 0
        ToolTip.text: hint
        ToolTip.delay: 650
        focusPolicy: Qt.StrongFocus
        onActiveFocusChanged: if (visualFocus) player.reveal()
        contentItem: Item {
            Glyph { anchors.centerIn: parent; visible: !!key.symbol; width: key.symbolSize; height: width; name: key.symbol; ink: "#f7f8f7" }
            Text { anchors.centerIn: parent; text: key.text; font.pixelSize: 13; font.weight: Font.DemiBold; color: "#f7f8f7" }
        }
        background: Rectangle {
            radius: key.circle ? width / 2 : 7
            color: key.circle ? (key.hovered ? "#e6145e54" : "#b30b1413") : key.down ? "#40ffffff" : key.hovered ? "#20ffffff" : "transparent"
            border.width: key.visualFocus ? 2 : key.circle ? 1 : 0
            border.color: key.visualFocus ? player.timelineColor : "#71ffffff"
            Behavior on color { ColorAnimation { duration: Studio.hover } }
        }
        scale: Studio.reduce ? 1 : down ? .94 : circle && hovered ? 1.07 : 1
        Behavior on scale { NumberAnimation { duration: Studio.quick; easing.type: Easing.OutCubic } }
    }

    Rectangle {
        id: stage
        parent: player.cinema ? player.Overlay.overlay : player
        anchors.fill: parent
        anchors.bottomMargin: !player.cinema && player.hit ? evidence.height : 0
        z: player.cinema ? 100 : 0
        clip: true
        color: "#090c0b"
        VideoSurface { id: surface; objectName: "playerSurface"; anchors.fill: parent }
        HoverHandler {
            property point previous: Qt.point(-1,-1)
            onPointChanged: {
                if (previous.x !== point.position.x || previous.y !== point.position.y) {
                    previous = point.position
                    player.reveal()
                }
            }
            onHoveredChanged: if (hovered) player.reveal()
        }
        TapHandler {
            onTapped: { player.forceActiveFocus(); player.playPause() }
        }
        PlayerButton {
            objectName: "centerPlayButton"
            anchors.centerIn: parent
            circle: true; symbol: "play"; symbolSize: 30; hint: "播放 (K)"
            visible: !surface.playing && !surface.error && surface.hasFrame
            onClicked: player.playPause()
        }

        Rectangle {
            id: overlay; objectName: "playbackControls"
            anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom
            height: 102
            readonly property bool show: player.controlsAwake || !surface.playing || seek.visualFocus || gain.visualFocus || row.activeFocus
            opacity: show ? 1 : 0
            gradient: Gradient {
                GradientStop { position: 0; color: "transparent" }
                GradientStop { position: .4; color: "#7a000000" }
                GradientStop { position: 1; color: "#d0000000" }
            }
            Behavior on opacity { NumberAnimation { duration: Studio.reduce ? 0 : overlay.show ? 160 : 200; easing.type: Easing.OutCubic } }
            HoverHandler { id: controlsPointer; onHoveredChanged: if (hovered) player.reveal() }
            ColumnLayout {
                anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom
                anchors.leftMargin: Math.max(12, Math.min(28,parent.width*.02))
                anchors.rightMargin: anchors.leftMargin; anchors.bottomMargin: 6
                spacing: 1
                Slider {
                    id: seek; objectName: "seekSlider"; Layout.fillWidth: true; implicitHeight: 21
                    from: 0; to: Math.max(1,surface.durationMs); leftPadding: 0; rightPadding: 0
                    hoverEnabled: true; activeFocusOnTab: true; Accessible.name: "播放位置"
                    Binding { target: seek; property: "value"; value: surface.positionMs; when: !seek.pressed }
                    onPressedChanged: { player.reveal(); if (!pressed) surface.seek(Math.round(value)) }
                    Keys.onLeftPressed: player.skip(-10000)
                    Keys.onRightPressed: player.skip(10000)
                    background: Rectangle {
                        x: 0; y: (seek.height - height)/2; width: seek.width; height: 6; radius: 3
                        color: "#60ffffff"
                        Rectangle { width: parent.width * seek.visualPosition; height: parent.height; radius: 3; color: player.timelineColor }
                    }
                    handle: Rectangle {
                        x: seek.visualPosition * (seek.width - width); y: (seek.height - height)/2
                        width: seek.pressed || seek.hovered || seek.visualFocus ? 16 : 13
                        height: width; radius: width/2; color: player.timelineColor
                        border.width: seek.visualFocus ? 2 : 0; border.color: "#ffffff"
                        Behavior on width { NumberAnimation { duration: Studio.quick } }
                    }
                }
                RowLayout {
                    id: row; Layout.fillWidth: true; Layout.preferredHeight: 40; spacing: 3
                    PlayerButton { objectName: "playPauseButton"; symbol: surface.playing ? "pause" : "play"; hint: surface.playing ? "暂停 (K)" : "播放 (K)"; onClicked: player.playPause() }
                    PlayerButton { text: "−10s"; hint: "后退 10 秒 (←)"; onClicked: player.skip(-10000) }
                    PlayerButton { text: "+10s"; hint: "前进 10 秒 (→)"; onClicked: player.skip(10000) }
                    Text {
                        Layout.leftMargin: 5; color: "#c6d0ce"; font.pixelSize: 12
                        text: Studio.time(seek.pressed ? seek.value : surface.positionMs, false) + " / " + Studio.time(surface.durationMs, false)
                    }
                    Item { Layout.fillWidth: true }
                    PlayerButton { objectName: "muteButton"; symbol: surface.muted || surface.volume === 0 ? "muted" : "volume"; hint: surface.muted || surface.volume === 0 ? "打开声音 (M)" : "静音 (M)"; onClicked: player.toggleMute() }
                    Slider {
                        id: gain; objectName: "volumeSlider"; visible: stage.width > 580; Layout.preferredWidth: 70
                        from: 0; to: 1; stepSize: .05; leftPadding: 0; rightPadding: 0; Accessible.name: "音量"
                        value: surface.muted ? 0 : surface.volume
                        onMoved: { player.reveal(); surface.volume = value; surface.muted = value === 0 }
                        background: Rectangle {
                            x: 0; y: (gain.height-height)/2; width: gain.width; height: 4; radius: 2; color: "#60ffffff"
                            Rectangle { width: parent.width * gain.visualPosition; height: parent.height; radius: 2; color: player.timelineColor }
                        }
                        handle: Rectangle {
                            x: (gain.width-width)*gain.visualPosition; y: (gain.height-height)/2
                            width: 11; height: 11; radius: 6; color: player.timelineColor
                            border.width: gain.visualFocus ? 1 : 0; border.color: "white"
                        }
                    }
                    PlayerButton { objectName: "fullscreenButton"; text: player.cinema ? "退出" : "全屏"; hint: player.cinema ? "退出全屏 (Esc)" : "全屏播放 (F)"; onClicked: player.toggleCinema() }
                }
            }
        }
        Text {
            anchors.top: parent.top; anchors.horizontalCenter: parent.horizontalCenter; anchors.topMargin: 15
            width: parent.width - 40; text: surface.error; visible: text.length > 0
            color: "#ffc5c5"; wrapMode: Text.Wrap; horizontalAlignment: Text.AlignHCenter
        }
    }

    RowLayout {
        id: evidence; anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom
        anchors.leftMargin: 18; anchors.rightMargin: 14; height: 48; spacing: 12
        visible: player.hit && !player.cinema
        Mark {
            Layout.fillWidth: true; font.pixelSize: 12; secondary: true
            text: "命中片段   " + Studio.time(player.startMs, true) + " – " + Studio.time(player.endMs, true)
        }
        Tool { text: "回到命中处"; hint: "从命中片段开头播放"; textSize: 12; onClicked: { surface.seek(player.startMs); surface.setPlaying(true); player.reveal() } }
    }
}
