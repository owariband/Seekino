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
    readonly property alias positionMs: surface.positionMs
    readonly property alias hasFrame: surface.hasFrame
    readonly property string hitDescription: hit ? (workspace?.playbackContext?.description || "") : ""
    readonly property bool insideHit: hit && positionMs >= startMs && positionMs < endMs
    readonly property string hitStatus: insideHit ? qsTr("命中区间内") : positionMs < startMs ? qsTr("命中区间前") : qsTr("已过命中区间")
    readonly property color rangeColor: "#f0c97b"
    property bool cinema: false
    // Full-screen playback covers the application chrome. Keep Qt's popup overlay above it.
    readonly property Item playbackHost: cinema ? player.Window.window.contentItem : player
    property int previousVisibility: Window.Windowed
    property bool controlsAwake: true
    readonly property bool chatOpen: companion.opened
    readonly property bool chatHasFocus: companion.hasPanelFocus
    readonly property bool chatEntryFocused: companion.entryFocused
    property bool modalOpen: false
    property bool resumeAfterQuestion: false
    readonly property bool episodeFindOpen: finder.visible
    readonly property var episodes: workspace.videosBySeries[workspace.playbackContext.seriesID || ""] || []
    readonly property int episodeIndex: episodes.findIndex(episode => episode.id === workspace.playbackContext.videoID)
    function openChat() { if (finder.visible) finder.dismiss(); companion.showPanel() }
    function openFind() {
        if (companion.opened) companion.dismiss()
        finder.openFind()
        reveal()
    }
    function playEpisode(index) {
        if (index >= 0 && index < episodes.length && index !== episodeIndex)
            workspace.openVideo(episodes[index].id, episodes[index].title || episodes[index].filename)
    }
    signal settingsRequested()
    function focusChat() { if (companion.opened) companion.focusQuestion() }
    readonly property color timelineColor: "#89d6c8"

    function open(path, start, end, duration, caption, isHit) {
        resumeAfterQuestion = false
        if (finder.visible) finder.dismiss()
        companion.reset()
        title = caption; startMs = start; endMs = end; hit = isHit
        if (workspace.playbackContext.seriesID) workspace.ensureSeriesLoaded(workspace.playbackContext.seriesID)
        surface.open(path, start, duration)
        reveal()
        Qt.callLater(() => { if (player.enabled) player.forceActiveFocus() })
    }
    function close() {
        if (cinema) toggleCinema()
        resumeAfterQuestion = false
        if (finder.visible) finder.dismiss()
        companion.reset()
        workspace.leaveWatch()
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
    Shortcut {
        sequence: "Escape"; enabled: player.enabled && !player.modalOpen && (finder.visible || player.chatOpen || player.cinema)
        onActivated: finder.visible ? finder.dismiss() : player.chatOpen ? companion.dismiss() : player.toggleCinema()
    }
    Shortcut {
        sequence: "F"; autoRepeat: false; enabled: player.enabled && !player.modalOpen && !finder.visible && !player.chatHasFocus
        onActivated: player.toggleCinema()
    }
    Keys.onPressed: event => {
        if (player.chatHasFocus || player.modalOpen || finder.visible || event.modifiers !== Qt.NoModifier) return
        switch (event.key) {
        case Qt.Key_Slash: openFind(); break
        case Qt.Key_Left: skip(-10000); break
        case Qt.Key_Right: skip(10000); break
        case Qt.Key_K: playPause(); break
        case Qt.Key_M: toggleMute(); break
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
        id: stage; objectName: "videoStage"
        parent: player.playbackHost
        anchors.fill: parent
        anchors.bottomMargin: (player.hit ? evidence.height : 0) + companion.reservedHeight
        anchors.rightMargin: companion.reservedWidth
        Behavior on anchors.rightMargin { enabled: !companion.resizing; NumberAnimation { duration: Studio.panel; easing.type: Easing.OutCubic } }
        Behavior on anchors.bottomMargin { NumberAnimation { duration: Studio.panel; easing.type: Easing.OutCubic } }
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
            circle: true; symbol: "play"; symbolSize: 30; hint: qsTr("播放 (K)")
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
                    hoverEnabled: true; activeFocusOnTab: true; Accessible.name: qsTr("播放位置")
                    Accessible.description: player.hit ? qsTr("%1，命中 %2 至 %3").arg(player.hitStatus).arg(Studio.time(player.startMs,true)).arg(Studio.time(player.endMs,true)) : ""
                    value: surface.positionMs
                    onPressedChanged: {
                        player.reveal()
                        if (pressed) surface.beginScrub()
                        else surface.endScrub(Math.round(value))
                    }
                    onMoved: if (pressed) surface.previewScrub(Math.round(value))
                    Keys.onLeftPressed: player.skip(-10000)
                    Keys.onRightPressed: player.skip(10000)
                    background: Rectangle {
                        x: 0; y: (seek.height - height)/2; width: seek.width; height: 6; radius: 3
                        color: "#60ffffff"
                        Rectangle { width: parent.width * seek.visualPosition; height: parent.height; radius: 3; color: player.timelineColor }
                        Rectangle {
                            id: hitBand; objectName: "hitRangeBand"
                            visible: player.hit && surface.durationMs > 0
                            readonly property real startX: Math.min(parent.width, Math.max(0, player.startMs / seek.to * parent.width))
                            readonly property real endX: Math.min(parent.width, Math.max(startX, player.endMs / seek.to * parent.width))
                            // Keep even a sub-pixel event legible above the six-pixel playback track.
                            width: Math.min(parent.width, Math.max(6, endX - startX))
                            x: Math.max(0, Math.min(parent.width - width, (startX + endX - width) / 2))
                            y: -8; height: 4; radius: 2; color: player.rangeColor
                        }
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
                    PlayerButton { objectName: "playPauseButton"; symbol: surface.playing ? "pause" : "play"; hint: surface.playing ? qsTr("暂停 (K)") : qsTr("播放 (K)"); onClicked: player.playPause() }
                    PlayerButton { visible: stage.width > 560; text: "−10s"; hint: qsTr("后退 10 秒 (←)"); onClicked: player.skip(-10000) }
                    PlayerButton { visible: stage.width > 560; text: "+10s"; hint: qsTr("前进 10 秒 (→)"); onClicked: player.skip(10000) }
                    Text {
                        Layout.leftMargin: 5; color: "#c6d0ce"; font.pixelSize: 12
                        font.features: ({ "tnum": 1 })
                        text: Studio.time(seek.pressed ? seek.value : surface.positionMs, player.hit) + " / " + Studio.time(surface.durationMs, false)
                    }
                    Item { Layout.fillWidth: true }
                    PlayerButton { objectName: "episodeFindButton"; symbol: "search"; hint: qsTr("查找本集 (/)")
                        onClicked: player.openFind() }
                    PlayerButton {
                        id: episodeButton; objectName: "episodeMenuButton"
                        symbol: "library"; hint: qsTr("选集"); visible: player.episodes.length > 1
                        onClicked: episodeMenu.open()
                        Menu {
                            id: episodeMenu; y: -height; x: Math.min(0, stage.width - episodeButton.mapToItem(stage,0,0).x - width)
                            width: Math.min(280, stage.width-16); height: Math.min(implicitHeight, stage.height-72)
                            palette.window: Studio.sidebar; palette.text: Studio.ink; palette.windowText: Studio.ink
                            MenuItem { text: qsTr("上一集"); enabled: player.episodeIndex > 0; onTriggered: player.playEpisode(player.episodeIndex-1) }
                            MenuItem { text: qsTr("下一集"); enabled: player.episodeIndex >= 0 && player.episodeIndex + 1 < player.episodes.length
                                onTriggered: player.playEpisode(player.episodeIndex+1) }
                            MenuSeparator {}
                            Instantiator {
                                model: player.episodes
                                MenuItem { required property int index; required property var modelData
                                    text: qsTr("第 ") + modelData.episode + qsTr(" 集 · ") + modelData.title
                                    checkable: true; checked: index === player.episodeIndex
                                    onTriggered: player.playEpisode(index) }
                                onObjectAdded: (index, object) => episodeMenu.insertItem(index+3,object)
                                onObjectRemoved: (index, object) => episodeMenu.removeItem(object)
                            }
                        }
                    }
                    PlayerButton {
                        objectName: "playbackRateButton"; text: surface.playbackRate + "×"; hint: qsTr("播放倍速")
                        onClicked: rates.open()
                        Menu {
                            id: rates; y: -height; width: 92
                            palette.window: Studio.sidebar; palette.text: Studio.ink; palette.windowText: Studio.ink
                            Instantiator {
                                model: [0.5, 0.75, 1, 1.25, 1.5, 2]
                                MenuItem { required property real modelData; text: modelData + "×"
                                    checkable: true; checked: modelData === surface.playbackRate
                                    onTriggered: surface.playbackRate = modelData }
                                onObjectAdded: (index, object) => rates.insertItem(index,object)
                                onObjectRemoved: (index, object) => rates.removeItem(object)
                            }
                        }
                    }
                    PlayerButton { objectName: "muteButton"; symbol: surface.muted || surface.volume === 0 ? "muted" : "volume"; hint: surface.muted || surface.volume === 0 ? qsTr("打开声音 (M)") : qsTr("静音 (M)"); onClicked: player.toggleMute() }
                    Slider {
                        id: gain; objectName: "volumeSlider"; visible: stage.width > 770; Layout.preferredWidth: 70
                        from: 0; to: 1; stepSize: .05; leftPadding: 0; rightPadding: 0; Accessible.name: qsTr("音量")
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
                    PlayerButton { objectName: "fullscreenButton"; symbol: player.cinema ? "exitFullscreen" : "fullscreen"; symbolSize: 20; hint: player.cinema ? qsTr("退出全屏 (Esc)") : qsTr("全屏播放 (F)"); onClicked: player.toggleCinema() }
                }
            }
        }
        Text {
            anchors.top: parent.top; anchors.horizontalCenter: parent.horizontalCenter; anchors.topMargin: 15
            width: parent.width - 40; text: Studio.translate(surface.error); visible: text.length > 0
            color: "#ffc5c5"; wrapMode: Text.Wrap; horizontalAlignment: Text.AlignHCenter
        }
    }

    EpisodeFind {
        id: finder
        parent: stage
        anchors.top: parent.top; anchors.right: parent.right; anchors.margins: 12
        z: 15
        onSeekRequested: position => { surface.seek(position); player.reveal() }
        onDismissed: player.forceActiveFocus()
    }

    WatchCompanion {
        id: companion
        parent: player.playbackHost
        anchors.fill: parent
        anchors.bottomMargin: player.hit ? evidence.height : 0
        z: player.cinema ? 102 : 2
        episodes: player.episodes; currentEpisode: player.episodeIndex
        onEpisodeRequested: index => player.playEpisode(index)
        videoTitle: player.title
        positionMs: player.positionMs
        onSettingsRequested: player.settingsRequested()
        onSeekRequested: position => { surface.seek(position); player.reveal() }
        onOpenedChanged: {
            if (opened && finder.visible) finder.dismiss()
            if (!opened && player.resumeAfterQuestion) {
                player.resumeAfterQuestion = false
                surface.setPlaying(true)
            }
        }
        onQuestionSent: if (Studio.preferences.pauseOnSend && surface.playing) {
            player.resumeAfterQuestion = true
            surface.setPlaying(false)
        }
    }

    Rectangle {
        id: evidence; objectName: "hitEvidenceBar"
        parent: player.playbackHost
        anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom
        z: player.cinema ? 101 : 1; visible: player.hit; height: details.implicitHeight + 20
        color: player.cinema ? "#171c1a" : Studio.sheet
        readonly property color ink: player.cinema ? "#dde5e1" : Studio.ink
        readonly property color secondary: player.cinema ? "#a7b5ae" : Studio.muted
        readonly property color labelColor: player.cinema || Studio.dark ? player.rangeColor : "#81541a"
        RowLayout {
            anchors.fill: parent; anchors.leftMargin: 16; anchors.rightMargin: 12
            spacing: 12
            ColumnLayout {
                id: details; Layout.fillWidth: true; spacing: 4
                RowLayout {
                    Layout.fillWidth: true; spacing: 7
                    Rectangle { implicitWidth: 12; implicitHeight: 3; radius: 1; color: evidence.labelColor }
                    Text { text: player.hitStatus; color: evidence.labelColor; font.weight: Font.DemiBold; font.pixelSize: 12 }
                    Text {
                        Layout.fillWidth: true; font.pixelSize: 12; color: evidence.secondary
                        font.features: ({ "tnum": 1 })
                        text: Studio.time(player.startMs, true) + " – " + Studio.time(player.endMs, true)
                        elide: Text.ElideRight
                    }
                }
                Text {
                    Layout.fillWidth: true; text: player.hitDescription; textFormat: Text.PlainText
                    color: evidence.ink; font.pixelSize: 14; font.weight: Font.Medium
                    maximumLineCount: 2; wrapMode: Text.Wrap; elide: Text.ElideRight; visible: text.length > 0
                }
            }
            Tool {
                objectName: "returnToHit"; contentColor: evidence.ink; text: qsTr("回到命中处"); hint: qsTr("从命中片段开头播放"); textSize: 12
                onClicked: { surface.seek(player.startMs); surface.setPlaying(true); player.reveal() }
            }
        }
    }
}
