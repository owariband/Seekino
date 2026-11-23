import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts

Sheet {
    id: prefs
    property bool saved: false
    function openSettings() { saved=false; ark.clear(); deep.clear(); open() }
    onClosed: { ark.clear(); deep.clear(); if (!saved) workspace.cancelDeferredImport() }
    function accept() {
        workspace.saveKeys(ark.text.trim(), deep.text.trim())
    }
    Connections {
        target: workspace
        function onCredentialsSaved() { prefs.saved=true; prefs.close() }
    }
    contentItem: ColumnLayout {
        spacing: 0
        RowLayout {
            Layout.fillWidth: true
            Mark { Layout.fillWidth: true; text: "设置"; font.pixelSize: 19; font.weight: Font.DemiBold }
            Tool { glyph: "close"; symbolSize: 14; hint: "关闭设置"; onClicked: prefs.close() }
        }
        Mark { Layout.topMargin: 18; text: "外观"; secondary: true; font.pixelSize: 12 }
        RowLayout {
            Layout.topMargin: 5; spacing: 3
            Repeater {
                model: [{id:"system",name:"跟随系统"},{id:"light",name:"浅色"},{id:"dark",name:"深色"}]
                Tool { required property var modelData; text: modelData.name; chosen: Studio.preferences.appearance === modelData.id; onClicked: Studio.preferences.appearance = modelData.id }
            }
        }
        Rectangle { Layout.fillWidth: true; Layout.topMargin: 15; Layout.bottomMargin: 15; Layout.preferredHeight: 1; color: Studio.line }
        Mark { text: "模型凭据"; font.weight: Font.DemiBold; font.pixelSize: 15 }
        Text { Layout.fillWidth: true; Layout.topMargin: 5; text: "视频解析和描述搜索使用模型服务。密钥保存在本机。"; color: Studio.muted; font.pixelSize: 12; wrapMode: Text.Wrap }
        Mark { text: "火山方舟 API Key"; Layout.topMargin: 16; font.pixelSize: 12 }
        Entry { id: ark; objectName: "arkKey"; Layout.fillWidth: true; Layout.topMargin: 4; echoMode: TextInput.Password; Accessible.name: "火山方舟 API Key"; placeholderText: workspace.modelKeys.ARK_API_KEY ? "已配置，留空保留" : "输入 API Key" }
        Mark { text: "DeepSeek API Key"; Layout.topMargin: 12; font.pixelSize: 12 }
        Entry { id: deep; objectName: "deepseekKey"; Layout.fillWidth: true; Layout.topMargin: 4; echoMode: TextInput.Password; Accessible.name: "DeepSeek API Key"; placeholderText: workspace.modelKeys.DEEPSEEK_API_KEY ? "已配置，留空保留" : "输入 API Key"; onAccepted: if (save.enabled) prefs.accept() }
        RowLayout {
            Layout.topMargin: 19; Layout.fillWidth: true
            Item { Layout.fillWidth: true }
            Tool { text: "关闭"; onClicked: prefs.close() }
            Tool { id: save; primary: true; text: "保存凭据"; enabled: !!(ark.text.trim() || deep.text.trim()); onClicked: prefs.accept() }
        }
    }
}
