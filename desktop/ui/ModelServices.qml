import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts

ColumnLayout {
    id: page
    property bool editing: false
    property var selected: ({})
    property string draftSignature: ""
    property string lastProtocol: ""
    property string revision: ""
    property string issue: ""
    property string confirmId: ""
    readonly property var config: workspace.modelConfig
    readonly property var profiles: config.serviceProfiles || []
    readonly property var protocols: [{id:"openai_chat", label:qsTr("OpenAI 兼容")}, {id:"ark", label:"Ark"}]
    readonly property string protocol: protocols[typePicker.currentIndex]?.id || "openai_chat"
    readonly property bool loopback: /^https?:\/\/(localhost|127(?:\.\d{1,3}){3}|\[::1\])(?::\d+)?(?:\/|$)/i.test(address.text.trim())
    readonly property bool anonymous: protocol === "openai_chat" && loopback && localAuth.checked
    readonly property bool keepKey: !!selected.hasKey && selected.protocol === protocol
        && selected.baseUrl.replace(/\/+$/, "") === address.text.trim().replace(/\/+$/, "")
    readonly property var matches: profiles.filter(p => {
        const needle = filter.text.trim().toLowerCase()
        return !needle || [p.name, p.baseUrl, p.protocol === "ark" ? "Ark" : qsTr("OpenAI 兼容")].some(v => String(v).toLowerCase().includes(needle))
    })
    readonly property bool canSave: !config.servicesIssue && editing && !workspace.savingKeys && workspace.ready && !!nameField.text.trim()
        && !!address.text.trim() && (anonymous || !!key.text.trim() || keepKey)
        && (draftSignature !== signature() || !!key.text.trim())
    signal saved(string id)
    signal configureRole(string id)
    signal reveal(var item)
    spacing: 14
    function signature() { return JSON.stringify([nameField.text.trim(), protocol, address.text.trim(), anonymous]) }
    function edit(row) {
        selected = row || ({})
        revision = config.servicesRevision || ""
        typePicker.currentIndex = selected.protocol === "ark" ? 1 : 0
        lastProtocol = protocol
        address.text = selected.baseUrl || ""
        nameField.text = selected.name || ""
        key.text = ""; localAuth.checked = selected.auth === "none"
        issue = ""; confirmId = ""; editing = true; draftSignature = signature()
        Qt.callLater(() => nameField.fieldFocus())
    }
    function reset() {
        key.text = ""; editing = false; issue = ""; selected = ({}); confirmId = ""; filter.clear()
    }
    function save() {
        if (!canSave) return
        const form = Object.assign({}, selected, {name:nameField.text.trim(), protocol:protocol,
            baseUrl:address.text.trim(), auth:anonymous ? "none" : "key", servicesRevision:revision})
        workspace.saveServiceProfile(form, anonymous ? "" : key.text.trim())
    }
    Connections {
        target: workspace
        function onServiceProfileSaved(id) {
            page.reset()
            page.saved(id)
        }
        function onServiceProfileFailed(message) { page.issue = message }
    }

    RowLayout {
        Layout.fillWidth: true
        Mark { Layout.fillWidth: true; text: page.editing ? (page.selected.id ? qsTr("编辑连接") : qsTr("添加连接")) : qsTr("模型服务")
            font.pixelSize: 19; font.weight: Font.DemiBold }
        Tool { objectName: "addService"; visible: !page.editing; enabled: !page.config.servicesIssue; text: qsTr("添加连接"); glyph: "plus"; outlined: true; corner: 8
            onClicked: page.edit(null) }
    }
    ColumnLayout {
        visible: !page.editing
        Layout.fillWidth: true; spacing: 8
        Entry { id: filter; objectName: "serviceSearch"; visible: page.profiles.length > 1
            Layout.fillWidth: true; placeholderText: qsTr("搜索名称或服务地址"); Accessible.name: placeholderText }
        Mark { visible: !page.matches.length; Layout.fillWidth: true; secondary: true; wrapMode: Text.Wrap
            text: page.profiles.length ? qsTr("没有匹配的连接。") : qsTr("添加模型服务后，可以为解析、检索和观看问答选择模型。") }
        Repeater {
            model: page.matches
            Rectangle {
                id: card
                required property var modelData
                Layout.fillWidth: true; implicitHeight: contents.implicitHeight + 22; radius: 9; color: Studio.sheet; border.color: Studio.line
                ColumnLayout {
                    id: contents; x: 12; y: 11; width: parent.width - 24; spacing: 5
                    RowLayout {
                        Layout.fillWidth: true
                        Mark { text: card.modelData.name; font.weight: Font.DemiBold; Layout.fillWidth: true; textFormat: Text.PlainText }
                        Mark { text: card.modelData.protocol === "ark" ? "Ark" : qsTr("OpenAI 兼容"); secondary: true; font.pixelSize: 12 }
                    }
                    Mark { text: card.modelData.baseUrl; Layout.fillWidth: true; secondary: true; textFormat: Text.PlainText; elide: Text.ElideMiddle }
                    RowLayout {
                        Layout.fillWidth: true; spacing: 3
                        Mark { Layout.fillWidth: true; secondary: true; font.pixelSize: 11
                            text: [card.modelData.parseActive ? qsTr("解析与检索") : "", card.modelData.watchActive ? qsTr("观看问答") : ""].filter(Boolean).join(" · ") }
                        Tool { objectName: "useService_" + card.modelData.id; text: qsTr("配置用途"); onClicked: page.configureRole(card.modelData.id) }
                        Tool { objectName: "editService_" + card.modelData.id; text: qsTr("编辑"); onClicked: page.edit(card.modelData) }
                        Tool { visible: !card.modelData.parseActive && !card.modelData.watchActive && !card.modelData.pendingRoles?.length && page.confirmId !== card.modelData.id
                            text: qsTr("删除"); onClicked: page.confirmId = card.modelData.id }
                    }
                    Mark { visible: !!card.modelData.pendingRoles?.length; Layout.fillWidth: true; secondary: true
                        text: qsTr("连接已更新，请在模型用途中应用更改。"); font.pixelSize: 12; wrapMode: Text.Wrap }
                    RowLayout {
                        visible: page.confirmId === card.modelData.id; Layout.fillWidth: true
                        Mark { text: qsTr("删除这个连接？"); Layout.fillWidth: true }
                        Tool { text: qsTr("取消"); onClicked: page.confirmId = "" }
                        Tool { text: qsTr("删除"); onClicked: workspace.removeServiceProfile(card.modelData.id, page.config.servicesRevision) }
                    }
                }
            }
        }
    }
    ColumnLayout {
        visible: page.editing; Layout.fillWidth: true; spacing: 14
        ModelField { id: nameField; inputName: "connectionName"; Layout.fillWidth: true; label: qsTr("连接名称")
            placeholder: qsTr("为连接命名"); onFocused: item => page.reveal(item) }
        ColumnLayout {
            Layout.fillWidth: true; spacing: 5
            Mark { text: qsTr("Provider 类型") }
            Picker { id: typePicker; objectName: "providerType"; Accessible.name: qsTr("Provider 类型")
                Layout.fillWidth: true; model: page.protocols; textRole: "label"
                onActivated: {
                    if (page.protocol === page.lastProtocol) return
                    page.lastProtocol = page.protocol
                    key.text = ""; localAuth.checked = false; page.issue = ""
                    address.text = page.selected.protocol === page.protocol ? page.selected.baseUrl :
                        page.protocol === "ark" ? "https://ark.cn-beijing.volces.com/api/v3" : ""
                }
            }
        }
        ModelField { id: address; inputName: "serviceUrl"; label: qsTr("服务地址"); Layout.fillWidth: true; placeholder: "https://…/v1"
            readOnly: page.protocol === "ark"; onEdited: { key.text = ""; localAuth.checked = false }
            onFocused: item => page.reveal(item) }
        CheckBox { id: localAuth; objectName: "serviceLocalAuth"; visible: page.protocol === "openai_chat" && page.loopback
            text: qsTr("本机免密"); palette.windowText: Studio.ink; onToggled: key.text = "" }
        ModelField { id: key; inputName: "connectionKey"; label: "API Key"; Layout.fillWidth: true; visible: !page.anonymous
            secret: TextInput.Password; placeholder: page.keepKey ? qsTr("留空保留当前密钥") : qsTr("输入此服务的 API Key")
            onFocused: item => page.reveal(item) }
    }
    Text { visible: !!page.issue || !!page.config.servicesIssue; Layout.fillWidth: true; text: Studio.translate(page.issue || page.config.servicesIssue || ""); textFormat: Text.PlainText
        color: Studio.danger; font.pixelSize: 13; wrapMode: Text.Wrap }
}
