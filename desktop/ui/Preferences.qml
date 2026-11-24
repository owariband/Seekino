import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts

Sheet {
    id: prefs
    property real toolbarHeight: 0
    property string section: "services"
    property string purpose: "manual"
    property bool pendingAction: false
    property bool returnToRoles: false
    property string feedback: ""
    readonly property bool narrow: width < 650
    readonly property var categories: [
        {id:"services", label:qsTr("模型服务"), glyph:"library"}, {id:"roles", label:qsTr("模型用途"), glyph:"sliders"},
        {id:"watch", label:qsTr("观看与 Kino"), glyph:"chat"}, {id:"appearance", label:qsTr("外观"), glyph:"sidebar"},
        {id:"usage", label:qsTr("用量"), glyph:"activity"}]
    readonly property var themes: [{key:"system",label:qsTr("跟随系统")},{key:"light",label:qsTr("浅色")},{key:"dark",label:qsTr("深色")}]
    width: Math.min(744, (parent?.width || 768) - 24)
    height: Math.min(594, (parent?.height || 640) - toolbarHeight - 20)
    anchors.centerIn: null
    x: parent ? Math.round((parent.width - width) / 2) : 0
    y: parent ? Math.round(toolbarHeight + (parent.height - toolbarHeight - height) / 2) : 0
    padding: narrow ? 14 : 20
    closePolicy: workspace.savingKeys ? Popup.NoAutoClose : Popup.CloseOnEscape

    function navigate(next) {
        section = next; feedback = ""; scroll.contentY = 0
        compactNavigation.currentIndex = categories.findIndex(c => c.id === next)
        if (next === "usage") workspace.loadUsage()
    }
    function openSettings(context = "manual", pending = false) {
        purpose = context; pendingAction = pending; returnToRoles = false
        services.reset()
        roles.openRole(context === "watch" ? "watch" : "model", "")
        navigate(context === "manual" ? "services" : context === "watchPreferences" ? "watch" : "roles")
        open()
    }
    function editForRole(profile) {
        returnToRoles = true; navigate("services"); services.edit(profile)
    }
    function reveal(field) {
        if (!field || scroll.contentHeight <= scroll.height) return
        const top = field.mapToItem(fields, 0, 0).y
        if (top < scroll.contentY) scroll.contentY = Math.max(0, top)
        else if (top + field.height > scroll.contentY + scroll.height)
            scroll.contentY = Math.min(scroll.contentHeight - scroll.height, top + field.height - scroll.height + 4)
    }
    onOpened: done.forceActiveFocus(Qt.OtherFocusReason)
    onClosed: {
        services.reset()
        roles.openRole("model", "")
        if (pendingAction) workspace.cancelDeferredImport()
        pendingAction = false
    }
    Connections {
        target: workspace
        function onDeferredActionResumed() { if (prefs.pendingAction) { prefs.pendingAction = false; prefs.close() } }
        function onModelConfigChanged() {
            if (prefs.visible && !roles.revision && !workspace.savingKeys) roles.openRole(roles.purpose, "")
        }
    }
    contentItem: ColumnLayout {
        spacing: 12
        RowLayout {
            Layout.fillWidth: true
            Mark { text: qsTr("设置"); font.pixelSize: Studio.pageTitleSize; font.weight: Font.DemiBold; Layout.fillWidth: true }
            Picker { id: compactNavigation; objectName:"settingsCategory"; visible:prefs.narrow
                model:prefs.categories; textRole:"label"; Accessible.name:qsTr("设置分类"); onActivated: i => prefs.navigate(prefs.categories[i].id) }
            Tool { glyph:"close"; hint:qsTr("关闭设置"); enabled:!workspace.savingKeys; onClicked:prefs.close() }
        }
        Rectangle { Layout.fillWidth:true; implicitHeight:1; color:Studio.line }
        RowLayout {
            Layout.fillWidth:true; Layout.fillHeight:true; spacing:prefs.narrow ? 0 : 18
            ColumnLayout {
                Layout.alignment:Qt.AlignTop; visible:!prefs.narrow; Layout.minimumWidth:130; Layout.maximumWidth:130; spacing:4
                Repeater {
                    model:prefs.categories
                    Tool {
                        required property var modelData
                        objectName:"settingsNav_" + modelData.id
                        Layout.fillWidth:true; text:modelData.label; glyph:modelData.glyph; chosen:prefs.section === modelData.id
                        corner:7; implicitHeight:34; onClicked:prefs.navigate(modelData.id)
                    }
                }
            }
            Rectangle { visible:!prefs.narrow; Layout.fillHeight:true; implicitWidth:1; color:Studio.line }
            Flickable {
                id: scroll; objectName:"settingsScroll"; Layout.fillWidth:true; Layout.preferredWidth:490; Layout.fillHeight:true
                contentWidth:width; contentHeight:fields.implicitHeight; clip:true; boundsBehavior:Flickable.StopAtBounds
                ScrollBar.vertical: ScrollBar { policy:scroll.contentHeight > scroll.height ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff }
                ColumnLayout {
                    id: fields; width:Math.max(0,scroll.width - (scroll.contentHeight > scroll.height ? 13 : 0)); spacing:12
                    ModelServices {
                        id:services; visible:prefs.section === "services"; Layout.fillWidth:true
                        onReveal: item => prefs.reveal(item)
                        onConfigureRole: id => { prefs.navigate("roles"); roles.openRole(roles.purpose, id) }
                        onSaved: id => {
                            const kind = roles.purpose
                            roles.openRole(kind, id)
                            if (prefs.returnToRoles && id) {
                                if (kind === "model" && roles.config.mode === "legacy") roles.resetLegacy = true
                                prefs.navigate("roles")
                            }
                            prefs.returnToRoles = false
                            prefs.feedback = id ? qsTr("连接已保存") : qsTr("连接已删除")
                        }
                    }
                    ModelAssignments {
                        id:roles; visible:prefs.section === "roles"; Layout.fillWidth:true
                        onReveal: item => prefs.reveal(item)
                        onAddRequested:prefs.editForRole(null)
                        onEditRequested: profile => prefs.editForRole(profile)
                        onSaved: {
                            if (!prefs.visible) return
                            openRole(purpose, ""); prefs.feedback = qsTr("模型用途已保存")
                        }
                    }
                    ColumnLayout {
                        visible:prefs.section === "watch"; Layout.fillWidth:true; spacing:14
                        Mark { text:qsTr("观看与 Kino"); font.pixelSize:19; font.weight:Font.DemiBold }
                        WatchPreferences { Layout.fillWidth:true }
                    }
                    ColumnLayout {
                        visible:prefs.section === "appearance"; Layout.fillWidth:true; spacing:12
                        Mark { text:qsTr("外观"); font.pixelSize:19; font.weight:Font.DemiBold }
                        RowLayout {
                            Repeater {
                                id:themeButtons; model:prefs.themes
                                Tool {
                                    required property var modelData; required property int index
                                    objectName:"theme_" + modelData.key; text:modelData.label; chosen:Studio.preferences.appearance === modelData.key
                                    outlined:true; corner:7; onClicked:Studio.preferences.appearance = modelData.key
                                    Keys.onLeftPressed:themeButtons.itemAt((index+2)%3).click()
                                    Keys.onRightPressed:themeButtons.itemAt((index+1)%3).click()
                                }
                            }
                        }
                        Mark { Layout.topMargin:10; text:qsTr("界面语言"); font.weight:Font.DemiBold }
                        RowLayout {
                            Tool { objectName:"language_zh"; text:qsTr("简体中文"); outlined:true; corner:7; chosen:desktopLocale.language === "zh"; onClicked:desktopLocale.setLanguage("zh") }
                            Tool { objectName:"language_en"; text:"English"; outlined:true; corner:7; chosen:desktopLocale.language === "en"; onClicked:desktopLocale.setLanguage("en") }
                        }
                    }
                    UsageReport { visible:prefs.section === "usage"; Layout.fillWidth:true }
                }
            }
        }
        Rectangle { Layout.fillWidth:true; implicitHeight:1; color:Studio.line }
        RowLayout {
            Layout.fillWidth:true; spacing:8
            Mark { objectName:"settingsStatus"; Layout.fillWidth:true; secondary:true; font.pixelSize:12
                text:workspace.savingKeys ? qsTr("正在保存…") : prefs.feedback; wrapMode:Text.Wrap }
            Tool { objectName:"cancelServiceEdit"; visible:prefs.section === "services" && services.editing
                text:qsTr("取消"); enabled:!workspace.savingKeys; onClicked:{
                    services.reset(); if (prefs.returnToRoles) prefs.navigate("roles"); prefs.returnToRoles=false
                } }
            Tool { objectName:"saveService"; visible:prefs.section === "services" && services.editing
                text:qsTr("保存连接"); primary:true; enabled:services.canSave; corner:8; onClicked:services.save() }
            Tool { objectName:"applyRole"; visible:prefs.section === "roles"; text:qsTr("应用"); primary:true; corner:8
                enabled:roles.complete && !roles.busy; onClicked:roles.apply() }
            Tool { id:done; objectName:"settingsDone"; visible:!services.editing || prefs.section !== "services"
                text:qsTr("完成"); outlined:true; corner:8; enabled:!workspace.savingKeys; onClicked:prefs.close() }
        }
    }
}
