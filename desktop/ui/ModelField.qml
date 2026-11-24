import QtQuick
import QtQuick.Layouts

ColumnLayout {
    id: field
    property string label: ""
    property string caption: ""
    property alias text: entry.text
    property alias placeholder: entry.placeholderText
    property alias secret: entry.echoMode
    property alias readOnly: entry.readOnly
    property alias validator: entry.validator
    property string inputName: ""
    function fieldFocus() { entry.forceActiveFocus(Qt.OtherFocusReason) }
    signal edited()
    signal focused(var item)
    spacing: 6
    RowLayout {
        Layout.fillWidth: true
        Mark { text: field.label; Layout.fillWidth: true }
        Mark { text: field.caption; visible: !!text; secondary: true; font.pixelSize: 12 }
    }
    Entry {
        id: entry; objectName: field.inputName; Layout.fillWidth: true
        passwordMaskDelay: 0; Accessible.name: field.label
        onTextEdited: field.edited()
        onActiveFocusChanged: if (activeFocus) field.focused(field)
    }
}
