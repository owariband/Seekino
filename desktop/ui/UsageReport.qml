import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts

ColumnLayout {
    id: report
    objectName: "usageReport"
    property var overview: workspace.usage || ({})
    property bool taskScoped: false
    property bool loading: workspace.usageLoading
    property string lookupError: workspace.usageError
    signal refreshRequested()
    readonly property bool available: overview.status === "available"
    readonly property var totals: available ? overview.totals : null
    readonly property bool partial: !!totals && (!!overview.truncated || totals.totalTokens === null)
    readonly property var cards: totals ? [
        {
            title: partial ? qsTr("已知 Token 小计") : qsTr("累计 Token"),
            value: group(totals.totalTokens ?? totals.knownTokens),
            note: (partial ? qsTr("记录不完整，不能代表总消耗。") : qsTr("仅包含已记录调用。"))
                + qsTr("输入 / 输出：") + group(totals.inputTokens) + " / " + group(totals.outputTokens)
        },
        {
            title: partial || (totals.cost && totals.cost.unpricedCallCount > 0) ? qsTr("已计价 API 小计") : qsTr("API 预估费用"),
            value: costLabel(totals.cost),
            note: qsTr("按配置单价估算，人民币显示，不含 Codex 订阅费用。")
        },
        {
            title: qsTr("已记录调用"),
            value: group(totals.callCount),
            note: qsTr("仅统计已记录调用；历史未记录用量无法还原。")
        }
    ] : []
    spacing: 14
    Connections {
        target: workspace
        function onReadyChanged() { if (report.visible && !report.taskScoped) workspace.loadUsage() }
    }

    function group(value) {
        if (value === null || value === undefined) return "—"
        return String(value).replace(/\B(?=(\d{3})+(?!\d))/g, ",")
    }
    function costLabel(cost) {
        if (!cost || !cost.currency) return qsTr("未计价")
        if (cost.excludedCallCount > 0 && cost.pricedCallCount === 0 && cost.unpricedCallCount === 0)
            return qsTr("不适用（Codex 订阅）")
        if (cost.pricedCallCount === 0 && cost.unpricedCallCount > 0) return qsTr("未计价")
        const subtotal = !!overview.truncated || cost.unpricedCallCount > 0 || cost.estimatedAmount === null
        const amount = subtotal ? cost.knownAmount : cost.estimatedAmount
        return amount === null ? qsTr("未计价") : "¥" + amount + (subtotal ? qsTr("（小计）") : "")
    }
    function operationLabel(value) {
        const labels = {
            candidate_transcript: qsTr("对白识别"), video_timeline: qsTr("时间线解析"), neutral_visual_evidence: qsTr("视觉核验"),
            fallback_video_timeline: qsTr("备用时间线解析"), text_embedding: qsTr("文本向量化"), video_embedding: qsTr("视频向量化"),
            understand: qsTr("理解"), refine: "Refine", index: qsTr("索引发布"), sdk_turn_run: qsTr("原生 Agent 执行")
        }
        return labels[value] || value || qsTr("未知用途")
    }
    function categoryLabel(value) {
        return ({parse: qsTr("解析"), embedding: qsTr("向量化"), chat: "Chat"})[value] || value || qsTr("未分类")
    }
    function taskLabel(value) {
        return ({video_parse: qsTr("解析任务"), chat_retrieval: "Chat"})[value] || value
    }
    function pad(value) {
        return value < 10 ? "0" + value : "" + value
    }
    function dateText(ms) {
        const value = new Date(Number(ms))
        if (isNaN(value.getTime())) return "—"
        return value.getFullYear() + "-" + pad(value.getMonth() + 1) + "-" + pad(value.getDate())
    }
    function momentText(ms) {
        const value = new Date(Number(ms))
        if (isNaN(value.getTime())) return "—"
        return dateText(ms) + " " + pad(value.getHours()) + ":" + pad(value.getMinutes())
    }
    RowLayout {
        Layout.fillWidth: true
        Mark { text: qsTr("模型消耗"); font.pixelSize: 15; font.weight: Font.DemiBold }
        Item { Layout.fillWidth: true }
        Tool {
            objectName: "refreshUsage"
            text: report.loading ? qsTr("正在读取…") : qsTr("刷新用量")
            outlined: true; corner: 7; implicitHeight: 30
            enabled: !report.loading && workspace.ready
            onClicked: report.taskScoped ? report.refreshRequested() : workspace.loadUsage()
        }
    }
    Text {
        Layout.fillWidth: true
        text: report.taskScoped ? qsTr("本次任务 · 最近 30 天已记录调用") : qsTr("本机已记录用量 · 最近 30 天 · 解析、向量化与 Chat")
        color: Studio.muted; font.pixelSize: 12; wrapMode: Text.Wrap
        textFormat: Text.PlainText
    }
    Text {
        Layout.fillWidth: true; visible: !!overview.fromStartTime
        text: qsTr("统计窗口：") + dateText(overview.fromStartTime) + qsTr(" 至 ") + dateText(overview.toStartTime)
            + qsTr(" · 读取于 ") + momentText(overview.fetchedAt)
        color: Studio.muted; font.pixelSize: 12; wrapMode: Text.Wrap
        textFormat: Text.PlainText
    }
    Text {
        Layout.fillWidth: true; visible: !overview.status && !report.lookupError
        text: report.loading ? qsTr("正在读取模型消耗…") : qsTr("打开用量后读取模型消耗。")
        color: Studio.muted; font.pixelSize: 13; wrapMode: Text.Wrap
        textFormat: Text.PlainText
    }
    Text {
        Layout.fillWidth: true; visible: !!report.lookupError
        text: report.lookupError + (overview.status ? qsTr(" 以下保留上次读取的状态与记录。") : "")
        color: Studio.ink; font.pixelSize: 13; wrapMode: Text.Wrap
        textFormat: Text.PlainText
    }
    Text {
        Layout.fillWidth: true; visible: !available && overview.status === "unavailable"
        text: overview.message || qsTr("模型消耗暂时无法读取，请稍后刷新重试。")
        color: Studio.ink; font.pixelSize: 13; wrapMode: Text.Wrap
        textFormat: Text.PlainText
    }
    RowLayout {
        Layout.fillWidth: true; visible: report.cards.length > 0; spacing: 8
        Repeater {
            model: report.cards
            Rectangle {
                required property var modelData
                Layout.fillWidth: true; Layout.fillHeight: true
                implicitHeight: card.implicitHeight + 24
                radius: 10; color: Studio.surfaceMuted
                ColumnLayout {
                    id: card
                    x: 12; y: 12; width: parent.width - 24; spacing: 3
                    Mark { text: modelData.title; secondary: true; font.pixelSize: 12 }
                    Mark { Layout.fillWidth: true; text: modelData.value; font.pixelSize: 20; font.weight: Font.DemiBold }
                    Text {
                        Layout.fillWidth: true; text: modelData.note
                        color: Studio.muted; font.pixelSize: 11; wrapMode: Text.Wrap
                        textFormat: Text.PlainText
                    }
                }
            }
        }
    }
    Text {
        Layout.fillWidth: true; visible: available && !!totals && (totals.missingUsageCount > 0 || overview.truncated)
        text: (totals && totals.missingUsageCount > 0 ? totals.missingUsageCount + qsTr(" 条调用记录缺少完整 Token 用量，显示已知小计。") : "")
            + (overview.truncated ? qsTr("已达到本次读取上限，当前结果被截断，不能代表统计窗口内的全部消耗。") : "")
        color: Studio.ink; font.pixelSize: 12; wrapMode: Text.Wrap
        textFormat: Text.PlainText
    }
    Text {
        Layout.fillWidth: true
        visible: available && !!totals && totals.callCount === 0 && !overview.truncated
        text: qsTr("当前窗口暂无已记录调用。已记录的模型调用会显示在这里；未记录的历史用量不会补记为零。")
        color: Studio.muted; font.pixelSize: 13; wrapMode: Text.Wrap
        textFormat: Text.PlainText
    }
    ColumnLayout {
        Layout.fillWidth: true; spacing: 5
        visible: available && overview.models.length > 0
        Mark { text: qsTr("按模型"); font.weight: Font.DemiBold }
        Repeater {
            model: report.available ? report.overview.models : []
            ColumnLayout {
                required property var modelData
                Layout.fillWidth: true; spacing: 1
                RowLayout {
                    Layout.fillWidth: true
                    Mark { Layout.fillWidth: true; text: modelData.model || qsTr("未知模型"); font.pixelSize: 12; font.weight: Font.DemiBold }
                    Mark { text: report.group(modelData.callCount) + qsTr(" 次 · ") + report.costLabel(modelData.cost); secondary: true; font.pixelSize: 12 }
                }
                Text {
                    Layout.fillWidth: true
                    text: (modelData.provider || qsTr("未知服务商")) + " · " + report.categoryLabel(modelData.category)
                        + " · " + report.operationLabel(modelData.operation)
                        + qsTr(" · 总 Token ") + report.group(modelData.totalTokens)
                        + (modelData.totalTokens === null ? qsTr("（已知 ") + report.group(modelData.knownTokens) + "）" : "")
                    color: Studio.muted; font.pixelSize: 11; wrapMode: Text.Wrap
                    textFormat: Text.PlainText
                }
                Text {
                    visible: report.taskScoped; Layout.fillWidth: true; wrapMode: Text.Wrap; textFormat: Text.PlainText
                    color: Studio.muted; font.pixelSize: 12
                    text: qsTr("来源 %1  ·  输入 %2  ·  输出 %3  ·  缓存读取 %4  ·  缓存写入 %5  ·  推理 %6")
                        .arg(modelData.src || "—").arg(report.group(modelData.inputTokens)).arg(report.group(modelData.outputTokens))
                        .arg(report.group(modelData.cachedInputTokens)).arg(report.group(modelData.cacheWriteInputTokens))
                        .arg(report.group(modelData.reasoningTokens))
                }
            }
        }
    }
    ColumnLayout {
        Layout.fillWidth: true; spacing: 5
        visible: !report.taskScoped && available && overview.runs.length > 0
        Mark { text: qsTr("任务与 Chat"); font.weight: Font.DemiBold }
        Repeater {
            model: report.available ? report.overview.runs : []
            ColumnLayout {
                required property var modelData
                Layout.fillWidth: true; spacing: 1
                RowLayout {
                    Layout.fillWidth: true
                    Mark { Layout.fillWidth: true; text: modelData.runId; font.pixelSize: 12; elide: Text.ElideMiddle }
                    Mark { text: report.taskLabel(modelData.taskType) + " · " + report.group(modelData.callCount) + qsTr(" 次"); secondary: true; font.pixelSize: 12 }
                }
                Text {
                    Layout.fillWidth: true
                    text: (modelData.seriesId || qsTr("无视频系列")) + (modelData.videoId ? " · " + modelData.videoId : "")
                        + qsTr(" · 总 Token ") + report.group(modelData.totalTokens)
                        + (modelData.totalTokens === null ? qsTr("（已知 ") + report.group(modelData.knownTokens) + "）" : "")
                        + " · " + report.costLabel(modelData.cost)
                    color: Studio.muted; font.pixelSize: 11; wrapMode: Text.Wrap
                    textFormat: Text.PlainText
                }
            }
        }
    }
    Text {
        Layout.fillWidth: true; visible: available
        text: qsTr("仅统计已记录调用；观看问答等未记录调用不计入，未知细项显示为 —，历史用量无法还原。")
        color: Studio.muted; font.pixelSize: 11; wrapMode: Text.Wrap
        textFormat: Text.PlainText
    }
}
