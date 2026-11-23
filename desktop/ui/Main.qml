import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Dialogs
import QtQuick.Layouts

ApplicationWindow {
    id: window
    objectName: "workspaceWindow"
    width: 1080; height: 700; minimumWidth: 640; minimumHeight: 440
    visible: true; color: Studio.paper
    topPadding: 0; bottomPadding: 0; leftPadding: 0; rightPadding: 0
    title: page === "player" ? watch.title + " — Seekino" : "Seekino"
    font.pixelSize: 13
    readonly property bool mac: Qt.platform.os === "osx"
    property string page: "library"
    property string priorPage: "library"
    property string activeVideo: ""
    property string pendingVideo: ""
    property string openFailure: ""
    readonly property string currentName: page === "player" ? watch.title : page === "series" ?
        (browser.selected?.title || "系列") : page === "search" ? "搜索片段" : page === "jobs" ? "解析任务" : "资料库"
    readonly property int treeWidth: Math.round(Math.max(170,Math.min(210,width*.19)))
    readonly property bool treeShown: Studio.preferences.treeVisibility < 0 ? workspace.series.length > 0 : Studio.preferences.treeVisibility === 1
    function go(next) {
        workspace.cancelOpenMedia()
        if (page === "player" && next !== "player") watch.close()
        page = next
    }
    function showSeries(id) { workspace.loadSeries(id); tree.setExpanded(id,true); go("series") }
    function play(id,caption) { pendingVideo=id; workspace.openVideo(id,caption) }
    function searchPage() { go("search"); Qt.callLater(search.focusQuery) }
    function back() { go(page === "player" ? priorPage : "library") }
    function choose() { if (workspace.ready && !workspace.busyImport) files.open() }
    function destination() { return page === "series" || page === "player" && priorPage === "series" ? workspace.selectedSeries : "" }
    function newFolder() { seriesName.clear(); createDialog.open(); seriesName.forceActiveFocus() }
    function toggleTree() { Studio.preferences.treeVisibility = treeShown ? 0 : 1 }
    function openSettings() { prefs.openSettings() }
    Loader {
        id: nativeMenus; objectName: "nativeMenuLoader"; active: window.mac && Qt.platform.pluginName === "cocoa"
        source: "MacMenu.qml"
        onLoaded: item.owner = window
    }
    Component.onCompleted: workspace.start()

    Shortcut { enabled: !nativeMenus.active; sequences: [StandardKey.Open]; onActivated: window.choose() }
    Shortcut { enabled: !nativeMenus.active; sequence: "Ctrl+K"; onActivated: window.searchPage() }
    Shortcut { sequences: [StandardKey.Find]; onActivated: window.searchPage() }
    Shortcut { enabled: !nativeMenus.active; sequence: "Ctrl+Shift+L"; onActivated: window.toggleTree() }
    Shortcut { enabled: !nativeMenus.active; sequence: "Ctrl+,"; onActivated: prefs.openSettings() }
    Shortcut { sequence: "Alt+Left"; enabled: window.page !== "library"; onActivated: window.back() }
    Shortcut { sequence: "Space"; enabled: window.page === "player" && !prefs.opened && !createDialog.opened && !alertDialog.opened; onActivated: watch.playPause() }
    FileDialog {
        id: files; title: "导入视频"; fileMode: FileDialog.OpenFiles
        nameFilters: ["视频 (*.mp4 *.mov *.avi *.MP4 *.MOV *.AVI)"]
        onAccepted: workspace.importFiles(selectedFiles,window.destination())
    }
    Connections {
        target: workspace
        function onSettingsNeeded() { prefs.openSettings() }
        function onSeriesCreated(id) { window.showSeries(id) }
        function onPlaybackReady(path,startMs,endMs,durationMs,title,hit) {
            if (window.page !== "player") window.priorPage=window.page
            window.activeVideo=hit ? "" : window.pendingVideo
            watch.open(path,startMs,endMs,durationMs,title,hit)
            window.page="player"
        }
        function onErrorChanged() {
            if (workspace.error) {
                if (!workspace.ready) window.openFailure = workspace.error
                alertDialog.open()
            }
        }
    }

    ColumnLayout {
        anchors.fill: parent; spacing: 0
        Item {
            id: toolbar; Layout.fillWidth: true; Layout.preferredHeight: window.mac ? 42 : 52
            Rectangle { anchors.fill: parent; color: Studio.sidebar }
            Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: Studio.line }
            Rectangle { width: split.width; height: parent.height; color: Studio.sidebar }
            MouseArea {
                anchors.fill: parent
                onPressed: window.startSystemMove()
                onDoubleClicked: window.visibility === Window.Maximized ? window.showNormal() : window.showMaximized()
            }
            Row {
                x: window.mac && window.visibility !== Window.FullScreen ? 80 : 9; anchors.verticalCenter: parent.verticalCenter; spacing: 2
                Tool { objectName: "toggleSidebar"; glyph: "sidebar"; hint: "显示或隐藏资料树"; onClicked: window.toggleTree() }
                Tool { visible: window.page !== "library"; glyph: "left"; hint: "返回"; onClicked: window.back() }
            }
            Brand { x: window.mac && window.visibility !== Window.FullScreen ? (window.page === "library" ? 114 : 147) : (window.page === "library" ? 42 : 78); anchors.verticalCenter: parent.verticalCenter; onClicked: window.go("library") }
            Row {
                visible: window.page === "player" || window.page === "series"
                id: caption
                anchors.centerIn: parent; spacing: 7
                Glyph { width: 14; height: width; anchors.verticalCenter: parent.verticalCenter; name: window.page === "player" ? "film" : window.page === "search" ? "search" : window.page === "jobs" ? "activity" : "folder"; ink: Studio.dim }
                Mark { text: window.currentName; width: Math.min(implicitWidth, window.width*.3); font.pixelSize: 12; font.weight: Font.DemiBold; secondary: true }
            }
            AbstractButton {
                id: quickSearch; objectName: "searchButton"
                visible: (window.page === "library" || window.page === "jobs") && window.width >= 790
                anchors.centerIn: parent; width: Math.min(360,window.width*.33); height: window.mac ? 32 : 36; hoverEnabled: true
                Accessible.name: "搜索片段"; onClicked: window.searchPage()
                background: Rectangle {
                    radius: height/2; color: Studio.sheet; border.color: (quickSearch.hovered || quickSearch.visualFocus) ? Studio.strongLine : Studio.line
                    Behavior on border.color { ColorAnimation { duration: Studio.hover } }
                }
                contentItem: RowLayout {
                    spacing: 8
                    Glyph { Layout.leftMargin: 13; Layout.preferredWidth: 17; Layout.preferredHeight: 17; name: "search"; ink: Studio.dim }
                    Mark { Layout.fillWidth: true; text: "描述一个画面，找回那一刻…"; color: Studio.dim; font.pixelSize: 13 }
                    Mark { Layout.rightMargin: 12; text: window.mac ? "⌘ K" : "Ctrl K"; color: Studio.dim; font.pixelSize: 10 }
                }
            }
            Row {
                anchors.right: parent.right; anchors.rightMargin: 9; anchors.verticalCenter: parent.verticalCenter; spacing: 2
                Tool { objectName: "searchIconButton"; visible: !quickSearch.visible; glyph: "search"; hint: "搜索片段"; chosen: window.page === "search"; onClicked: window.searchPage() }
                Tool {
                    objectName: "taskButton"; glyph: "activity"; hint: "解析任务"; chosen: window.page === "jobs"; onClicked: window.go(window.page === "jobs" ? "library" : "jobs")
                    Rectangle { visible: workspace.jobs.some(task => task.latest && !task.paused && ["PENDING","PREPARING","PARSING","INDEXING"].includes(task.status)); x: parent.width-7; y: 3; width: 5; height: 5; radius: 3; color: Studio.accent }
                }
                Tool { objectName: "importButton"; glyph: "import"; hint: "导入视频"; visible: workspace.series.length > 0 && !(window.page === "series" && browser.videos.length === 0); enabled: workspace.ready && !workspace.busyImport; onClicked: window.choose() }
                Tool {
                    id: more; objectName: "settingsButton"; glyph: "more"; hint: "更多操作"; onClicked: actions.open()
                    Popup {
                        id: actions; x: more.width-width; y: more.height+4; width: 156; padding: 5; focus: true; modal: true; dim: false
                        enter: Transition { NumberAnimation { property: "opacity"; from: 0; to: 1; duration: Studio.quick } }
                        background: Rectangle { color: Studio.sheet; border.color: Studio.line; radius: 6 }
                        contentItem: Column {
                            Tool { text: "新建系列…"; glyph: "folder"; width: parent.width; enabled: workspace.ready; onClicked: { actions.close(); window.newFolder() } }
                            Tool { text: "设置…"; glyph: "more"; width: parent.width; onClicked: { actions.close(); prefs.openSettings() } }
                        }
                    }
                }
            }
        }
        RowLayout {
            Layout.fillWidth: true; Layout.fillHeight: true; spacing: 0
            Item {
                id: split; Layout.fillHeight: true; Layout.preferredWidth: width
                width: window.treeShown ? window.treeWidth : 0; clip: true
                Behavior on width { NumberAnimation { duration: Studio.panel; easing.type: Easing.OutCubic } }
                FileTree {
                    id: tree; objectName: "treePane"
                    width: window.treeWidth; height: parent.height; x: parent.width-width; visible: parent.width > 0
                    activeVideo: window.activeVideo; page: window.page
                    onSeriesPicked: id => { workspace.loadSeries(id); window.go("series") }
                    onVideoPicked: (id,title) => window.play(id,title)
                    onNewSeriesRequested: window.newFolder()
                    onLibraryRequested: window.go("library")
                }
            }
            Item {
                Layout.fillWidth: true; Layout.fillHeight: true; clip: true
                BrowserPane {
                    id: browser
                    anchors.fill: parent; browsingSeries: window.page === "series"
                    enabled: workspace.ready && ["library","series"].includes(window.page); visible: opacity > 0
                    opacity: enabled ? 1 : 0; Behavior on opacity { NumberAnimation { duration: Studio.page; easing.type: Easing.OutCubic } }
                    onOpenSeries: id => window.showSeries(id)
                    onOpenVideo: (id,title) => window.play(id,title)
                    onImportRequested: window.choose()
                }
                SearchPane {
                    id: search; anchors.fill: parent
                    enabled: workspace.ready && window.page === "search"; opacity: enabled ? 1 : 0; visible: opacity > 0
                    Behavior on opacity { NumberAnimation { duration: Studio.page; easing.type: Easing.OutCubic } }
                    onOpenHit: (ref,title) => workspace.openResult(ref,title)
                    onSettingsRequested: prefs.openSettings()
                    onImportRequested: window.choose()
                }
                JobsPane {
                    anchors.fill: parent; enabled: workspace.ready && window.page === "jobs"; opacity: enabled ? 1 : 0; visible: opacity > 0
                    Behavior on opacity { NumberAnimation { duration: Studio.page; easing.type: Easing.OutCubic } }
                }
                WatchPane {
                    id: watch; anchors.fill: parent; enabled: window.page === "player"; opacity: enabled ? 1 : 0; visible: opacity > 0
                    Behavior on opacity { NumberAnimation { duration: Studio.panel; easing.type: Easing.OutCubic } }
                }
                EmptyView {
                    visible: !workspace.ready; anchors.centerIn: parent; width: Math.min(400,parent.width-32)
                    glyph: "library"; title: window.openFailure ? "资料库未打开" : "正在打开资料库…"
                    action: window.openFailure ? "重新打开" : ""; onActivated: { window.openFailure = ""; workspace.start() }
                }
            }
        }
        Rectangle {
            Layout.fillWidth: true; Layout.preferredHeight: workspace.busyImport ? 32 : 0; color: Studio.sidebar; clip: true
            Behavior on Layout.preferredHeight { NumberAnimation { duration: Studio.panel; easing.type: Easing.OutCubic } }
            Mark { x: 15; anchors.verticalCenter: parent.verticalCenter; text: workspace.importLabel + (workspace.importProgress >= 0 ? "   " + Math.round(workspace.importProgress*100) + "%" : ""); font.pixelSize: 12 }
            Rectangle { anchors.bottom: parent.bottom; width: parent.width*Math.max(0,workspace.importProgress); height: 2; color: Studio.accent; Behavior on width { NumberAnimation { duration: Studio.hover } } }
        }
    }
    DropArea {
        anchors.fill: parent; enabled: workspace.ready && !workspace.busyImport && !prefs.opened && !createDialog.opened && !alertDialog.opened
        onEntered: drag => { drag.accepted=drag.hasUrls }
        onDropped: drop => { if(drop.hasUrls) { workspace.importFiles(drop.urls,window.destination()); drop.acceptProposedAction() } }
        Rectangle {
            anchors.fill: parent; anchors.margins: 5; radius: 7; color: Qt.alpha(Studio.accent,.08)
            border.width: 2; border.color: Studio.accent; opacity: parent.containsDrag ? 1 : 0
            Behavior on opacity { NumberAnimation { duration: Studio.hover } }
        }
    }
    Preferences { id: prefs; objectName: "preferences" }
    Sheet {
        id: createDialog; objectName: "createSeriesSheet"
        contentItem: ColumnLayout {
            spacing: 12
            Mark { text: "新建系列"; font.pixelSize: 18; font.weight: Font.DemiBold }
            Entry { id: seriesName; objectName: "seriesName"; Layout.fillWidth: true; placeholderText: "系列名称"; Accessible.name: "系列名称"; onAccepted: if (create.enabled) create.clicked() }
            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                Tool { text: "取消"; onClicked: createDialog.close() }
                Tool { id: create; objectName: "createSeriesConfirm"; primary: true; text: "创建"; enabled: !!seriesName.text.trim(); onClicked: { workspace.createSeries(seriesName.text.trim()); createDialog.close() } }
            }
        }
    }
    Sheet {
        id: alertDialog
        onClosed: workspace.clearError()
        contentItem: ColumnLayout {
            spacing: 12
            Mark { text: "操作未完成"; font.pixelSize: 17; font.weight: Font.DemiBold }
            Text { Layout.fillWidth: true; text: workspace.error; color: Studio.ink; wrapMode: Text.Wrap; font.pixelSize: 13 }
            Tool { Layout.alignment: Qt.AlignRight; text: "好"; primary: true; onClicked: alertDialog.close() }
        }
    }
}
