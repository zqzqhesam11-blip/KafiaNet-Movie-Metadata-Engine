import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "pages"

ApplicationWindow {

    id: window

    visible: true

    width: 1500
    height: 900

    minimumWidth: 1100
    minimumHeight: 700

    title: "كافيه نت هشام الريمي"

    color: "#070910"

    property string activePage: "الألعاب"

    property bool wirelessConnected: true
    property bool usbReady: false
    property string activeSource: "WIRELESS"

    property bool appUnlocked: false

    property int totalGames: 7
    property int transferPercent: 0
    property string transferSpeed: "0 B/s"
    property string transferEta: "00:00"

    // =============================================================
    // ANDROID / ADB DEVICE STATE
    // =============================================================

    property bool adbDeviceConnected: false
    property string adbDeviceText: "لا يوجد جهاز متصل"


    // =============================================================
    // MAIN
    // =============================================================

    Row {

        anchors.fill: parent

        spacing: 0


        // =========================================================
        // SIDEBAR
        // =========================================================

        Rectangle {

            width: 240

            height: parent.height

            color: "#0B0E16"


            Column {

                anchors.fill: parent

                anchors.margins: 18

                spacing: 10


                Rectangle {

                    width: parent.width

                    height: 75

                    color: "transparent"


                    Column {

                        anchors.left: parent.left

                        anchors.verticalCenter: parent.verticalCenter

                        spacing: 2


                        Text {

                            text: "كافيه نت"

                            color: "#00D9FF"

                            font.pixelSize: 27

                            font.bold: true
                        }


                        Text {

                            text: "هشام الريمي"

                            color: "#737B8F"

                            font.pixelSize: 13

                            font.bold: true
                        }

                    }

                }


                Rectangle {

                    width: parent.width

                    height: 1

                    color: "#202532"

                }


                Item {

                    width: 1

                    height: 8

                }


                Repeater {

                    model: [

                        { title: "🎮  الألعاب", page: "الألعاب" },

                        { title: "📂  نقل الملفات", page: "نقل الملفات" },

                        { title: "📦  التثبيت", page: "التثبيت" },

                        { title: "📜  سجل النقل", page: "سجل النقل" },

                        { title: "💾  التخزين", page: "التخزين" },

                        { title: "⚙  الإعدادات", page: "الإعدادات" }

                    ]


                    delegate: Rectangle {

                        width: parent.width

                        height: 48

                        radius: 10

                        color: window.activePage === modelData.page
                               ? "#102C38"
                               : "transparent"


                        Rectangle {

                            visible: window.activePage === modelData.page

                            width: 3

                            height: 28

                            radius: 2

                            anchors.left: parent.left

                            anchors.verticalCenter: parent.verticalCenter

                            color: "#00D9FF"

                        }


                        Text {

                            anchors.fill: parent

                            anchors.leftMargin: 18

                            verticalAlignment: Text.AlignVCenter

                            text: modelData.title

                            color: window.activePage === modelData.page
                                   ? "#FFFFFF"
                                   : "#9AA2B3"

                            font.pixelSize: 15

                            font.bold: window.activePage === modelData.page

                        }


                        MouseArea {

                            anchors.fill: parent

                            cursorShape: Qt.PointingHandCursor

                            onClicked: {

                                window.activePage = modelData.page

                            }

                        }

                    }

                }


                Item {

                    width: 1

                    height: 6

                }


                // SYSTEM STATUS

                Rectangle {

                    width: parent.width

                    height: 112

                    radius: 14

                    color: "#0F141E"

                    border.width: 1

                    border.color: "#1E2634"


                    Column {

                        anchors.fill: parent

                        anchors.margins: 14

                        spacing: 9


                        Text {

                            text: "حالة النظام"

                            color: "#7E879A"

                            font.pixelSize: 12

                        }


                        Row {

                            spacing: 8


                            Rectangle {

                                width: 9

                                height: 9

                                radius: 5

                                color: "#27D17F"

                                anchors.verticalCenter: parent.verticalCenter

                            }


                            Text {

                                text: "النظام يعمل"

                                color: "#D9E0EC"

                                font.pixelSize: 13

                            }

                        }


                        Row {

                            spacing: 8


                            Text {

                                text: "الألعاب"

                                color: "#687285"

                                font.pixelSize: 11

                            }


                            Text {

                                text: window.totalGames

                                color: "#00D9FF"

                                font.pixelSize: 11

                                font.bold: true

                            }

                        }

                    }

                }


                Item {

                    width: 1

                    height: 1

                }


                Text {

                    width: parent.width

                    text: "هشام الريمي"

                    color: "#3F4757"

                    horizontalAlignment: Text.AlignHCenter

                    font.pixelSize: 11

                }

            }

        }


        // =========================================================
        // MAIN AREA
        // =========================================================

        Rectangle {

            width: parent.width - 240

            height: parent.height

            color: "#070910"


            Column {

                anchors.fill: parent

                spacing: 0


                // =================================================
                // HEADER
                // =================================================

                Rectangle {

                    width: parent.width

                    height: 108

                    color: "#0B0E16"


                    RowLayout {

                        anchors.fill: parent

                        anchors.leftMargin: 30

                        anchors.rightMargin: 30

                        spacing: 14


                        ColumnLayout {

                            Layout.fillWidth: true

                            spacing: 3


                            Text {

                                text: window.activePage

                                color: "#FFFFFF"

                                font.pixelSize: 28

                                font.bold: true

                            }


                            Text {

                                text: "إدارة ونقل ألعاب الأجهزة بسهولة وسرعة"

                                color: "#737C90"

                                font.pixelSize: 13

                            }

                        }


                        // =================================================
                        // TRANSFER MINI STATUS
                        // =================================================

                        Rectangle {

                            Layout.preferredWidth: 175

                            Layout.preferredHeight: 58

                            radius: 12

                            color: "#0E121A"

                            border.width: 1

                            border.color: "#202938"


                            Column {

                                anchors.fill: parent

                                anchors.margins: 10

                                spacing: 3


                                Text {

                                    text: "النقل الحالي"

                                    color: "#697386"

                                    font.pixelSize: 10

                                }


                                Row {

                                    spacing: 8


                                    Text {

                                        text: window.transferPercent + "%"

                                        color: "#FFFFFF"

                                        font.pixelSize: 15

                                        font.bold: true

                                    }


                                    Text {

                                        text: window.transferSpeed

                                        color: "#00D9FF"

                                        font.pixelSize: 11

                                    }

                                }

                            }

                        }


                        // =================================================
                        // WIRELESS
                        // =================================================

                        Rectangle {

                            Layout.preferredWidth: 155

                            Layout.preferredHeight: 58

                            radius: 12

                            color: window.wirelessConnected
                                   ? "#0E171C"
                                   : "#151015"

                            border.width: 1

                            border.color: window.wirelessConnected
                                           ? "#193642"
                                           : "#3A2025"


                            Column {

                                anchors.fill: parent

                                anchors.margins: 10

                                spacing: 3


                                Text {

                                    text: "LAN"

                                    color: window.wirelessConnected
                                           ? "#00D9FF"
                                           : "#D96B76"

                                    font.pixelSize: 12

                                    font.bold: true

                                }


                                Row {

                                    spacing: 6


                                    Rectangle {

                                        width: 7

                                        height: 7

                                        radius: 4

                                        color: window.wirelessConnected
                                               ? "#27D17F"
                                               : "#D94D5B"

                                        anchors.verticalCenter: parent.verticalCenter

                                    }


                                    Text {

                                        text: window.wirelessConnected
                                              ? "متصل"
                                              : "غير متصل"

                                        color: "#B9C3D2"

                                        font.pixelSize: 11

                                    }

                                }

                            }


                            MouseArea {

                                anchors.fill: parent

                                cursorShape: Qt.PointingHandCursor

                                onClicked: {

                                    window.wirelessConnected = true
                                    window.usbReady = false

                                    if (window.wirelessConnected) {
                                        appController.useWirelessSource()
                                    }

                                }

                            }

                        }


                        // =================================================
                        // USB
                        // =================================================

                        Rectangle {

                            Layout.preferredWidth: 135

                            Layout.preferredHeight: 58

                            radius: 12

                            color: window.usbReady
                                   ? "#101820"
                                   : "#151015"

                            border.width: 1

                            border.color: window.usbReady
                                           ? "#243A43"
                                           : "#3A2025"


                            Column {

                                anchors.fill: parent

                                anchors.margins: 10

                                spacing: 3


                                Text {

                                    text: "HDD"

                                    color: "#FFFFFF"

                                    font.pixelSize: 12

                                    font.bold: true

                                }


                                Row {

                                    spacing: 6


                                    Rectangle {

                                        width: 7

                                        height: 7

                                        radius: 4

                                        color: window.usbReady
                                               ? "#27D17F"
                                               : "#D94D5B"

                                        anchors.verticalCenter: parent.verticalCenter

                                    }


                                    Text {

                                        text: window.usbReady
                                              ? "متصل"
                                              : "غير متصل"

                                        color: "#B9C3D2"

                                        font.pixelSize: 11

                                    }

                                }

                            }


                            MouseArea {

                                anchors.fill: parent

                                cursorShape: Qt.PointingHandCursor

                                onClicked: {

                                    window.usbReady = true
                                     window.wirelessConnected = false

                                    if (window.usbReady) {
                                        appController.useUsbSource()
                                    }

                                }

                            }

                        }



                        // =================================================
                        // ANDROID DEVICES / ADB
                        // =================================================

                        Rectangle {

                            Layout.preferredWidth: 190
                            Layout.preferredHeight: 58

                            radius: 12

                            color: window.adbDeviceConnected
                                   ? "#0E171C"
                                   : "#151015"

                            border.width: 1

                            border.color: window.adbDeviceConnected
                                           ? "#193642"
                                           : "#3A2025"

                            Column {

                                anchors.fill: parent
                                anchors.margins: 10
                                spacing: 3

                                Text {

                                    text: "📱 الأجهزة"

                                    color: window.adbDeviceConnected
                                           ? "#00D9FF"
                                           : "#D96B76"

                                    font.pixelSize: 12
                                    font.bold: true

                                }

                                Row {

                                    spacing: 6

                                    Rectangle {

                                        width: 7
                                        height: 7
                                        radius: 4

                                        color: window.adbDeviceConnected
                                               ? "#27D17F"
                                               : "#D94D5B"

                                        anchors.verticalCenter: parent.verticalCenter

                                    }

                                    Text {

                                        text: window.adbDeviceConnected
                                              ? window.adbDeviceText
                                              : "لا يوجد جهاز متصل"

                                        color: "#B9C3D2"
                                        font.pixelSize: 10

                                        elide: Text.ElideRight
                                        maximumLineCount: 1

                                    }

                                }

                            }

                            MouseArea {

                                anchors.fill: parent

                                cursorShape: Qt.PointingHandCursor

                                onClicked: {
                                    appController.getADBDevices()
                                }

                            }

                        }


                        // =================================================
                        // REFRESH
                        // =================================================

                        Rectangle {

                            Layout.preferredWidth: 48

                            Layout.preferredHeight: 48

                            radius: 12

                            color: "#10141D"

                            border.width: 1

                            border.color: "#242B39"


                            Text {

                                anchors.centerIn: parent

                                text: "↻"

                                color: "#AAB3C3"

                                font.pixelSize: 23

                            }


                            MouseArea {

                                anchors.fill: parent

                                cursorShape: Qt.PointingHandCursor

                                onClicked: {

                                    console.log("REFRESH")

                                }

                            }

                        }


                        // SETTINGS

                        Rectangle {

                            Layout.preferredWidth: 48

                            Layout.preferredHeight: 48

                            radius: 12

                            color: "#10141D"

                            border.width: 1

                            border.color: "#242B39"


                            Text {

                                anchors.centerIn: parent

                                text: "⚙"

                                color: "#AAB3C3"

                                font.pixelSize: 19

                            }


                            MouseArea {

                                anchors.fill: parent

                                cursorShape: Qt.PointingHandCursor

                                onClicked: {

                                    window.activePage = "الإعدادات"

                                }

                            }

                        }

                    }

                }


                // =================================================
                // CONTENT
                // =================================================

                Item {

                    width: parent.width

                    height: parent.height - 108


                    Dashboard {

                        anchors.fill: parent

                    }

                }

            }

        }

    }


    Component.onCompleted: {
        if (appController && appController.getADBDevices) {
            appController.getADBDevices()
        }
    }


    // =============================================================
    // CONNECT TO EXISTING CONTROLLER SIGNALS
    // =============================================================

    Connections {

        target: appController

        function onTransferPercentChanged(percent) {

            window.transferPercent = percent

        }

        function onTransferSpeedChanged(speed) {

            window.transferSpeed = speed

        }

        function onTransferEtaChanged(eta) {

            window.transferEta = eta

        }

        function onTransferFinished(success, message) {

            if (success) {

                window.transferPercent = 100

            }

            else {

                window.transferPercent = 0

            }

        }


        function onAdbDevicesChanged(devices) {

            console.log("ADB DEVICES FROM PYTHON:", devices)

            if (!devices || devices.length === 0) {

                window.adbDeviceConnected = false
                window.adbDeviceText = "لا يوجد جهاز متصل"

                return
            }

            window.adbDeviceConnected = true

            var first = devices[0]

            if (typeof first === "object") {

                var serial = first.serial || ""
                var model = first.model || first.name || ""

                if (model && serial)
                    window.adbDeviceText = model + " / " + serial
                else if (serial)
                    window.adbDeviceText = serial
                else
                    window.adbDeviceText = "جهاز Android متصل"

            } else {

                window.adbDeviceText = String(first)

            }

        }

    }


    Dialog {
        id: startupPasswordDialog

        modal: true
        visible: !window.appUnlocked

        width: 390
        height: 240

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
            spacing: 14

            Text {
                width: parent.width
                text: "???? ???? ???? ???? ???????"
                color: "white"
                font.pixelSize: 16
                font.bold: true
                horizontalAlignment: Text.AlignHCenter
            }

            TextField {
                id: startupPasswordField

                width: parent.width
                height: 42

                echoMode: TextInput.Password

                placeholderText: "???? ????"

                horizontalAlignment: TextInput.AlignHCenter
            }

            Text {
                id: startupPasswordError

                width: parent.width
                text: "???? ???? ??? ?????"
                color: "#FF5C5C"
                visible: false
                horizontalAlignment: Text.AlignHCenter
            }

            Button {
                width: parent.width
                height: 40

                text: "????"

                onClicked: {
                    if (startupPasswordField.text === "??????") {
                        window.appUnlocked = true
                        startupPasswordDialog.close()
                    } else {
                        startupPasswordError.visible = true
                        startupPasswordField.selectAll()
                        startupPasswordField.forceActiveFocus()
                    }
                }
            }
        }
    }

}
