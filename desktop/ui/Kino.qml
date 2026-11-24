import QtQuick
import QtQuick.Controls.Basic
import "KinoEngine.mjs" as Engine

AbstractButton {
    id: kino
    property string mood: "idle"
    property int sceneWidth: 64
    property bool ground: true
    property bool interactive: true
    property bool dozeWhenIdle: false
    signal reactionEnded(string name)

    readonly property int columns: Math.max(Engine.MIN_WIDTH, sceneWidth)
    readonly property bool looking: interactive && (hovered || visualFocus) && (mood === "idle" || mood === "wander")
    readonly property bool dozing: dozeWhenIdle && mood === "idle" && desktopPlatform.inputIdle && !Studio.reduce
    readonly property string pose: reaction || (looking ? "look" : dozing ? "sleep" : mood)
    readonly property real pixelScale: Math.min(availableWidth / columns, availableHeight / Engine.SCENE_H)
    readonly property bool animating: visible && Window.window?.active === true && !Studio.reduce
    property var frame: null
    property var player: null
    property string reaction: ""
    property int pokeCount: 0
    property double lastPoke: 0
    property bool ready: false

    implicitWidth: columns * 3; implicitHeight: Engine.SCENE_H * 3
    padding: 0; enabled: interactive; hoverEnabled: interactive
    focusPolicy: interactive ? Qt.StrongFocus : Qt.NoFocus
    Accessible.ignored: !interactive
    Accessible.name: qsTr("戳一戳 Kino")

    function syncFrame() {
        if (!ready) return
        if (Studio.reduce) {
            player = null
            frame = Engine.staticFrame(pose, columns)
        } else {
            if (!player) player = new Engine.MascotPlayer(pose, columns)
            player.set(pose, columns)
            frame = player.next()
        }
    }
    function resetPose() {
        cancelFlight()
        player = null
        if (reaction) reaction = ""
        else syncFrame()
    }
    function playReaction(name) {
        if (Studio.reduce) { reactionEnded(name); return }
        if (name !== "leap") cancelFlight()
        if (pose === name && frame) {
            player = new Engine.MascotPlayer(name, columns, Engine.cursorOf(frame))
            reaction = name
            syncFrame()
        } else reaction = name
    }
    onClicked: {
        const now = Date.now()
        pokeCount = now - lastPoke < 2000 ? pokeCount + 1 : 0
        lastPoke = now
        playReaction(pokeCount % 5 === 4 ? "oops" : ["jump", "happy", "jump", "happy"][pokeCount % 4])
    }
    onPoseChanged: syncFrame()
    onColumnsChanged: syncFrame()
    Component.onCompleted: { ready = true; syncFrame() }
    onAnimatingChanged: if (!animating && flightOrigin) cancelFlight()
    Connections {
        target: Studio
        function onReduceChanged() {
            if (Studio.reduce && kino.reaction) {
                const finished = kino.reaction
                kino.cancelFlight()
                kino.reaction = ""
                kino.reactionEnded(finished)
            }
            kino.syncFrame()
        }
    }
    Timer {
        interval: 1000 / Engine.STATE_FPS[kino.pose]
        running: kino.ready && kino.animating && !flight.running
        repeat: true
        onTriggered: {
            kino.frame = kino.player.next()
            if (kino.flightOrigin && Engine.isAirborne(kino.frame)) flight.start()
            if (kino.reaction && kino.player.loopsCompleted >= 1) {
                const finished = kino.reaction
                kino.reaction = ""
                kino.reactionEnded(finished)
            }
        }
    }

    // Bubble handoffs use the feet in scroll-content coordinates, so scrolling the thread
    // during layout doesn't change the launch point. The artwork keeps its airborne pose in flight.
    function capturePerch(stage) {
        if (!stage || !frame || !ready) return null
        const foot = pixels.mapToItem(stage, frame.x, Engine.GROUND)
        return { x: foot.x, y: foot.y, cursor: Engine.cursorOf(frame), at: Date.now() }
    }
    function arriveFrom(perch, stage) {
        if (!perch || !stage || Date.now() - perch.at > 1000 || !animating || !ready) return
        const edges = Engine.bounds(columns)
        const x = edges.left + 2 * Math.floor(Math.random() * ((edges.right - edges.left) / 2 + 1))
        const to = mapToItem(stage, sceneLeft + x * pixelScale, sceneTop + Engine.GROUND * pixelScale)
        cancelFlight()
        flightOrigin = Qt.point(perch.x - to.x, perch.y - to.y)
        player = new Engine.MascotPlayer("leap", columns, { x, facing: perch.cursor.facing, lying: perch.cursor.lying })
        if (reaction === "leap") syncFrame()
        else reaction = "leap"
    }
    function cancelFlight() { flight.stop(); flightOrigin = null; flightTime = 0 }
    readonly property real sceneLeft: leftPadding + Math.round((availableWidth - columns * pixelScale) / 2)
    readonly property real sceneTop: topPadding + Math.round((availableHeight - Engine.SCENE_H * pixelScale) / 2)
    property var flightOrigin: null
    property real flightTime: 0
    readonly property real launchSpeed: flightOrigin ? -Math.sqrt(2 * 4200 * (22 + Math.max(0, flightOrigin.y))) : 0
    readonly property real flightDuration: flightOrigin ? (-launchSpeed + Math.sqrt(launchSpeed * launchSpeed - 2 * 4200 * flightOrigin.y)) / 4200 : 0
    NumberAnimation {
        id: flight
        target: kino; property: "flightTime"
        from: 0; to: kino.flightDuration; duration: Math.round(kino.flightDuration * 1000)
        onFinished: {
            kino.cancelFlight()
            if (kino.player) kino.frame = kino.player.next()
        }
    }
    background: Rectangle { radius: 12; color: "transparent"; border.width: kino.visualFocus ? 2 : 0; border.color: Studio.accent }
    contentItem: Item {
        Canvas {
            id: pixels
            // Draw in sprite pixels. Qt scales the tiny texture with nearest-neighbour sampling.
            width: kino.columns; height: Engine.SCENE_H
            scale: kino.pixelScale; transformOrigin: Item.TopLeft; smooth: false
            x: kino.sceneLeft - kino.leftPadding + (kino.flightOrigin ? Math.round(kino.flightOrigin.x * (1 - kino.flightTime / kino.flightDuration)) : 0)
            y: kino.sceneTop - kino.topPadding + (kino.flightOrigin ? Math.round(kino.flightOrigin.y + kino.launchSpeed * kino.flightTime + 2100 * kino.flightTime * kino.flightTime) : 0)
            readonly property color groundColor: Studio.ink
            Connections {
                target: kino
                function onFrameChanged() { pixels.requestPaint() }
                function onGroundChanged() { pixels.requestPaint() }
            }
            onGroundColorChanged: requestPaint()
            Connections { target: Studio; function onDarkChanged() { pixels.requestPaint() } }
            onPaint: {
                if (!kino.frame) return
                const ctx = getContext("2d"), raster = Engine.rasterize(kino.frame, kino.columns)
                ctx.clearRect(0, 0, width, height)
                if (kino.ground) {
                    ctx.globalAlpha = 0.18; ctx.fillStyle = groundColor
                    ctx.path = Engine.groundPath(width, raster.groundShift); ctx.fill()
                    if (raster.shadow) {
                        ctx.globalAlpha = 0.3; ctx.fillStyle = Engine.PALETTE.ink
                        ctx.fillRect(raster.shadow.x, Engine.GROUND, raster.shadow.width, 1)
                        ctx.fillRect(raster.shadow.x + 2, Engine.GROUND + 1, raster.shadow.width - 4, 1)
                    }
                    ctx.globalAlpha = 1
                }
                const paths = Engine.toPaths(raster)
                for (const colour of Engine.COLOURS) {
                    if (!paths[colour]) continue
                    ctx.fillStyle = colour === "spark" && !Studio.dark ? Engine.PALETTE.shade : Engine.PALETTE[colour]
                    ctx.path = paths[colour]; ctx.fill()
                }
            }
        }
    }
}
