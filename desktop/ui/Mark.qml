import QtQuick

Text {
    property bool secondary: false
    color: secondary ? Studio.muted : Studio.ink
    font.pixelSize: Studio.labelSize
    verticalAlignment: Text.AlignVCenter
    elide: Text.ElideRight
    maximumLineCount: 1
}
