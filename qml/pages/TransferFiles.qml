import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root

    property var games: [
        "Fortnite",
        "Genshin Impact",
        "eFootball",
        "Free Fire",
        "PUBG Mobile",
        "PUBG Mobile Flash",
        "Call of Duty Mobile"
    ]
    property string statusText: "حدد هاتفاً من زر الأجهزة أعلى النافذة، ثم اختر اللعبة."
    property int progress: 0
    property var progressByDevice: ({})

    function updateDeviceProgress(serial, percent, status, operation, speed, eta, error) {
        var next = Object.assign({}, root.progressByDevice)
        next[String(serial)] = Math.max(0, Math.min(100, Number(percent || 0)))
        root.progressByDevice = next

        var keys = Object.keys(next)
        var total = 0
        for (var i = 0; i < keys.length; ++i)
            total += Number(next[keys[i]] || 0)
        root.progress = keys.length > 0 ? Math.round(total / keys.length) : 0
        if (status)
            root.statusText = String(status)
        if (error)
            root.statusText = String(error)
    }

    Rectangle {
        anchors.fill: parent
        color: "#070910"
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 24
        spacing: 14

        RowLayout {
            Layout.fillWidth: true
            spacing: 12

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 3

                Text {
                    text: "نسخ الملفات من الهاتف إلى الكمبيوتر"
                    color: "#FFFFFF"
                    font.pixelSize: 22
                    font.bold: true
                }

                Text {
                    text: "يتم سحب ملفات اللعبة من مسارها الفعلي على الهاتف وحفظها باسم ومسار المصدر المقابل."
                    color: "#8290A4"
                    font.pixelSize: 11
                    wrapMode: Text.Wrap
                }
            }

            Rectangle {
                radius: 10
                color: "#10251F"
                border.color: "#285642"
                border.width: 1
                Layout.preferredWidth: 185
                Layout.preferredHeight: 58

                Column {
                    anchors.centerIn: parent
                    spacing: 3
                    Text {
                        text: "وجهة الكمبيوتر"
                        color: "#91A29E"
                        font.pixelSize: 10
                        anchors.horizontalCenter: parent.horizontalCenter
                    }
                    Text {
                        text: "D:\\games\\من الهاتف"
                        color: "#42D995"
                        font.pixelSize: 12
                        font.bold: true
                        anchors.horizontalCenter: parent.horizontalCenter
                    }
                }
            }
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 58
            radius: 10
            color: "#101722"
            border.color: "#28384C"
            border.width: 1

            RowLayout {
                anchors.fill: parent
                anchors.margins: 12
                spacing: 10

                Text {
                    Layout.fillWidth: true
                    text: root.statusText
                    color: "#C5D0DF"
                    font.pixelSize: 11
                    wrapMode: Text.Wrap
                    verticalAlignment: Text.AlignVCenter
                }

                Text {
                    text: appController
                          ? "الأجهزة/الوحدات المحددة: " + appController.getSelectedTargetUnits()
                          : "لا توجد أجهزة محددة"
                    color: "#00D9FF"
                    font.pixelSize: 11
                    font.bold: true
                }
            }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: 10

            Text {
                text: "تقدم النسخ"
                color: "#91A29E"
                font.pixelSize: 11
            }

            ProgressBar {
                Layout.fillWidth: true
                from: 0
                to: 100
                value: root.progress
            }

            Text {
                text: root.progress + "%"
                color: "#42D995"
                font.pixelSize: 12
                font.bold: true
            }
        }

        Text {
            text: "اختر اللعبة للاسترجاع — مسار الهاتف هو نفسه وجهة النسخ إليه"
            color: "#00D9FF"
            font.pixelSize: 13
            font.bold: true
        }

        ListView {
            id: gameList
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            spacing: 8
            model: root.games
            boundsBehavior: Flickable.StopAtBounds

            ScrollBar.vertical: ScrollBar {
                policy: ScrollBar.AsNeeded
                width: 8
            }

            delegate: Rectangle {
                width: gameList.width
                height: 88
                radius: 11
                color: "#101722"
                border.width: 1
                border.color: "#2B3B50"

                RowLayout {
                    anchors.fill: parent
                    anchors.margins: 12
                    spacing: 12

                    Rectangle {
                        Layout.preferredWidth: 38
                        Layout.preferredHeight: 38
                        radius: 9
                        color: "#17313A"
                        Text {
                            anchors.centerIn: parent
                            text: "↓"
                            color: "#00D9FF"
                            font.pixelSize: 22
                            font.bold: true
                        }
                    }

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 3

                        Text {
                            Layout.fillWidth: true
                            text: String(modelData)
                            color: "#EDF3FA"
                            font.pixelSize: 13
                            font.bold: true
                        }

                        Text {
                            Layout.fillWidth: true
                            text: "من الهاتف: "
                                  + (appController
                                     ? appController.getPhoneGameDestination(String(modelData))
                                     : "")
                            color: "#8290A4"
                            font.pixelSize: 9
                            elide: Text.ElideMiddle
                        }

                        Text {
                            Layout.fillWidth: true
                            text: "إلى الكمبيوتر: "
                                  + (appController
                                     ? appController.getPullLocalDestination(String(modelData))
                                     : "")
                            color: "#42D995"
                            font.pixelSize: 9
                            elide: Text.ElideMiddle
                        }
                    }

                    Button {
                        Layout.preferredWidth: 128
                        Layout.preferredHeight: 40
                        text: "نسخ من الهاتف"
                        onClicked: {
                            if (appController)
                                appController.pullGame(String(modelData))
                        }
                    }
                }
            }
        }

        Text {
            Layout.fillWidth: true
            text: "حدد هاتفاً واحداً فقط قبل الاسترجاع. تستخدم هذه العملية ADB لسحب البيانات إلى الكمبيوتر، ولا ترفع أو تحذف ملفات من الهاتف."
            color: "#68788E"
            font.pixelSize: 10
            wrapMode: Text.Wrap
        }
    }

    Connections {
        target: appController
        function onDeviceProgressChanged(serial, percent, status, operation, speed, eta, error) {
            root.updateDeviceProgress(serial, percent, status, operation, speed, eta, error)
        }
        function onMessageChanged(message) {
            if (message)
                root.statusText = String(message)
        }
        function onOperationFinished(message) {
            root.statusText = String(message)
        }
    }
}
