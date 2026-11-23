import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root

    property string selectedSerial: ""
    property string selectedDeviceName: ""
    property bool compatibilityChecksEnabled: appController
                                              ? appController.getOemCompatibilityChecksEnabled()
                                              : false
    property string reportText: "فعّل الفحص ثم حدّد هاتفاً متصلاً واضغط «فحص الجهاز»."

    function runDeviceCheck() {
        if (!appController || !root.compatibilityChecksEnabled || !root.selectedSerial.length)
            return

        var result = appController.inspectDeviceCompatibility(root.selectedSerial)
        if (!result || !result.ok) {
            root.reportText = String(result && result.message
                                     ? result.message : "تعذر إكمال فحص الجهاز.")
            return
        }

        var lines = []
        lines.push("نتيجة الفحص الفعلي — قراءة فقط")
        lines.push("الشركة: " + String(result.manufacturer || "غير معروف"))
        lines.push("الموديل: " + String(result.model || "Android"))
        lines.push("Android: " + String(result.android_release || "؟")
                   + " (SDK " + String(result.android_sdk || "؟") + ")")
        lines.push("اتصال ADB: مصرح ومتصل")
        lines.push("هوية ADB shell: " + String(result.adb_identity_label || "غير معروفة"))
        lines.push("تفاصيل الهوية: " + String(result.adb_shell_identity || "غير متاحة"))
        lines.push("تطبيق Shizuku: "
                   + (result.shizuku_installed === true ? "مثبت"
                      : result.shizuku_installed === false ? "غير مثبت" : "تعذر التحقق"))
        lines.push("خادم Shizuku: "
                   + (result.shizuku_server_running === true
                      ? "تم رصد PID " + String(result.shizuku_server_pid || "")
                      : "لم يُرصد؛ لا يعني ذلك وحده أنه متوقف"))
        lines.push("KafiaNet Bridge: "
                   + (result.helper_installed === true ? "مثبت"
                      : result.helper_installed === false ? "غير مثبت" : "تعذر التحقق"))

        if (String(result.oem_setting_name || "").length) {
            lines.push("إعداد الشركة الموثق: " + String(result.oem_setting_name))
            lines.push(String(result.oem_setting_source || ""))
        } else {
            lines.push("إعداد خاص بالشركة: لا يوجد مفتاح Xiaomi/OnePlus ينطبق على الشركة التي اكتشفها ADB.")
        }
        lines.push(String(result.oem_setting_note || ""))
        lines.push("لم يتم تغيير أي إعداد، ولم تُكتب ملفات على الهاتف.")
        root.reportText = lines.join("\n")
    }

    Rectangle {
        anchors.fill: parent
        color: "#070910"
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 22
        spacing: 14

        RowLayout {
            Layout.fillWidth: true
            Text {
                text: "الإعدادات"
                color: "#F5F8FC"
                font.pixelSize: 22
                font.bold: true
            }
            Item { Layout.fillWidth: true }
            Text {
                text: "KAFIA NET CONTROL PRO"
                color: "#73859B"
                font.pixelSize: 10
            }
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 148
            radius: 12
            color: "#101722"
            border.width: 1
            border.color: "#2B394B"

            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 16
                spacing: 8

                Text {
                    text: "فحص توافق الشركات الحقيقي"
                    color: "#66E8C1"
                    font.pixelSize: 16
                    font.bold: true
                }

                Switch {
                    id: oemCompatibilitySwitch
                    objectName: "oemCompatibilitySwitch"
                    text: "تفعيل فحص توافق ADB / Shizuku"
                    checked: root.compatibilityChecksEnabled
                    onToggled: {
                        root.compatibilityChecksEnabled = checked
                        if (appController)
                            appController.setOemCompatibilityChecksEnabled(checked)
                    }
                }

                Text {
                    Layout.fillWidth: true
                    text: "يفحص الهاتف المتصل فعلياً: الشركة والموديل وإصدار Android وهوية ADB ووجود Shizuku وBridge. لا يحاكي MIUI أو OxygenOS/ColorOS، ولا يغيّر إعدادات الحماية أو يكتب على الهاتف."
                    color: "#AAB8C8"
                    font.pixelSize: 11
                    wrapMode: Text.WordWrap
                }
            }
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 92
            radius: 10
            color: "#0D141F"
            border.width: 1
            border.color: "#34465B"

            RowLayout {
                anchors.fill: parent
                anchors.margins: 14
                spacing: 12

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 4
                    Text {
                        text: "الهاتف المحدد"
                        color: "#9AAEC2"
                        font.pixelSize: 10
                    }
                    Text {
                        text: root.selectedDeviceName.length
                              ? root.selectedDeviceName + "  •  " + root.selectedSerial
                              : "لا يوجد هاتف محدد. اختر هاتفاً من قائمة الأجهزة أولاً."
                        color: "#E6EDF6"
                        font.pixelSize: 12
                        wrapMode: Text.WordWrap
                    }
                }

                Button {
                    objectName: "runOemCompatibilityCheckButton"
                    text: "فحص الجهاز"
                    enabled: root.compatibilityChecksEnabled && root.selectedSerial.length > 0
                    onClicked: root.runDeviceCheck()
                }
            }
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.minimumHeight: 180
            radius: 10
            color: "#0B111A"
            border.width: 1
            border.color: "#2A394C"

            ScrollView {
                anchors.fill: parent
                anchors.margins: 14
                clip: true
                Text {
                    width: parent.width
                    text: root.reportText
                    color: "#D5E1EF"
                    font.pixelSize: 12
                    font.family: "Consolas"
                    wrapMode: Text.WordWrap
                }
            }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: 10

            Button {
                text: "توثيق Shizuku الرسمي"
                onClicked: Qt.openUrlExternally("https://shizuku.rikka.app/guide/setup/")
            }
            Button {
                text: "أدوات Android الرسمية"
                onClicked: Qt.openUrlExternally("https://developer.android.com/tools/releases/platform-tools")
            }
        }
    }
}
