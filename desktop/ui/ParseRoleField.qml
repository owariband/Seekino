import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts

ColumnLayout {
    id: field
    required property string roleKey
    required property string label
    required property var profiles
    required property var effective
    required property string revision
    property Component headingControl
    property bool showConnection: true
    property bool optional: false
    property string serviceId: ""
    property alias modelId: entry.text
    property bool expanded: false
    readonly property var selected: profiles.find(p => p.id === serviceId) || ({})
    readonly property var choices: (optional ? [{id:"", name:qsTr("沿用视频理解")}] : []).concat(profiles)
    readonly property bool complete: optional && !serviceId || !!selected.id && (selected.hasKey || selected.auth === "none") && !!entry.text.trim()
    signal editRequested(var profile)
    signal reveal(var item)
    spacing: 6
    function setService(id, model) {
        serviceId = id || ""
        picker.currentIndex = choices.findIndex(p => p.id === serviceId)
        entry.text = model ?? selected[roleKey] ?? ""
        expanded = false
    }
    RowLayout {
        Layout.fillWidth: true
        Mark { text: field.label; font.weight: Font.DemiBold; Layout.fillWidth: true }
        Loader { active: !!field.headingControl; visible: active; sourceComponent: field.headingControl }
        Tool { visible: field.showConnection && !!field.selected.id && field.selected.protocol !== "ark"; objectName: "chooseModel_" + field.roleKey
            text: qsTr("获取模型列表"); textSize: 12; chosen: field.expanded
            onClicked: { field.expanded = !field.expanded; if (field.expanded) discovery.fetchModels() } }
    }
    RowLayout {
        visible: field.showConnection; Layout.fillWidth: true; spacing: 8
        Picker {
            id: picker; objectName: "roleService_" + field.roleKey
            Layout.fillWidth: true; Layout.preferredWidth: 1; Layout.maximumWidth: field.width * (entry.visible ? .48 : 1)
            model: field.choices; textRole: "name"; Accessible.name: field.label + " · " + qsTr("服务连接")
            onActivated: i => field.setService(field.choices[i].id, null)
        }
        Entry { id: entry; objectName: field.roleKey + "Model"; Layout.fillWidth: true; Layout.preferredWidth: 1
            visible: !field.optional || !!field.serviceId; placeholderText: field.selected.protocol === "ark" ? qsTr("接入点或模型 ID") : qsTr("模型 ID")
            Accessible.name: field.label + " · " + qsTr("模型 ID")
            onActiveFocusChanged: if (activeFocus) field.reveal(field) }
    }
    Text { visible: field.showConnection && !!field.selected.pendingRoles?.includes(field.roleKey); Layout.fillWidth: true
        text: qsTr("当前使用 %1，应用后切换到新的连接设置。").arg(field.effective.baseUrl || "")
        color: Studio.muted; font.pixelSize: 12; wrapMode: Text.Wrap; textFormat: Text.PlainText }
    Tool { visible: field.showConnection && !!field.selected.id && !field.selected.hasKey && field.selected.auth !== "none"
        text: qsTr("填写 API Key"); onClicked: field.editRequested(field.selected) }
    ModelDiscovery {
        id: discovery; visible: field.showConnection && field.expanded; Layout.fillWidth: true
        showFetchButton: false
        protocol: field.selected.protocol || ""; baseUrl: field.selected.baseUrl || ""
        purpose: "service:" + field.serviceId; revision: field.revision
        targets: [{key:field.roleKey, label:qsTr("使用")}]
        onPicked: (model, target) => { entry.text = model; field.expanded = false }
    }
}
