import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts

ColumnLayout {
    id: discovery
    objectName: "modelDiscovery"
    property string protocol: ""
    property string baseUrl: ""
    property string apiKey: ""
    property string purpose: "model"
    property string revision: ""
    property var targets: []
    property var models: []
    property bool listAvailable: true
    property bool fetched: false
    property bool fetching: false
    property string issue: ""
    property string requestId: ""
    signal picked(string modelId, string target)
    spacing: 8

    function clear() {
        models = []
        listAvailable = true
        fetched = false
        fetching = false
        issue = ""
        requestId = ""
    }
    function fetchModels() {
        if (fetching) return
        if (!baseUrl.trim()) {
            issue = qsTr("请先填写服务地址。")
            return
        }
        issue = ""
        models = []
        listAvailable = true
        fetched = false
        fetching = true
        requestId = String(Date.now()) + "-" + Math.random().toString(16).slice(2)
        workspace.discoverModels(requestId, purpose, protocol, baseUrl.trim(), apiKey, revision)
    }
    onProtocolChanged: clear()
    onBaseUrlChanged: clear()
    onApiKeyChanged: clear()
    onRevisionChanged: clear()
    Connections {
        target: workspace
        function onModelsDiscovered(requestId, modelIds, available) {
            if (requestId !== discovery.requestId) return
            discovery.fetching = false
            discovery.fetched = true
            discovery.models = modelIds
            discovery.listAvailable = available
        }
        function onModelDiscoveryFailed(requestId, message) {
            if (requestId !== discovery.requestId) return
            discovery.fetching = false
            discovery.issue = message
        }
    }
    Text {
        Layout.fillWidth: true; visible: discovery.protocol === "ark"
        text: qsTr("方舟使用推理接入点 ID，请手动填写模型 ID。")
        color: Studio.muted; font.pixelSize: 12; wrapMode: Text.Wrap
        textFormat: Text.PlainText
    }
    RowLayout {
        Layout.fillWidth: true; visible: discovery.protocol !== "ark"; spacing: 8
        Tool {
            objectName: "discoverModels"
            text: discovery.fetching ? qsTr("正在获取…") : qsTr("获取模型列表")
            outlined: true; corner: 7; implicitHeight: 30
            enabled: !discovery.fetching
            onClicked: discovery.fetchModels()
        }
        Text {
            Layout.fillWidth: true
            text: discovery.fetching ? qsTr("正在获取模型列表…") : discovery.issue
            visible: text.length > 0
            color: Studio.muted; font.pixelSize: 12; wrapMode: Text.Wrap
            textFormat: Text.PlainText
        }
    }
    Text {
        Layout.fillWidth: true
        visible: discovery.protocol !== "ark" && !discovery.fetching && !discovery.issue && !discovery.fetched
        text: qsTr("可从服务读取模型 ID，或手动填写。")
        color: Studio.muted; font.pixelSize: 12; wrapMode: Text.Wrap
        textFormat: Text.PlainText
    }
    Text {
        Layout.fillWidth: true
        visible: discovery.protocol !== "ark" && discovery.fetched && !discovery.fetching && !discovery.issue
            && (!discovery.listAvailable || discovery.models.length === 0)
        text: discovery.listAvailable ? qsTr("未获取到模型 ID，可手动填写。") : qsTr("该服务不提供模型列表，请手动填写模型 ID。")
        color: Studio.muted; font.pixelSize: 12; wrapMode: Text.Wrap
        textFormat: Text.PlainText
    }
    Text {
        Layout.fillWidth: true
        visible: discovery.protocol !== "ark" && discovery.fetched && !discovery.fetching && !discovery.issue
            && discovery.listAvailable && discovery.models.length > 0
        text: qsTr("获取到 ") + discovery.models.length + qsTr(" 个模型，点击用途填入。")
        color: Studio.muted; font.pixelSize: 12; wrapMode: Text.Wrap
        textFormat: Text.PlainText
    }
    ListView {
        id: list
        Layout.fillWidth: true
        Layout.preferredHeight: visible ? Math.min(168, contentHeight) : 0
        visible: models.length > 0
        clip: true
        spacing: 4
        model: discovery.models
        delegate: RowLayout {
            required property string modelData
            property string discoveredId: modelData
            width: list.width
            spacing: 6
            Mark {
                Layout.fillWidth: true; text: discoveredId; elide: Text.ElideMiddle
                font.pixelSize: 12
            }
            Repeater {
                model: discovery.targets
                Tool {
                    required property var modelData
                    text: modelData.label
                    hint: modelData.hint || modelData.label
                    textSize: 11
                    outlined: true
                    implicitHeight: 28
                    onClicked: discovery.picked(discoveredId, modelData.key)
                }
            }
        }
    }
}
