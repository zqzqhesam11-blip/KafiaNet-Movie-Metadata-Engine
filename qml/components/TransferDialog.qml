import QtQuick
import QtQuick.Controls

Rectangle {

    id: root

    property string gameName: ""
    property bool transferPaused: false

    color: "#F20A0D14"

    radius: 20

    border.width: 1
    border.color: "#293142"


    Column {

        anchors.centerIn: parent

        width: parent.width - 70

        spacing: 18


        Text {

            width: parent.width

            text: "نقل الملفات"

            color: "white"

            font.pixelSize: 26

            font.bold: true

            horizontalAlignment: Text.AlignRight
        }


        Text {

            width: parent.width

            text: root.gameName

            color: "#00D9FF"

            font.pixelSize: 18

            horizontalAlignment: Text.AlignRight
        }


        Rectangle {

            width: parent.width

            height: 12

            radius: 6

            color: "#252B38"


            Rectangle {

                width: parent.width * transferPercent / 100

                height: parent.height

                radius: 6

                color: "#00D9FF"
            }
        }


        Text {

            width: parent.width

            text: transferPercent + "%"

            color: "white"

            font.pixelSize: 30

            font.bold: true

            horizontalAlignment: Text.AlignHCenter
        }


        Row {

            width: parent.width

            spacing: 25

            layoutDirection: Qt.RightToLeft


            Text {
                text: "السرعة: " + transferSpeed
                color: "#B9C0CF"
            }

            Text {
                text: "متبقي: " + transferEta
                color: "#B9C0CF"
            }
        }


        Row {

            anchors.horizontalCenter: parent.horizontalCenter

            spacing: 10


            Button {

                text: root.transferPaused
                      ? "استئناف"
                      : "إيقاف مؤقت"

                width: 120

                onClicked: {

                    if (root.transferPaused) {

                        appController.resumeTransfer()
                        root.transferPaused = false

                    } else {

                        appController.pauseTransfer()
                        root.transferPaused = true
                    }
                }
            }


            Button {

                text: "إلغاء"

                width: 90

                onClicked: {

                    appController.cancelTransfer()
                    root.transferPaused = false
                }
            }
        }
    }
}
