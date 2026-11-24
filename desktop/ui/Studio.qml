pragma Singleton
import QtQuick
import QtCore

QtObject {
    property var openSheets: []
    property Settings preferences: Settings {
        category: "workspace"
        property string appearance: "system"
        property bool pauseOnSend: true
        property bool episodeVisualFind: true
        property real kinoFadeSeconds: 8
        property bool kinoFadeEnabled: true
        // -1 follows the presence of materials until the user chooses a layout.
        property int treeVisibility: -1
    }
    // Keep the existing native preference keys when opening a library from 0.2.16.
    property Settings watchPanel: Settings {
        category: "watch-companion"
        property bool docked: false
        property real dockWidth: 440
        property bool leftEdge: false
        property real entryBottom: 84
    }
    readonly property int pageTitleSize: Qt.platform.os === "windows" ? 24 : 22
    readonly property int labelSize: Qt.platform.os === "windows" ? 14 : 13
    readonly property bool dark: preferences.appearance === "dark" ||
        (preferences.appearance === "system" && Application.styleHints.colorScheme === Qt.Dark)
    readonly property bool reduce: desktopPlatform?.reducedMotion ?? true
    readonly property int quick: reduce ? 0 : 110
    readonly property int hover: reduce ? 0 : 160
    readonly property int panel: reduce ? 0 : 240
    readonly property int page: reduce ? 0 : 320
    // The same sRGB interpolation and token endpoints as web/src/styles/global.css.
    property real night: dark ? 1 : 0
    Behavior on night { NumberAnimation { duration: reduce ? 0 : 360; easing.type: Easing.InOutCubic } }
    function mix(a, b) {
        const l = Qt.color(a), r = Qt.color(b), t = night
        return Qt.rgba(l.r+(r.r-l.r)*t, l.g+(r.g-l.g)*t, l.b+(r.b-l.b)*t, l.a+(r.a-l.a)*t)
    }
    readonly property color paper: mix("#e4eae5","#0d1110")
    readonly property color sidebar: mix("#ebf0ec","#171c1a")
    readonly property color sheet: mix("#f6f8f5","#202624")
    readonly property color field: sheet
    readonly property color ink: mix("#17312b","#dde5e1")
    readonly property color muted: mix("#566b65","#a7b5ae")
    readonly property color dim: mix("#596e65","#8a9b96")
    readonly property color line: mix("#26244940","#24a0cdc0")
    readonly property color strongLine: mix("#61238c78","#6b74c5ba")
    readonly property color wash: mix("#eaf5f0","#1f2e2a")
    readonly property color selected: mix("#d9eee7","#213330")
    readonly property color surfaceMuted: mix("#d9e1db","#29312e")
    readonly property color accent: mix("#238c78","#74c5ba")
    readonly property color actionBg: mix("#126b5d","#8dd3c8")
    readonly property color actionFg: paper
    readonly property color danger: mix("#b9564d","#f28b82")
    readonly property color wordmark: mix("#0b3b33","#74c5ba")
    readonly property color heroBase: mix("#10251f","#0d1110")
    readonly property color heroShade: mix("#05100d","#0d1110")
    readonly property color stage: mix("#0c1412","#080d0c")
    function time(ms, fractional) {
        var n = Math.max(0, Math.floor(Number(ms) || 0))
        var s = Math.floor(n / 1000), h = Math.floor(s / 3600)
        function pad(v) { return v < 10 ? "0" + v : "" + v }
        return (h ? h + ":" + pad(Math.floor(s / 60) % 60) : Math.floor(s / 60)) + ":" + pad(s % 60)
            + (fractional ? "." + String(n % 1000).padStart(3, "0") : "")
    }
    // Translate text that comes from Python (server errors, import states). Reading
    // desktopLocale.language keeps each binding live when the interface switches.
    function translate(message) {
        return typeof desktopLocale !== "undefined" && desktopLocale.language === "en" ? desktopLocale.translate(message) : message
    }
}
