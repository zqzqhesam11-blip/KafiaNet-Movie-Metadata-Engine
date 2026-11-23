import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../components"

Item {
    id: root
    anchors.fill: parent

    property var games: []
    property string gameSignature: ""

    function refreshGames() {
        if (!appController || !appController.getDashboardGames)
            return

        var discovered = appController.getDashboardGames() || []
        var signatureParts = []
        for (var i = 0; i < discovered.length; ++i) {
            var item = discovered[i]
            signatureParts.push([
                item.name || "",
                item.sourcePath || "",
                item.imageSource || "",
                item.isCustom ? "1" : "0"
            ].join("|"))
        }
        var nextSignature = signatureParts.join("\n")
        if (nextSignature !== root.gameSignature) {
            root.games = discovered
            root.gameSignature = nextSignature
        }
    }

    Rectangle {
        anchors.fill: parent
        color: "#070910"
    }

    Flickable {
        anchors.fill: parent
        anchors.margins: 30
        contentWidth: width
        contentHeight: contentColumn.height + 30
        clip: true

        Column {
            id: contentColumn
            width: parent.width
            spacing: 22

            Grid {
                id: grid
                width: parent.width
                columns: width >= 1250 ? 4 : width >= 850 ? 3 : 2
                spacing: 22

                Repeater {
                    model: root.games
                    delegate: GameCard {
                        gameName: String(modelData.name || "")
                        imageSource: String(modelData.imageSource || "")
                        gameColor: String(modelData.color || "#00D9FF")
                        sourcePath: String(modelData.sourcePath || "")
                        isCustom: Boolean(modelData.isCustom)
                    }
                }
            }
        }
    }

    Timer {
        interval: 5000
        running: true
        repeat: true
        onTriggered: root.refreshGames()
    }

    Component.onCompleted: root.refreshGames()
}
