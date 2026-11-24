import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts

Rectangle {
    id: tree
    property string activeVideo: ""
    property string page: ""
    property var expanded: ({})
    signal seriesPicked(string seriesID)
    signal videoPicked(string videoID, string title)
    signal newSeriesRequested()
    signal libraryRequested()
    function setExpanded(id, value) {
        if (value) workspace.ensureSeriesLoaded(id)
        if (!!expanded[id] === value) return
        var copy = Object.assign({}, expanded)
        copy[id] = value; expanded = copy
    }
    color: Studio.sidebar
    ColumnLayout {
        anchors.fill: parent; anchors.topMargin: 6
        spacing: 0
        RowLayout {
            Layout.fillWidth: true; Layout.leftMargin: 14; Layout.rightMargin: 8; spacing: 4
            Tool {
                text: "资料库"; hint: "查看全部系列"; textSize: 12; onClicked: tree.libraryRequested()
                implicitWidth: contentItem.implicitWidth + 5
            }
            Item { Layout.fillWidth: true }
            Tool { objectName: "createSeriesButton"; glyph: "plus"; symbolSize: 14; hint: "新建系列"; enabled: workspace.ready; onClicked: tree.newSeriesRequested() }
        }
        ListView {
            Layout.fillHeight: true; Layout.fillWidth: true; Layout.leftMargin: 5; Layout.rightMargin: 5
            model: workspace.series; clip: true; boundsBehavior: Flickable.StopAtBounds
            ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
            delegate: Column {
                id: seriesNode
                required property var modelData
                width: ListView.view.width
                readonly property bool open: !!tree.expanded[modelData.id]
                readonly property var files: workspace.videosBySeries[modelData.id] || []
                TreeRow {
                    objectName: "treeSeries_" + seriesNode.modelData.id
                    text: seriesNode.modelData.title; count: seriesNode.modelData.count; branch: true; hasDisclosure: true; expanded: seriesNode.open
                    current: tree.page === "series" && workspace.selectedSeries === seriesNode.modelData.id
                    onClicked: tree.seriesPicked(seriesNode.modelData.id)
                    onDisclose: tree.setExpanded(seriesNode.modelData.id, !expanded)
                }
                Item {
                    width: parent.width; clip: true
                    height: seriesNode.open ? fileList.implicitHeight : 0
                    Behavior on height { NumberAnimation { duration: Studio.hover; easing.type: Easing.OutCubic } }
                    Column {
                        id: fileList; width: parent.width
                        visible: parent.height > 0
                        Repeater {
                            model: seriesNode.open || parent.parent.height > 0 ? seriesNode.files : []
                            delegate: TreeRow {
                                required property var modelData
                                objectName: "treeVideo_" + modelData.id
                                text: modelData.title; current: tree.page === "player" && tree.activeVideo === modelData.id
                                onClicked: tree.videoPicked(modelData.id, modelData.title)
                            }
                        }
                    }
                }
            }
            Mark {
                visible: workspace.ready && workspace.series.length === 0
                x: 13; y: 10; text: "暂无系列"; font.pixelSize: 12; color: Studio.dim
            }
        }
        Mark {
            Layout.leftMargin: 16; Layout.topMargin: 8; Layout.bottomMargin: 12
            font.pixelSize: 11; color: Studio.dim
            text: workspace.series.length + " 个系列"
        }
    }
    Rectangle { anchors.right: parent.right; width: 1; height: parent.height; color: Studio.line }
}
