import QtQuick
import QtQuick.Layouts

Item {
    id: hero
    required property var series
    property var featured: null
    property bool seriesPage: false
    property int positionMs: 0
    property int durationMs: 0
    property int gutter: 24
    readonly property bool compact: width < 580
    readonly property bool quietSize: width < 700
    readonly property string cover: positionMs > 0 && featured?.cover ? featured.cover : series?.cover || ""
    readonly property bool playable: !!series?.firstVideoID
    readonly property bool hasCover: !!cover
    readonly property color backdrop: Studio.mix("#10251f", "#101a17")
    signal playRequested()
    signal browseRequested()
    signal importRequested()
    implicitHeight: compact ? Math.max(186, copy.implicitHeight + 42)
        : Math.max(copy.implicitHeight + 62, Math.min(426, Math.max(292, width * .39)))

    component HeroAction: Tool {
        id: action
        property bool trailing: false
        primary: true
        implicitHeight: hero.quietSize ? 36 : 40
        implicitWidth: contentItem.implicitWidth + (hero.quietSize ? 28 : 32)
        fillColor: trailing ? "#203a31" : "#8cd4c2"
        contentColor: trailing ? "#f3f8f5" : "#10251f"
        contentItem: Item {
            implicitWidth: labels.implicitWidth
            Row {
                id: labels; anchors.centerIn: parent; spacing: hero.quietSize ? 7 : 9
                layoutDirection: action.trailing ? Qt.RightToLeft : Qt.LeftToRight
                Glyph { anchors.verticalCenter: parent.verticalCenter; width: hero.quietSize ? 15 : 18; height: width; name: action.glyph; ink: action.contentColor }
                Mark { anchors.verticalCenter: parent.verticalCenter; text: action.text; color: action.contentColor; font.pixelSize: hero.quietSize ? 13 : 14; font.weight: Font.DemiBold }
            }
        }
        background: Rectangle {
            radius: height/2
            color: action.down ? Qt.darker(action.fillColor,1.1) : action.hovered ? Qt.lighter(action.fillColor,1.14) : action.fillColor
            border.color: action.visualFocus ? "#d3fff1" : "#65a5c4b8"
            border.width: action.visualFocus ? 2 : action.trailing ? 1 : 0
            Behavior on color { ColorAnimation { duration: Studio.hover } }
        }
    }

    Rectangle { anchors.fill: parent; color: hero.backdrop }
    Item {
        id: artwork
        objectName: "heroArtwork"
        visible: hero.hasCover && !hero.compact
        anchors.right: parent.right
        width: Math.max(0, parent.width - copy.x - copy.width)
        height: parent.height
        clip: true
        Image {
            id: poster
            anchors.fill: parent
            source: artwork.visible ? hero.cover : ""
            asynchronous: true; mipmap: true
            // Keep faces, details and portrait framing inside the photograph's own part of the hero.
            fillMode: Image.PreserveAspectFit
            opacity: status === Image.Ready ? 1 : 0
            Behavior on opacity { NumberAnimation { duration: Studio.page; easing.type: Easing.OutCubic } }
        }
        Item {
            anchors.centerIn: parent; width: Math.ceil(poster.paintedWidth) + 2; height: Math.ceil(poster.paintedHeight) + 2
            Rectangle {
                anchors.fill: parent
                gradient: Gradient {
                    orientation: Gradient.Horizontal
                    GradientStop { position: 0; color: hero.backdrop }
                    GradientStop { position: .12; color: Qt.alpha(hero.backdrop, .28) }
                    GradientStop { position: .25; color: Qt.alpha(hero.backdrop, 0) }
                    GradientStop { position: .83; color: Qt.alpha(hero.backdrop, 0) }
                    GradientStop { position: 1; color: hero.backdrop }
                }
            }
            Rectangle {
                anchors.fill: parent
                gradient: Gradient {
                    GradientStop { position: 0; color: hero.backdrop }
                    GradientStop { position: .14; color: Qt.alpha(hero.backdrop, .13) }
                    GradientStop { position: .42; color: Qt.alpha(hero.backdrop, 0) }
                    GradientStop { position: .66; color: Qt.alpha(hero.backdrop, .18) }
                    GradientStop { position: .77; color: Qt.alpha(hero.backdrop, .71) }
                    GradientStop { position: 1; color: hero.backdrop }
                }
            }
        }
    }

    ColumnLayout {
        id: copy
        objectName: "heroCopy"
        x: hero.gutter
        y: (hero.height - height) / 2
        width: hero.compact || !hero.hasCover ? hero.width - 2 * x : Math.round(hero.width * .43) - x
        height: implicitHeight; spacing: 0
        Mark {
            Layout.fillWidth: true; color: "#a6cdbf"; font.pixelSize: 12; font.weight: Font.DemiBold
            text: hero.seriesPage ? qsTr("视频系列") : hero.positionMs > 0 ? qsTr("上次观看") : qsTr("从这里开始")
        }
        Text {
            objectName: "heroTitle"
            Layout.fillWidth: true; Layout.topMargin: 8
            text: hero.series?.title || ""; textFormat: Text.PlainText; color: "#f3f8f5"
            font.pixelSize: copy.width >= 448 ? 32 : Qt.platform.os === "windows" ? 28 : 26
            font.weight: Font.DemiBold
            lineHeightMode: Text.FixedHeight; lineHeight: font.pixelSize + 8
            wrapMode: Text.Wrap; maximumLineCount: 2; elide: Text.ElideRight
        }
        RowLayout {
            Layout.topMargin: hero.compact ? 5 : 9; spacing: 9
            Mark { visible: !!hero.featured?.episode; text: qsTr("第 %1 集").arg(hero.featured?.episode || ""); font.pixelSize: 13; font.weight: Font.DemiBold; color: "#9bd5c4" }
            Mark { visible: hero.series?.count > 0; text: qsTr("共 %1 集").arg(hero.series?.count || 0); font.pixelSize: 13; color: "#c2d2ca" }
        }
        Text {
            Layout.fillWidth: true; Layout.topMargin: hero.compact ? 9 : 15
            text: hero.series?.description || (hero.playable ? (hero.seriesPage ? (hero.featured?.title || hero.series?.firstVideoTitle) : qsTr("选一集开始观看，也可以一起寻找故事里的名场面。")) : qsTr("导入视频，开始这个系列。"))
            textFormat: Text.PlainText; color: "#c8d8d0"; font.pixelSize: hero.compact ? 13 : 14
            lineHeightMode: Text.FixedHeight; lineHeight: hero.compact ? 20 : 23
            wrapMode: Text.Wrap; maximumLineCount: 2; elide: Text.ElideRight
        }
        RowLayout {
            visible: hero.positionMs > 0 && hero.durationMs > 0
            Layout.fillWidth: true; Layout.maximumWidth: 260; Layout.topMargin: 10; spacing: 8
            Rectangle {
                Layout.fillWidth: true; Layout.preferredHeight: 3; radius: 2; color: "#385347"
                Rectangle { height: parent.height; width: parent.width * Math.min(1, hero.positionMs / Math.max(1,hero.durationMs)); radius: 2; color: "#8cd4c2" }
            }
            Mark { text: qsTr("已看至 %1").arg(Studio.time(hero.positionMs,hero.positionMs < 1000)); color: "#c8d8d0"; font.pixelSize: 12; font.features: ({ "tnum": 1 }) }
        }
        Flow {
            id: actions
            Layout.fillWidth: true; Layout.topMargin: hero.compact ? 14 : 21
            Layout.preferredHeight: childrenRect.height; spacing: 10
            HeroAction {
                objectName: "heroPlay"; visible: hero.playable; glyph: "play"
                text: hero.positionMs > 0 ? qsTr("继续观看") : hero.seriesPage ? qsTr("开始观看") : qsTr("播放本集")
                onClicked: hero.playRequested()
            }
            HeroAction {
                objectName: "heroBrowse"; visible: hero.playable || !hero.seriesPage
                trailing: true; glyph: hero.seriesPage ? "down" : "chevron"; text: hero.seriesPage ? qsTr("查看剧集") : qsTr("查看系列")
                onClicked: hero.browseRequested()
            }
            HeroAction {
                visible: !hero.playable && hero.seriesPage; glyph: "import"; text: qsTr("导入视频")
                onClicked: hero.importRequested()
            }
        }
    }
}
