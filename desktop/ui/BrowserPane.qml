import QtQuick
import QtQuick.Layouts
import QtQuick.Controls.Basic

Item {
    id: browser
    property bool browsingSeries: false
    property var selected: workspace.series.find(item => item.id === workspace.selectedSeries)
    readonly property var videos: workspace.videosBySeries[workspace.selectedSeries] || []
    readonly property var entries: browsingSeries ? videos : workspace.series
    readonly property var hero: browsingSeries ? selected : workspace.series[0]
    readonly property int gutter: Math.round(Math.max(20, Math.min(72,width*.05)))
    signal openSeries(string id)
    signal openVideo(string id, string title)
    signal importRequested()

    RowLayout {
        visible: !browser.browsingSeries && workspace.series.length === 0
        anchors.horizontalCenter: parent.horizontalCenter; anchors.top: parent.top; anchors.topMargin: 40
        spacing: 16
        Image { Layout.preferredWidth: 72; Layout.preferredHeight: 72; source: "art/mascot-puzzled.png"; sourceSize: Qt.size(144,144); fillMode: Image.PreserveAspectFit
            SequentialAnimation on rotation {
                running: parent.visible && !Studio.reduce && browser.Window.window?.active !== false; loops: Animation.Infinite
                NumberAnimation { to: -4; duration: 1600; easing.type: Easing.InOutSine }
                NumberAnimation { to: -7; duration: 1600; easing.type: Easing.InOutSine }
            }
        }
        Mark { text: "资料库里还没有视频。"; color: Studio.muted; font.pixelSize: 14 }
        AbstractButton {
            objectName: "emptyImport"; Accessible.name: "选取视频导入资料库"; hoverEnabled: true; onClicked: browser.importRequested()
            contentItem: Mark { text: "导入视频"; color: Studio.actionBg; font.pixelSize: 14; font.weight: Font.DemiBold; font.underline: true }
        }
    }
    Flickable {
        id: shelf
        visible: !!browser.hero; anchors.fill: parent; contentWidth: width
        contentHeight: heroPanel.height + collection.implicitHeight + 40
        clip: true; boundsBehavior: Flickable.StopAtBounds
        ScrollBar.vertical: ScrollBar {}
        Rectangle {
            id: heroPanel
            width: shelf.width; height: Math.max(Math.min(520, shelf.height*.8), Math.min(720,width*.54))
            color: Studio.heroBase; clip: true
            Image {
                anchors.fill: parent; source: browser.hero?.cover || ""; asynchronous: true; mipmap: true
                fillMode: Image.PreserveAspectCrop; opacity: Studio.dark ? .84 : 1
            }
            Rectangle { anchors.fill: parent; gradient: Gradient { orientation: Gradient.Horizontal
                GradientStop { position: 0; color: Qt.alpha(Studio.heroShade,.92) }
                GradientStop { position: .36; color: Qt.alpha(Studio.heroShade,.62) }
                GradientStop { position: .87; color: Qt.alpha(Studio.heroShade,.08) }
            } }
            Rectangle { width: parent.width; height: Math.min(130,parent.height/3); anchors.bottom: parent.bottom; gradient: Gradient {
                GradientStop { position: 0; color: "transparent" }
                GradientStop { position: 1; color: Qt.alpha(Studio.heroShade,Studio.dark ? 1 : .72) }
            } }
            ColumnLayout {
                x: browser.gutter; anchors.verticalCenter: parent.verticalCenter
                width: parent.width-2*browser.gutter; spacing: 0
                Mark { text: browser.browsingSeries ? "视频系列" : "从这里开始"; color: "#b6e4d7"; font.pixelSize: 14; font.weight: Font.DemiBold }
                Mark { Layout.topMargin: 12; Layout.fillWidth: true; text: browser.hero?.title || ""; color: "#f3f8f5"; font.pixelSize: Math.min(64,Math.max(38,browser.width*.05)); font.weight: Font.Bold; font.letterSpacing: -1 }
                Mark { Layout.topMargin: 12; text: (browser.hero?.count || 0) + " 集"; color: "#d1dfd8"; font.pixelSize: 13 }
                Text { Layout.fillWidth: true; Layout.topMargin: 17; text: browser.hero?.description || "选一集开始观看，也可以一起寻找故事里的名场面。"; color: "#d1dfd8"; font.pixelSize: 14; wrapMode: Text.Wrap; maximumLineCount: 2; elide: Text.ElideRight }
                RowLayout {
                    Layout.topMargin: 20; spacing: 12
                    Tool { objectName: "heroPlay"; visible: !!browser.hero?.firstVideoID; primary: true; fillColor: "#8cd4c2"; contentColor: "#10251f"; text: "播放本集"; glyph: "play"; onClicked: browser.openVideo(browser.hero.firstVideoID,browser.hero.firstVideoTitle) }
                    Tool {
                        primary: true; outlined: true; fillColor: "#4005100d"; contentColor: "#f3f8f5"; text: browser.browsingSeries ? "导入视频" : "查看系列"
                        glyph: browser.browsingSeries ? "import" : "chevron"
                        onClicked: browser.browsingSeries ? browser.importRequested() : browser.openSeries(browser.hero.id)
                    }
                }
            }
        }
        ColumnLayout {
            id: collection; x: browser.gutter; width: shelf.width-2*x; y: heroPanel.height+20; spacing: 14
            RowLayout {
                spacing: 10
                Mark { text: browser.browsingSeries ? "选集" : "视频系列"; font.pixelSize: 25; font.weight: Font.DemiBold }
                Mark { text: browser.entries.length + (browser.browsingSeries ? " 集" : " 部作品"); font.pixelSize: 12; secondary: true }
            }
            GridLayout {
                Layout.fillWidth: true; columns: Math.max(1, Math.floor((collection.width+18)/200))
                rowSpacing: 24; columnSpacing: 18
                Repeater {
                    model: browser.entries
                    MediaCard {
                        required property var modelData
                        Layout.preferredWidth: (collection.width-(parent.columns-1)*18)/parent.columns; Layout.maximumWidth: Layout.preferredWidth
                        episode: browser.browsingSeries; title: modelData.title; cover: modelData.cover || ""
                        badge: episode ? Studio.time(modelData.durationMs,false) : ""
                        detail: episode ? ("第 " + modelData.episode + " 集" + (["READY","INDEXED"].includes(modelData.indexStatus) ? "  ·  可搜索" : ""))
                                        : modelData.count + " 集 · 查看系列"
                        onClicked: episode ? browser.openVideo(modelData.id,modelData.title) : browser.openSeries(modelData.id)
                    }
                }
            }
            Mark { visible: browser.browsingSeries && !browser.videos.length; text: "这个系列还没有视频。"; color: Studio.muted }
        }
    }
    onHeroChanged: shelf.contentY = 0
}
