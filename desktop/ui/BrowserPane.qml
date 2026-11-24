import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts

Item {
    id: browser
    property bool browsingSeries: false
    readonly property var selected: workspace.series.find(item => item.id === workspace.selectedSeries)
    readonly property var videos: workspace.videosBySeries[workspace.selectedSeries] || []
    readonly property var hero: browsingSeries ? selected : (workspace.series.find(item => item.id === workspace.lastWatchedSeries) || workspace.series[0])
    readonly property string heroID: hero?.id || ""
    readonly property var members: workspace.videosBySeries[heroID] || []
    readonly property var recent: workspace.recentPlayback?.[heroID]
    readonly property var resumeVideo: !!recent && recent.durationMs > 0
        ? members.find(video => video.id === recent.videoID) : null
    readonly property string playID: resumeVideo?.id || hero?.firstVideoID || ""
    readonly property string playTitle: resumeVideo?.title || hero?.firstVideoTitle || ""
    readonly property var featured: resumeVideo || members.find(video => video.id === playID)
        || workspace.jobs.find(job => job.latest && job.videoID === playID)
    readonly property real gutter: !browsingSeries && hero ? Math.min(96, Math.max(20, width * .05)) : width < 580 ? 16 : 24
    readonly property real shelfGutter: Math.max(0,(width - 1920) / 2) + gutter
    readonly property real shelfTitleSize: Math.min(36, Math.max(25.6, width * .022))
    readonly property int columns: Math.max(1, Math.floor((page.width - 2 * gutter + gap) / (190 + gap)))
    readonly property int gap: width < 580 ? 12 : 16
    readonly property real cellWidth: (page.width - 2 * gutter - gap * (columns-1)) / columns
    signal manageSeries(string id, string section)
    signal openSeries(string id)
    signal openVideo(string id, string title)
    signal importRequested()
    signal settingsRequested()

    function focusEpisode(index) {
        if (index < 0 || index >= videos.length) return
        const row = Math.floor(index / columns)
        page.positionViewAtIndex(row, ListView.Contain)
        page.forceLayout()
        Qt.callLater(() => page.itemAtIndex(row)?.focusCard(index % columns))
    }
    // Resolve the featured episode and verify any saved position against this library.
    readonly property bool loadHomeHistory: enabled && workspace.ready && !browsingSeries && !!heroID
    onLoadHomeHistoryChanged: if (loadHomeHistory) workspace.ensureSeriesLoaded(heroID)
    onHeroIDChanged: {
        if (loadHomeHistory) workspace.ensureSeriesLoaded(heroID)
        Qt.callLater(() => page.positionViewAtBeginning())
    }
    onBrowsingSeriesChanged: Qt.callLater(() => page.positionViewAtBeginning())

    Rectangle {
        id: welcome
        objectName: "firstImportArea"
        anchors.centerIn: parent
        anchors.verticalCenterOffset: -12
        width: Math.min(652, parent.width - 2 * browser.gutter)
        readonly property bool narrow: width < 492
        readonly property int inset: narrow ? 24 : 32
        height: copy.implicitHeight + 2 * inset
        visible: !browser.browsingSeries && workspace.series.length === 0
        radius: 14; color: Studio.mix("#edf2ee", "#131a17"); border.color: Studio.line

        ColumnLayout {
            id: copy
            x: welcome.inset; y: welcome.inset
            width: welcome.width - 2 * welcome.inset - (welcome.narrow ? 0 : still.width + 20)
            spacing: 0
            Text {
                Layout.fillWidth: true
                text: qsTr("把视频拖到这里"); textFormat: Text.PlainText
                font.pixelSize: Studio.pageTitleSize; font.weight: Font.Medium
                color: Studio.ink; wrapMode: Text.Wrap
            }
            Text {
                Layout.fillWidth: true; Layout.topMargin: 7
                text: qsTr("导入并解析后，就能按台词和画面找到片段。")
                font.pixelSize: Studio.labelSize + 1; color: Studio.muted
                wrapMode: Text.Wrap; lineHeightMode: Text.FixedHeight; lineHeight: 22
            }
            RowLayout {
                Layout.topMargin: 22; spacing: 8
                Tool {
                    objectName: "emptyImport"; text: qsTr("选择视频…"); glyph: "plus"
                    primary: true; symbolSize: 14; implicitHeight: 34; corner: 8
                    onClicked: browser.importRequested()
                }
                Tool {
                    objectName: "firstImportSettings"; visible: !workspace.importModelReady
                    text: qsTr("配置解析模型…"); textSize: Studio.labelSize
                    onClicked: browser.settingsRequested()
                }
            }
        }

        Rectangle {
            id: still
            visible: !welcome.narrow
            anchors.right: parent.right; anchors.rightMargin: welcome.inset
            anchors.verticalCenter: parent.verticalCenter
            width: 144; height: 88; radius: 6
            color: Studio.mix("#e1ebe4", "#1a2822"); border.color: Studio.line
            Column {
                anchors.centerIn: parent; spacing: still.height - 22
                Repeater {
                    model: 2
                    Row {
                        spacing: 9
                        Repeater {
                            model: 9
                            Rectangle { width: 5; height: 3; radius: 1; color: Studio.mix("#b8c8bd", "#384b41") }
                        }
                    }
                }
            }
            Kino {
                anchors.centerIn: parent
                width: 128; height: 56; focusPolicy: Qt.ClickFocus
            }
        }
    }

    ListView {
        id: page
        objectName: "libraryScroll"
        anchors.fill: parent; visible: !!browser.hero; clip: true
        boundsBehavior: Flickable.StopAtBounds
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
        model: browser.browsingSeries ? Math.ceil(browser.videos.length / browser.columns) : 0
        function revealItem(item) {
            const top = item.mapToItem(contentItem, 0, 0).y
            const bottom = top + item.height
            if (top < contentY || item.height > height) contentY = top
            else if (bottom > contentY + height) contentY = bottom - height
        }
        Keys.onPressed: event => {
            if (event.key === Qt.Key_PageDown) contentY = Math.min(originY + Math.max(0,contentHeight - height),contentY + height * .85)
            else if (event.key === Qt.Key_PageUp) contentY = Math.max(originY,contentY - height * .85)
            else return
            event.accepted = true
        }

        header: Column {
            width: page.width
            Loader {
                width: parent.width
                sourceComponent: browser.browsingSeries ? seriesHero : homeHero
                Component {
                    id: homeHero
                    HomeHero {
                        series: browser.hero; featured: browser.featured; gutter: browser.gutter
                        hasHistory: !!browser.resumeVideo
                        positionMs: hasHistory ? browser.recent.positionMs : 0
                        durationMs: hasHistory ? browser.recent.durationMs : 0
                        onPlayRequested: browser.openVideo(browser.playID, browser.playTitle)
                        onBrowseRequested: if (browser.hero) browser.openSeries(browser.hero.id)
                        onShowFocusedItem: item => page.revealItem(item)
                    }
                }
                Component {
                    id: seriesHero
                    BrowserHero {
                        series: browser.hero; featured: browser.featured; seriesPage: true; gutter: browser.gutter
                        positionMs: browser.resumeVideo ? browser.recent.positionMs : 0
                        durationMs: browser.resumeVideo ? browser.recent.durationMs : 0
                        onPlayRequested: browser.openVideo(browser.playID, browser.playTitle)
                        onBrowseRequested: if (browser.videos.length) browser.focusEpisode(0)
                        onImportRequested: browser.importRequested()
                    }
                }
            }
            Item {
                width: parent.width
                height: browser.browsingSeries ? (browser.width < 580 ? 60 : 78)
                    : 16 + Math.max(browser.shelfTitleSize * 1.3, rail.overflow ? 36 : 0) + 24
                RowLayout {
                    anchors.fill: parent; anchors.leftMargin: browser.browsingSeries ? browser.gutter : browser.shelfGutter
                    anchors.rightMargin: anchors.leftMargin
                    anchors.topMargin: browser.browsingSeries ? 0 : 16; anchors.bottomMargin: browser.browsingSeries ? 0 : 24
                    spacing: browser.browsingSeries ? 10 : 12
                    Mark { text: browser.browsingSeries ? qsTr("选集") : qsTr("视频系列"); font.pixelSize: browser.browsingSeries ? Studio.pageTitleSize : Math.round(browser.shelfTitleSize)
                        font.weight: Font.DemiBold; objectName: "shelfHeading" }
                    Mark { text: browser.browsingSeries ? qsTr("%1 集").arg(browser.videos.length) : qsTr("%1 部作品").arg(workspace.series.length); font.pixelSize: 12; secondary: true }
                    Item { Layout.fillWidth: true }
                    Tool { objectName:"seriesActions"; visible: browser.browsingSeries; text:qsTr("系列操作"); glyph:"more"
                        onClicked: menu.open()
                        Menu { id: menu; y: parent.height
                            MenuItem { text:qsTr("编辑系列资料…"); onTriggered: browser.manageSeries(browser.heroID,"edit") }
                            MenuItem { text:qsTr("编辑集号…"); enabled: browser.videos.length>0; onTriggered: browser.manageSeries(browser.heroID,"episode") }
                            MenuItem { text:qsTr("导出解析结果…"); enabled: browser.videos.length>0; onTriggered: browser.manageSeries(browser.heroID,"export") }
                        }
                    }
                    Tool { visible: rail.overflow; outlined: true; corner: 18; implicitWidth: 36; implicitHeight: 36; glyph: "left"; hint: qsTr("上一组系列"); enabled: !rail.atXBeginning; onClicked: rail.moveByPage(-1) }
                    Tool { visible: rail.overflow; outlined: true; corner: 18; implicitWidth: 36; implicitHeight: 36; glyph: "chevron"; hint: qsTr("下一组系列"); enabled: !rail.atXEnd; onClicked: rail.moveByPage(1) }
                }
            }
            ListView {
                id: rail
                objectName: "seriesShelf"
                x: browser.shelfGutter; width: parent.width - 2 * x
                visible: !browser.browsingSeries
                readonly property real cardWidth: browser.width <= 580 ? width * .76 : Math.min(400, Math.max(240, width * .28))
                readonly property bool overflow: visible && contentWidth > width + 1
                height: visible ? Math.ceil(cardWidth * 10 / 16) + 100 : 0
                model: visible ? workspace.series : []
                orientation: ListView.Horizontal; spacing: browser.width <= 580 ? 16 : Math.max(18, Math.min(32, browser.width * .016))
                snapMode: ListView.SnapToItem
                boundsBehavior: Flickable.StopAtBounds; clip: true
                property bool waitingForFirstCard: true
                onCountChanged: {
                    if (!count) waitingForFirstCard = true
                    else if (waitingForFirstCard) {
                        waitingForFirstCard = false
                        Qt.callLater(() => { forceLayout(); positionViewAtBeginning() })
                    }
                }
                function moveByPage(direction) {
                    const stride = cardWidth + spacing
                    const offset = Math.round((contentX - originX + direction * width * .85) / stride) * stride
                    const dest = originX + Math.max(0, Math.min(contentWidth - width, offset))
                    shelfSlide.stop()
                    if (Studio.reduce) contentX = dest
                    else { shelfSlide.from = contentX; shelfSlide.to = dest; shelfSlide.start() }
                }
                NumberAnimation { id: shelfSlide; target: rail; property: "contentX"; duration: Studio.panel; easing.type: Easing.OutCubic }
                onDraggingChanged: if (dragging) shelfSlide.stop()
                function focusCard(index, alignStart) {
                    if (index < 0 || index >= count) return
                    positionViewAtIndex(index, alignStart ? ListView.Beginning : ListView.Contain)
                    forceLayout()
                    Qt.callLater(() => itemAtIndex(index)?.forceActiveFocus(Qt.TabFocusReason))
                }
                delegate: MediaCard {
                    required property var modelData
                    required property int index
                    width: rail.cardWidth; height: implicitHeight
                    readonly property var history: browser.enabled ? workspace.recentPlayback[modelData.id] : null
                    readonly property var saved: history ? (workspace.videosBySeries[modelData.id] || []).find(v => v.id === history.videoID) : null
                    onHistoryChanged: if (history) workspace.ensureSeriesLoaded(modelData.id)
                    title: modelData.title
                    Binding on cover {
                        when: browser.enabled; delayed: true
                        value: saved?.posterID ? "image://posters/shelf/" + saved.posterID + "/" + history.positionMs
                             : modelData.posterID ? "image://posters/shelf/" + modelData.posterID + "/-1" : (modelData.cover || "")
                        restoreMode: Binding.RestoreNone
                    }
                    coverFallback: modelData.cover || ""
                    compactShelf: browser.width <= 580
                    detail: qsTr("%1 集 · 查看系列").arg(modelData.count); objectName: "seriesCard_" + modelData.id
                    onClicked: browser.openSeries(modelData.id)
                    onActiveFocusChanged: if (activeFocus) rail.positionViewAtIndex(index, ListView.Contain)
                    onVisualFocusChanged: if (visualFocus) page.revealItem(this)
                    Keys.onLeftPressed: rail.focusCard(index - 1,false)
                    Keys.onRightPressed: rail.focusCard(index + 1,false)
                }
            }
            Mark {
                x: browser.gutter; width: parent.width - 2*x
                visible: browser.browsingSeries && !!browser.hero && !browser.hero.count
                text: qsTr("这个系列还没有视频。"); color: Studio.muted
            }
        }
        delegate: Item {
            id: episodeRow
            required property int index
            width: page.width; height: Math.ceil(browser.cellWidth * 9/16) + 78
            function focusCard(column) { episodeCards.itemAt(column)?.forceActiveFocus(Qt.TabFocusReason) }
            Row {
                x: browser.gutter; spacing: browser.gap
                Repeater {
                    id: episodeCards
                    model: Math.min(browser.columns, Math.max(0,browser.videos.length - episodeRow.index * browser.columns))
                    MediaCard {
                        required property int index
                        readonly property int entryIndex: episodeRow.index * browser.columns + index
                        readonly property var video: browser.videos[entryIndex]
                        width: browser.cellWidth; height: implicitHeight; episode: true
                        title: video?.title || ""; cover: video?.cover || ""; objectName: "episodeCard_" + (video?.id || "")
                        badge: video?.durationMs > 0 ? Studio.time(video.durationMs,false) : ""
                        readonly property var progress: video ? workspace.playbackByVideo[video.id] : null
                        detail: video ? qsTr("第 ") + video.episode + qsTr(" 集") + (progress ? qsTr("  ·  已看至 ") + Studio.time(progress.positionMs, false) : "") + (["READY","INDEXED"].includes(video.indexStatus) ? qsTr("  ·  可搜索") : "") : ""
                        onClicked: if (video) browser.openVideo(video.id, video.title)
                        onActiveFocusChanged: if (activeFocus) page.positionViewAtIndex(episodeRow.index, ListView.Contain)
                        Keys.onLeftPressed: browser.focusEpisode(entryIndex-1)
                        Keys.onRightPressed: browser.focusEpisode(entryIndex+1)
                        Keys.onUpPressed: browser.focusEpisode(entryIndex-browser.columns)
                        Keys.onDownPressed: browser.focusEpisode(entryIndex+browser.columns)
                    }
                }
            }
        }
        footer: Item { width: page.width; height: browser.browsingSeries ? 24 : 64 }
    }
}
