import QtQuick
import Qt.labs.platform as Native

Native.MenuBar {
    id: bar
    property var owner
    window: owner || null
    Native.Menu {
        title: qsTr("文件")
        Native.MenuItem { text: qsTr("导入视频…"); shortcut: StandardKey.Open; enabled: workspace.ready && !workspace.busyImport; onTriggered: bar.owner.choose() }
        Native.MenuItem { text: qsTr("新建系列…"); enabled: workspace.ready; onTriggered: bar.owner.newFolder() }
        Native.MenuItem { text: qsTr("设置…"); role: Native.MenuItem.PreferencesRole; shortcut: "Ctrl+,"; onTriggered: bar.owner.openSettings() }
    }
    Native.Menu {
        title: qsTr("显示")
        Native.MenuItem { text: bar.owner?.treeShown ? qsTr("隐藏资料树") : qsTr("显示资料树"); shortcut: "Ctrl+Shift+L"; onTriggered: bar.owner.toggleTree() }
        Native.MenuItem { text: qsTr("搜索片段"); shortcut: "Ctrl+K"; onTriggered: bar.owner.searchPage() }
        Native.MenuItem { text: qsTr("解析任务"); onTriggered: bar.owner.go("jobs") }
    }
    Native.Menu {
        title: qsTr("语言")
        Native.MenuItem {
            text: qsTr("简体中文"); checkable: true; checked: desktopLocale.language === "zh"
            onTriggered: desktopLocale.setLanguage("zh")
        }
        Native.MenuItem {
            text: "English"; checkable: true; checked: desktopLocale.language === "en"
            onTriggered: desktopLocale.setLanguage("en")
        }
    }
    Native.Menu {
        title: qsTr("帮助")
        Native.MenuItem {
            text: qsTr("使用文档…"); shortcut: StandardKey.HelpContents
            onTriggered: bar.owner.openHelp()
        }
        Native.MenuItem {
            text: qsTr("在浏览器中打开用户文档")
            onTriggered: Qt.openUrlExternally("https://github.com/owariband/Seekino")
        }
    }
}
