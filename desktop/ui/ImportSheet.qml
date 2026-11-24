import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts

Sheet {
    id: sheet; objectName: "importQueue"
    width: Math.min(610, parent.width - 28)
    height: Math.min(parent.height - 28, 560, 216 + Math.max(1, rows.count) * 40)
    property var tools: workspace.libraryTools
    property string target: ""
    property int revision: 0
    property var taken: ({})
    property var resumable: ({})
    property var options: []
    property bool loading: false
    property bool started: false
    property bool available: false
    property string issue: ""
    property int activeRow: -1
    signal finished(string series)
    closePolicy: activeRow !== -1 ? Popup.NoAutoClose : Popup.CloseOnEscape
    function openFiles(urls, series) {
        if (workspace.busyImport) return
        const items = tools.files(urls)
        if (!items.length) return
        rows.clear()
        for (const item of items) rows.append({path:item.path, name:item.name, hint:item.hint, episode:0, status:"queued", fraction:0.0, reason:""})
        options = workspace.series.map(row => ({label:row.title, key:row.id}))
        let home = options.find(row => row.label === "我的素材")
        if (!home) options = [{label:qsTr("我的素材"), key:""}].concat(options)
        target = series || (home ? home.key : "")
        picker.currentIndex = options.findIndex(row => row.key === target)
        started = false; activeRow = -1
        open(); selectTarget(target)
    }
    function selectTarget(value) {
        target = value; issue = ""; loading = !!value; ++revision
        if (value) tools.load(value, revision)
        else populate({})
    }
    function populate(meta) {
        const used = {}, partial = meta.occupied || {}
        for (const video of (meta.videos || [])) used[video.episode] = true
        taken = used; resumable = partial
        let next = Math.max(0,...Object.keys(used).map(Number),...Object.keys(partial).map(Number)) + 1
        for (let i = 0; i < rows.count; ++i) {
            const row = rows.get(i)
            const match = Object.keys(partial).find(n => partial[n] === row.name)
            const episode = match ? Number(match) : row.hint || next
            rows.setProperty(i, "episode", episode); next = Math.max(next, episode + 1)
        }
        loading = false; validate()
    }
    function validate() {
        let used = {}, ok = rows.count > 0
        for (let i = 0; i < rows.count; ++i) {
            const row = rows.get(i), n = row.episode
            if (n < 1 || n > 2147483647 || used[n] || taken[n] || (resumable[n] && resumable[n] !== row.name)) ok = false
            used[n] = true
        }
        available = ok
        issue = ok || !rows.count ? "" : qsTr("集号重复或已被占用，请修改后导入。")
    }
    function nextFile() {
        if (activeRow !== -1) return
        for (let i = 0; i < rows.count; ++i) {
            const row = rows.get(i)
            if (row.status !== "queued") continue
            activeRow = i; rows.setProperty(i, "status", "running")
            tools.transfer(i, row.path, target, row.episode)
            return
        }
    }
    Connections {
        target: sheet.tools
        function onMetadataReady(series, generation, meta) {
            if (sheet.visible && sheet.revision === generation && sheet.target === series) sheet.populate(meta)
        }
        function onFailed(action, message) {
            if (sheet.visible && action === "load") { sheet.loading = false; sheet.available = false; sheet.issue = message }
            if (action === "prepare") { sheet.issue = message; sheet.available = false; sheet.open() }
        }
        function onProgress(index, value) { if (sheet.activeRow === index) rows.setProperty(index, "fraction", value) }
        function onCompleted(action, result) { if (sheet.started && action === "destination") sheet.target = result }
        function onFileFinished(index, status, message) {
            if (sheet.activeRow !== index) return
            rows.setProperty(index,"status",status); rows.setProperty(index,"reason",message)
            sheet.activeRow = -1; Qt.callLater(sheet.nextFile)
        }
    }
    ListModel { id: rows }
    contentItem: ColumnLayout {
        spacing: 12
        Mark { text: qsTr("导入视频"); font.pixelSize: 21; font.weight: Font.DemiBold }
        RowLayout {
            Mark { text: qsTr("目标系列"); secondary: true }
            Picker { id: picker; objectName: "importSeriesChoice"; Layout.fillWidth: true
                enabled: !sheet.started; model: sheet.options; textRole: "label"
                onActivated: i => sheet.selectTarget(sheet.options[i].key) }
        }
        ListView {
            id: list; Layout.fillHeight: true; Layout.fillWidth: true; model: rows; spacing: 6; clip: true
            ScrollBar.vertical: ScrollBar {}
            delegate: ColumnLayout {
                required property int index
                required property string name
                required property int episode
                required property string status
                required property string reason
                required property real fraction
                width: list.width - 12
                RowLayout {
                    Layout.fillWidth: true
                    Mark { Layout.fillWidth: true; text: name; ToolTip.text: name }
                    Mark { text: qsTr("集号"); secondary: true; visible: !sheet.started }
                    Entry { objectName: "episodeInput_" + index; visible: !sheet.started; Layout.preferredWidth: 64
                        text: String(episode); validator: IntValidator { bottom: 1; top: 2147483647 }
                        onTextEdited: { rows.setProperty(index,"episode", Number(text) || 0); sheet.validate() } }
                    Mark { visible: sheet.started; secondary: true
                        text: status === "running" ? Math.round(fraction*100)+"%" : ({"queued":qsTr("待导入"),"paused":qsTr("已暂停"),"done":qsTr("已导入"),"cancelled":qsTr("已取消"),"failed":qsTr("未完成")})[status] || "" }
                    Tool { visible: !sheet.started; glyph: "close"; hint: qsTr("移除文件"); onClicked: { rows.remove(index); sheet.validate() } }
                    Tool { visible: sheet.started && ["paused","queued","running"].includes(status)
                        text: status === "paused" ? qsTr("继续") : qsTr("暂停"); onClicked: {
                            const active = index === sheet.activeRow
                            const s = status === "paused" ? (active ? "running" : "queued") : "paused"
                            rows.setProperty(index,"status",s)
                            if (active) sheet.tools.controlFile(index,s)
                            else sheet.nextFile()
                        } }
                    Tool { visible: sheet.started && ["paused","queued","running"].includes(status); text: qsTr("取消")
                        onClicked: { if (index === sheet.activeRow) sheet.tools.controlFile(index,"stop"); else rows.setProperty(index,"status","cancelled") } }
                    Tool { visible: status === "failed"; text: qsTr("重试"); onClicked: { rows.setProperty(index,"status","queued"); rows.setProperty(index,"reason",""); sheet.nextFile() } }
                }
                Text { Layout.fillWidth: true; visible: !!reason; text: Studio.translate(reason); color: Studio.danger; wrapMode: Text.Wrap }
            }
        }
        Text { Layout.fillWidth: true; visible: !!text; wrapMode: Text.Wrap; color: sheet.issue ? Studio.danger : Studio.muted
            text: Studio.translate(sheet.issue) || (!workspace.importModelReady ? qsTr("视频入库后即可播放，配置模型后开始解析。") : "") }
        RowLayout {
            Layout.alignment: Qt.AlignRight
            Tool { text: sheet.started ? qsTr("完成") : qsTr("取消"); enabled: sheet.activeRow === -1
                onClicked: { sheet.close(); if (sheet.started && sheet.target) sheet.finished(sheet.target) } }
            Tool { objectName: "startImportBatch"; visible: !sheet.started; primary: true; text: qsTr("开始导入")
                enabled: sheet.available && !sheet.loading; onClicked: { sheet.started = true; sheet.nextFile() } }
        }
    }
}
