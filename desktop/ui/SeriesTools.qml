import QtQuick
import QtQuick.Layouts
import QtQuick.Controls.Basic
import QtQuick.Dialogs

Sheet {
    id: sheet; objectName: "seriesTools"
    width: Math.min(540, parent.width - 32)
    height: Math.min(570, parent.height - 32)
    property string seriesID: ""
    property string tab: "edit"
    property int generation: 0
    property bool busy: false
    property var meta: ({})
    property var videos: []
    property var types: []
    closePolicy: busy ? Popup.NoAutoClose : Popup.CloseOnEscape
    property string feedback: ""
    property var tools: workspace.libraryTools
    property string originalBody: ""
    property string originalFormat: "markdown"
    function openFor(id, section) {
        sheet.seriesID = id; tab = section; busy = true; feedback = ""; meta = ({}); open()
        tools.load(id, --generation)
    }
    function showData(data) {
        meta = data; busy = false
        display.text = data.name || ""; summary.text = data.description || ""; language.text = data.language || ""
        const bg = data.background || {}
        const known = typeof bg.content === "string" && Object.keys(bg).every(k => ["format","content"].includes(k))
        const legacy = Object.keys(bg).length === 1 && typeof bg.seriesBible === "string"
        originalFormat = known ? bg.format || "markdown" : (legacy || !Object.keys(bg).length ? "markdown" : "json")
        originalBody = known ? bg.content : (legacy ? bg.seriesBible : (Object.keys(bg).length ? JSON.stringify(bg,null,2) : ""))
        format.currentIndex = originalFormat === "json" ? 1 : 0; backgroundText.text = originalBody
        videos = (data.videos || []).map(v=>v.id); types = (data.eventTypes || []).slice()
        selectVideo.currentIndex = (data.videos || []).length ? 0 : -1; setEpisode()
    }
    function setEpisode() { const v = meta.videos?.[selectVideo.currentIndex]; episode.text = v ? String(v.episode) : "" }
    function changeSelection(original, value, checked) { return checked ? [...original.filter(x=>x!==value), value] : original.filter(x=>x!==value) }
    function saveData() {
        const changes = {}
        if (display.text !== meta.name) changes.display_name = display.text.trim() || null
        if (summary.text !== meta.description) changes.description = summary.text.trim() || null
        if (language.text !== meta.language) changes.transcript_language_hint = language.text.trim() || null
        const fmt = format.currentIndex === 1 ? "json" : "markdown"
        if (originalBody !== backgroundText.text || originalFormat !== fmt)
            changes.important_extra_data = backgroundText.text ? {format:fmt, content:backgroundText.text} : null
        busy = true; tools.manage("updateLibrary",{seriesID:seriesID, changes:changes})
    }
    Connections {
        target: sheet.tools
        function onMetadataReady(series, gen, value) { if (sheet.visible && gen === sheet.generation && series === sheet.seriesID) sheet.showData(value) }
        function onCompleted(action, result) {
            if (!sheet.visible || !["changeEpisode","exportLibrary","updateLibrary","backgroundImage"].includes(action)) return
            sheet.busy = false
            if (action === "backgroundImage") { const cursor = backgroundText.cursorPosition
                const ref = format.currentIndex === 1 ? result : "![参考图](" + result + ")"
                backgroundText.insert(cursor, ref); backgroundText.cursorPosition = cursor + ref.length
                backgroundText.forceActiveFocus(); return }
            if (action === "changeEpisode") { sheet.tools.load(sheet.seriesID,--sheet.generation); sheet.feedback = qsTr("集号已保存") }
            else if (action === "exportLibrary") sheet.feedback = result
            else sheet.close()
        }
        function onFailed(action, message) { if (sheet.visible) { sheet.busy=false; sheet.feedback = message } }
    }
    FileDialog {
        id: referenceImage; title: qsTr("选择背景参考图"); nameFilters:[qsTr("参考图 (*.png *.jpg *.jpeg *.webp)")]
        onAccepted: { sheet.busy = true; sheet.tools.manage("backgroundImage",{seriesID:sheet.seriesID,path:sheet.tools.localPath(selectedFile.toString())}) }
    }
    FileDialog {
        id: destination; title: qsTr("导出解析结果"); fileMode: FileDialog.SaveFile
        nameFilters:[qsTr("解析结果 (*.jsonl)")]; defaultSuffix:"jsonl"
        onAccepted: { sheet.busy = true; sheet.tools.manage("exportLibrary",{seriesID:sheet.seriesID, videos:sheet.videos, types:sheet.types,
                     destination:sheet.tools.localPath(selectedFile.toString())}) }
    }
    contentItem: ColumnLayout {
        spacing: 12
        Mark { text: sheet.tab==="export" ? qsTr("导出解析结果") : sheet.tab==="episode" ? qsTr("编辑集号") : qsTr("编辑系列资料"); font.pixelSize: 21; font.weight: Font.DemiBold }
        ScrollView {
            id: scroll; Layout.fillWidth: true; Layout.fillHeight: true; clip: true; contentWidth: availableWidth
            ColumnLayout {
                width: scroll.availableWidth; spacing: 11; enabled: !sheet.busy
                ModelField { id: display; visible: sheet.tab==="edit"; label: qsTr("显示名称"); Layout.fillWidth: true }
                ModelField { id: summary; visible: display.visible; label: qsTr("系列简介"); Layout.fillWidth: true }
                ModelField { id: language; visible: display.visible; label: qsTr("ASR 原文语言"); placeholder: qsTr("例如 ja、zh 或 en"); Layout.fillWidth: true }
                RowLayout { visible: display.visible; Mark { text: qsTr("背景资料"); Layout.fillWidth: true } Tool { text: qsTr("插入参考图…"); onClicked: referenceImage.open() } Picker { id: format; model: ["Markdown","JSON"] } }
                TextArea { id: backgroundText; visible: display.visible; Layout.fillWidth: true; Layout.minimumHeight: 100; color: Studio.ink
                    wrapMode: TextEdit.Wrap; selectByMouse: true; placeholderText:qsTr("人物关系、别名或剧情背景"); placeholderTextColor:Studio.muted
                    background: Rectangle { radius:8; color: Studio.field; border.color:Studio.line } }
                Picker { id: selectVideo; visible: sheet.tab==="episode"; Layout.fillWidth:true; model: sheet.meta.videos || []; textRole:"title"; onActivated: sheet.setEpisode() }
                ModelField { id: episode; visible: selectVideo.visible; Layout.fillWidth: true; label:qsTr("集号"); validator: IntValidator { bottom:1; top:2147483647 } }
                Mark { text:qsTr("视频"); visible: sheet.tab==="export" }
                Repeater {
                    model: sheet.tab==="export" ? sheet.meta.videos || [] : []
                    CheckBox { required property var modelData; Layout.fillWidth:true; palette.windowText:Studio.ink; text:qsTr("第 ")+modelData.episode+qsTr(" 集 · ")+modelData.title
                        checked: sheet.videos.includes(modelData.id); onToggled: sheet.videos=sheet.changeSelection(sheet.videos,modelData.id,checked) }
                }
                Mark { text:qsTr("事件类型"); visible: sheet.tab==="export" }
                Flow {
                    visible: sheet.tab==="export"; Layout.fillWidth:true
                    Repeater { model: sheet.meta.eventTypes || []
                        CheckBox { required property string modelData; palette.windowText: Studio.ink
                            text: ({"DIALOGUE":qsTr("对白"),"ACTION":qsTr("动作"),"SCENE":qsTr("场景"),"EMOTION":qsTr("情绪"),"SCREEN_TEXT":qsTr("画面文字")})[modelData] || modelData
                            checked: sheet.types.includes(modelData); onToggled: sheet.types=sheet.changeSelection(sheet.types,modelData,checked) }
                    }
                }
            }
        }
        Text { visible: !!text; text: sheet.feedback; color:Studio.ink; Layout.fillWidth:true; wrapMode:Text.Wrap }
        RowLayout { Layout.alignment:Qt.AlignRight
            Tool { text:qsTr("关闭"); enabled: !sheet.busy; onClicked: sheet.close() }
            Tool { text: sheet.tab === "export" ? qsTr("导出 JSONL…") : qsTr("保存"); primary:true
                enabled: !sheet.busy && !!sheet.meta.videos && (sheet.tab === "export" ? sheet.videos.length>0 && sheet.types.length>0 : sheet.tab !== "episode" || !!episode.text && selectVideo.currentIndex >= 0)
                onClicked: {
                    if (sheet.tab==="export") destination.open()
                    else if (sheet.tab==="episode") { sheet.busy=true; sheet.tools.manage("changeEpisode",{seriesID:sheet.seriesID,videoID:sheet.meta.videos[selectVideo.currentIndex].id,episode:Number(episode.text)}) }
                    else sheet.saveData()
                } }
        }
    }
}
