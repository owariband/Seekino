import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts

Sheet {
    id: sheet
    objectName: "watchModelSettings"
    property string protocol: "openai_chat"
    property string revision: ""
    property string issue: ""
    property bool saving: false
    readonly property var saved: workspace.modelConfig.watchSettings || ({})
    readonly property bool sameService: protocol === saved.protocol && address.text.trim().replace(/\/+$/, "") === saved.baseUrl
    readonly property bool canKeepKey: sameService && workspace.modelConfig.watchHasKey
    readonly property bool localAddress: /^https?:\/\/(localhost|127\.\d+\.\d+\.\d+|\[::1\])([:/]|$)/i.test(address.text.trim())
    function choose(value) {
        protocol = value
        const original = value === saved.protocol
        address.text = original ? saved.baseUrl : value === "ark" ? "https://ark.cn-beijing.volces.com/api/v3" : ""
        modelId.text = original ? saved.model : ""
        apiKey.text = ""
        noKey.checked = original && saved.auth === "none"
        issue = ""
    }
    function openSettings() {
        revision = workspace.modelConfig.watchRevision || ""
        saving = false
        choose(saved.protocol || (workspace.modelConfig.watchInheritedModel ? "inherit" : "openai_chat"))
        issue = workspace.modelConfig.watchSettingsIssue || ""
        open()
    }
    function save() {
        const form = protocol === "inherit" ? {inherit: true} : {
            protocol: protocol, baseUrl: address.text.trim(), model: modelId.text.trim(),
            auth: noKey.visible && noKey.checked ? "none" : "key"
        }
        form.revision = revision
        saving = true
        issue = ""
        workspace.saveWatchConnection(form, apiKey.text)
    }
    onClosed: apiKey.text = ""
    width: Math.min(488, parent.width - 24)
    height: Math.min(body.implicitHeight + topPadding + bottomPadding, parent.height - 24)
    closePolicy: saving ? Popup.NoAutoClose : Popup.CloseOnEscape
    Connections {
        target: workspace
        function onWatchConnectionSaved() { sheet.saving = false; if (sheet.visible) sheet.close() }
        function onWatchConnectionFailed(message) { sheet.saving = false; sheet.issue = message }
    }
    contentItem: ColumnLayout {
        id: body
        spacing: 16
        Mark { text: qsTr("观看问答模型"); font.pixelSize: 20; font.weight: Font.DemiBold }
        Flickable {
            id: scroll
            Layout.fillWidth: true; Layout.fillHeight: true; implicitHeight: fields.implicitHeight
            contentHeight: fields.implicitHeight; clip: true; boundsBehavior: Flickable.StopAtBounds
            ScrollBar.vertical: ScrollBar { }
            function reveal(field) {
                const y = field.mapToItem(fields, 0, 0).y
                contentY = Math.max(0, Math.min(contentHeight - height, y < contentY ? y : Math.max(contentY, y + field.height - height)))
            }
            ColumnLayout {
                id: fields; width: parent.width; spacing: 12
                RowLayout {
                    spacing: 4
                    Repeater {
                        model: [{label:qsTr("沿用资料库"), key:"inherit"}, {label:qsTr("OpenAI 兼容"), key:"openai_chat"}, {label:"Ark", key:"ark"}]
                        Tool { required property var modelData; text: modelData.label; chosen: sheet.protocol === modelData.key
                            onClicked: sheet.choose(modelData.key) }
                    }
                }
                Mark {
                    visible: sheet.protocol === "inherit"; Layout.fillWidth: true
                    text: workspace.modelConfig.watchInheritedModel || qsTr("资料库尚未配置视觉模型。"); secondary: true
                }
                ModelField { id: address; visible: sheet.protocol !== "inherit"; Layout.fillWidth: true
                    label: qsTr("服务地址"); inputName: "watchModelAddress"; placeholder: "https://…/v1"; readOnly: sheet.protocol === "ark"
                    onEdited: { apiKey.text = ""; sheet.issue = "" }
                    onFocused: field => scroll.reveal(field) }
                ModelField { id: modelId; visible: address.visible; Layout.fillWidth: true
                    label: qsTr("视觉模型 ID"); inputName: "watchModelId"; placeholder: qsTr("支持图片输入的模型")
                    onFocused: field => scroll.reveal(field) }
                ModelDiscovery {
                    objectName: "watchModelDiscovery"; Layout.fillWidth: true; visible: address.visible
                    enabled: !sheet.saving
                    protocol: sheet.protocol; baseUrl: address.text
                    apiKey: noKey.visible && noKey.checked ? "" : apiKey.text
                    revision: sheet.revision; purpose: "watch"
                    targets: [{key:"watch", label:qsTr("观看模型"), hint:qsTr("填入观看问答")}]
                    onPicked: (model, target) => { modelId.text = model; sheet.issue = "" }
                }
                CheckBox { id: noKey; visible: sheet.protocol === "openai_chat" && sheet.localAddress; text: qsTr("本机服务无需 API Key")
                    palette.windowText: Studio.ink }
                ModelField { id: apiKey; visible: address.visible && !(noKey.visible && noKey.checked); Layout.fillWidth: true
                    label: "API Key"; inputName: "watchModelKey"; secret: TextInput.Password
                    placeholder: sheet.canKeepKey ? qsTr("已保存，留空可继续使用") : ""
                    onFocused: field => scroll.reveal(field) }
                Text { Layout.fillWidth: true; text: qsTr("提问时，Kino 会把当前画面和问题发送给所选模型。")
                    color: Studio.muted; font.pixelSize: 13; wrapMode: Text.WordWrap }
                Text { Layout.fillWidth: true; visible: !!sheet.issue; text: Studio.translate(sheet.issue); color: Studio.ink; wrapMode: Text.WordWrap }
            }
        }
        RowLayout {
            Layout.alignment: Qt.AlignRight
            Tool { text: qsTr("取消"); enabled: !sheet.saving; onClicked: sheet.close() }
            Tool { objectName: "saveWatchModel"; text: sheet.saving ? qsTr("保存中…") : qsTr("保存"); primary: true
                enabled: !sheet.saving && (sheet.protocol === "inherit" || (!!address.text.trim() && !!modelId.text.trim()
                    && ((noKey.visible && noKey.checked) || !!apiKey.text.trim() || sheet.canKeepKey)))
                onClicked: sheet.save() }
        }
    }
}
