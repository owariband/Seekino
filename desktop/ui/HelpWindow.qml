import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import QtQuick.Window

Window {
    id: help
    objectName: "helpWindow"
    property var mainWindow
    width: 640; height: 620
    minimumWidth: 480; minimumHeight: 420
    title: qsTr("使用文档 — Seekino")
    color: Studio.paper
    flags: Qt.Dialog
    transientParent: mainWindow || null
    function open() {
        show()
        raise()
        requestActivate()
    }
    Shortcut { sequence: "Escape"; enabled: help.visible; onActivated: help.close() }

    component Heading: Text {
        Layout.fillWidth: true
        color: Studio.ink
        font.pixelSize: 16
        font.weight: Font.DemiBold
        wrapMode: Text.Wrap
    }
    component Copy: Text {
        Layout.fillWidth: true
        color: Studio.muted
        font.pixelSize: 14
        lineHeight: 1.3
        wrapMode: Text.Wrap
    }
    component Step: RowLayout {
        id: step
        required property int number
        required property string stepTitle
        required property string stepBody
        Layout.fillWidth: true
        spacing: 12
        Rectangle {
            Layout.preferredWidth: 24; Layout.preferredHeight: 24
            Layout.alignment: Qt.AlignTop
            radius: 12; color: Studio.selected
            Text {
                anchors.centerIn: parent
                text: String(step.number)
                color: Studio.actionBg; font.pixelSize: 12; font.weight: Font.DemiBold
            }
        }
        ColumnLayout {
            Layout.fillWidth: true
            spacing: 3
            Text { Layout.fillWidth: true; text: step.stepTitle; color: Studio.ink; font.pixelSize: 14; font.weight: Font.DemiBold; wrapMode: Text.Wrap }
            Text { Layout.fillWidth: true; text: step.stepBody; color: Studio.muted; font.pixelSize: 13; lineHeight: 1.25; wrapMode: Text.Wrap }
        }
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 24
        spacing: 14
        RowLayout {
            Layout.fillWidth: true
            Text {
                Layout.fillWidth: true
                text: qsTr("使用文档")
                color: Studio.ink; font.pixelSize: Studio.pageTitleSize; font.weight: Font.DemiBold
            }
            Tool { glyph: "close"; hint: qsTr("关闭"); onClicked: help.close() }
        }
        Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Studio.line }
        Flickable {
            id: docsScroll
            Layout.fillWidth: true; Layout.fillHeight: true
            contentWidth: width; contentHeight: body.implicitHeight
            clip: true; boundsBehavior: Flickable.StopAtBounds
            ScrollBar.vertical: ScrollBar { policy: docsScroll.contentHeight > docsScroll.height ? ScrollBar.AsNeeded : ScrollBar.AlwaysOff }
            ColumnLayout {
                id: body
                width: docsScroll.width - (docsScroll.contentHeight > docsScroll.height ? 12 : 0)
                spacing: 14
                Copy { text: qsTr("Seekino 把视频变成可以搜索的素材库。说出你记得的画面或想弄懂的概念，它把你直接送到原片里的那一秒。") }
                Heading { text: qsTr("三步找回片段") }
                Step {
                    number: 1
                    stepTitle: qsTr("说出你要找的")
                    stepBody: qsTr("按 ⌘K（Windows 上是 Ctrl+K），像问朋友一样描述。不用记得原话，也不用知道在第几集。")
                }
                Step {
                    number: 2
                    stepTitle: qsTr("点开时间，直接看")
                    stepBody: qsTr("每条结果都带着原片和精确到秒的时间范围。点一下，视频从那一秒开始播放，不用再前后拖。")
                }
                Step {
                    number: 3
                    stepTitle: qsTr("看不懂，就接着问")
                    stepBody: qsTr("播放时打开右侧的助手，问“刚才这段在讲什么”。它知道你看到了哪里，回答里的每个片段都能点开核对。")
                }
                Heading { text: qsTr("导入与解析") }
                Copy { text: qsTr("把视频拖入窗口，或按 ⌘O 选择文件。首次使用先在设置中配置模型服务；解析完成后就可以搜索。桌面版把视频、索引和播放记录保存在本机。") }
                Heading { text: qsTr("快捷键") }
                GridLayout {
                    Layout.fillWidth: true
                    columns: 2
                    columnSpacing: 20; rowSpacing: 7
                    Text { text: qsTr("导入"); color: Studio.muted; font.pixelSize: 13 }
                    Text { text: help.mainWindow?.mac ? "⌘O" : "Ctrl+O"; color: Studio.ink; font.pixelSize: 13 }
                    Text { text: qsTr("搜索"); color: Studio.muted; font.pixelSize: 13 }
                    Text { text: help.mainWindow?.mac ? "⌘K" : "Ctrl+K"; color: Studio.ink; font.pixelSize: 13 }
                    Text { text: qsTr("资料树"); color: Studio.muted; font.pixelSize: 13 }
                    Text { text: help.mainWindow?.mac ? "⌘⇧L" : "Ctrl+Shift+L"; color: Studio.ink; font.pixelSize: 13 }
                    Text { text: qsTr("设置"); color: Studio.muted; font.pixelSize: 13 }
                    Text { text: help.mainWindow?.mac ? "⌘," : "Ctrl+,"; color: Studio.ink; font.pixelSize: 13 }
                    Text { text: qsTr("帮助"); color: Studio.muted; font.pixelSize: 13 }
                    Text { text: "F1"; color: Studio.ink; font.pixelSize: 13 }
                }
                Heading { text: qsTr("更多资料") }
                Copy { text: qsTr("桌面版与浏览器版共用同一套用户文档。完整使用说明、部署方式和版本差异见仓库。") }
            }
        }
        Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Studio.line }
        RowLayout {
            Layout.fillWidth: true
            Tool {
                text: qsTr("在浏览器中打开用户文档"); glyph: "arrow"
                onClicked: Qt.openUrlExternally("https://github.com/owariband/Seekino")
            }
            Item { Layout.fillWidth: true }
            Tool { text: qsTr("关闭"); outlined: true; onClicked: help.close() }
        }
    }
}
