import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts

Sheet {
    id: prefs
    property real toolbarHeight: 0
    property string purpose: "manual"
    property bool pendingAction: false
    property bool justSaved: false
    property bool replacing: false
    property bool appearance: false
    property bool usage: false
    property string protocol: ""
    property bool keyless: false
    readonly property bool localEndpoint: protocol === "openai_chat" && isLoopbackUrl(address.text)
    readonly property bool anonymous: localEndpoint && keyless
    property string baseline: ""
    property string revision: ""
    property var secretEdits: ({})
    readonly property var config: workspace.modelConfig || ({})
    readonly property var stored: config.form || ({})
    readonly property bool retain: config.mode === "custom" || (config.mode === "legacy" && !replacing)
    readonly property bool compact: !!parent && parent.height < 520
    readonly property bool busy: workspace.savingKeys || workspace.busyImport || workspace.searching || !workspace.ready
    readonly property bool sameTarget: config.mode === "managed" && protocol === stored.protocol
        && !anonymous && stored.auth !== "none" && connectionName.text.trim() === stored.name
        && address.text.trim().replace(/\/+$/, "") === String(stored.baseUrl || "").trim().replace(/\/+$/, "")
    readonly property var savedConnection: (config.connections || []).find(c => c.label === stored.name && c.protocol === stored.protocol && c.baseUrl === stored.baseUrl)
    readonly property bool hasSavedKey: sameTarget && !!savedConnection && !!config.credentials?.[savedConnection.credentialName]
    readonly property bool needsEncoder: protocol === "ark" && embedding.text.trim().startsWith("ep-")
    readonly property bool complete: !!(protocol && connectionName.text.trim() && address.text.trim() && (anonymous || key.text.trim() || hasSavedKey)
        && vision.text.trim() && transcription.text.trim() && embedding.text.trim()
        && Number(dimensions.text) >= 1 && Number(dimensions.text) <= 2048 && (!needsEncoder || encoder.text.trim()))
    readonly property bool changed: retain ? Object.values(secretEdits).some(value => !!value.trim())
        : !!protocol && (signature() !== baseline || !!key.text.trim())
    readonly property var themes: [{key:"system", label:qsTr("跟随系统")}, {key:"light", label:qsTr("浅色")}, {key:"dark", label:qsTr("深色")}]
    readonly property var providers: [{key:"ark", label:"Ark"}, {key:"openai_chat", label:qsTr("OpenAI 兼容")}]
    readonly property var roles: ({vision:qsTr("视频理解"), transcription:qsTr("语音转录"), embedding:qsTr("向量嵌入"), rerank:qsTr("重排"), ocr:qsTr("文字识别"), summarize:qsTr("摘要"), agent:qsTr("通用模型"), supervisor:qsTr("复核"), diarization:qsTr("说话人识别"), vision_fallback:qsTr("备用视频理解")})
    width: Math.min(588, parent ? parent.width - 24 : 588)
    anchors.centerIn: null
    x: parent ? Math.round((parent.width - width)/2) : 0
    y: parent ? Math.round(toolbarHeight + (parent.height - toolbarHeight - height)/2) : 0
    height: Math.min(implicitHeight, parent ? parent.height - toolbarHeight - 24 : implicitHeight)
    Behavior on height { NumberAnimation { duration: prefs.opened ? Studio.panel : 0; easing.type: Easing.OutCubic } }
    padding: compact ? 16 : 24
    closePolicy: workspace.savingKeys ? Popup.NoAutoClose : Popup.CloseOnEscape

    function isLoopbackUrl(value) {
        const match = /^https?:\/\/(localhost|\[::1\]|127(?:\.[0-9]{1,3}){3})(?::[0-9]+)?(?:\/|$)/i.exec(value.trim())
        if (!match) return false
        return !match[1].startsWith("127.") || match[1].split(".").every(n => String(Number(n)) === n && Number(n) <= 255)
    }
    function draft() {
        return {name: connectionName.text.trim(), protocol: protocol, baseUrl: address.text.trim(), auth: anonymous ? "none" : "key",
            vision: vision.text.trim(), transcription: transcription.text.trim(), embedding: embedding.text.trim(),
            dimensions: Number(dimensions.text), rerank: rerank.text.trim(), encoderModel: needsEncoder ? encoder.text.trim() : ""}
    }
    function signature() { return JSON.stringify(draft()) }
    function setForm(form) {
        protocol = form.protocol || ""
        keyless = form.auth === "none"
        connectionName.text = form.name || ""
        address.text = form.baseUrl || (protocol === "ark" ? "https://ark.cn-beijing.volces.com/api/v3" : "")
        vision.text = form.vision || ""; transcription.text = form.transcription || ""
        embedding.text = form.embedding || ""; dimensions.text = form.dimensions ? String(form.dimensions) : ""
        rerank.text = form.rerank || ""; encoder.text = form.encoderModel || ""
        key.text = ""
    }
    function load() {
        revision = config.revision || ""
        setForm(config.mode === "managed" ? stored : ({}))
        replacing = false; secretEdits = ({})
        for (let i=0; i<retainedKeys.count; ++i) { const field = retainedKeys.itemAt(i); if (field) field.text = "" }
        baseline = signature()
    }
    function selectProvider(next) {
        if (protocol === next) return
        setForm(config.mode === "managed" && stored.protocol === next ? stored : {protocol: next, name: next === "ark" ? "Ark" : qsTr("OpenAI 兼容")})
        justSaved = false
    }
    function openSettings(context, pending = false) {
        if (visible && !pending) return
        purpose = context || "manual"; pendingAction = pending; appearance = false; usage = false; justSaved = false
        load(); open()
    }
    function reveal(field) {
        if (!field || scroll.contentHeight <= scroll.height) return
        const top = field.mapToItem(scroll.contentItem, 0, 0).y
        if (top < scroll.contentY) scroll.contentY = top
        else if (top + field.height > scroll.contentY + scroll.height)
            scroll.contentY = Math.min(scroll.contentHeight-scroll.height, top+field.height-scroll.height+4)
    }
    function save() {
        if (!apply.enabled) return
        const form = retain ? {retainConfig:true} : draft()
        form.revision = revision
        workspace.saveModelConfig(form, retain ? secretEdits : !anonymous && key.text.trim() ? {connection:key.text.trim()} : ({}))
    }
    onOpened: { scroll.contentY = 0; modelTab.forceActiveFocus(Qt.OtherFocusReason) }
    onClosed: {
        key.text = ""; secretEdits = ({})
        for (let i=0; i<retainedKeys.count; ++i) { const field = retainedKeys.itemAt(i); if (field) field.text = "" }
        if (pendingAction) workspace.cancelDeferredImport()
        pendingAction = false
    }
    Connections {
        target: workspace
        function onModelConfigChanged() { if (prefs.visible && !prefs.revision && !prefs.changed) prefs.load() }
        function onCredentialsSaved() { prefs.load(); prefs.justSaved = true }
        function onDeferredActionResumed() { if (prefs.pendingAction) { prefs.pendingAction=false; prefs.close() } }
    }
    component Note: Text {
        Layout.fillWidth: true; color: Studio.muted; font.pixelSize: 12; wrapMode: Text.Wrap
        textFormat: Text.PlainText; lineHeight: 1.24
    }
    component Heading: Mark { font.pixelSize: 15; font.weight: Font.DemiBold }
    component Field: ModelField {
        Layout.fillWidth: true
        Layout.preferredWidth: 1
        onFocused: item => prefs.reveal(item)
    }
    component Choice: Tool {
        outlined: true; corner: 7; implicitHeight: 32
        contentColor: chosen ? Studio.actionBg : Studio.muted
        Accessible.role: Accessible.RadioButton; Accessible.checked: chosen
    }
    contentItem: ColumnLayout {
        spacing: prefs.compact ? 8 : 12
        RowLayout {
            Layout.fillWidth: true
            Mark { text: qsTr("设置"); font.pixelSize: Studio.pageTitleSize; font.weight: Font.DemiBold }
            Item { Layout.fillWidth: true }
            RowLayout {
                spacing: 5
                Choice { id: modelTab; objectName: "modelTab"; text: qsTr("模型连接"); chosen: !prefs.appearance && !prefs.usage; onClicked: { prefs.appearance=false; prefs.usage=false; scroll.contentY=0 } }
                Choice { objectName: "usageTab"; text: qsTr("用量"); chosen: prefs.usage; onClicked: { prefs.appearance=false; prefs.usage=true; scroll.contentY=0 } }
                Choice { objectName: "appearanceTab"; text: qsTr("外观"); chosen: prefs.appearance; onClicked: { prefs.appearance=true; prefs.usage=false; scroll.contentY=0 } }
            }
            Item { Layout.fillWidth: true }
            Tool { glyph: "close"; hint: qsTr("关闭设置"); enabled: !workspace.savingKeys; onClicked: prefs.close() }
        }
        Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Studio.line }
        Flickable {
            id: scroll; objectName: "settingsScroll"; Layout.fillWidth: true; Layout.fillHeight: true
            implicitHeight: fields.implicitHeight; contentWidth: width; contentHeight: fields.implicitHeight
            clip: true; boundsBehavior: Flickable.StopAtBounds
            ScrollBar.vertical: ScrollBar {
                policy: scroll.contentHeight > scroll.height ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff
            }
            ColumnLayout {
                id: fields; width: scroll.width - (scroll.contentHeight > scroll.height ? 12 : 0); spacing: prefs.compact ? 16 : 24
                ColumnLayout {
                    visible: prefs.appearance; Layout.fillWidth: true; spacing: 8
                    Heading { text: qsTr("外观") }
                    RowLayout {
                        Repeater {
                            id: themeButtons; model: prefs.themes
                            Choice {
                                required property int index; required property var modelData
                                objectName: "theme_" + modelData.key; text: modelData.label; chosen: Studio.preferences.appearance === modelData.key
                                onClicked: Studio.preferences.appearance = modelData.key
                                Keys.onLeftPressed: themeButtons.itemAt((index+2)%3).click()
                                Keys.onRightPressed: themeButtons.itemAt((index+1)%3).click()
                            }
                        }
                    }
                    Heading { text: qsTr("观看与 Kino"); Layout.topMargin: 10 }
                    CheckBox { text: qsTr("发问时暂停，返回播放时继续"); palette.windowText: Studio.ink
                        checked: Studio.preferences.pauseOnSend; onToggled: Studio.preferences.pauseOnSend = checked }
                    RowLayout {
                        Mark { text: qsTr("Kino 浮窗闲置后淡化") }
                        SpinBox { from: 1; to: 60; value: Studio.preferences.kinoFadeSeconds
                            onValueModified: Studio.preferences.kinoFadeSeconds = value }
                        Mark { text: qsTr("秒"); secondary: true }
                    }
                    Item { Layout.preferredHeight: 8 }
                    Heading { text: qsTr("界面语言") }
                    RowLayout {
                        spacing: 8
                        Choice {
                            objectName: "language_zh"; text: qsTr("简体中文")
                            chosen: desktopLocale.language === "zh"; onClicked: desktopLocale.setLanguage("zh")
                        }
                        Choice {
                            objectName: "language_en"; text: "English"
                            chosen: desktopLocale.language === "en"; onClicked: desktopLocale.setLanguage("en")
                        }
                    }
                    Note { text: qsTr("语言更改立即生效，并保存在此设备。") }
                }
                ColumnLayout {
                    visible: !prefs.appearance && !prefs.usage; Layout.fillWidth: true; spacing: 8
                    Heading { text: prefs.retain ? qsTr("已配置的模型服务") : qsTr("选择模型服务") }
                    Note {
                        objectName: "settingsPurpose"
                        visible: prefs.purpose !== "manual" || prefs.retain || !prefs.protocol
                        text: prefs.purpose === "import" ? qsTr("导入并解析视频需要视频理解、语音转录和向量模型。")
                            : prefs.purpose === "search" ? qsTr("描述检索使用资料库当前的向量模型。已建索引的关键词检索可直接使用。")
                            : prefs.purpose === "retry" ? qsTr("重试任务将使用这里保存的模型和凭据。")
                            : prefs.retain ? qsTr("当前连接来自资料库的模型配置，可在这里更新对应的 API Key。")
                            : qsTr("用自己的模型服务解析视频、建立索引和检索画面。")
                    }
                    RowLayout {
                        visible: !prefs.retain; spacing: 8
                        Repeater {
                            model: prefs.providers
                            Choice {
                                required property var modelData
                                objectName: "provider_" + modelData.key; text: modelData.label; chosen: prefs.protocol === modelData.key
                                enabled: !prefs.busy; onClicked: prefs.selectProvider(modelData.key)
                            }
                        }
                    }
                }
                ColumnLayout {
                    visible: !prefs.appearance && !prefs.usage && !prefs.retain && !!prefs.protocol
                    Layout.fillWidth: true; spacing: 8; enabled: !prefs.busy
                    RowLayout {
                        Layout.fillWidth: true; spacing: 12
                        Field {
                            id: address; inputName: "serviceUrl"; Layout.preferredWidth: 2
                            label: qsTr("服务地址"); caption: prefs.protocol === "ark" ? qsTr("北京") : ""
                            placeholder: "https://…/v1"; readOnly: prefs.protocol === "ark"
                            onEdited: { key.text=""; prefs.keyless=false; prefs.justSaved=false }
                        }
                        Field {
                            id: connectionName; inputName: "connectionName"
                            label: qsTr("连接名称"); placeholder: qsTr("为连接命名")
                        }
                    }
                    RowLayout {
                        visible: prefs.localEndpoint; Layout.fillWidth: true; Layout.topMargin: prefs.compact ? 8 : 12; spacing: 8
                        Mark { text: qsTr("鉴权") }
                        Choice {
                            objectName: "authKey"; text: qsTr("使用密钥"); chosen: !prefs.keyless
                            onClicked: { prefs.keyless=false; prefs.justSaved=false }
                        }
                        Choice {
                            objectName: "authNone"; text: qsTr("本机免密"); chosen: prefs.keyless
                            onClicked: { prefs.keyless=true; key.text=""; prefs.justSaved=false }
                        }
                    }
                    Field {
                        id: key; inputName: "connectionKey"; visible: !prefs.anonymous
                        Layout.topMargin: prefs.localEndpoint ? 0 : prefs.compact ? 8 : 12
                        label: "API Key"; caption: prefs.hasSavedKey ? qsTr("此地址已保存密钥") : ""
                        placeholder: prefs.hasSavedKey ? qsTr("留空保留当前密钥") : qsTr("输入此服务的 API Key")
                        secret: TextInput.Password
                    }
                    Heading { Layout.topMargin: prefs.compact ? 8 : 16; text: qsTr("模型 ID") }
                    ModelDiscovery {
                        Layout.fillWidth: true
                        protocol: prefs.protocol; baseUrl: address.text; apiKey: key.text
                        revision: prefs.revision; purpose: "model"
                        targets: [{key:"vision", label:qsTr("视频理解"), hint:qsTr("填入视频理解")},
                                  {key:"transcription", label:qsTr("语音转录"), hint:qsTr("填入语音转录")},
                                  {key:"embedding", label:qsTr("向量嵌入"), hint:qsTr("填入向量嵌入")},
                                  {key:"rerank", label:qsTr("重排"), hint:qsTr("填入检索重排")}]
                        onPicked: (model, target) => {
                            if (target === "vision") vision.text = model
                            else if (target === "transcription") transcription.text = model
                            else if (target === "embedding") embedding.text = model
                            else rerank.text = model
                            prefs.justSaved = false
                        }
                    }
                    GridLayout {
                        Layout.fillWidth: true; columns: 2; columnSpacing: 12; rowSpacing: 12
                        Field {
                            id: vision; inputName: "visionModel"
                            label: qsTr("视频理解"); placeholder: qsTr("支持视频输入的模型 ID")
                        }
                        Field {
                            id: transcription; inputName: "transcriptionModel"
                            label: qsTr("语音转录"); placeholder: qsTr("支持音频输入的模型 ID")
                        }
                        Field {
                            id: embedding; inputName: "embeddingModel"
                            label: qsTr("向量嵌入"); placeholder: qsTr("嵌入模型 ID")
                        }
                        Field {
                            id: dimensions; inputName: "embeddingDimensions"
                            label: qsTr("向量维度"); placeholder: "1–2048"
                            validator: IntValidator { bottom: 1; top: 2048 }
                        }
                        Field {
                            id: encoder; inputName: "encoderModel"; visible: prefs.needsEncoder
                            label: qsTr("底层嵌入模型"); placeholder: qsTr("接入点对应的实际模型 ID")
                        }
                        Field {
                            id: rerank; inputName: "rerankModel"
                            label: qsTr("重排模型"); placeholder: qsTr("可选，默认使用视频理解模型")
                        }
                    }
                }
                ColumnLayout {
                    visible: !prefs.appearance && !prefs.usage && prefs.retain; Layout.fillWidth: true; spacing: 16
                    Repeater {
                        model: prefs.retain ? prefs.config.connections || [] : []
                        ColumnLayout {
                            required property var modelData
                            Layout.fillWidth: true; spacing: 4
                            Mark { Layout.fillWidth: true; text: modelData.label || modelData.name; font.weight: Font.DemiBold }
                            Note { text: modelData.baseUrl || "" }
                            Note { visible: !!text; text: (modelData.roles || []).map(r => (prefs.roles[r.role] || r.role) + "   " + r.model).join("\n") }
                        }
                    }
                    Repeater {
                        id: retainedKeys; model: prefs.retain ? prefs.config.credentialNames || [] : []
                        ModelField {
                            required property string modelData
                            inputName: "key_" + modelData; Layout.fillWidth: true; enabled: !prefs.busy
                            label: ((prefs.config.connections || []).filter(c => c.credentialName === modelData).map(c => c.label || c.name).join(" / ") || modelData) + " API Key"
                            caption: prefs.config.credentials?.[modelData] ? qsTr("此地址已保存密钥") : qsTr("待填写")
                            placeholder: prefs.config.credentials?.[modelData] ? qsTr("留空保留当前密钥") : qsTr("输入此服务的 API Key")
                            secret: TextInput.Password; onFocused: item => prefs.reveal(item)
                            onEdited: { const values = Object.assign({},prefs.secretEdits); values[modelData]=text; prefs.secretEdits=values; prefs.justSaved=false }
                        }
                    }
                    Tool { visible: prefs.config.mode === "legacy"; text: qsTr("改用新的模型连接…"); outlined: true; corner: 6; enabled: !prefs.busy; onClicked: { prefs.replacing=true; prefs.setForm({}); prefs.justSaved=false } }
                }
                Note { visible: prefs.replacing && !prefs.appearance && !prefs.usage; text: qsTr("保存新连接会替换资料库当前的模型配置。") }
                UsageReport {
                    visible: prefs.usage; Layout.fillWidth: true
                    onVisibleChanged: if (visible) workspace.loadUsage()
                }
            }
        }
        Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Studio.line }
        RowLayout {
            Layout.fillWidth: true; spacing: 9
            Note {
                objectName: "settingsStatus"
                text: prefs.appearance || prefs.usage ? "" : workspace.savingKeys ? qsTr("正在保存…") : prefs.justSaved && !prefs.changed ? qsTr("已保存。服务连通性尚未测试。")
                    : prefs.busy ? qsTr("当前操作结束后可保存") : prefs.changed && !prefs.retain && !prefs.complete ? (prefs.anonymous ? qsTr("请补齐必填模型信息") : qsTr("请补齐密钥和必填模型信息")) : prefs.anonymous ? qsTr("此本机连接使用免密访问。") : qsTr("密钥保存在本机。")
            }
            Tool { id: apply; objectName: "saveCredentials"; visible: !prefs.appearance && !prefs.usage; text: qsTr("保存配置"); primary: enabled; outlined: !enabled; corner: 7; implicitHeight: 34; enabled: !prefs.busy && prefs.changed && (prefs.retain || prefs.complete); onClicked: prefs.save() }
            Tool { id: done; objectName: "settingsDone"; text: qsTr("完成"); outlined: true; corner: 7; enabled: !workspace.savingKeys; onClicked: prefs.close() }
        }
    }
}
