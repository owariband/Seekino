import QtQuick
import QtQuick.Effects

RectangularShadow {
    anchors.fill: parent
    z: -1; radius: 18; blur: 17; spread: -2; offset: Qt.vector2d(0,6)
    color: Qt.rgba(0,0,0,Studio.dark ? .28 : .09)
}
