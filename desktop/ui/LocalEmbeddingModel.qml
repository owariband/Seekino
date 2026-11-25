import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts

ColumnLayout {
    id: model
    required property var download
    readonly property string status: download?.state || "missing"
    readonly property bool working: status === "downloading" || status === "verifying"
    readonly property real fraction: Math.max(0, Math.min(1, download?.progress || 0))
    spacing: 6

    RowLayout {
        Layout.fillWidth: true; spacing: 12
        ColumnLayout {
            Layout.fillWidth: true; spacing: 2
            Mark { Layout.fillWidth: true; text: "Qwen3-Embedding-0.6B"; font.weight: Font.Medium }
            Mark {
                Layout.fillWidth: true; objectName: "localEmbeddingStatus"
                secondary: true; font.pixelSize: 12
                text: model.status === "verifying" ? qsTr("正在校验模型…")
                    : model.status === "downloading" ? qsTr("正在下载 · %1%").arg(Math.floor(model.fraction * 100))
                    : model.status === "ready" ? qsTr("已下载 · 1024 维") : qsTr("639 MB · 1024 维")
            }
        }
        Tool {
            objectName: "localEmbeddingDownloadAction"
            visible: model.status !== "ready"
            enabled: !!model.download
            text: model.working ? qsTr("取消") : model.status === "error" ? qsTr("重试") : qsTr("下载模型")
            Accessible.name: text + " · Qwen3-Embedding-0.6B"
            outlined: true; corner: 7
            onClicked: model.working ? model.download.cancel() : model.download.start()
        }
    }
    ProgressBar {
        id: downloadProgress
        objectName: "localEmbeddingProgress"; visible: model.working; Layout.fillWidth: true
        implicitHeight: 4; from: 0; to: 1; value: model.status === "verifying" ? 1 : model.fraction
        Accessible.name: qsTr("模型下载进度")
        background: Rectangle { color: Studio.line; radius: 2 }
        contentItem: Item {
            Rectangle {
                width: parent.width * downloadProgress.visualPosition; height: parent.height
                radius: 2; color: Studio.actionBg
                Behavior on width { NumberAnimation { duration: Studio.quick } }
            }
        }
    }
    Text {
        Layout.fillWidth: true; visible: model.status === "error" && text.length > 0
        text: Studio.translate(model.download?.errorMessage || ""); textFormat: Text.PlainText
        color: Studio.danger; font.pixelSize: 12; wrapMode: Text.Wrap
    }
}
