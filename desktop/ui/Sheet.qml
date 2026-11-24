import QtQuick
import QtQuick.Controls.Basic

Popup {
    id: sheet
    parent: Overlay.overlay
    anchors.centerIn: parent
    width: Math.min(420,parent.width-40)
    padding: 26; modal: true; dim: true; focus: true
    closePolicy: Popup.CloseOnEscape
    onAboutToShow: Studio.openSheets = Studio.openSheets.concat([sheet])
    onClosed: Studio.openSheets = Studio.openSheets.filter(openSheet => openSheet !== sheet)
    background: Rectangle { radius: 16; color: Studio.sidebar; border.color: Studio.line; CardShadow { radius: 16 } }
    Overlay.modal: Rectangle { color: "#47000000" }
    enter: Transition { ParallelAnimation { NumberAnimation { property: "opacity"; from: 0; to: 1; duration: Studio.hover } NumberAnimation { property: "scale"; from: Studio.reduce ? 1 : .98; to: 1; duration: Studio.hover; easing.type: Easing.OutCubic } } }
    exit: Transition { NumberAnimation { property: "opacity"; from: 1; to: 0; duration: Studio.quick } }
}
