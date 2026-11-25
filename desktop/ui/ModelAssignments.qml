import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts

ColumnLayout {
    id: page
    property string purpose: "model"
    readonly property bool watch: purpose === "watch"
    property var activeProfile: ({})
    property string selection: ""
    property string revision: ""
    property string servicesRevision: ""
    property string issue: ""
    property bool resetLegacy: false
    property var secretEdits: ({})
    readonly property var config: workspace.modelConfig
    readonly property var profiles: config.serviceProfiles || []
    readonly property bool retain: !watch && (config.mode === "custom" || (config.mode === "legacy" && !resetLegacy))
    readonly property bool inherit: watch && selection === "inherit"
    property bool localEmbedding: false
    readonly property var download: typeof embeddingDownload !== "undefined" ? embeddingDownload : null
    readonly property bool savedLocal: config.form?.embeddingMode === "local"
    readonly property bool needsEncoder: !localEmbedding && embedding.selected.protocol === "ark" && embedding.modelId.trim().startsWith("ep-")
    readonly property bool busy: !workspace.ready || workspace.savingKeys || workspace.busyImport || workspace.searching
    readonly property bool complete: retain ? Object.values(secretEdits).some(v => v.trim()) :
        inherit ? !!Object.keys(config.watchSettings || {}).length :
        watch ? !!activeProfile.id && (activeProfile.auth === "none" || activeProfile.hasKey) && !!watchModel.text.trim() :
            vision.complete && transcription.complete && rerank.complete && (localEmbedding ? download?.state === "ready"
                : embedding.complete && Number(dimensions.text) >= 1 && Number(dimensions.text) <= 2048 && (!needsEncoder || !!encoder.text.trim()))
    readonly property var choices: (watch ? [{id:"inherit", name:qsTr("沿用资料库")}] : []).concat(profiles)
    readonly property var roleNames: ({vision:qsTr("视频理解"), transcription:qsTr("语音转录"), embedding:qsTr("向量嵌入"),
        rerank:qsTr("重排"), ocr:qsTr("文字识别"), summarize:qsTr("摘要"), agent:qsTr("通用模型"), supervisor:qsTr("复核"),
        diarization:qsTr("说话人识别"), vision_fallback:qsTr("备用视频理解")})
    signal addRequested()
    signal editRequested(var profile)
    signal reveal(var item)
    signal saved()
    spacing: 13

    function openRole(kind, id) {
        purpose = kind; issue = ""; resetLegacy = false
        revision = (watch ? config.watchRevision : config.revision) || ""
        servicesRevision = config.servicesRevision || ""
        secretEdits = ({})
        for (let i = 0; i < retainedKeys.count; i++) retainedKeys.itemAt(i).text = ""
        const assigned = profiles.find(p => watch ? p.id === config.effectiveRoleServices?.watch?.id : p.id === config.parseRoleServices?.vision)
        choose(id || assigned?.id || (watch ? "inherit" : profiles[0]?.id) || "")
        if (watch) issue = config.watchSettingsIssue || ""
        if (!watch) {
            const bound = config.parseRoleServices || ({})
            const form = config.form || ({})
            localEmbedding = savedLocal
            for (const field of [vision, transcription, embedding, rerank]) {
                if (field === embedding && savedLocal) {
                    field.setService(selection, null)
                    continue
                }
                const original = field.optional && !form.rerank ? "" : (bound[field.roleKey] || assigned?.id)
                const source = original || (field.optional ? "" : selection)
                field.setService(source, source && original === source ? form[field.roleKey] : null)
            }
            dimensions.text = String((savedLocal ? null : form.dimensions) || embedding.selected.dimensions || "")
            encoder.text = (savedLocal ? "" : form.encoderModel) || embedding.selected.encoderModel || ""
        }
    }
    function choose(id) {
        selection = id; activeProfile = profiles.find(p => p.id === id) || ({})
        providerPicker.currentIndex = choices.findIndex(p => p.id === id)
        issue = ""
        watchModel.text = activeProfile.watchModel || activeProfile.vision || ""
        discovery.clear()
    }
    function apply() {
        if (!complete || busy) return
        issue = ""
        if (retain) {
            workspace.saveModelConfig({retainConfig:true, revision:revision}, secretEdits)
        } else if (inherit) {
            workspace.saveWatchConnection({inherit:true, revision:revision}, "")
        } else {
            const roles = watch ? {watchModel:watchModel.text.trim()} : {
                roleServices:{vision:vision.serviceId, transcription:transcription.serviceId, rerank:rerank.serviceId},
                vision:vision.modelId.trim(), transcription:transcription.modelId.trim(),
                rerank:rerank.serviceId ? rerank.modelId.trim() : "", embeddingMode:localEmbedding ? "local" : "api"
            }
            if (!watch && !localEmbedding) {
                roles.roleServices.embedding = embedding.serviceId
                roles.embedding = embedding.modelId.trim()
                roles.dimensions = Number(dimensions.text)
                roles.encoderModel = needsEncoder ? encoder.text.trim() : ""
            }
            roles.revision = revision; roles.servicesRevision = servicesRevision
            workspace.applyServiceProfile(watch ? selection : vision.serviceId, purpose, roles)
        }
    }
    Connections {
        target: workspace
        function onWatchConnectionFailed(message) { page.issue = message }
        function onServiceProfileFailed(message) { page.issue = message }
        function onWatchConnectionSaved() { page.saved() }
        function onCredentialsSaved() { page.saved() }
    }
    component RoleField: ModelField { Layout.fillWidth: true; Layout.preferredWidth: 1; onFocused: item => page.reveal(item) }
    component Note: Text { Layout.fillWidth: true; textFormat: Text.PlainText; color: Studio.muted; font.pixelSize: 12; wrapMode: Text.Wrap }
    Mark { text: qsTr("模型用途"); font.pixelSize: 19; font.weight: Font.DemiBold }
    RowLayout {
        spacing: 4
        Tool { objectName: "parseRole"; text: qsTr("解析与检索"); chosen: !page.watch; outlined: chosen; corner: 7
            onClicked: if (page.watch) page.openRole("model", "") }
        Tool { objectName: "watchRole"; text: qsTr("观看问答"); chosen: page.watch; outlined: chosen; corner: 7
            onClicked: if (!page.watch) page.openRole("watch", "") }
    }
    ColumnLayout {
        Layout.fillWidth: true; visible: !page.retain && (page.watch || !page.profiles.length); spacing: 7
        Mark { text: qsTr("服务连接") }
        RowLayout {
            Layout.fillWidth: true; spacing: 8
            Picker { id: providerPicker; objectName: "roleConnection"; Layout.fillWidth: true; model: page.choices; textRole:"name"
                visible: page.choices.length > 0; Accessible.name: qsTr("服务连接"); onActivated: i => page.choose(page.choices[i].id) }
            Tool { objectName: "addRoleService"; text: qsTr("添加连接"); glyph: "plus"; onClicked: page.addRequested() }
        }
        Note { visible: !!page.activeProfile.id; text: page.activeProfile.baseUrl || "" }
        Note { visible: page.watch && !!page.activeProfile.pendingRoles?.includes("watch")
            text: qsTr("当前使用 %1，应用后切换到新的连接设置。").arg(page.config.effectiveRoleServices?.watch?.baseUrl || "") }
        Note { visible: page.inherit; text: page.config.watchInheritedModel || qsTr("资料库尚未配置视觉模型。") }
        Note {
            visible: !page.choices.length
            text: !page.watch && page.localEmbedding ? qsTr("视频理解和语音转录需要服务连接。") : qsTr("先添加服务连接，再为这个用途选择模型。")
        }
        Tool { visible: !!page.activeProfile.id && page.activeProfile.auth !== "none" && !page.activeProfile.hasKey
            text: qsTr("填写 API Key"); onClicked: page.editRequested(page.activeProfile) }
    }
    ColumnLayout {
        Layout.fillWidth: true; visible: !page.retain && (!page.watch || !!page.activeProfile.id); spacing: 12
        ModelDiscovery {
            id: discovery; visible: page.watch; Layout.fillWidth: true; enabled: !page.busy
            protocol: page.activeProfile.protocol || ""; baseUrl: page.activeProfile.baseUrl || ""
            purpose: "service:" + (page.activeProfile.id || ""); revision: page.servicesRevision
            targets: [{key:"watchModel", label:qsTr("视觉模型 ID")}]
            onPicked: (model, target) => watchModel.text = model
        }
        ColumnLayout {
            visible: !page.watch; Layout.fillWidth: true; spacing: 10
            component SourceField: ParseRoleField {
                Layout.fillWidth: true; effective: page.config.effectiveRoleServices?.[roleKey] || ({}); profiles: page.profiles; revision: page.servicesRevision
                onEditRequested: profile => page.editRequested(profile)
                onReveal: item => page.reveal(item)
            }
            SourceField { id: vision; visible: page.profiles.length > 0; roleKey:"vision"; label:qsTr("视频理解") }
            SourceField { id: transcription; visible: page.profiles.length > 0; roleKey:"transcription"; label:qsTr("语音转录") }
            SourceField {
                id: embedding; roleKey:"embedding"; label:qsTr("向量嵌入")
                showConnection: !page.localEmbedding && page.profiles.length > 0
                onServiceIdChanged: { dimensions.text = selected.dimensions ? String(selected.dimensions) : ""; encoder.text = selected.encoderModel || "" }
                headingControl: Component {
                    Rectangle {
                        implicitWidth: modeRow.implicitWidth + 4; implicitHeight: 32
                        radius: 7; color: Studio.wash; border.color: Studio.line
                        Row {
                            id: modeRow; anchors.centerIn: parent; spacing: 1
                            component Mode: Tool {
                                implicitHeight: 27; textSize: 12; corner: 5
                                Accessible.role: Accessible.RadioButton; Accessible.checked: chosen
                                Keys.onLeftPressed: { apiMode.click(); apiMode.forceActiveFocus(Qt.TabFocusReason) }
                                Keys.onRightPressed: { localMode.click(); localMode.forceActiveFocus(Qt.TabFocusReason) }
                            }
                            Mode {
                                id: apiMode; objectName: "embeddingUseApi"; text: qsTr("使用 API"); chosen: !page.localEmbedding
                                onClicked: page.localEmbedding = false
                            }
                            Mode {
                                id: localMode; objectName: "embeddingUseLocal"; text: qsTr("本机模型"); chosen: page.localEmbedding
                                onClicked: page.localEmbedding = true
                            }
                        }
                    }
                }
            }
            LocalEmbeddingModel { visible: page.localEmbedding; Layout.fillWidth: true; download: page.download }
            RowLayout { visible: embedding.showConnection; Layout.fillWidth: true
                RoleField { id: dimensions; inputName:"embeddingDimensions"; label:qsTr("向量维度"); placeholder:"1–2048"
                    validator:IntValidator {bottom:1; top:2048} }
                RoleField { id: encoder; inputName:"encoderModel"; visible:page.needsEncoder; label:qsTr("底层嵌入模型"); placeholder:qsTr("接入点对应的实际模型 ID") }
            }
            SourceField { id: rerank; visible: page.profiles.length > 0; roleKey:"rerank"; label:qsTr("重排"); optional:true }
        }
        RoleField { id: watchModel; visible: page.watch; inputName:"watchModelId"; label: qsTr("视觉模型 ID"); placeholder: qsTr("支持图片输入的模型") }
        Note { visible: page.watch; text: qsTr("提问时，Kino 会把当前画面和问题发送给所选模型。") }
    }
    ColumnLayout {
        visible: page.retain; Layout.fillWidth: true; spacing: 10
        Note { text: qsTr("当前连接来自资料库的模型配置，可在这里更新对应的 API Key。") }
        Repeater {
            model: page.retain ? page.config.connections || [] : []
            ColumnLayout {
                required property var modelData
                Layout.fillWidth: true
                Mark { text: modelData.label || modelData.name; font.weight: Font.DemiBold }
                Note { text: modelData.baseUrl || "" }
                Note { text: (modelData.roles || []).map(r => (page.roleNames[r.role] || r.role) + "   " + r.model).join("\n") }
            }
        }
        LocalEmbeddingModel { visible: page.savedLocal; Layout.fillWidth: true; download: page.download }
        Repeater {
            id: retainedKeys; model: page.retain ? page.config.credentialNames || [] : []
            RoleField {
                required property string modelData
                inputName: "key_" + modelData
                label: ((page.config.connections || []).filter(c => c.credentialName === modelData).map(c => c.label || c.name).join(" / ") || modelData) + " API Key"
                caption: page.config.credentials?.[modelData] ? qsTr("此地址已保存密钥") : qsTr("待填写")
                placeholder: qsTr("留空保留当前密钥"); secret: TextInput.Password
                onEdited: { const edits = Object.assign({},page.secretEdits); edits[modelData] = text; page.secretEdits = edits }
            }
        }
        Tool { visible: page.config.mode === "legacy"; text: qsTr("改用新的模型连接…")
            onClicked: { page.resetLegacy = true; if (!page.profiles.length) page.addRequested() } }
    }
    Note { visible: page.resetLegacy; text: qsTr("保存新连接会替换资料库当前的模型配置。") }
    Note { visible: !!page.issue; text: Studio.translate(page.issue); color: Studio.danger }
}
