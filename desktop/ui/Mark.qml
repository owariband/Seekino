import QtQuick

Text {
    property bool secondary: false
    color: secondary ? Studio.muted : Studio.ink
    font.pixelSize: 13
    verticalAlignment: Text.AlignVCenter
    elide: Text.ElideRight
    maximumLineCount: 1
}
