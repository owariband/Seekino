import QtQuick
import Qt.labs.platform as Native

Native.MenuBar {
    id: bar
    property var owner
    window: owner || null
    Native.Menu {
        title: "文件"
        Native.MenuItem { text: "导入视频…"; shortcut: StandardKey.Open; enabled: workspace.ready && !workspace.busyImport; onTriggered: bar.owner.choose() }
        Native.MenuItem { text: "新建系列…"; enabled: workspace.ready; onTriggered: bar.owner.newFolder() }
        Native.MenuItem { text: "设置…"; role: Native.MenuItem.PreferencesRole; shortcut: "Ctrl+,"; onTriggered: bar.owner.openSettings() }
    }
    Native.Menu {
        title: "显示"
        Native.MenuItem { text: bar.owner?.treeShown ? "隐藏资料树" : "显示资料树"; shortcut: "Ctrl+Shift+L"; onTriggered: bar.owner.toggleTree() }
        Native.MenuItem { text: "搜索片段"; shortcut: "Ctrl+K"; onTriggered: bar.owner.searchPage() }
        Native.MenuItem { text: "解析任务"; onTriggered: bar.owner.go("jobs") }
    }
}
