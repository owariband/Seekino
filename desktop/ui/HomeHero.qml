import QtQuick
import QtQuick.Layouts

Item {
    id: hero
    objectName: "homeHero"
    required property var series
    property var featured: null
    property bool hasHistory: false
    property int positionMs: 0
    property int durationMs: 0
    property real gutter: 20
    readonly property bool compact: width <= 760
    readonly property real titleSize: compact ? Math.min(56, Math.max(32, width * .08)) : Math.min(80, Math.max(40, width * .05))
    readonly property string posterSource: {
        const candidate = series?.cover || featured?.cover || ""
        return candidate.startsWith("image://posters/") && frame.status !== Image.Error ? "" : candidate
    }
    readonly property string artworkSource: {
        const posterID = hasHistory ? featured?.posterID : series?.posterID
        return posterID ? "image://posters/hero/" + posterID + "/" + (hasHistory ? positionMs : -1) : ""
    }
    signal playRequested()
    signal browseRequested()
    signal showFocusedItem(Item item)
    clip: true
    // Same border-box height and vertical padding as the Web homepage.
    implicitHeight: compact ? Math.max(590, 230 + 32 + copy.implicitHeight + 70)
                            : Math.max(520, Math.min(720, width * .54))

    component HeroAction: Tool {
        id: action
        property bool trailing: false
        primary: true
        onVisualFocusChanged: if (visualFocus) hero.showFocusedItem(action)
        implicitHeight: 48
        implicitWidth: labels.implicitWidth + (hero.compact ? 36 : 48)
        contentColor: trailing ? "#f3f8f5" : "#10251f"
        contentItem: Item {
            implicitWidth: labels.implicitWidth
            Row {
                id: labels; anchors.centerIn: parent; spacing: 10
                layoutDirection: action.trailing ? Qt.RightToLeft : Qt.LeftToRight
                Glyph { anchors.verticalCenter: parent.verticalCenter; width: action.trailing ? 16 : 20; height: width; name: action.glyph; ink: action.contentColor }
                Mark { anchors.verticalCenter: parent.verticalCenter; text: action.text; color: action.contentColor
                    font.pixelSize: hero.compact ? 13 : 14; font.weight: Font.DemiBold }
            }
        }
        background: Rectangle {
            radius: height / 2
            color: action.trailing ? (action.hovered ? "#1fffffff" : "#4005100d") : action.hovered ? "#b1e9da" : "#8cd4c2"
            border.color: action.visualFocus ? "white" : "#59ffffff"; border.width: action.visualFocus ? 2 : action.trailing ? 1 : 0
            Behavior on color { ColorAnimation { duration: Studio.hover } }
        }
        scale: down && !Studio.reduce ? .97 : 1
        Behavior on scale { NumberAnimation { duration: Studio.quick } }
    }

    Rectangle { anchors.fill: parent; color: Studio.heroBase }
    Item {
        objectName: "heroArtwork"; clip: true
        width: hero.width; height: hero.compact ? Math.max(0, hero.height - 130) : hero.height * 1.22
        Image {
            anchors.fill: parent; asynchronous: true; mipmap: true; fillMode: Image.PreserveAspectCrop
            source: hero.posterSource; visible: frame.status !== Image.Ready
        }
        Image {
            id: frame; objectName: "homeHeroFrame"
            anchors.fill: parent; asynchronous: true; mipmap: true; fillMode: Image.PreserveAspectCrop
            source: hero.artworkSource; opacity: status === Image.Ready ? 1 : 0
            Behavior on opacity { NumberAnimation { duration: Studio.hover } }
        }
        Rectangle { anchors.fill: parent; color: "#29000000"; visible: Studio.dark }
    }
    Rectangle {
        anchors.fill: parent; visible: !hero.compact
        gradient: Gradient {
            orientation: Gradient.Horizontal
            GradientStop { position: 0; color: Qt.alpha(Studio.heroShade,.92) }
            GradientStop { position: .36; color: Qt.alpha(Studio.heroShade,.62) }
            GradientStop { position: .78; color: Qt.alpha(Studio.heroShade,.08) }
        }
    }
    Rectangle {
        anchors.fill: parent; visible: hero.compact
        gradient: Gradient {
            GradientStop { position: .15; color: Qt.alpha(Studio.heroShade,.04) }
            GradientStop { position: .60; color: Qt.alpha(Studio.heroShade,.88) }
            GradientStop { position: .88; color: Studio.heroBase }
        }
    }
    Rectangle {
        anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom
        height: hero.compact ? 80 : 150
        gradient: Gradient {
            GradientStop { position: 0; color: Qt.alpha(Studio.heroShade,0) }
            GradientStop { position: 1; color: Qt.alpha(Studio.heroShade,Studio.dark ? 1 : .72) }
        }
    }

    ColumnLayout {
        id: copy; objectName: "heroCopy"
        x: hero.gutter
        y: hero.compact ? hero.height - 70 - 16 - height : (hero.height + 64 - 100 - height) / 2
        width: hero.compact ? hero.width - 2*x : Math.min(620, (hero.width - 2*x) * .55)
        height: implicitHeight; spacing: 0
        Mark {
            Layout.fillWidth: true; Layout.preferredHeight: 22
            color: "#b6e4d7"; font.pixelSize: 14; font.weight: Font.DemiBold
            text: hero.hasHistory ? qsTr("继续上次的故事") : qsTr("从这里开始")
        }
        Text {
            objectName: "heroTitle"; Layout.fillWidth: true; Layout.topMargin: 14
            Layout.preferredHeight: Math.ceil(lineCount * lineHeight)
            text: hero.series?.title || ""; textFormat: Text.PlainText; color: "#f3f8f5"
            font.pixelSize: Math.round(hero.titleSize); font.weight: Font.Bold; font.letterSpacing: -.022 * hero.titleSize
            lineHeightMode: Text.FixedHeight; lineHeight: hero.titleSize * 1.05
            wrapMode: Text.Wrap; verticalAlignment: Text.AlignVCenter
        }
        Flow {
            Layout.fillWidth: true; Layout.preferredHeight: childrenRect.height; Layout.topMargin: hero.compact ? 14 : 20; spacing: 20
            Mark { visible: !!hero.featured?.episode; font.pixelSize: hero.compact ? 12 : 13; color: "#9bd5c4"; text: qsTr("第 %1 集").arg(hero.featured?.episode || "") }
            Mark { font.pixelSize: hero.compact ? 12 : 13; color: "#d1dfd8"; text: qsTr("%1 集").arg(hero.series?.count || 0) }
            Mark { visible: hero.hasHistory; color: "#d1dfd8"; font.pixelSize: hero.compact ? 12 : 13; text: qsTr("上次看到 %1").arg(Studio.time(hero.positionMs,false)) }
        }
        Text {
            Layout.fillWidth: true; Layout.maximumWidth: 460; Layout.topMargin: hero.compact ? 16 : 22
            objectName: "heroDescription"
            text: hero.series?.description || (hero.hasHistory ? qsTr("故事还在继续，回到上次停下的那一刻。") : qsTr("选一集开始观看，也可以一起寻找故事里的名场面。"))
            textFormat: Text.PlainText; color: "#d1dfd8"; font.pixelSize: hero.compact ? 14 : 15
            lineHeightMode: Text.FixedHeight; lineHeight: (hero.compact ? 13.6 : 15.2) * 1.8
            wrapMode: Text.Wrap; maximumLineCount: hero.compact ? 2 : 3; elide: Text.ElideRight; verticalAlignment: Text.AlignVCenter
        }
        Flow {
            Layout.fillWidth: true; Layout.topMargin: hero.compact ? 22 : 30
            Layout.preferredHeight: childrenRect.height; spacing: 14
            HeroAction {
                objectName: "heroPlay"; visible: !!hero.series?.firstVideoID || !!hero.featured?.id; glyph: "play"
                text: hero.hasHistory ? qsTr("继续观看") : qsTr("播放本集"); onClicked: hero.playRequested()
            }
            HeroAction { objectName: "heroBrowse"; trailing: true; glyph: "chevron"; text: qsTr("查看系列"); onClicked: hero.browseRequested() }
        }
        ColumnLayout {
            visible: hero.hasHistory && hero.durationMs > 0; Layout.fillWidth: true; Layout.maximumWidth: 320
            Layout.topMargin: hero.compact ? 22 : 30; spacing: 9
            Rectangle {
                Layout.fillWidth: true; Layout.preferredHeight: 3; radius: 2; color: "#38ffffff"
                Rectangle { height: parent.height; width: parent.width * Math.min(1, hero.positionMs / Math.max(1,hero.durationMs)); radius: 2; color: "#8cd4c2" }
            }
            Mark { color: "#c3d8ce"; font.pixelSize: 12; font.features: ({ "tnum": 1 })
                text: Studio.time(hero.positionMs,false) + " / " + Studio.time(hero.durationMs,false) }
        }
    }
}
