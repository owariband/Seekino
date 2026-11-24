import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts

Item {
    id: browser
    property bool browsingSeries: false
    readonly property var selected: workspace.series.find(item => item.id === workspace.selectedSeries)
    readonly property var videos: workspace.videosBySeries[workspace.selectedSeries] || []
    readonly property var hero: browsingSeries ? selected : workspace.series[0]
    readonly property string heroID: hero?.id || ""
    readonly property var members: workspace.videosBySeries[heroID] || []
    readonly property var recent: workspace.recentPlayback?.[heroID]
    readonly property var resumeVideo: recent?.positionMs > 0 && recent.positionMs < recent.durationMs
        ? members.find(video => video.id === recent.videoID) : null
    readonly property string playID: resumeVideo?.id || hero?.firstVideoID || ""
    readonly property string playTitle: resumeVideo?.title || hero?.firstVideoTitle || ""
    readonly property var featured: resumeVideo || members.find(video => video.id === playID)
        || workspace.jobs.find(job => job.latest && job.videoID === playID)
    readonly property int gutter: width < 580 ? 16 : 24
    readonly property int columns: Math.max(1, Math.floor((page.width - 2 * gutter + gap) / (190 + gap)))
    readonly property int gap: width < 580 ? 12 : 16
    readonly property real cellWidth: (page.width - 2 * gutter - gap * (columns-1)) / columns
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
    // Verify the history's video still belongs to this library before offering to resume it.
    readonly property bool loadHomeHistory: workspace.ready && !browsingSeries && !!recent && !!heroID
    onLoadHomeHistoryChanged: if (loadHomeHistory) workspace.ensureSeriesLoaded(heroID)
    onHeroIDChanged: {
        if (loadHomeHistory) workspace.ensureSeriesLoaded(heroID)
        Qt.callLater(() => page.positionViewAtBeginning())
    }
    onBrowsingSeriesChanged: Qt.callLater(() => page.positionViewAtBeginning())

    EmptyView {
        anchors.centerIn: parent; anchors.verticalCenterOffset: -12
        width: Math.min(430, parent.width - 48)
        visible: !browser.browsingSeries && workspace.series.length === 0
        firstImport: true; needsModel: !workspace.importModelReady
        title: "导入第一部视频"
        message: "把视频拖到这里，或从电脑选择文件。\n解析后，就能按台词和画面找回片段。"
        action: "导入视频"; onActivated: browser.importRequested()
        onSettingsRequested: browser.settingsRequested()
    }

    ListView {
        id: page
        objectName: "libraryScroll"
        anchors.fill: parent; visible: !!browser.hero; clip: true
        boundsBehavior: Flickable.StopAtBounds
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
        model: browser.browsingSeries ? Math.ceil(browser.videos.length / browser.columns) : 0
        Keys.onPressed: event => {
            if (event.key === Qt.Key_PageDown) contentY = Math.min(originY + Math.max(0,contentHeight - height),contentY + height * .85)
            else if (event.key === Qt.Key_PageUp) contentY = Math.max(originY,contentY - height * .85)
            else return
            event.accepted = true
        }

        header: Column {
            width: page.width
            BrowserHero {
                width: parent.width; series: browser.hero; featured: browser.featured
                positionMs: browser.resumeVideo ? browser.recent.positionMs : 0
                durationMs: browser.resumeVideo ? browser.recent.durationMs : 0
                seriesPage: browser.browsingSeries; gutter: browser.gutter
                onPlayRequested: browser.openVideo(browser.playID, browser.playTitle)
                onBrowseRequested: {
                    if (browser.browsingSeries && browser.videos.length) browser.focusEpisode(0)
                    else if (!browser.browsingSeries && browser.hero) browser.openSeries(browser.hero.id)
                }
                onImportRequested: browser.importRequested()
            }
            Item {
                width: parent.width; height: browser.width < 580 ? 60 : 78
                RowLayout {
                    anchors.fill: parent; anchors.leftMargin: browser.gutter; anchors.rightMargin: browser.gutter
                    spacing: 10
                    Mark { text: browser.browsingSeries ? "选集" : "视频系列"; font.pixelSize: Studio.pageTitleSize; font.weight: Font.DemiBold }
                    Mark { text: browser.browsingSeries ? browser.videos.length + " 集" : workspace.series.length + " 个系列"; font.pixelSize: 12; secondary: true }
                    Item { Layout.fillWidth: true }
                    Tool { visible: rail.visible && rail.contentWidth > rail.width; outlined: true; glyph: "left"; hint: "上一组系列"; enabled: !rail.atXBeginning; onClicked: rail.moveByPage(-1) }
                    Tool { visible: rail.visible && rail.contentWidth > rail.width; outlined: true; glyph: "chevron"; hint: "下一组系列"; enabled: !rail.atXEnd; onClicked: rail.moveByPage(1) }
                }
            }
            ListView {
                id: rail
                objectName: "seriesShelf"
                x: browser.gutter; width: parent.width - 2 * x
                visible: !browser.browsingSeries
                readonly property real cardWidth: Math.min(width,Math.min(340,Math.max(214,width * .34)))
                height: visible ? Math.ceil(cardWidth * 10 / 16) + 68 : 0
                model: visible ? workspace.series : []
                orientation: ListView.Horizontal; spacing: browser.gap
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
                    const steps = Math.max(1,Math.floor(width / (cardWidth + spacing)))
                    const first = Math.round((contentX - originX) / (cardWidth + spacing))
                    focusCard(Math.max(0, Math.min(count-1, first + direction * steps)), true)
                }
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
                    title: modelData.title; cover: modelData.cover || ""
                    detail: modelData.count + " 集"; objectName: "seriesCard_" + modelData.id
                    onClicked: browser.openSeries(modelData.id)
                    onActiveFocusChanged: if (activeFocus) rail.positionViewAtIndex(index, ListView.Contain)
                    Keys.onLeftPressed: rail.focusCard(index - 1,false)
                    Keys.onRightPressed: rail.focusCard(index + 1,false)
                }
            }
            Mark {
                x: browser.gutter; width: parent.width - 2*x
                visible: browser.browsingSeries && !!browser.hero && !browser.hero.count
                text: "这个系列还没有视频。"; color: Studio.muted
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
                        detail: video ? "第 " + video.episode + " 集" + (["READY","INDEXED"].includes(video.indexStatus) ? "  ·  可搜索" : "") : ""
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
        footer: Item { width: page.width; height: 24 }
    }
}
