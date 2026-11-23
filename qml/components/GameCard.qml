import QtQuick
import QtQuick.Controls

Item {
    id: root

    property string gameName: ""
    property string imageSource: ""
    property string gameColor: "#00D9FF"
    property string sourcePath: ""
    property bool isCustom: false
    property bool hasVersions: !isCustom && (gameName === "eFootball" || gameName === "Grand Theft Auto")
    property string selectedVersion: ""

    function openCustomCopyDialog() {
        var safeName = root.gameName.replace(/[\\/]/g, "_")
        customDestinationField.text = "/sdcard/Download/KafiaNetControlPro/" + safeName + "/"
        customDestinationError.visible = false
        customCopyDialog.open()
    }

    function showVersionPicker() {
        if (!root.hasVersions || !appController)
            return
        root.selectedVersion = appController.getSelectedGameVersion(root.gameName) || ""
        versionPicker.versionItems = appController.getGameVersions(root.gameName) || []
        versionPicker.open()
    }

    Component.onCompleted: {
        if (root.hasVersions && appController)
            root.selectedVersion = appController.getSelectedGameVersion(root.gameName) || ""
    }

    width: 245
    height: 310

    Rectangle {
        id: card

        anchors.fill: parent

        radius: 22

        color: "#151927"

        border.width: mouseArea.containsMouse ? 2 : 1

        border.color: mouseArea.containsMouse
                      ? root.gameColor
                      : "#252A38"

        clip: true

        Rectangle {
            anchors.fill: parent
            visible: root.imageSource.length === 0
            color: "#111B2D"
            gradient: Gradient {
                GradientStop { position: 0.0; color: "#1B2940" }
                GradientStop { position: 1.0; color: "#080D18" }
            }
            Text {
                anchors.centerIn: parent
                text: "🎮"
                font.pixelSize: 76
                opacity: 0.7
            }
        }

        Image {
            anchors.fill: parent
            visible: root.imageSource.length > 0
            source: root.imageSource

            fillMode: Image.PreserveAspectCrop

            asynchronous: true
            cache: true

            scale: mouseArea.containsMouse ? 1.04 : 1.0

            Behavior on scale {
                NumberAnimation {
                    duration: 250
                    easing.type: Easing.OutCubic
                }
            }
        }

        Rectangle {
            anchors.fill: parent

            gradient: Gradient {
                GradientStop {
                    position: 0.0
                    color: "#00000000"
                }

                GradientStop {
                    position: 0.55
                    color: "#22000000"
                }

                GradientStop {
                    position: 1.0
                    color: "#E8000000"
                }
            }
        }

        Text {
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.bottom: parent.bottom

            anchors.leftMargin: 18
            anchors.rightMargin: 18
            anchors.bottomMargin: 16

            objectName: "gameCardNameLabel"
            text: root.gameName

            color: "white"

            font.pixelSize: 20
            font.bold: true
            wrapMode: Text.WrapAtWordBoundaryOrAnywhere

            opacity: mouseArea.containsMouse ? 0 : 1

            Behavior on opacity {
                NumberAnimation {
                    duration: 130
                }
            }
        }

        MouseArea {
            id: versionOpenArea
            anchors.fill: parent
            acceptedButtons: root.hasVersions ? Qt.LeftButton : Qt.NoButton
            cursorShape: root.hasVersions ? Qt.PointingHandCursor : Qt.ArrowCursor
            onClicked: root.showVersionPicker()
        }

        Rectangle {
            anchors.fill: parent

            radius: 22

            color: "#E6111522"

            opacity: mouseArea.containsMouse ? 1 : 0

            Behavior on opacity {
                NumberAnimation {
                    duration: 180
                }
            }

            Column {
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter

                anchors.margins: 20

                spacing: 8

                Text {
                    text: root.gameName
                    width: parent.width
                    wrapMode: Text.WrapAtWordBoundaryOrAnywhere
                    color: "white"
                    font.pixelSize: 18
                    font.bold: true
                }

                Rectangle {
                    width: 45
                    height: 3

                    radius: 2

                    color: root.gameColor
                }

                Text {
                    text: "📂 المصدر: " + (root.sourcePath.length > 0 ? root.sourcePath : "العابي/" + root.gameName)

                    color: "#D0D5E0"

                    font.pixelSize: 12

                    elide: Text.ElideRight

                    width: parent.width
                }

                Text {
                    text: "💾 الحجم: -- GB"

                    color: root.gameColor

                    font.pixelSize: 13
                    font.bold: true
                }

                Text {
                    text: "📦 الوجهة: قابلة للتعديل"

                    color: "#B8BDCA"

                    font.pixelSize: 12
                }

                Rectangle {
                    visible: root.hasVersions
                    width: parent.width
                    height: 34
                    radius: 10
                    color: "#182C39"
                    border.width: 1
                    border.color: root.gameColor
                    Text {
                        anchors.fill: parent
                        anchors.margins: 7
                        text: root.selectedVersion.length > 0
                              ? "الإصدار المختار: " + root.selectedVersion
                              : "اضغط البطاقة لاختيار الإصدار"
                        color: "#E7F8FF"
                        font.pixelSize: 11
                        font.bold: true
                        elide: Text.ElideLeft
                        verticalAlignment: Text.AlignVCenter
                        horizontalAlignment: Text.AlignRight
                    }
                }

                Column {
                    width: parent.width
                    spacing: 7
                    visible: true

                    Row {
                        width: parent.width
                        spacing: 7

                        Button {
                            width: (parent.width - 7) / 2
                            height: 36


                            onClicked: {
                                if (root.hasVersions && !root.selectedVersion) {
                                    root.showVersionPicker()
                                    return
                                }
                                appController.installGame(root.gameName)
                            }

                            background: Rectangle {
                                radius: 9
                                color: "#252C3A"
                                border.width: 1
                                border.color: "#3A4355"
                            }

                            contentItem: Text {
                                text: "تثبيت"
                                color: "white"
                                font.pixelSize: 13
                                font.bold: true
                                horizontalAlignment: Text.AlignHCenter
                                verticalAlignment: Text.AlignVCenter
                            }
                        }

                        Button {
                            width: (parent.width - 7) / 2
                            height: 36


                            onClicked: {
                                if (root.hasVersions && !root.selectedVersion) {
                                    root.showVersionPicker()
                                    return
                                }
                                appController.launchGame(root.gameName)
                            }

                            background: Rectangle {
                                radius: 9
                                color: enabled
                                       ? "#173A32"
                                       : "#3A4050"
                                border.width: 1
                                border.color: "#2B6656"
                            }

                            contentItem: Text {
                                text: "فتح اللعبة"
                                color: "white"
                                font.pixelSize: 12
                                font.bold: true
                                horizontalAlignment: Text.AlignHCenter
                                verticalAlignment: Text.AlignVCenter
                            }
                        }
                    }

                    Row {
                        width: parent.width
                        spacing: 7

                        Button {
                            width: (parent.width - 7) / 2
                            height: 36
                            onClicked: {
                                if (root.isCustom) {
                                    root.openCustomCopyDialog()
                                    return
                                }
                                if (root.hasVersions && !root.selectedVersion) {
                                    root.showVersionPicker()
                                    return
                                }
                                passwordField.text = ""
                                passwordError.visible = false
                                appController.copyGame(root.gameName)
                            }

                            background: Rectangle {
                                radius: 9
                                color: enabled ? root.gameColor : "#3A4050"
                            }

                            contentItem: Text {
                                text: "نسخ"
                                color: "white"
                                font.pixelSize: 13
                                font.bold: true
                                horizontalAlignment: Text.AlignHCenter
                                verticalAlignment: Text.AlignVCenter
                            }
                        }

                        Button {
                            width: (parent.width - 7) / 2
                            height: 36
                            onClicked: {
                                if (root.hasVersions && !root.selectedVersion) {
                                    root.showVersionPicker()
                                    return
                                }
                                uninstallConfirmDialog.open()
                            }

                            background: Rectangle {
                                radius: 9
                                color: "#3A2028"
                                border.width: 1
                                border.color: "#87404A"
                            }

                            contentItem: Text {
                                text: "إزالة التثبيت"
                                color: "#FFC0C7"
                                font.pixelSize: 11
                                font.bold: true
                                horizontalAlignment: Text.AlignHCenter
                                verticalAlignment: Text.AlignVCenter
                            }
                        }
                    }

                    Button {
                        width: parent.width
                        height: 36
                        onClicked: {
                            if (root.hasVersions && !root.selectedVersion) {
                                root.showVersionPicker()
                                return
                            }
                            appController.closeGame(root.gameName)
                        }

                        background: Rectangle {
                            radius: 9
                            color: "#202B3A"
                            border.width: 1
                            border.color: "#43546B"
                        }

                        contentItem: Text {
                            text: "إغلاق اللعبة"
                            color: "#DCE8FA"
                            font.pixelSize: 12
                            font.bold: true
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                        }
                    }
                }


            }
        }

        Dialog {
            id: uninstallConfirmDialog
            modal: true
            focus: true
            title: "تأكيد إزالة التثبيت"
            width: 390
            height: 205
            anchors.centerIn: parent
            standardButtons: Dialog.NoButton

            background: Rectangle {
                radius: 14
                color: "#111722"
                border.width: 1
                border.color: "#713843"
            }

            Column {
                anchors.fill: parent
                anchors.margins: 22
                spacing: 14

                Text {
                    width: parent.width
                    text: "سيتم حذف " + root.gameName + " وبيانات التطبيق من الهاتف المحدد. هل تريد المتابعة؟"
                    color: "#E8EDF5"
                    font.pixelSize: 13
                    wrapMode: Text.Wrap
                    horizontalAlignment: Text.AlignHCenter
                }

                Row {
                    width: parent.width
                    spacing: 8

                    Button {
                        width: (parent.width - 8) / 2
                        height: 38
                        onClicked: {
                            uninstallConfirmDialog.close()
                            appController.uninstallGame(root.gameName)
                        }
                        background: Rectangle {
                            radius: 9
                            color: "#5A252F"
                            border.width: 1
                            border.color: "#A64B5A"
                        }
                        contentItem: Text {
                            text: "إزالة التثبيت"
                            color: "#FFFFFF"
                            font.pixelSize: 12
                            font.bold: true
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                        }
                    }

                    Button {
                        width: (parent.width - 8) / 2
                        height: 38
                        onClicked: uninstallConfirmDialog.close()
                        background: Rectangle {
                            radius: 9
                            color: "#252C3A"
                            border.width: 1
                            border.color: "#3A4355"
                        }
                        contentItem: Text {
                            text: "إلغاء"
                            color: "#FFFFFF"
                            font.pixelSize: 12
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                        }
                    }
                }
            }
        }

        Dialog {
            id: copyPasswordDialog

            modal: true
            title: "تأكيد النسخ"

            width: 390
            height: 250

            anchors.centerIn: parent

            standardButtons: Dialog.NoButton

            background: Rectangle {
                radius: 14
                color: "#111722"
                border.width: 1
                border.color: "#2A3445"
            }

            Column {
                anchors.fill: parent
                anchors.margins: 22
                spacing: 12

                Text {
                    width: parent.width
                    text: "أدخل كلمة السر لبدء نسخ اللعبة"
                    color: "white"
                    font.pixelSize: 15
                    font.bold: true
                    horizontalAlignment: Text.AlignHCenter
                }

                TextField {
                    id: passwordField

                    width: parent.width
                    height: 42

                    echoMode: TextInput.Password

                    placeholderText: "كلمة السر"

                    horizontalAlignment: TextInput.AlignHCenter

                    color: "white"

                    background: Rectangle {
                        radius: 9
                        color: "#1B2230"
                        border.width: 1
                        border.color: "#344055"
                    }

                    onAccepted: {
                        passwordCheckButton.clicked()
                    }
                }

                Text {
                    id: passwordError

                    width: parent.width

                    text: "كلمة السر غير صحيحة"

                    color: "#FF5C5C"

                    font.pixelSize: 12
                    horizontalAlignment: Text.AlignHCenter

                    visible: false
                }

                Row {
                    width: parent.width
                    spacing: 8

                    Button {
                        id: passwordCheckButton

                        width: (parent.width - 8) / 2
                        height: 38

                        onClicked: {
                            if (passwordField.text === "مثفسلخ") {

                                passwordError.visible = false
                                copyPasswordDialog.close()

                                appController.copyGame(
                                    root.gameName
                                )

                            } else {

                                passwordError.visible = true
                                passwordField.selectAll()
                                passwordField.forceActiveFocus()
                            }
                        }

                        background: Rectangle {
                            radius: 9
                            color: "#173A32"
                        }

                        contentItem: Text {
                            text: "تأكيد"
                            color: "white"
                            font.pixelSize: 13
                            font.bold: true
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                        }
                    }

                    Button {
                        width: (parent.width - 8) / 2
                        height: 38

                        onClicked: {
                            copyPasswordDialog.close()
                        }

                        background: Rectangle {
                            radius: 9
                            color: "#252C3A"
                        }

                        contentItem: Text {
                            text: "إلغاء"
                            color: "white"
                            font.pixelSize: 13
                            font.bold: true
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                        }
                    }
                }
            }
        }

        MouseArea {
            id: mouseArea

            anchors.fill: parent

            hoverEnabled: true

            cursorShape: Qt.PointingHandCursor

            acceptedButtons: Qt.NoButton
        }
    }

    Connections {
        target: appController
        function onSelectedGameVersionChanged(gameName, versionName) {
            if (gameName === root.gameName)
                root.selectedVersion = String(versionName || "")
        }
    }

    Dialog {
        id: customCopyDialog
        parent: Overlay.overlay
        modal: true
        focus: true
        title: "نسخ المجلد"
        width: 480
        height: 255
        anchors.centerIn: Overlay.overlay
        standardButtons: Dialog.NoButton
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside

        background: Rectangle {
            radius: 15
            color: "#111722"
            border.width: 1
            border.color: root.gameColor
        }

        Column {
            anchors.fill: parent
            anchors.margins: 20
            spacing: 11

            Text {
                width: parent.width
                text: "المجلد: " + root.gameName
                color: "#FFFFFF"
                font.pixelSize: 14
                font.bold: true
                elide: Text.ElideRight
            }

            Text {
                width: parent.width
                text: "أدخل مجلد الوجهة داخل الهاتف. يبدأ المسار بـ /sdcard/. وعند تحديد فلاشة سيتم النسخ إليها داخل مجلد KAFIA NET CONTROL PRO تلقائيًا."
                color: "#AAB6C8"
                font.pixelSize: 11
                wrapMode: Text.Wrap
            }

            TextField {
                id: customDestinationField
                width: parent.width
                placeholderText: "/sdcard/Download/KafiaNetControlPro/"
                color: "#F4F7FC"
                selectByMouse: true
            }

            Text {
                id: customDestinationError
                width: parent.width
                visible: false
                text: "تعذر بدء النسخ. تحقق من وجهة Android واتصال الجهاز."
                color: "#FF8C8C"
                font.pixelSize: 10
                wrapMode: Text.Wrap
            }

            Row {
                width: parent.width
                spacing: 8

                Button {
                    width: (parent.width - 8) / 2
                    height: 36
                    text: "إلغاء"
                    onClicked: customCopyDialog.close()
                }

                Button {
                    width: (parent.width - 8) / 2
                    height: 36
                    text: "بدء النسخ"
                    onClicked: {
                        var ok = appController.copyDiscoveredFolder(
                            root.sourcePath,
                            root.gameName,
                            customDestinationField.text
                        )
                        customDestinationError.visible = !ok
                        if (ok)
                            customCopyDialog.close()
                    }
                }
            }
        }
    }

    Dialog {
        id: versionPicker
        objectName: "gameVersionPickerDialog"
        parent: Overlay.overlay
        modal: true
        focus: true
        dim: true
        width: 900
        height: 640
        anchors.centerIn: Overlay.overlay
        standardButtons: Dialog.NoButton
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        property var versionItems: []

        background: Rectangle {
            radius: 26
            color: "#0C1220"
            border.width: 1
            border.color: "#33445E"
            gradient: Gradient {
                GradientStop { position: 0.0; color: "#18243A" }
                GradientStop { position: 0.55; color: "#101827" }
                GradientStop { position: 1.0; color: "#0A0F19" }
            }
        }

        enter: Transition {
            NumberAnimation { property: "opacity"; from: 0; to: 1; duration: 180 }
            NumberAnimation { property: "scale"; from: 0.94; to: 1; duration: 220; easing.type: Easing.OutCubic }
        }
        exit: Transition {
            NumberAnimation { property: "opacity"; from: 1; to: 0; duration: 120 }
        }

        Column {
            anchors.fill: parent
            anchors.margins: 22
            spacing: 14

            Row {
                width: parent.width
                height: 76
                spacing: 14

                Rectangle {
                    width: 58
                    height: 58
                    radius: 17
                    anchors.verticalCenter: parent.verticalCenter
                    color: root.gameColor
                    opacity: 0.18
                    border.width: 1
                    border.color: root.gameColor
                    Text {
                        anchors.centerIn: parent
                        text: root.gameName === "eFootball" ? "⚽" : "🚘"
                        font.pixelSize: 28
                    }
                }

                Column {
                    width: parent.width - 120
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: 5
                    Text {
                        width: parent.width
                        text: "مكتبة الإصدارات"
                        color: "#FFFFFF"
                        font.pixelSize: 23
                        font.bold: true
                        horizontalAlignment: Text.AlignRight
                    }
                    Text {
                        width: parent.width
                        text: root.gameName + "  •  اختر النسخة التي تريد استخدامها"
                        color: "#AAB8CC"
                        font.pixelSize: 12
                        horizontalAlignment: Text.AlignRight
                        elide: Text.ElideRight
                    }
                }

                Button {
                    width: 42
                    height: 42
                    anchors.verticalCenter: parent.verticalCenter
                    onClicked: versionPicker.close()
                    background: Rectangle {
                        radius: 12
                        color: "#263246"
                        border.width: 1
                        border.color: "#42516A"
                    }
                    contentItem: Text {
                        text: "×"
                        color: "#E8F0FA"
                        font.pixelSize: 24
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }
                }
            }

            Rectangle {
                width: parent.width
                height: 1
                color: "#29364A"
            }

            GridView {
                id: versionGrid
                width: parent.width
                height: parent.height - 150
                clip: true
                cellWidth: 276
                cellHeight: 280
                model: versionPicker.versionItems
                boundsBehavior: Flickable.StopAtBounds
                ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

                delegate: Rectangle {
                    property var versionInfo: modelData
                    width: 258
                    height: 264
                    radius: 19
                    color: versionInfo.name === root.selectedVersion ? "#1B3440" : "#151F30"
                    border.width: versionInfo.name === root.selectedVersion ? 2 : 1
                    border.color: versionInfo.name === root.selectedVersion ? root.gameColor : "#34435B"

                    Image {
                        id: editionArtwork
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.top: parent.top
                        anchors.margins: 9
                        height: 126
                        source: versionInfo.image && versionInfo.image.length > 0
                                ? versionInfo.image : root.imageSource
                        fillMode: Image.PreserveAspectCrop
                        asynchronous: true
                        cache: true
                        clip: true
                        layer.enabled: true
                        Rectangle {
                            anchors.fill: parent
                            radius: 11
                            color: "transparent"
                            border.width: 1
                            border.color: "#52617A"
                        }
                    }

                    Column {
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.top: editionArtwork.bottom
                        anchors.leftMargin: 12
                        anchors.rightMargin: 12
                        anchors.topMargin: 9
                        spacing: 5

                        Row {
                            width: parent.width
                            height: 24
                            Text {
                                width: parent.width - 76
                                text: versionInfo.name
                                color: "#FFFFFF"
                                font.pixelSize: 14
                                font.bold: true
                                elide: Text.ElideRight
                                verticalAlignment: Text.AlignVCenter
                            }
                            Rectangle {
                                visible: versionInfo.name === root.selectedVersion
                                width: 70
                                height: 22
                                radius: 8
                                color: root.gameColor
                                Text {
                                    anchors.centerIn: parent
                                    text: "محدد"
                                    color: "#06111A"
                                    font.pixelSize: 10
                                    font.bold: true
                                }
                            }
                        }

                        Text {
                            width: parent.width
                            text: versionInfo.hasApk
                                  ? "APK  •  " + versionInfo.apkName
                                  : "APK غير موجود"
                            color: versionInfo.hasApk ? "#A8D8C8" : "#E4A2A8"
                            font.pixelSize: 10
                            elide: Text.ElideMiddle
                        }

                        Row {
                            width: parent.width
                            spacing: 6
                            Repeater {
                                model: [
                                    { "label": "DATA", "ok": versionInfo.hasData },
                                    { "label": "OBB", "ok": versionInfo.hasObb }
                                ]
                                delegate: Rectangle {
                                    width: 66
                                    height: 22
                                    radius: 7
                                    color: modelData.ok ? "#173D35" : "#342B32"
                                    border.width: 1
                                    border.color: modelData.ok ? "#2D8069" : "#70414D"
                                    Text {
                                        anchors.centerIn: parent
                                        text: modelData.label + (modelData.ok ? " ✓" : " —")
                                        color: modelData.ok ? "#A6E8D2" : "#D5A7AE"
                                        font.pixelSize: 9
                                        font.bold: true
                                    }
                                }
                            }
                        }
                    }

                    Button {
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.bottom: parent.bottom
                        anchors.leftMargin: 11
                        anchors.rightMargin: 11
                        anchors.bottomMargin: 10
                        height: 36
                        onClicked: {
                            if (appController.selectGameVersion(root.gameName, versionInfo.name)) {
                                root.selectedVersion = versionInfo.name
                                versionPicker.close()
                            }
                        }
                        background: Rectangle {
                            radius: 10
                            color: versionInfo.name === root.selectedVersion ? "#214B49" : root.gameColor
                            border.width: 1
                            border.color: versionInfo.name === root.selectedVersion ? "#4C9D90" : root.gameColor
                        }
                        contentItem: Text {
                            text: versionInfo.name === root.selectedVersion ? "الإصدار الحالي ✓" : "اعتماد هذا الإصدار"
                            color: versionInfo.name === root.selectedVersion ? "#C8FFF0" : "#071019"
                            font.pixelSize: 12
                            font.bold: true
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                        }
                    }
                }
            }

            Text {
                visible: versionPicker.versionItems.length === 0
                width: parent.width
                height: 80
                text: "لم يتم العثور على مجلدات إصدارات داخل مصدر اللعبة. تحقق من اتصال مكتبة الألعاب."
                color: "#E6B3BA"
                font.pixelSize: 14
                wrapMode: Text.Wrap
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
            }

            Row {
                width: parent.width
                height: 34
                Text {
                    width: parent.width - 125
                    text: root.selectedVersion.length > 0
                          ? "المحدد: " + root.selectedVersion + "  •  سيُستخدم في النسخ والتثبيت والتشغيل"
                          : "لم يتم اعتماد إصدار بعد"
                    color: "#AAB8CC"
                    font.pixelSize: 11
                    elide: Text.ElideRight
                    verticalAlignment: Text.AlignVCenter
                }
                Button {
                    width: 118
                    height: 32
                    onClicked: versionPicker.close()
                    background: Rectangle {
                        radius: 9
                        color: "#263246"
                        border.width: 1
                        border.color: "#42516A"
                    }
                    contentItem: Text {
                        text: "إغلاق"
                        color: "#EAF0F8"
                        font.pixelSize: 12
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }
                }
            }
        }
    }
}
