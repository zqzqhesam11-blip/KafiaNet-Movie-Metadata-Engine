import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root

    property string sourcePath: ""
    property var packages: []
    property bool busy: false
    property string statusText: "جاهز"

    function refreshPackages() {
        if (!appController)
            return
        root.sourcePath = appController.getInstallSourcePath()
        root.packages = appController.previewInstallBatch()
    }

    function availableGameCount() {
        var seen = ({})
        for (var i = 0; i < root.packages.length; ++i) {
            var key = String(root.packages[i].gameKey || "")
            if (key.length)
                seen[key] = true
        }
        return Object.keys(seen).length
    }

    function requestedGameCount() {
        return appController
               ? Number(appController.getGameCountForAmount(installAmountField.text) || 0)
               : 0
    }

    function plannedGameCount() {
        return Math.min(root.requestedGameCount(), root.availableGameCount())
    }

    function updateDeviceProgress(serial, percent, status, operation, speed, eta, error) {
        if (error)
            root.statusText = String(error)
        else if (status)
            root.statusText = String(status)
    }

    Rectangle {
        anchors.fill: parent
        color: "#070910"
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 18
        spacing: 8

        RowLayout {
            Layout.fillWidth: true
            spacing: 8

            Text {
                text: "تثبيت"
                color: "#FFFFFF"
                font.pixelSize: 19
                font.bold: true
            }

            Item {
                Layout.fillWidth: true
            }

            Text {
                text: "APK / APKS / XAPK"
                color: "#8290A4"
                font.pixelSize: 10
            }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: 7

            ColumnLayout {
                Layout.preferredWidth: 118
                Layout.alignment: Qt.AlignVCenter
                spacing: 3

                TextField {
                    id: installAmountField
                    Layout.fillWidth: true
                    Layout.preferredHeight: 40
                    placeholderText: "المبلغ"
                    inputMethodHints: Qt.ImhDigitsOnly
                    selectByMouse: true
                    horizontalAlignment: TextInput.AlignHCenter
                    color: "#F7FAFF"
                    selectionColor: "#187F70"
                    selectedTextColor: "#FFFFFF"
                    palette.text: "#F7FAFF"
                    palette.placeholderText: "#91A6BA"
                    palette.highlight: "#187F70"
                    palette.highlightedText: "#FFFFFF"
                    font.pixelSize: 14
                    font.bold: true
                    background: Rectangle {
                        radius: 8
                        color: "#111C29"
                        border.width: installAmountField.activeFocus ? 2 : 1
                        border.color: installAmountField.activeFocus ? "#35D6B4" : "#52677E"
                    }
                }

                Text {
                    Layout.fillWidth: true
                    text: "السعر يقابل: " + root.requestedGameCount()
                          + " لعبة • المتاح: " + root.availableGameCount()
                          + " • سيُثبت: " + root.plannedGameCount()
                    color: "#62E7B5"
                    font.pixelSize: 11
                    font.bold: true
                    horizontalAlignment: Text.AlignHCenter
                }
            }

            Button {
                Layout.preferredWidth: 124
                Layout.preferredHeight: 42
                Layout.alignment: Qt.AlignVCenter
                text: "مصدر اختياري"
                font.pixelSize: 11
                onClicked: {
                    if (appController && appController.chooseInstallSource())
                        root.refreshPackages()
                }
                background: Rectangle {
                    radius: 8
                    color: parent.down ? "#1A403C" : "#15302D"
                    border.width: 1
                    border.color: "#347A6B"
                }
                contentItem: Text {
                    text: "مصدر اختياري"
                    color: "#D8FFF2"
                    font.pixelSize: 11
                    font.bold: true
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                }
            }

            Button {
                Layout.preferredWidth: 90
                Layout.preferredHeight: 42
                Layout.alignment: Qt.AlignVCenter
                text: "تثبيت"
                enabled: !root.busy
                         && root.packages.length > 0
                         && appController
                         && root.plannedGameCount() > 0
                onClicked: {
                    if (appController)
                        appController.installGamesByAmount(installAmountField.text)
                }
                background: Rectangle {
                    radius: 8
                    color: parent.enabled
                           ? (parent.down ? "#009B80" : "#16B99A")
                           : "#26343E"
                    border.width: 1
                    border.color: parent.enabled ? "#36D8B7" : "#46535E"
                }
                contentItem: Text {
                    text: "تثبيت"
                    color: parent.parent.enabled ? "#041713" : "#AAB6C0"
                    font.pixelSize: 12
                    font.bold: true
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                }
            }

            Rectangle {
                Layout.preferredWidth: 140
                Layout.preferredHeight: 42
                Layout.alignment: Qt.AlignVCenter
                radius: 8
                color: "#10251F"
                border.width: 1
                border.color: "#34765B"

                Text {
                    anchors.centerIn: parent
                    text: "الحزم المكتشفة: " + root.packages.length
                    color: "#79F0C1"
                    font.pixelSize: 11
                    font.bold: true
                }
            }

            Button {
                Layout.preferredWidth: 76
                Layout.preferredHeight: 42
                Layout.alignment: Qt.AlignVCenter
                text: "تحديث"
                font.pixelSize: 11
                enabled: !root.busy
                onClicked: root.refreshPackages()
                background: Rectangle {
                    radius: 8
                    color: parent.down ? "#253447" : "#192638"
                    border.width: 1
                    border.color: "#536A83"
                }
                contentItem: Text {
                    text: "تحديث"
                    color: "#E0EAF5"
                    font.pixelSize: 11
                    font.bold: true
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                }
            }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: 10

            Text {
                Layout.fillWidth: true
                text: root.sourcePath
                      ? "المصدر: " + root.sourcePath
                      : "المصدر الافتراضي: D:\\games\\تثبيت"
                color: "#91A1B5"
                font.pixelSize: 9
                elide: Text.ElideMiddle
            }
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 29
            radius: 6
            color: "#0D141F"
            border.width: 1
            border.color: "#34465B"

            Text {
                anchors.fill: parent
                anchors.leftMargin: 9
                anchors.rightMargin: 9
                text: root.statusText
                color: "#D1DCE8"
                font.pixelSize: 10
                verticalAlignment: Text.AlignVCenter
                elide: Text.ElideRight
            }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: 8

            Text {
                text: "الحزم"
                color: "#00D9FF"
                font.pixelSize: 12
                font.bold: true
            }

            Item {
                Layout.fillWidth: true
            }

            Text {
                text: root.packages.length + " عنصر"
                color: "#91A1B5"
                font.pixelSize: 9
            }
        }

        ListView {
            id: packageList
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            spacing: 5
            model: root.packages
            boundsBehavior: Flickable.StopAtBounds

            ScrollBar.vertical: ScrollBar {
                policy: ScrollBar.AsNeeded
                width: 7
            }

            delegate: Rectangle {
                width: packageList.width
                height: 49
                radius: 7
                color: "#101722"
                border.width: 1
                border.color: "#3A4C61"

                RowLayout {
                    anchors.fill: parent
                    anchors.margins: 9
                    spacing: 8

                    Rectangle {
                        Layout.preferredWidth: 40
                        Layout.preferredHeight: 28
                        radius: 6
                        color: modelData.type === "xapk" ? "#3A2D17" : "#17313A"
                        Text {
                            anchors.centerIn: parent
                            text: modelData.type === "xapk" ? "XAPK"
                                  : modelData.type === "apks" ? "APKS" : "APK"
                            color: modelData.type === "xapk" ? "#FFD27A" : "#00D9FF"
                            font.pixelSize: 8
                            font.bold: true
                        }
                    }

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 1
                        Text {
                            Layout.fillWidth: true
                            text: String(modelData.name || "")
                            color: "#F2F6FC"
                            font.pixelSize: 11
                            font.bold: true
                            elide: Text.ElideRight
                        }
                        Text {
                            Layout.fillWidth: true
                            text: String(modelData.fileCount || 1)
                                  + " ملف • "
                                  + (modelData.type === "xapk" ? "حزمة XAPK"
                                     : modelData.type === "apks" ? "حزمة APKS"
                                     : modelData.type === "apk_group" ? "حزمة APK مقسمة"
                                     : "APK")
                            color: "#A4B3C4"
                            font.pixelSize: 8
                        }
                    }

                    Text {
                        text: "جاهز"
                        color: "#64E7B4"
                        font.pixelSize: 9
                    }
                }
            }
        }
    }

    Connections {
        target: appController
        function onDeviceProgressChanged(serial, percent, status, operation, speed, eta, error) {
            root.updateDeviceProgress(serial, percent, status, operation, speed, eta, error)
        }
        function onOperationStateChanged(state) {
            root.busy = String(state) !== "IDLE"
        }
        function onMessageChanged(message) {
            if (message)
                root.statusText = String(message)
        }
        function onOperationFinished(message) {
            root.statusText = String(message)
            root.refreshPackages()
        }
    }

    Component.onCompleted: root.refreshPackages()
}
