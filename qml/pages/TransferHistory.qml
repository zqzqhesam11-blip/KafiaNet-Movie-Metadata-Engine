import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root

    property var entries: []
    property string periodMode: "today"
    property string previousPeriodMode: "today"
    property string customFromDate: ""
    property string customToDate: ""
    property int dateRefreshPulse: 0

    readonly property var visibleEntries: root.filterEntries()
    readonly property var successfulDevices: root.buildSuccessfulDevices(root.visibleEntries)
    readonly property int successfulDeviceUnits: successfulDevices.length

    readonly property int completedCount: {
        var count = 0
        for (var i = 0; i < visibleEntries.length; ++i) {
            if (visibleEntries[i].kind === "نسخ"
                    && visibleEntries[i].target_type !== "usb_storage"
                    && visibleEntries[i].status === "اكتمل")
                count += Math.max(1, Number(visibleEntries[i].units || 1))
        }
        return count
    }

    readonly property int usbCopiedFileCount: {
        var count = 0
        for (var i = 0; i < visibleEntries.length; ++i) {
            if (visibleEntries[i].target_type === "usb_storage")
                count += Math.max(0, Number(visibleEntries[i].copied_files || 0))
        }
        return count
    }

    readonly property int usbSkippedFileCount: {
        var count = 0
        for (var i = 0; i < visibleEntries.length; ++i) {
            if (visibleEntries[i].target_type === "usb_storage")
                count += Math.max(0, Number(visibleEntries[i].skipped_files || 0))
        }
        return count
    }

    readonly property int failedCount: {
        var count = 0
        for (var i = 0; i < visibleEntries.length; ++i) {
            if (visibleEntries[i].target_type !== "usb_storage"
                    && (visibleEntries[i].status === "فشل"
                        || visibleEntries[i].status === "غير مكتمل"
                        || visibleEntries[i].status === "فشل التثبيت"
                        || visibleEntries[i].status === "فشل إزالة التثبيت"))
                count += Math.max(1, Number(visibleEntries[i].units || 1))
        }
        return count
    }

    readonly property int reviewCount: {
        var count = 0
        for (var i = 0; i < visibleEntries.length; ++i) {
            if (visibleEntries[i].kind === "نسخ"
                    && visibleEntries[i].target_type !== "usb_storage"
                    && visibleEntries[i].status === "مراجعة")
                count += Math.max(1, Number(visibleEntries[i].units || 1))
        }
        return count
    }

    readonly property int insufficientSpaceCount: {
        var count = 0
        for (var i = 0; i < visibleEntries.length; ++i) {
            if (visibleEntries[i].kind === "نسخ"
                    && visibleEntries[i].target_type !== "usb_storage"
                    && visibleEntries[i].status === "مساحة غير كافية")
                count += Math.max(1, Number(visibleEntries[i].units || 1))
        }
        return count
    }

    function twoDigits(value) {
        return value < 10 ? "0" + value : String(value)
    }

    function localDateKey(date) {
        return date.getFullYear() + "-"
                + twoDigits(date.getMonth() + 1) + "-"
                + twoDigits(date.getDate())
    }

    function isValidDateKey(value) {
        var match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value || "").trim())
        if (!match)
            return false
        var year = Number(match[1])
        var month = Number(match[2])
        var day = Number(match[3])
        var date = new Date(year, month - 1, day)
        return date.getFullYear() === year
                && date.getMonth() === month - 1
                && date.getDate() === day
    }

    function selectedDateRange() {
        var pulse = root.dateRefreshPulse
        var today = new Date()
        today.setHours(0, 0, 0, 0)
        var endDate = new Date(today.getTime())
        var startDate = new Date(today.getTime())

        if (root.periodMode === "yesterday") {
            startDate.setDate(startDate.getDate() - 1)
            endDate = new Date(startDate.getTime())
        } else if (root.periodMode === "last7") {
            startDate.setDate(startDate.getDate() - 6)
        } else if (root.periodMode === "last2months") {
            var targetMonth = new Date(today.getFullYear(), today.getMonth() - 2, 1)
            var lastDay = new Date(
                targetMonth.getFullYear(), targetMonth.getMonth() + 1, 0
            ).getDate()
            targetMonth.setDate(Math.min(today.getDate(), lastDay))
            startDate = targetMonth
        } else if (root.periodMode === "custom"
                   && root.isValidDateKey(root.customFromDate)
                   && root.isValidDateKey(root.customToDate)) {
            return {
                start: root.customFromDate,
                end: root.customToDate
            }
        }

        return {
            start: root.localDateKey(startDate),
            end: root.localDateKey(endDate)
        }
    }

    function filterEntries() {
        var range = root.selectedDateRange()
        var filtered = []
        for (var i = 0; i < root.entries.length; ++i) {
            var entry = root.entries[i]
            var dateKey = String(entry.date || "").slice(0, 10)
            if (dateKey && dateKey >= range.start && dateKey <= range.end)
                filtered.push(entry)
        }
        return filtered
    }

    function buildSuccessfulDevices(history) {
        var records = []
        var source = history || []
        for (var i = 0; i < source.length; ++i) {
            var entry = source[i]
            if (entry.kind !== "نسخ"
                    || entry.target_type === "usb_storage"
                    || (entry.status !== "اكتمل" && entry.status !== "مراجعة"))
                continue

            var serial = String(entry.serial || "").trim()
            if (!serial)
                continue

            var recordIndex = -1
            for (var j = 0; j < records.length; ++j) {
                if (records[j].serial === serial) {
                    recordIndex = j
                    break
                }
            }

            var entryDate = String(entry.date || "")
            var deviceName = String(entry.device_name || entry.device || serial)
            var units = Math.max(1, Number(entry.units || 1))
            if (recordIndex < 0) {
                records.push({
                    serial: serial,
                    name: deviceName,
                    successful_copies: units,
                    last_success_at: entryDate
                })
            } else {
                records[recordIndex].successful_copies += units
                if (entryDate >= String(records[recordIndex].last_success_at || "")) {
                    records[recordIndex].last_success_at = entryDate
                    records[recordIndex].name = deviceName
                }
            }
        }

        records.sort(function(left, right) {
            return String(right.last_success_at || "")
                    .localeCompare(String(left.last_success_at || ""))
        })
        return records
    }

    function periodIndexForMode() {
        if (root.periodMode === "yesterday") return 1
        if (root.periodMode === "last7") return 2
        if (root.periodMode === "last2months") return 3
        if (root.periodMode === "custom") return 4
        return 0
    }

    function activePeriodName() {
        if (root.periodMode === "yesterday") return "أمس"
        if (root.periodMode === "last7") return "آخر 7 أيام"
        if (root.periodMode === "last2months") return "آخر شهرين"
        if (root.periodMode === "custom") return "فترة مخصصة"
        return "اليوم"
    }

    function openCustomDateDialog() {
        root.previousPeriodMode = root.periodMode
        var today = root.localDateKey(new Date())
        customFromDateField.text = root.customFromDate || today
        customToDateField.text = root.customToDate || today
        customDateError.visible = false
        root.periodMode = "custom"
        customDateDialog.open()
    }

    function restorePreviousPeriod() {
        root.periodMode = root.previousPeriodMode
        periodSelector.currentIndex = root.periodIndexForMode()
        customDateError.visible = false
    }

    function applyCustomDateRange() {
        var fromDate = String(customFromDateField.text || "").trim()
        var toDate = String(customToDateField.text || "").trim()
        if (!root.isValidDateKey(fromDate)
                || !root.isValidDateKey(toDate)
                || fromDate > toDate) {
            customDateError.visible = true
            return false
        }

        root.customFromDate = fromDate
        root.customToDate = toDate
        root.periodMode = "custom"
        periodSelector.currentIndex = 4
        customDateError.visible = false
        customDateDialog.close()
        return true
    }

    function statusColor(status) {
        if (status === "اكتمل" || status === "اكتمل مع تخطي"
                || status === "تم التخطي" || status === "مراجعة"
                || status === "تمت المعالجة")
            return "#42D995"
        if (status === "مساحة غير كافية")
            return "#FFC56E"
        if (status === "فشل" || status === "غير مكتمل"
                || status === "فشل التثبيت" || status === "فشل إزالة التثبيت")
            return "#FF8C8C"
        if (status === "تم التثبيت" || status === "تمت إزالة التثبيت")
            return "#67C7F2"
        return "#AAB6C8"
    }

    function refreshHistory() {
        if (appController && appController.getTransferHistory)
            root.entries = appController.getTransferHistory()
    }

    Rectangle {
        anchors.fill: parent
        color: "#070910"
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 26
        spacing: 14

        RowLayout {
            Layout.fillWidth: true
            Layout.preferredHeight: 44
            spacing: 12

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 2

                Text {
                    text: "سجل النقل"
                    color: "#FFFFFF"
                    font.pixelSize: 23
                    font.bold: true
                }

                Text {
                    text: "سجل عمليات النسخ والتثبيت وإزالة الألعاب"
                    color: "#7F8A9D"
                    font.pixelSize: 11
                }
            }

            RowLayout {
                spacing: 8

                Text {
                    text: "الفترة"
                    color: "#AAB6C8"
                    font.pixelSize: 11
                }

                ComboBox {
                    id: periodSelector
                    Layout.preferredWidth: 170
                    model: ["اليوم", "أمس", "آخر 7 أيام", "آخر شهرين", "مخصص"]
                    currentIndex: root.periodIndexForMode()
                    onActivated: function(index) {
                        if (index === 4) {
                            root.openCustomDateDialog()
                        } else {
                            root.periodMode = ["today", "yesterday", "last7", "last2months"][index]
                        }
                    }
                }
            }

            Button {
                text: "تحديث"
                Layout.preferredHeight: 36
                onClicked: root.refreshHistory()
            }
        }

        Text {
            Layout.fillWidth: true
            text: "الفترة المعروضة: " + root.activePeriodName()
                  + " • " + root.selectedDateRange().start
                  + " إلى " + root.selectedDateRange().end
                  + " • " + root.visibleEntries.length + " عملية"
            color: "#77869B"
            font.pixelSize: 10
        }

        RowLayout {
            Layout.fillWidth: true
            Layout.preferredHeight: 82
            spacing: 10

            Rectangle {
                Layout.fillWidth: true
                Layout.fillHeight: true
                radius: 12
                color: "#111722"
                border.width: 1
                border.color: "#245C49"
                Column {
                    anchors.fill: parent
                    anchors.margins: 12
                    spacing: 4
                    Text { text: "نسخ مكتملة"; color: "#91A29E"; font.pixelSize: 10 }
                    Text { text: root.completedCount; color: "#42D995"; font.pixelSize: 23; font.bold: true }
                }
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.fillHeight: true
                radius: 12
                color: "#111722"
                border.width: 1
                border.color: "#713843"
                Column {
                    anchors.fill: parent
                    anchors.margins: 12
                    spacing: 4
                    Text { text: "عمليات فاشلة"; color: "#A99A9E"; font.pixelSize: 10 }
                    Text { text: root.failedCount; color: "#FF8C8C"; font.pixelSize: 23; font.bold: true }
                }
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.fillHeight: true
                radius: 12
                color: "#111722"
                border.width: 1
                border.color: "#28516A"
                Column {
                    anchors.fill: parent
                    anchors.margins: 12
                    spacing: 4
                    Text { text: "مراجعة"; color: "#91A7B4"; font.pixelSize: 10 }
                    Text { text: root.reviewCount; color: "#67C7F2"; font.pixelSize: 23; font.bold: true }
                }
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.fillHeight: true
                radius: 12
                color: "#111722"
                border.width: 1
                border.color: "#765A2B"
                Column {
                    anchors.fill: parent
                    anchors.margins: 12
                    spacing: 4
                    Text { text: "مساحة غير كافية"; color: "#B7A58A"; font.pixelSize: 10 }
                    Text { text: root.insufficientSpaceCount; color: "#FFC56E"; font.pixelSize: 23; font.bold: true }
                }
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.fillHeight: true
                radius: 12
                color: "#111722"
                border.width: 1
                border.color: "#245C49"
                Column {
                    anchors.fill: parent
                    anchors.margins: 12
                    spacing: 4
                    Text {
                        text: "الفلاشة: ملفات جديدة"
                        color: "#91A29E"
                        font.pixelSize: 10
                    }
                    Text {
                        text: root.usbCopiedFileCount + "  |  تخطي " + root.usbSkippedFileCount
                        color: "#42D995"
                        font.pixelSize: 18
                        font.bold: true
                    }
                }
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.fillHeight: true
                radius: 12
                color: "#111722"
                border.width: 1
                border.color: "#245C49"
                Column {
                    anchors.fill: parent
                    anchors.margins: 12
                    spacing: 4
                    Text {
                        text: "هواتف ناجحة"
                        color: "#91A29E"
                        font.pixelSize: 10
                    }
                    Text {
                        text: root.successfulDeviceUnits
                        color: "#42D995"
                        font.pixelSize: 23
                        font.bold: true
                    }
                }
            }
        }

        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.minimumHeight: 260
            spacing: 12

            Rectangle {
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.minimumWidth: 300
                Layout.preferredWidth: 1
                radius: 12
                color: "#0E141E"
                border.width: 1
                border.color: "#263548"

                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 12
                    spacing: 8

                    Text {
                        Layout.fillWidth: true
                        text: "حالة الأجهزة والعمليات"
                        color: "#00D9FF"
                        font.pixelSize: 13
                        font.bold: true
                    }

                    Text {
                        Layout.fillWidth: true
                        text: "النسخ والتثبيت والإزالة والعمليات الفاشلة"
                        color: "#77869B"
                        font.pixelSize: 10
                    }

                    ListView {
                        id: historyList
                        objectName: "transferHistoryStatusList"
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        clip: true
                        spacing: 7
                        model: root.visibleEntries
                        boundsBehavior: Flickable.StopAtBounds
                        reuseItems: true

                        ScrollBar.vertical: ScrollBar {
                            policy: ScrollBar.AsNeeded
                            width: 9
                        }

                        delegate: Rectangle {
                            width: historyList.width
                            height: 82
                            radius: 9
                            color: "#111722"
                            border.width: 1
                            border.color: root.statusColor(modelData.status)

                            RowLayout {
                                anchors.fill: parent
                                anchors.margins: 10
                                spacing: 9

                                ColumnLayout {
                                    Layout.fillWidth: true
                                    spacing: 3

                                    Text {
                                        Layout.fillWidth: true
                                        text: String(modelData.device_name || modelData.device || "Android")
                                              + "  •  " + String(modelData.game || "")
                                              + "  •  " + String(modelData.serial || "")
                                        color: "#EDF3FA"
                                        font.pixelSize: 11
                                        font.bold: true
                                        elide: Text.ElideRight
                                    }

                                    Text {
                                        Layout.fillWidth: true
                                        text: String(modelData.kind || "")
                                              + (modelData.game_size ? " • " + modelData.game_size : "")
                                              + (modelData.target_type === "usb_storage"
                                                 ? " • فلاشة • جديد: " + String(modelData.copied_files || 0)
                                                   + " • تخطي: " + String(modelData.skipped_files || 0)
                                                 : modelData.kind === "نسخ"
                                                   ? " • هاتف • " + String(modelData.units || 1)
                                                     + " وحدة"
                                                   : modelData.kind === "نسخ من الهاتف"
                                                     ? " • استرجاع إلى الكمبيوتر"
                                                     : "")
                                              + "  |  " + String(modelData.date || "").replace("T", " ")
                                        color: "#8996AA"
                                        font.pixelSize: 9
                                        elide: Text.ElideRight
                                    }

                                    Text {
                                        Layout.fillWidth: true
                                        text: String(modelData.message || "")
                                        color: "#AAB6C8"
                                        font.pixelSize: 9
                                        elide: Text.ElideRight
                                    }
                                }

                                ColumnLayout {
                                    Layout.preferredWidth: 90
                                    spacing: 4

                                    Text {
                                        Layout.fillWidth: true
                                        text: String(modelData.source || "")
                                        color: "#C5D0DF"
                                        font.pixelSize: 9
                                        horizontalAlignment: Text.AlignHCenter
                                        elide: Text.ElideRight
                                    }

                                    Text {
                                        Layout.fillWidth: true
                                        text: String(modelData.status || "")
                                        color: root.statusColor(modelData.status)
                                        font.pixelSize: 10
                                        font.bold: true
                                        horizontalAlignment: Text.AlignHCenter
                                        wrapMode: Text.Wrap
                                    }
                                }
                            }
                        }
                    }

                    Text {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        visible: root.visibleEntries.length === 0
                        text: "لا توجد عمليات مسجلة حتى الآن"
                        color: "#6E7A8E"
                        font.pixelSize: 12
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }
                }
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.minimumWidth: 210
                Layout.preferredWidth: 1
                radius: 12
                color: "#0E141E"
                border.width: 1
                border.color: "#285642"

                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 12
                    spacing: 8

                    Text {
                        Layout.fillWidth: true
                        text: "الهواتف الناجحة خلال الفترة"
                              + "  (" + root.successfulDeviceUnits + ")"
                        color: "#42D995"
                        font.pixelSize: 13
                        font.bold: true
                    }

                    Text {
                        Layout.fillWidth: true
                        text: "أجهزة اكتمل نسخها ضمن التاريخ المحدد"
                        color: "#77869B"
                        font.pixelSize: 10
                    }

                    ListView {
                        id: successfulDeviceList
                        objectName: "successDevicesVerticalList"
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        clip: true
                        spacing: 7
                        model: root.successfulDevices
                        boundsBehavior: Flickable.StopAtBounds

                        ScrollBar.vertical: ScrollBar {
                            policy: ScrollBar.AsNeeded
                            width: 9
                        }

                        delegate: Rectangle {
                            width: successfulDeviceList.width
                            height: 48
                            radius: 9
                            color: "#15372F"
                            border.width: 1
                            border.color: "#24644D"

                            RowLayout {
                                anchors.fill: parent
                                anchors.leftMargin: 10
                                anchors.rightMargin: 10
                                spacing: 8

                                Rectangle {
                                    Layout.preferredWidth: 7
                                    Layout.preferredHeight: 7
                                    radius: 4
                                    color: "#42D995"
                                }

                                Text {
                                    Layout.fillWidth: true
                                    text: String(modelData.name || "Android")
                                          + " • " + String(modelData.successful_copies || 1)
                                          + " نسخ ناجحة"
                                    color: "#42D995"
                                    font.pixelSize: 11
                                    font.bold: true
                                    elide: Text.ElideRight
                                    verticalAlignment: Text.AlignVCenter
                                }

                                Text {
                                    text: "ناجح"
                                    color: "#9BE5C5"
                                    font.pixelSize: 9
                                }
                            }
                        }
                    }

                    Text {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        visible: root.successfulDevices.length === 0
                        text: "لا توجد هواتف ناجحة في هذه الفترة"
                        color: "#657286"
                        font.pixelSize: 12
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }
                }
            }
        }

    Dialog {
        id: customDateDialog
        modal: true
        focus: true
        title: "تحديد فترة مخصصة"
        width: Math.min(460, root.width - 32)
        x: Math.max(16, (root.width - width) / 2)
        y: Math.max(16, (root.height - height) / 2)
        standardButtons: Dialog.NoButton

        background: Rectangle {
            radius: 12
            color: "#111722"
            border.width: 1
            border.color: "#28516A"
        }

        contentItem: ColumnLayout {
            spacing: 12

            Text {
                Layout.fillWidth: true
                text: "أدخل تاريخ البداية والنهاية بصيغة سنة-شهر-يوم (YYYY-MM-DD)."
                color: "#DCE4F0"
                font.pixelSize: 12
                wrapMode: Text.Wrap
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 8

                Text {
                    text: "من"
                    color: "#AAB6C8"
                    font.pixelSize: 11
                }

                TextField {
                    id: customFromDateField
                    Layout.fillWidth: true
                    placeholderText: "YYYY-MM-DD"
                    horizontalAlignment: TextInput.AlignHCenter
                }
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 8

                Text {
                    text: "إلى"
                    color: "#AAB6C8"
                    font.pixelSize: 11
                }

                TextField {
                    id: customToDateField
                    Layout.fillWidth: true
                    placeholderText: "YYYY-MM-DD"
                    horizontalAlignment: TextInput.AlignHCenter
                }
            }

            Text {
                id: customDateError
                Layout.fillWidth: true
                visible: false
                text: "التاريخ غير صحيح أو تاريخ البداية بعد تاريخ النهاية."
                color: "#FF7777"
                font.pixelSize: 11
                horizontalAlignment: Text.AlignHCenter
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 8

                Button {
                    Layout.fillWidth: true
                    text: "إلغاء"
                    onClicked: {
                        root.restorePreviousPeriod()
                        customDateDialog.close()
                    }
                }

                Button {
                    Layout.fillWidth: true
                    text: "عرض السجل"
                    onClicked: root.applyCustomDateRange()
                }
            }
        }

        onRejected: root.restorePreviousPeriod()
    }

    Connections {
        target: appController
        function onTransferHistoryChanged(history) {
            root.entries = history || []
        }
    }

    Timer {
        interval: 60000
        running: true
        repeat: true
        onTriggered: root.dateRefreshPulse += 1
    }

    Component.onCompleted: root.refreshHistory()
}
}
