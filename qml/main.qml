import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "pages"

ApplicationWindow {

    id: window
    objectName: "mainWindow"

    visible: true

    width: 1500
    height: 900

    minimumWidth: 1100
    minimumHeight: 700

    title: "CN PlayHub"

    color: "#070910"

    property string activePage: "الألعاب"
    property bool sidebarCollapsed: false
    property int sidebarWidth: sidebarCollapsed ? 78 : 240

    property bool appUnlocked: false

    property bool wirelessConnected: true
    property bool usbReady: false
    property bool customSourceReady: false
    property string customSourcePath: ""
    property string activeSource: "WIRELESS"
    property string pubgSourceSize: "Saved"

    property int totalGames: 0
    property int transferPercent: 0
    property int transferCompletedCount: appController && appController.getTransferSummary
                                         ? Number(appController.getTransferSummary().completedToday || 0)
                                         : 0
    property string transferSpeed: "0 B/s"
    property string transferEta: "00:00"

    // =============================================================
    // ANDROID / ADB DEVICE STATE
    // =============================================================

    property bool adbDeviceConnected: false
    function deviceUnitsForSerials(serials) {
        var total = 0
        var selected = serials || []
        for (var i = 0; i < selected.length; ++i) {
            var target = null
            for (var j = 0; j < window.adbDevices.length; ++j) {
                if (window.adbDevices[j].serial === selected[i]) {
                    target = window.adbDevices[j]
                    break
                }
            }
            if (target)
                total += Math.max(1, Number(target.units || 1))
        }
        return total
    }

    function refreshDashboardGameCount() {
        if (!appController || !appController.getDashboardGames) {
            window.totalGames = 0
            return
        }
        try {
            var discoveredGames = appController.getDashboardGames() || []
            window.totalGames = discoveredGames.length
        } catch (error) {
            console.warn("Could not refresh dashboard game count:", error)
        }
    }

    function refreshDailyTransferCount() {
        if (!appController || !appController.getTransferSummary)
            return
        try {
            var summary = appController.getTransferSummary() || {}
            window.transferCompletedCount = Number(summary.completedToday || 0)
        } catch (error) {
            console.warn("Could not refresh today's transfer count:", error)
        }
    }

    function updateAdbDeviceStatus() {
        var availableUnits = window.deviceUnitsForSerials(
            window.adbDevices.map(function(item) { return item.serial })
        )
        var selectedUnits = window.deviceUnitsForSerials(window.selectedAdbSerials)
        window.adbDeviceConnected = window.adbDevices.length > 0
        if (window.adbDevices.length === 0) {
            window.adbDeviceText = "لا توجد هواتف أو فلاشات متصلة"
        } else {
            window.adbDeviceText = "المتاح: " + availableUnits
                    + " وحدات — المحدد: " + selectedUnits + " وحدات"
        }
    }

    property string adbDeviceText: "لا يوجد جهاز متصل"
    property var adbDevices: []
    property var selectedAdbSerials: []
    property bool operationBusy: false
    property bool operationPaused: false
    property var transferDevices: []
    property bool transferPanelDismissed: false
    property bool transferPanelCollapsed: false
    property bool transferCompletedCountVisible: true

    // Full-screen guard is an application-level kiosk-style lock.
    property bool fullscreenLocked: false
    property bool fullscreenGuardEnabled: false
    property bool pendingFullscreenUnlock: false
    property bool pendingFullscreenClose: false
    property bool wasMaximizedBeforeLock: false
    property real savedWindowX: 0
    property real savedWindowY: 0
    property real savedWindowWidth: 1500
    property real savedWindowHeight: 900

    function enterFullscreenLock() {
        if (fullscreenLocked) {
            askWindowUnlock()
            return
        }

        // Fullscreen is the primary UI action. Native Windows protections
        // are an additional layer and must not block the fullscreen transition.
        var guardReady = Boolean(
            appController && appController.setFullscreenProtection(true)
        )
        // Do not present this as a protected fullscreen lock if Windows did not
        // confirm the native guard. The controller already explains the failure.
        if (!guardReady) {
            fullscreenGuardEnabled = false
            console.warn(
                "FULLSCREEN LOCK: Windows protection was not confirmed; "
                + "refusing to enter the locked fullscreen state."
            )
            return
        }
        fullscreenGuardEnabled = true

        wasMaximizedBeforeLock = visibility === Window.Maximized
        savedWindowX = x
        savedWindowY = y
        savedWindowWidth = width
        savedWindowHeight = height
        fullscreenLocked = true
        // Keep the normal decorated Qt window flags intact. showFullScreen()
        // changes only its visibility state; showNormal() can then restore
        // the native caption, close button, and taskbar without recreating HWND.
        Qt.callLater(function() {
            window.showFullScreen()
            window.requestActivate()
        })
    }

    function openFullscreenPasswordDialog(closeApplicationOnSuccess) {
        if (windowUnlockDialog.visible) {
            if (closeApplicationOnSuccess)
                pendingFullscreenClose = true
            return
        }

        pendingFullscreenClose = Boolean(closeApplicationOnSuccess)
        windowUnlockPassword.text = ""
        windowUnlockError.visible = false
        windowUnlockDialog.open()
        Qt.callLater(function() {
            windowUnlockPassword.forceActiveFocus()
        })
    }

    function askWindowUnlock() {
        openFullscreenPasswordDialog(false)
    }

    function handleWindowUnlock() {
        if (pendingFullscreenUnlock)
            return

        if (appController && appController.verifyWindowPassword(windowUnlockPassword.text)) {
            windowUnlockError.visible = false
            pendingFullscreenUnlock = true
            // Wait for the modal popup's closed signal before changing
            // the window state; closing the root window too early can race
            // the popup transition.
            windowUnlockDialog.close()
        } else {
            windowUnlockError.visible = true
            windowUnlockPassword.selectAll()
            windowUnlockPassword.forceActiveFocus()
        }
    }

    function leaveFullscreenLock(closeApplicationAfterUnlock) {
        if (!fullscreenLocked)
            return

        // Do not mutate Window.flags or hide/recreate the native window.
        // Switching visibility state preserves Windows caption controls.
        // Restore the native taskbar protection only after Qt has left
        // fullscreen, otherwise the fullscreen HWND can cover the taskbar
        // during the restore operation.
        fullscreenLocked = false
        Qt.callLater(function() {
            window.showNormal()
            window.x = savedWindowX
            window.y = savedWindowY
            window.width = savedWindowWidth
            window.height = savedWindowHeight

            if (wasMaximizedBeforeLock)
                window.showMaximized()

            window.raise()
            window.requestActivate()

            Qt.callLater(function() {
                if (appController && fullscreenGuardEnabled)
                    appController.setFullscreenProtection(false)
                fullscreenGuardEnabled = false
                if (closeApplicationAfterUnlock)
                    window.close()
            })
        })
    }

    onClosing: function(closeEvent) {
        // Normal window closing is deliberately password-free. While the
        // fullscreen lock is active, however, closing is treated as a
        // protected exit and the close event must wait for the password.
        if (fullscreenLocked) {
            closeEvent.accepted = false
            openFullscreenPasswordDialog(true)
            return
        }

        closeEvent.accepted = true
        if (appController)
            appController.releaseFullscreenProtection()
    }

    onVisibilityChanged: {
        if (fullscreenLocked && visibility !== Window.FullScreen) {
            Qt.callLater(function() {
                if (window.fullscreenLocked
                        && window.visibility !== Window.FullScreen)
                    window.showFullScreen()
            })
        }
    }

    onActiveChanged: {
        if (fullscreenLocked && active) {
            Qt.callLater(function() {
                if (!window.fullscreenLocked)
                    return
                window.showFullScreen()
                if (appController && window.fullscreenGuardEnabled)
                    appController.setFullscreenProtection(true)
            })
        }
    }

    // Stable model: update existing rows in place so progress ticks do not
    // reset ListView's model or jump the user's scroll position.
    ListModel {
        id: transferDevicesModel
    }

    function syncTransferDeviceModel(devices) {
        const incomingRows = devices || []
        const desiredSerials = []

        for (let i = 0; i < incomingRows.length; ++i) {
            const incoming = incomingRows[i]
            const serial = String(incoming.serial || "")
            if (serial.length === 0)
                continue

            desiredSerials.push(serial)
            const rowData = {
                serial: serial,
                deviceModel: String(incoming.model || "Android"),
                game: String(incoming.game || ""),
                kind: String(incoming.kind || ""),
                status: String(incoming.status || "انتظار"),
                percent: Number(incoming.percent === undefined ? 0 : incoming.percent),
                operation: String(incoming.operation || ""),
                speed: String(incoming.speed || "--"),
                eta: String(incoming.eta || "--"),
                error: String(incoming.error || ""),
                cancelled: Boolean(incoming.cancelled),
                completed: Boolean(incoming.completed),
                awaitingFallback: Boolean(incoming.awaitingFallback)
            }

            let rowIndex = -1
            for (let j = 0; j < transferDevicesModel.count; ++j) {
                if (transferDevicesModel.get(j).serial === serial) {
                    rowIndex = j
                    break
                }
            }

            if (rowIndex < 0) {
                transferDevicesModel.append(rowData)
            } else {
                const roleNames = Object.keys(rowData)
                const existingRow = transferDevicesModel.get(rowIndex)
                for (let k = 0; k < roleNames.length; ++k) {
                    const roleName = roleNames[k]
                    if (existingRow[roleName] !== rowData[roleName]) {
                        transferDevicesModel.setProperty(
                            rowIndex, roleName, rowData[roleName]
                        )
                    }
                }
            }
        }

        // Remove only devices that are no longer in the controller's snapshot.
        // Ordinary percentage/speed updates never clear or replace the model.
        for (let i = transferDevicesModel.count - 1; i >= 0; --i) {
            const serial = transferDevicesModel.get(i).serial
            if (desiredSerials.indexOf(serial) < 0)
                transferDevicesModel.remove(i)
        }
    }
    property string operationMessage: ""
    property string deviceCopyHelpTitle: "إرشادات إصلاح النسخ"
    property string deviceCopyHelpText: ""

    property var androidDataFallbackQueue: []
    property bool androidDataFallbackDialogActive: false
    property string androidDataFallbackSerial: ""
    property string androidDataFallbackGame: ""
    property string androidDataFallbackDestination: ""
    property string androidDataFallbackReason: ""

    function queueAndroidDataFallback(serial, gameName, destination, reason) {
        const queue = window.androidDataFallbackQueue.slice()
        queue.push({
            serial: String(serial || ""),
            gameName: String(gameName || "اللعبة"),
            destination: String(destination || "/sdcard/"),
            reason: String(reason || "")
        })
        window.androidDataFallbackQueue = queue
        window.openNextAndroidDataFallback()
    }

    function openNextAndroidDataFallback() {
        if (window.androidDataFallbackDialogActive
                || window.androidDataFallbackQueue.length === 0)
            return

        const queue = window.androidDataFallbackQueue.slice()
        const next = queue.shift()
        window.androidDataFallbackQueue = queue
        window.androidDataFallbackSerial = next.serial
        window.androidDataFallbackGame = next.gameName
        window.androidDataFallbackDestination = next.destination
        window.androidDataFallbackReason = next.reason
        window.androidDataFallbackDialogActive = true
        androidDataFallbackDialog.open()
    }

    function finishAndroidDataFallback(useRoot) {
        const serial = window.androidDataFallbackSerial
        if (!serial)
            return

        if (useRoot) {
            if (!appController.copyToDeviceRoot(serial))
                appController.declineRootFallback(serial)
        } else {
            appController.declineRootFallback(serial)
        }

        window.androidDataFallbackDialogActive = false
        androidDataFallbackDialog.close()
        window.androidDataFallbackSerial = ""
        window.androidDataFallbackGame = ""
        window.androidDataFallbackDestination = ""
        window.androidDataFallbackReason = ""
        Qt.callLater(window.openNextAndroidDataFallback)
    }

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

            objectName: "mainSidebar"

            width: window.sidebarWidth

            Behavior on width {
                NumberAnimation { duration: 180; easing.type: Easing.OutCubic }
            }

            height: parent.height

            color: "#0B0E16"


            Column {

                anchors.fill: parent

                anchors.margins: window.sidebarCollapsed ? 10 : 18

                spacing: 10


                Rectangle {

                    width: parent.width

                    height: 75

                    color: "transparent"


                    RowLayout {
                        anchors.fill: parent
                        spacing: 5

                        Column {
                            visible: !window.sidebarCollapsed
                            Layout.fillWidth: true
                            Layout.alignment: Qt.AlignVCenter
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

                        Text {
                            visible: window.sidebarCollapsed
                            Layout.fillWidth: true
                            text: "KN"
                            color: "#00D9FF"
                            font.pixelSize: 21
                            font.bold: true
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                        }

                        Button {
                            Layout.preferredWidth: 32
                            Layout.preferredHeight: 32
                            text: window.sidebarCollapsed ? "›" : "‹"
                            onClicked: window.sidebarCollapsed = !window.sidebarCollapsed

                            background: Rectangle {
                                radius: 8
                                color: parent.hovered ? "#1C2A3A" : "#111925"
                                border.width: 1
                                border.color: "#2C3B50"
                            }

                            contentItem: Text {
                                text: window.sidebarCollapsed ? "›" : "‹"
                                color: "#B9C9DE"
                                font.pixelSize: 19
                                horizontalAlignment: Text.AlignHCenter
                                verticalAlignment: Text.AlignVCenter
                            }
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
                        { icon: "🎮", title: "الألعاب", page: "الألعاب" },
                        { icon: "📂", title: "نقل الملفات", page: "نقل الملفات" },
                        { icon: "📦", title: "التثبيت", page: "التثبيت" },
                        { icon: "📜", title: "سجل النقل", page: "سجل النقل" },
                        { icon: "💾", title: "التخزين", page: "التخزين" },
                        { icon: "⚙", title: "الإعدادات", page: "الإعدادات" }
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

                            anchors.leftMargin: window.sidebarCollapsed ? 0 : 18

                            verticalAlignment: Text.AlignVCenter
                            horizontalAlignment: window.sidebarCollapsed
                                                 ? Text.AlignHCenter : Text.AlignLeft

                            text: window.sidebarCollapsed
                                  ? modelData.icon
                                  : modelData.icon + "  " + modelData.title
                            font.pixelSize: window.sidebarCollapsed ? 21 : 15

                            color: window.activePage === modelData.page
                                   ? "#FFFFFF"
                                   : "#9AA2B3"

                            font.bold: window.activePage === modelData.page

                        }


                        ToolTip.visible: window.sidebarCollapsed && navMouseArea.containsMouse
                        ToolTip.text: modelData.title
                        ToolTip.delay: 350

                        MouseArea {
                            id: navMouseArea
                            anchors.fill: parent
                            hoverEnabled: true
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
                    height: window.sidebarCollapsed ? 72 : 112
                    radius: 14
                    color: "#0F141E"
                    border.width: 1
                    border.color: "#1E2634"

                    Column {
                        anchors.fill: parent
                        anchors.margins: window.sidebarCollapsed ? 6 : 14
                        spacing: window.sidebarCollapsed ? 5 : 9

                        Text {
                            visible: !window.sidebarCollapsed
                            text: "حالة النظام"
                            color: "#7E879A"
                            font.pixelSize: 12
                        }

                        Row {
                            width: parent.width
                            spacing: window.sidebarCollapsed ? 0 : 8

                            Rectangle {
                                width: 9
                                height: 9
                                radius: 5
                                color: "#27D17F"
                                anchors.verticalCenter: parent.verticalCenter
                            }

                            Text {
                                visible: !window.sidebarCollapsed
                                text: "النظام يعمل"
                                color: "#D9E0EC"
                                font.pixelSize: 13
                            }
                        }

                        Row {
                            width: parent.width
                            spacing: window.sidebarCollapsed ? 0 : 8

                            Text {
                                visible: !window.sidebarCollapsed
                                text: "الألعاب"
                                color: "#687285"
                                font.pixelSize: 11
                            }

                            Text {
                                text: String(window.totalGames)
                                color: "#00D9FF"
                                font.pixelSize: 12
                                font.bold: true
                                width: window.sidebarCollapsed ? parent.width : implicitWidth
                                horizontalAlignment: window.sidebarCollapsed
                                                     ? Text.AlignHCenter : Text.AlignLeft
                            }
                        }
                    }
                }


                Item {

                    width: 1

                    height: 1

                }


                Text {
                    visible: !window.sidebarCollapsed
                    width: parent.width
                    text: "هشام الريمي"
                    color: "#3F4757"
                    horizontalAlignment: Text.AlignHCenter
                    font.pixelSize: 11
                }

                Text {
                    visible: window.sidebarCollapsed
                    width: parent.width
                    text: "👤"
                    color: "#3F4757"
                    horizontalAlignment: Text.AlignHCenter
                    font.pixelSize: 18
                }

            }

        }


        // =========================================================
        // MAIN AREA
        // =========================================================

        Rectangle {

            width: parent.width - window.sidebarWidth

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
                        // PUBG SOURCE SIZE
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
                                anchors.margins: 8
                                spacing: 2

                                Text {
                                    text: "حجم PUBG / FLASH"
                                    color: "#AAB6C8"
                                    font.pixelSize: 10
                                    font.bold: true
                                }

                                ComboBox {
                                    id: pubgSourceSizeSelector
                                    width: parent.width
                                    height: 30
                                    model: ["كامل", "متوسط", "منخفض"]
                                    currentIndex: window.pubgSourceSize === "Medium"
                                                  ? 1
                                                  : window.pubgSourceSize === "Low" ? 2 : 0

                                    onActivated: {
                                        var values = ["Saved", "Medium", "Low"]
                                        appController.setPubgSourceSize(values[index])
                                    }

                                    background: Rectangle {
                                        radius: 7
                                        color: "#151C29"
                                        border.width: 1
                                        border.color: "#2B3A50"
                                    }

                                    contentItem: Text {
                                        text: pubgSourceSizeSelector.displayText
                                        color: "#F0F5FC"
                                        font.pixelSize: 11
                                        font.bold: true
                                        verticalAlignment: Text.AlignVCenter
                                        horizontalAlignment: Text.AlignHCenter
                                        rightPadding: 18
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
                                    if (appController.useWirelessSource()) {
                                        window.wirelessConnected = true
                                        window.usbReady = false
                                        window.customSourceReady = false
                                        window.activeSource = "WIRELESS"
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
                                    if (appController.useUsbSource()) {
                                        window.usbReady = true
                                        window.wirelessConnected = false
                                        window.customSourceReady = false
                                        window.activeSource = "USB"
                                    }
                                }

                            }

                        }



                        // =================================================
                        // CUSTOM GAMES SOURCE
                        // =================================================

                        Rectangle {
                            Layout.preferredWidth: 132
                            Layout.preferredHeight: 58
                            radius: 12
                            color: window.activeSource === "CUSTOM"
                                   ? "#10202B" : "#10141D"
                            border.width: 1
                            border.color: window.activeSource === "CUSTOM"
                                           ? "#00B8D9" : "#263445"

                            Column {
                                anchors.fill: parent
                                anchors.margins: 9
                                spacing: 3

                                Text {
                                    text: "مخصص"
                                    color: window.activeSource === "CUSTOM"
                                           ? "#00D9FF" : "#FFFFFF"
                                    font.pixelSize: 12
                                    font.bold: true
                                }

                                Text {
                                    width: parent.width
                                    text: window.customSourcePath.length > 0
                                          ? "مجلد ألعاب محدد"
                                          : "اختيار مجلد"
                                    color: "#AAB6C8"
                                    font.pixelSize: 10
                                    elide: Text.ElideRight
                                }
                            }

                            MouseArea {
                                anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor
                                onClicked: {
                                    if (appController.chooseCustomSource()) {
                                        var sourceState = appController.getSourceState()
                                        window.activeSource = "CUSTOM"
                                        window.customSourceReady = true
                                        window.customSourcePath = sourceState.customPath || ""
                                        window.wirelessConnected = false
                                        window.usbReady = false
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

                                    text: "📱 الأجهزة ووسائط التخزين"

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

                                        color: "#FFFFFF"
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
                                    appController.refreshADBDevices()
                                    adbDevicePopup.open()
                                }

                            }

                        }


                        Rectangle {
                            id: transferCompletedBadge
                            Layout.preferredWidth: 94
                            Layout.minimumWidth: 88
                            Layout.preferredHeight: 58
                            radius: 12
                            color: "#101622"
                            border.width: 1
                            border.color: "#245C49"

                            Column {
                                anchors.centerIn: parent
                                width: parent.width - 6
                                spacing: 2

                                Text {
                                    text: window.transferCompletedCountVisible ? "نسخ ناجح اليوم  ◉" : "نسخ ناجح اليوم  ◌"
                                    color: "#91A29E"
                                    font.pixelSize: 10
                                    horizontalAlignment: Text.AlignHCenter
                                    width: parent.width
                                }

                                Text {
                                    objectName: "transferCompletedCountToggle"
                                    text: window.transferCompletedCountVisible
                                          ? String(window.transferCompletedCount) : "•••"
                                    color: "#42D995"
                                    font.pixelSize: 18
                                    font.bold: true
                                    horizontalAlignment: Text.AlignHCenter
                                    width: parent.width
                                }
                            }

                            MouseArea {
                                anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor
                                onClicked: window.transferCompletedCountVisible =
                                               !window.transferCompletedCountVisible
                            }
                        }

                        Rectangle {
                            id: fullscreenLockButton
                            objectName: "fullscreenLockButton"
                            Layout.preferredWidth: 46
                            Layout.preferredHeight: 46
                            radius: 11
                            color: window.fullscreenLocked ? "#15372F" : "#10141D"
                            border.width: 1
                            border.color: window.fullscreenLocked ? "#287A58" : "#344254"

                            Text {
                                anchors.centerIn: parent
                                text: window.fullscreenLocked ? "↙" : "⛶"
                                color: window.fullscreenLocked ? "#9BF0C5" : "#C6CEDB"
                                font.pixelSize: 22
                                font.bold: true
                            }

                            MouseArea {
                                anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor
                                onClicked: {
                                    if (window.fullscreenLocked)
                                        window.askWindowUnlock()
                                    else
                                        window.enterFullscreenLock()
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
                                    appController.refreshADBDevices()
                                    adbDevicePopup.open()

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


                    Loader {
                        anchors.fill: parent
                        sourceComponent: window.activePage === "سجل النقل"
                                         ? transferHistoryPage
                                         : window.activePage === "نقل الملفات"
                                           ? transferFilesPage
                                           : window.activePage === "التثبيت"
                                             ? installAllPage
                                             : window.activePage === "الإعدادات"
                                               ? settingsPage
                                               : dashboardPage
                    }

                }

            }

        }

    }


    Timer {
        interval: 15000
        running: true
        repeat: true
        onTriggered: window.refreshDashboardGameCount()
    }

    Timer {
        interval: 60000
        running: true
        repeat: true
        onTriggered: window.refreshDailyTransferCount()
    }

    Component.onCompleted: {
        window.refreshDashboardGameCount()
        window.refreshDailyTransferCount()
        if (appController && appController.getSourceState) {
            var sourceState = appController.getSourceState()
            window.activeSource = sourceState.source || "WIRELESS"
            window.customSourcePath = sourceState.customPath || ""
            window.wirelessConnected =
                    window.activeSource === "WIRELESS" && Boolean(sourceState.exists)
            window.usbReady =
                    window.activeSource === "USB" && Boolean(sourceState.exists)
            window.customSourceReady =
                    window.activeSource === "CUSTOM" && Boolean(sourceState.exists)
        }
        if (appController && appController.getPubgSourceSize) {
            window.pubgSourceSize = appController.getPubgSourceSize() || "Saved"
        }
        if (appController && appController.getADBDevices) {
            appController.refreshADBDevices()
        }
    }


    // =============================================================
    // CONNECT TO EXISTING CONTROLLER SIGNALS
    // =============================================================

    Connections {

        target: appController

        function onAndroidDataFallbackRequested(serial, gameName, destination, reason) {
            window.queueAndroidDataFallback(serial, gameName, destination, reason)
        }

        function onTransferPercentChanged(percent) {

            window.transferPercent = percent

        }

        function onTransferSpeedChanged(speed) {

            window.transferSpeed = speed

        }

        function onTransferEtaChanged(eta) {

            window.transferEta = eta

        }

        function onTransferSummaryChanged(summary) {
            window.transferCompletedCount = Number(summary && summary.completedToday || 0)
        }

        function onPubgSourceSizeChanged(size) {
            window.pubgSourceSize = String(size || "Saved")
        }

        function onTransferFinished(success, message) {

            if (success) {

                window.transferPercent = 100

            }

            else {

                window.transferPercent = 0

            }

        }

        function onOperationStateChanged(state) {
            window.operationBusy = state !== "IDLE"
        }

        function onTransferPausedChanged(paused) {
            window.operationPaused = paused
        }

        function onTransferBatchChanged(devices) {
            window.transferDevices = devices || []
            window.syncTransferDeviceModel(window.transferDevices)
            if (window.transferDevices.length > 0)
                window.transferPanelDismissed = false
        }

        function onOperationFinished(message) {
            window.operationMessage = message
        }

        function onOperationRejected(message) {
            window.operationMessage = message
        }

        function onMessageChanged(message) {
            window.operationMessage = message
        }

        function onAdbDevicesChanged(devices) {
            console.log("COPY TARGETS FROM PYTHON:", devices || [])
            window.adbDevices = devices || []
            window.selectedAdbSerials = window.selectedAdbSerials.filter(
                function(serial) {
                    return window.adbDevices.some(function(device) {
                        return device.serial === serial
                    })
                }
            )
            window.updateAdbDeviceStatus()
        }

    }

    Component {
        id: dashboardPage
        Dashboard {
            objectName: "dashboardPage"
            anchors.fill: parent
        }
    }

    Component {
        id: transferHistoryPage
        TransferHistory {
            anchors.fill: parent
        }
    }

    Component {
        id: transferFilesPage
        TransferFiles {
            anchors.fill: parent
        }
    }

    Component {
        id: installAllPage
        InstallAll {
            anchors.fill: parent
        }
    }

    Component {
        id: settingsPage
        AppSettings {
            anchors.fill: parent
            selectedSerial: window.selectedAdbSerials.length > 0
                            ? window.selectedAdbSerials[0] : ""
            selectedDeviceName: {
                for (var i = 0; i < window.adbDevices.length; ++i) {
                    if (window.adbDevices[i].serial === selectedSerial)
                        return String(window.adbDevices[i].name || window.adbDevices[i].model || selectedSerial)
                }
                return selectedSerial
            }
        }
    }

    Rectangle {
        id: transferPanel
        objectName: "transferPanel"

        visible: window.transferDevices.length > 0 && !window.transferPanelDismissed
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.rightMargin: 16
        anchors.bottomMargin: 16
        width: Math.min(460, parent.width - 32)
        height: window.transferPanelCollapsed
               ? 56
               : Math.min(parent.height - 32, 850)
        radius: 16
        color: "#111722"
        border.width: 1
        border.color: window.operationBusy ? "#00AFCF" : "#2A3445"
        z: 20

        Column {
            anchors.fill: parent
            anchors.margins: 10
            spacing: 5

            RowLayout {
                width: parent.width
                height: 32
                spacing: 6

                Text {
                    Layout.fillWidth: true
                    text: window.operationPaused
                          ? "النقل متوقف مؤقتًا"
                          : window.operationBusy
                            ? "العملية قيد التنفيذ"
                            : "نتيجة العملية"
                    color: "#00D9FF"
                    font.pixelSize: 16
                    font.bold: true
                    elide: Text.ElideRight
                }

                Row {
                    Layout.alignment: Qt.AlignRight | Qt.AlignVCenter
                    spacing: 8

                    Button {
                        width: 82
                        height: 34
                        visible: window.operationBusy && !window.operationPaused
                        text: "إيقاف مؤقت"
                        onClicked: appController.pauseTransfer()

                        background: Rectangle {
                            radius: 10
                            color: parent.pressed ? "#59451F"
                                   : parent.hovered ? "#3C321F"
                                   : "#282519"
                            border.width: 1
                            border.color: "#80662B"
                        }

                        contentItem: Text {
                            text: parent.text
                            color: "#FFE19A"
                            font.pixelSize: 11
                            font.bold: true
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                        }
                    }

                    Button {
                        width: 82
                        height: 34
                        visible: window.operationBusy && window.operationPaused
                        text: "استئناف"
                        onClicked: appController.resumeTransfer()

                        background: Rectangle {
                            radius: 10
                            color: parent.pressed ? "#1C6B4A"
                                   : parent.hovered ? "#184D3A"
                                   : "#153629"
                            border.width: 1
                            border.color: "#287A58"
                        }

                        contentItem: Text {
                            text: parent.text
                            color: "#9BF0C5"
                            font.pixelSize: 11
                            font.bold: true
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                        }
                    }

                    Button {
                        width: 104
                        height: 34
                        visible: window.operationBusy
                        text: "إلغاء الكل"
                        onClicked: appController.cancelTransfer()

                        background: Rectangle {
                            radius: 10
                            color: parent.pressed ? "#7A2734"
                                   : parent.hovered ? "#542630"
                                   : "#2C1B24"
                            border.width: 1
                            border.color: "#944052"
                        }

                        contentItem: Text {
                            text: parent.text
                            color: "#FFC0C7"
                            font.pixelSize: 12
                            font.bold: true
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                        }
                    }

                    Button {
                        width: 68
                        height: 34
                        text: window.transferPanelCollapsed ? "تكبير" : "تصغير"
                        onClicked: window.transferPanelCollapsed = !window.transferPanelCollapsed

                        background: Rectangle {
                            radius: 10
                            color: parent.pressed ? "#343C4B"
                                   : parent.hovered ? "#252E3D"
                                   : "#1A2230"
                            border.width: 1
                            border.color: "#3A4659"
                        }

                        contentItem: Text {
                            text: parent.text
                            color: "#C6CEDB"
                            font.pixelSize: 10
                            font.bold: true
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                        }
                    }

                    Button {
                        width: 34
                        height: 34
                        text: "✕"
                        onClicked: window.transferPanelDismissed = true

                        background: Rectangle {
                            radius: 10
                            color: parent.pressed ? "#343C4B"
                                   : parent.hovered ? "#252E3D"
                                   : "#1A2230"
                            border.width: 1
                            border.color: "#3A4659"
                        }

                        contentItem: Text {
                            text: parent.text
                            color: "#C6CEDB"
                            font.pixelSize: 15
                            font.bold: true
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                        }
                    }
                }
            }

            Text {
                width: parent.width
                height: 22
                visible: !window.transferPanelCollapsed
                text: window.operationMessage
                color: "#B9C3D2"
                wrapMode: Text.Wrap
                maximumLineCount: 2
                elide: Text.ElideRight
                verticalAlignment: Text.AlignVCenter
            }

            ListView {
                id: transferListView
                visible: !window.transferPanelCollapsed
                width: parent.width
                height: Math.max(120, parent.height - 70)
                clip: true
                spacing: 5
                model: transferDevicesModel
                boundsBehavior: Flickable.StopAtBounds

                ScrollBar.vertical: ScrollBar {
                    policy: ScrollBar.AsNeeded
                }

                delegate: Rectangle {
                    width: ListView.view.width
                    height: 98
                    radius: 10
                    color: "#181F2B"
                    border.width: 1
                    border.color: status === "فشل"
                                  ? "#A83232"
                                  : status === "مساحة غير كافية"
                                    ? "#A87624"
                                    : status === "اكتمل" || status === "مراجعة"
                                      ? "#247A58"
                                      : "#29384A"

                    Column {
                        anchors.fill: parent
                        anchors.margins: 8
                        spacing: 3

                        RowLayout {
                            width: parent.width
                            height: 20
                            spacing: 6

                            Text {
                                Layout.fillWidth: true
                                text: deviceModel
                                color: "white"
                                font.pixelSize: 13
                                font.bold: true
                                elide: Text.ElideRight
                                verticalAlignment: Text.AlignVCenter
                            }

                            Button {
                                visible: completed && !awaitingFallback
                                Layout.preferredWidth: 23
                                Layout.preferredHeight: 21
                                text: "✕"
                                padding: 0
                                onClicked: appController.dismissDeviceOperation(serial)

                                background: Rectangle {
                                    radius: 6
                                    color: parent.hovered ? "#44252D" : "#242B38"
                                    border.width: 1
                                    border.color: "#6C3945"
                                }

                                contentItem: Text {
                                    text: parent.text
                                    color: "#FFC0C7"
                                    font.pixelSize: 12
                                    font.bold: true
                                    horizontalAlignment: Text.AlignHCenter
                                    verticalAlignment: Text.AlignVCenter
                                }
                            }
                        }

                        RowLayout {
                            width: parent.width
                            height: 16
                            spacing: 6

                            Text {
                                Layout.fillWidth: true
                                text: game + "  •  " + serial
                                color: "#AAB6C8"
                                font.pixelSize: 10
                                elide: Text.ElideRight
                            }

                            Text {
                                text: status
                                color: status === "فشل" || status === "ملغي"
                                       ? "#FF8C8C"
                                       : status === "مساحة غير كافية"
                                         ? "#FFC56E"
                                         : status === "اكتمل" || status === "مراجعة"
                                           ? "#42D995" : "#00D9FF"
                                font.pixelSize: 10
                                font.bold: true
                            }
                        }

                        RowLayout {
                            width: parent.width
                            height: 12
                            spacing: 6

                            Rectangle {
                                Layout.fillWidth: true
                                height: 6
                                radius: 3
                                color: "#252C3A"

                                Rectangle {
                                    width: parent.width
                                           * Math.max(0, Math.min(100, percent)) / 100
                                    height: parent.height
                                    radius: 3
                                    color: status === "فشل" ? "#D95B66"
                                           : status === "مساحة غير كافية" ? "#D89A38"
                                           : "#00B8D9"
                                }
                            }

                            Text {
                                text: percent >= 0 ? percent + "%" : "..."
                                color: "#DCE4F0"
                                font.pixelSize: 10
                            }
                        }

                        RowLayout {
                            width: parent.width
                            height: 22
                            spacing: 5

                            Text {
                                Layout.fillWidth: true
                                text: status === "فشل" && error !== ""
                                      ? error
                                      : operation + "  |  " + speed + "  |  ETA " + eta
                                color: "#AAB6C8"
                                font.pixelSize: 9
                                elide: Text.ElideRight
                                verticalAlignment: Text.AlignVCenter
                            }

                            Button {
                                visible: !completed && !awaitingFallback
                                Layout.preferredWidth: 62
                                Layout.preferredHeight: 22
                                text: "إلغاء"
                                padding: 0
                                onClicked: appController.cancelDevice(serial)

                                background: Rectangle {
                                    radius: 6
                                    color: parent.pressed ? "#702630" : "#281B22"
                                    border.width: 1
                                    border.color: "#8D3A49"
                                }

                                contentItem: Text {
                                    text: parent.text
                                    color: "#FFB6BF"
                                    font.pixelSize: 10
                                    font.bold: true
                                    horizontalAlignment: Text.AlignHCenter
                                    verticalAlignment: Text.AlignVCenter
                                }
                            }

                            Button {
                                visible: status === "فشل" && kind === "copy"
                                         && error !== ""
                                Layout.preferredWidth: 88
                                Layout.preferredHeight: 22
                                text: "إرشادات"
                                padding: 0
                                onClicked: {
                                    window.deviceCopyHelpTitle =
                                        "إصلاح النسخ — " + deviceModel
                                    window.deviceCopyHelpText =
                                        appController.deviceCopyHelp(serial)
                                    deviceCopyHelpDialog.open()
                                }

                                background: Rectangle {
                                    radius: 6
                                    color: parent.hovered ? "#153846" : "#122B35"
                                    border.width: 1
                                    border.color: "#26768A"
                                }

                                contentItem: Text {
                                    text: parent.text
                                    color: "#9CEBFA"
                                    font.pixelSize: 10
                                    font.bold: true
                                    horizontalAlignment: Text.AlignHCenter
                                    verticalAlignment: Text.AlignVCenter
                                }
                            }
                        }

                    }
                }
            }
        }
    }

    Dialog {
        id: androidDataFallbackDialog
        modal: true
        focus: true
        closePolicy: Popup.NoAutoClose
        title: "تعذر النسخ إلى Android/data"
        width: Math.min(640, window.width - 40)
        height: Math.min(460, window.height - 40)
        x: Math.max(20, (window.width - width) / 2)
        y: Math.max(20, (window.height - height) / 2)

        background: Rectangle {
            color: "#111722"
            radius: 14
            border.width: 1
            border.color: "#C18A39"
        }

        contentItem: ScrollView {
            clip: true
            Column {
                width: androidDataFallbackDialog.width - 48
                spacing: 12

                Text {
                    width: parent.width
                    text: "رفض الهاتف " + window.androidDataFallbackSerial
                          + " الكتابة إلى Android/data أثناء نسخ "
                          + window.androidDataFallbackGame + "."
                    color: "#DCE4F0"
                    wrapMode: Text.Wrap
                    font.pixelSize: 14
                }

                Text {
                    width: parent.width
                    text: "هل تريد إعادة النسخ إلى مجلد مستقل في جذر الذاكرة الداخلية بدلًا من ذلك؟"
                    color: "#DCE4F0"
                    wrapMode: Text.Wrap
                    font.pixelSize: 13
                }

                Rectangle {
                    width: parent.width
                    height: 48
                    radius: 8
                    color: "#1A2533"
                    border.width: 1
                    border.color: "#5B6E86"

                    Text {
                        anchors.fill: parent
                        anchors.margins: 10
                        verticalAlignment: Text.AlignVCenter
                        text: window.androidDataFallbackDestination
                        color: "#9CEBFA"
                        font.pixelSize: 13
                        elide: Text.ElideMiddle
                    }
                }

                Text {
                    width: parent.width
                    text: window.androidDataFallbackReason
                    color: "#AAB6C8"
                    wrapMode: Text.Wrap
                    font.pixelSize: 11
                    textFormat: Text.PlainText
                }
            }
        }

        footer: RowLayout {
            width: androidDataFallbackDialog.width - 24
            spacing: 12

            Button {
                text: "نسخ إلى الجذر"
                Layout.alignment: Qt.AlignRight
                onClicked: window.finishAndroidDataFallback(true)
            }

            Button {
                text: "إلغاء"
                onClicked: window.finishAndroidDataFallback(false)
            }
        }
    }

    Dialog {
        id: deviceCopyHelpDialog
        modal: true
        focus: true
        title: window.deviceCopyHelpTitle
        width: Math.min(660, window.width - 40)
        height: Math.min(540, window.height - 40)
        x: Math.max(20, (window.width - width) / 2)
        y: Math.max(20, (window.height - height) / 2)
        standardButtons: Dialog.Ok

        background: Rectangle {
            color: "#111722"
            radius: 14
            border.width: 1
            border.color: "#26768A"
        }

        contentItem: ScrollView {
            clip: true

            Text {
                width: deviceCopyHelpDialog.width - 72
                text: window.deviceCopyHelpText
                color: "#DCE4F0"
                font.pixelSize: 13
                wrapMode: Text.Wrap
                padding: 10
                textFormat: Text.PlainText
            }
        }
    }

    Popup {
        id: adbDevicePopup

        x: 250
        y: 95
        width: 390
        height: Math.min(window.height - 130, 460)
        modal: true
        focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside

        background: Rectangle {
            color: "#111722"
            radius: 14
            border.width: 1
            border.color: "#26394A"
        }

        contentItem: Column {
            spacing: 10
            padding: 16

            Text {
                text: "الهواتف والفلاشات المتصلة"
                color: "#00D9FF"
                font.pixelSize: 16
                font.bold: true
            }

            ListView {
                width: parent.width
                height: 300
                clip: true
                model: window.adbDevices

                delegate: CheckBox {
                    id: deviceSelectionCheckBox
                    width: ListView.view.width
                    height: 42
                    padding: 8
                    spacing: 8
                    text: modelData.type === "usb_storage"
                          ? "💾 " + modelData.model + " — "
                            + modelData.units + " وحدات محسوبة ("
                            + modelData.billingValue + ") — متاح: "
                            + modelData.freeText
                          : "📱 " + modelData.model + " / " + modelData.serial
                    checked: window.selectedAdbSerials.indexOf(
                                 modelData.serial
                             ) !== -1

                    // Explicit high-contrast text and selection styling:
                    // the selected device remains readable against the popup.
                    indicator: Rectangle {
                        implicitWidth: 20
                        implicitHeight: 20
                        x: deviceSelectionCheckBox.leftPadding
                        y: deviceSelectionCheckBox.height / 2 - height / 2
                        radius: 5
                        color: deviceSelectionCheckBox.checked
                               ? "#00D9FF"
                               : "#0B111A"
                        border.width: 1
                        border.color: deviceSelectionCheckBox.checked
                                      ? "#00D9FF"
                                      : "#65748A"

                        Text {
                            anchors.centerIn: parent
                            visible: deviceSelectionCheckBox.checked
                            text: "✓"
                            color: "#071019"
                            font.pixelSize: 14
                            font.bold: true
                        }
                    }

                    contentItem: Text {
                        text: deviceSelectionCheckBox.text
                        color: deviceSelectionCheckBox.checked
                               ? "#FFFFFF"
                               : "#DCE4F0"
                        font.pixelSize: 12
                        font.bold: deviceSelectionCheckBox.checked
                        verticalAlignment: Text.AlignVCenter
                        leftPadding: deviceSelectionCheckBox.indicator.width
                                     + deviceSelectionCheckBox.spacing
                        rightPadding: 8
                        elide: Text.ElideRight
                    }

                    background: Rectangle {
                        radius: 8
                        color: deviceSelectionCheckBox.checked
                               ? "#153845"
                               : deviceSelectionCheckBox.hovered
                                 ? "#1A2432"
                                 : "#141B27"
                        border.width: 1
                        border.color: deviceSelectionCheckBox.checked
                                      ? "#00D9FF"
                                      : "#2A3546"
                    }

                    onClicked: {
                        var selected = window.selectedAdbSerials.slice()
                        var index = selected.indexOf(modelData.serial)
                        if (checked && index === -1)
                            selected.push(modelData.serial)
                        else if (!checked && index !== -1)
                            selected.splice(index, 1)
                        window.selectedAdbSerials = selected
                        appController.setADBDeviceSelection(selected)
                        window.updateAdbDeviceStatus()
                    }
                }
            }

            Row {
                spacing: 8

                Button {
                    text: "تحديد الكل"
                    onClicked: {
                        var selected = []
                        for (var i = 0; i < window.adbDevices.length; i++)
                            selected.push(window.adbDevices[i].serial)
                        window.selectedAdbSerials = selected
                        appController.setADBDeviceSelection(selected)
                        window.updateAdbDeviceStatus()
                    }
                }

                Button {
                    text: "مسح الكل"
                    onClicked: {
                        window.selectedAdbSerials = []
                        appController.setADBDeviceSelection([])
                        window.updateAdbDeviceStatus()
                    }
                }

                Text {
                    text: window.deviceUnitsForSerials(window.selectedAdbSerials)
                          + " وحدات محسوبة (" + window.selectedAdbSerials.length + " وسائط)"
                    color: "#B9C3D2"
                    anchors.verticalCenter: parent.verticalCenter
                }
            }
        }
    }



    Dialog {
        id: windowUnlockDialog
        objectName: "windowUnlockDialog"
        modal: true
        focus: true
        closePolicy: Popup.NoAutoClose
        onClosed: {
            if (window.pendingFullscreenUnlock) {
                window.pendingFullscreenUnlock = false
                var closeAfterUnlock = window.pendingFullscreenClose
                window.pendingFullscreenClose = false
                Qt.callLater(function() {
                    window.leaveFullscreenLock(closeAfterUnlock)
                })
            }
        }
        width: 390
        height: 245
        x: Math.max(16, (window.width - width) / 2)
        y: Math.max(16, (window.height - height) / 2)
        standardButtons: Dialog.NoButton

        background: Rectangle {
            radius: 14
            color: "#111722"
            border.width: 1
            border.color: "#26768A"
        }

        contentItem: ColumnLayout {
            spacing: 12

            Text {
                Layout.fillWidth: true
                text: window.pendingFullscreenClose
                      ? "أدخل كلمة المرور لإغلاق التطبيق أثناء ملء الشاشة"
                      : "الخروج من الشاشة الكاملة محمي بكلمة مرور"
                color: "#FFFFFF"
                font.pixelSize: 15
                font.bold: true
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.Wrap
            }

            Text {
                Layout.fillWidth: true
                text: window.pendingFullscreenClose
                      ? "سيتم إغلاق التطبيق بعد التحقق من كلمة المرور."
                      : "أدخل كلمة المرور للخروج من الشاشة الكاملة."
                color: "#AAB6C8"
                font.pixelSize: 11
                horizontalAlignment: Text.AlignHCenter
            }

            TextField {
                id: windowUnlockPassword
                Layout.fillWidth: true
                echoMode: TextInput.Password
                placeholderText: "كلمة المرور"
                horizontalAlignment: TextInput.AlignHCenter
                onAccepted: window.handleWindowUnlock()
            }

            Text {
                id: windowUnlockError
                Layout.fillWidth: true
                visible: false
                text: "كلمة المرور غير صحيحة."
                color: "#FF7777"
                font.pixelSize: 11
                horizontalAlignment: Text.AlignHCenter
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 8

                Button {
                    Layout.fillWidth: true
                    text: window.pendingFullscreenClose ? "إلغاء الإغلاق" : "إلغاء"
                    onClicked: {
                        window.pendingFullscreenUnlock = false
                        window.pendingFullscreenClose = false
                        windowUnlockDialog.close()
                    }
                }

                Button {
                    id: windowUnlockConfirmButton
                    Layout.fillWidth: true
                    text: "تأكيد"
                    onClicked: window.handleWindowUnlock()
                }
            }
        }
    }

    Dialog {
        id: startupPasswordDialog

        modal: true
        closePolicy: Popup.NoAutoClose
        visible: true

        width: 380
        height: 230

        anchors.centerIn: parent

        standardButtons: Dialog.NoButton

        background: Rectangle {
            radius: 15
            color: "#111722"
            border.width: 1
            border.color: "#2A3445"
        }

        Column {
            anchors.fill: parent
            anchors.margins: 25
            spacing: 15

            Text {
                width: parent.width
                text: "ادخل كلمة السر"
                color: "white"
                font.pixelSize: 18
                horizontalAlignment: Text.AlignHCenter
            }

            TextField {
                id: startupPasswordField

                width: parent.width
                height: 42

                echoMode: TextInput.Password

                placeholderText: "كلمة السر"

                horizontalAlignment: TextInput.AlignHCenter
            }

            Text {
                id: startupPasswordError

                width: parent.width

                text: "كلمة المرور غير صحيحة"

                color: "#FF5555"

                visible: false

                horizontalAlignment: Text.AlignHCenter
            }

            Button {
                width: parent.width
                height: 40

                text: "دخول"

                onClicked: {

                    if (appController && appController.verifyWindowPassword(startupPasswordField.text)) {

                        startupPasswordError.visible = false
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
