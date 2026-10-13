import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Bluetooth
import qs.Ui
import qs.Commons

// Bar widget for the `lctl daemon` (github.com/kevinlouie/lctl).
//
// The daemon owns the light and call detection; this widget only reads and
// writes files. Settings live in ~/.config/lctl/config.json (written through
// `lctl set`, which also tells the daemon to reload) and what the daemon is
// doing lives in $XDG_RUNTIME_DIR/lctl/state.json. Both are replaced
// atomically and watched, so nothing here polls.
//
// The header switch starts/stops (and enables/disables) the systemd user
// service, so switching it off stops all detection and BLE traffic.
Panel {
  id: root
  moduleName: "kevin.light"
  ipcTarget: "kevin.light"
  manageIpc: false

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.4)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  readonly property string home: Quickshell.env("HOME") || ""
  readonly property string lctl: home + "/.local/bin/lctl"
  readonly property string configPath: (Quickshell.env("XDG_CONFIG_HOME") || home + "/.config") + "/lctl/config.json"
  readonly property string statePath: (Quickshell.env("XDG_RUNTIME_DIR") || "/tmp") + "/lctl/state.json"
  readonly property string serviceName: "lctl.service"

  property var config: ({})
  property var daemon: ({})
  property bool configLoaded: false
  property bool stateLoaded: false
  property bool serviceActive: false
  property bool serviceBusy: false
  // Shown on the switch while systemctl runs, so the knob moves on click.
  property bool serviceWanted: false

  readonly property bool running: serviceActive && daemon.running === true
  readonly property string mode: config.mode || "auto"
  readonly property int brightness: config.brightness !== undefined ? config.brightness : 80
  readonly property int colorTemp: config.color_temp !== undefined ? config.color_temp : 4000
  readonly property string address: config.address || ""
  readonly property bool lightOn: running && daemon.lightOn === true
  readonly property bool inCall: running && daemon.inCall === true

  readonly property color hoverFill: bar ? Style.hoverFillFor(bar.foreground, Color.accent) : "transparent"
  readonly property color selectedFill: bar ? Style.selectedFillFor(bar.foreground, Color.accent) : "transparent"

  // ---------------------------------------------------------------- labels

  readonly property string icon: lightOn ? "󰛨" : "󰌶"

  function sourceLabel(source) {
    if (!source) return ""
    var name = source.name ? source.name.charAt(0).toUpperCase() + source.name.slice(1) : ""
    if (source.kind === "mic") return (name || "App") + " mic"
    if (source.kind === "camera") return name ? name + " camera" : "Camera"
    return name
  }

  readonly property string callText: {
    var sources = daemon.sources || []
    var labels = []
    for (var i = 0; i < sources.length; i++) {
      var label = sourceLabel(sources[i])
      if (label !== "" && labels.indexOf(label) < 0) labels.push(label)
    }
    return labels.join(", ")
  }

  readonly property string statusText: {
    if (serviceBusy) return serviceWanted ? "Starting" : "Stopping"
    if (!serviceActive) return "Stopped"
    if (daemon.error) return daemon.error
    if (!running) return "Starting"
    if (mode === "on") return "Pinned on"
    if (mode === "off") return "Pinned off"
    if (inCall) return callText !== "" ? "In a call · " + callText : "In a call"
    return "Waiting for a call"
  }

  readonly property string tooltipText: {
    if (!serviceActive) return "Light controller stopped"
    return (lightOn ? "Light on" : "Light off") + " · " + statusText
  }

  readonly property var selectedDevice: {
    for (var i = 0; i < lightDevices.length; i++)
      if (lightDevices[i].address === address) return lightDevices[i]
    return null
  }

  readonly property string lightName: selectedDevice ? selectedDevice.name : "Light"

  // ---------------------------------------------------------------- devices

  readonly property var adapter: Bluetooth.defaultAdapter
  readonly property var btDevices: Bluetooth.devices ? Bluetooth.devices.values : []
  property bool ownsDiscovery: false

  function deviceName(device) {
    return String(device.name || device.deviceName || "")
  }

  // Ulanzi lights BlueZ knows about (paired, or seen while scanning), plus the
  // saved light so it stays listed while out of range. Rows are plain
  // objects so delegates never hold BlueZ wrappers.
  readonly property var lightDevices: {
    var rows = []
    var seen = {}
    for (var i = 0; i < btDevices.length; i++) {
      var d = btDevices[i]
      var name = deviceName(d)
      if (!d || !d.address || !/ulanzi/i.test(name)) continue
      seen[d.address] = true
      rows.push({ address: d.address, name: name, connected: !!d.connected, known: true })
    }
    if (address !== "" && !seen[address])
      rows.push({ address: address, name: "Saved light", connected: false, known: false })
    return rows
  }

  function selectLight(addr) {
    if (addr === address) return
    lctlSet(["--light", addr])
  }

  function scan() {
    if (!adapter || !adapter.enabled || adapter.discovering) return
    ownsDiscovery = true
    adapter.discovering = true
    scanTimer.restart()
  }

  // ---------------------------------------------------------------- actions

  function lctlSet(args) {
    Quickshell.execDetached([root.lctl, "set"].concat(args))
  }

  function setMode(next) {
    if (next !== mode) lctlSet(["--mode", next])
  }

  function toggleService() {
    if (serviceBusy) return
    serviceWanted = !serviceActive
    serviceBusy = true
    serviceToggle.command = ["systemctl", "--user", serviceWanted ? "enable" : "disable", "--now", root.serviceName]
    serviceToggle.running = true
  }

  function refreshService() {
    if (!serviceCheck.running) serviceCheck.running = true
  }

  function parseJson(text) {
    try {
      var parsed = JSON.parse(String(text || ""))
      return parsed && typeof parsed === "object" ? parsed : null
    } catch (e) {
      return null
    }
  }

  // ---------------------------------------------------------------- files

  FileView {
    id: configView
    path: root.configPath
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: {
      root.configLoaded = true
      var parsed = root.parseJson(text())
      if (parsed) root.config = parsed
    }
    onLoadFailed: {
      root.configLoaded = false
      root.config = ({})
    }
  }

  FileView {
    id: stateView
    path: root.statePath
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: {
      root.stateLoaded = true
      var parsed = root.parseJson(text())
      if (!parsed) return
      root.daemon = parsed
      // The daemon flips `running` on start/exit; confirm with systemd.
      if ((parsed.running === true) !== root.serviceActive) root.refreshService()
    }
    onLoadFailed: {
      root.stateLoaded = false
      root.daemon = ({})
    }
  }

  // A FileView can only watch a file that exists. state.json lives in the
  // runtime dir, so it is missing at login until the daemon first starts (and
  // config.json until the first `lctl set`). Every bar - one per monitor -
  // keeps retrying until its files appear; after that the watch takes over.
  Timer {
    interval: 2000
    repeat: true
    running: !root.stateLoaded || !root.configLoaded
    onTriggered: {
      if (!root.stateLoaded) stateView.reload()
      if (!root.configLoaded) configView.reload()
    }
  }

  Process {
    id: serviceCheck
    command: ["systemctl", "--user", "is-active", root.serviceName]
    stdout: StdioCollector {
      onStreamFinished: root.serviceActive = text.trim() === "active"
    }
  }

  Process {
    id: serviceToggle
    onExited: {
      root.serviceBusy = false
      root.refreshService()
      // state.json may not have existed when the watch was set up.
      stateView.reload()
    }
  }

  Component.onCompleted: refreshService()

  onOpenedChanged: {
    if (opened) {
      refreshService()
      stateView.reload()
    } else if (ownsDiscovery) {
      // Don't leave the radio scanning if the panel closes mid-scan.
      scanTimer.triggered()
    }
  }

  Timer {
    id: scanTimer
    interval: 15000
    onTriggered: {
      if (root.ownsDiscovery && root.adapter && root.adapter.discovering) root.adapter.discovering = false
      root.ownsDiscovery = false
    }
  }

  IpcHandler {
    target: "kevin.light"

    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function toggleService(): void { root.toggleService() }
    function setMode(mode: string): void { root.setMode(mode) }
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.icon
    active: root.inCall
    dimmed: !root.serviceActive
    tooltipText: root.opened ? "" : root.tooltipText
    onPressed: function(b) {
      if (b === Qt.RightButton) root.toggleService()
      else root.toggle()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(380))
    contentHeight: panel.fittedContentHeight(column.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) {
        if (t === "a") root.setMode("auto")
        else if (t === "o") root.setMode("on")
        else if (t === "f") root.setMode("off")
        else if (t === "l" || t === "L") root.toggleService()
      }

      Column {
        id: column
        anchors.fill: parent
        spacing: Style.space(14)

        // ---------- Hero: bulb · name · status · service switch ----------
        Item {
          width: parent.width
          implicitHeight: Math.max(heroIcon.implicitHeight, heroLabels.implicitHeight, serviceSwitch.implicitHeight)

          Text {
            id: heroIcon
            textFormat: Text.PlainText
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            text: root.icon
            color: root.inCall ? root.urgent : root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.display
            opacity: root.serviceActive ? 1.0 : 0.5
          }

          ToggleSwitch {
            id: serviceSwitch
            checked: root.serviceBusy ? root.serviceWanted : root.serviceActive
            busy: root.serviceBusy
            foreground: root.foreground
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            onToggled: root.toggleService()

            PanelToolTip {
              visible: serviceSwitch.containsMouse
              text: root.serviceActive ? "Stop lctl (no detection, no Bluetooth)" : "Start lctl"
              fontFamily: root.fontFamily
            }
          }

          Column {
            id: heroLabels
            anchors.left: heroIcon.right
            anchors.leftMargin: Style.space(14)
            anchors.right: parent.right
            anchors.rightMargin: serviceSwitch.width + Style.space(12)
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(2)

            Text {
              textFormat: Text.PlainText
              text: root.lightName
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.title
              font.bold: true
              elide: Text.ElideRight
              width: parent.width
            }

            Text {
              textFormat: Text.PlainText
              text: root.statusText.toUpperCase()
              color: root.daemon.error && root.serviceActive ? root.urgent : root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
              font.letterSpacing: 1.2
              elide: Text.ElideRight
              width: parent.width
            }
          }
        }

        PanelSeparator { foreground: root.foreground }

        // ---------- Mode ----------
        Column {
          width: parent.width
          spacing: Style.space(10)

          PanelSectionHeader {
            text: "MODE"
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          Row {
            id: modeRow
            width: parent.width
            spacing: Style.space(6)
            readonly property var modes: [
              { value: "on", label: "On", tooltip: "Keep the light on" },
              { value: "auto", label: "Auto", tooltip: "On during calls (camera or meeting-app mic)" },
              { value: "off", label: "Off", tooltip: "Keep the light off" }
            ]
            readonly property real cellWidth: (width - spacing * (modes.length - 1)) / modes.length

            Repeater {
              model: modeRow.modes
              Button {
                required property var modelData
                width: modeRow.cellWidth
                text: modelData.label
                tooltipText: modelData.tooltip
                selected: root.mode === modelData.value
                bordered: true
                foreground: root.foreground
                fontFamily: root.fontFamily
                onClicked: root.setMode(modelData.value)
              }
            }
          }
        }

        // ---------- Brightness / warmth ----------
        SliderRow {
          width: parent.width
          title: "BRIGHTNESS"
          valueText: Math.round(brightnessSlider.liveValue) + "%"

          PanelSlider {
            id: brightnessSlider
            bar: root.bar
            width: parent.width
            minimum: 0
            maximum: 100
            step: 5
            integer: true
            value: root.brightness
            onReleased: function(v) { if (Math.round(v) !== root.brightness) root.lctlSet(["--brightness", String(Math.round(v))]) }
          }
        }

        SliderRow {
          width: parent.width
          title: "WARMTH"
          valueText: (Math.round(tempSlider.liveValue / 100) * 100) + "K"

          PanelSlider {
            id: tempSlider
            bar: root.bar
            width: parent.width
            minimum: 2700
            maximum: 6500
            step: 100
            integer: true
            tickCount: 3
            value: root.colorTemp
            onReleased: function(v) {
              var k = Math.round(v / 100) * 100
              if (k !== root.colorTemp) root.lctlSet(["--color-temp", String(k)])
            }
          }
        }

        PanelSeparator { foreground: root.foreground }

        // ---------- Light picker ----------
        Column {
          id: lightList
          width: parent.width
          spacing: Style.space(10)

          Item {
            width: parent.width
            implicitHeight: Math.max(lightsHeader.implicitHeight, scanButton.implicitHeight)

            PanelSectionHeader {
              id: lightsHeader
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              text: "LIGHTS"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            PanelActionButton {
              id: scanButton
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              visible: !!root.adapter && root.adapter.enabled
              iconText: root.adapter && root.adapter.discovering ? "󰑓" : "󰍉"
              tooltipText: root.adapter && root.adapter.discovering ? "Scanning…" : "Scan for lights"
              foreground: root.foreground
              hoverColor: root.foreground
              fontFamily: root.fontFamily
              onClicked: root.scan()
            }
          }

          Repeater {
            model: root.lightDevices
            LightRow {
              required property var modelData
              width: lightList.width
              dev: modelData
            }
          }

          Text {
            textFormat: Text.PlainText
            visible: root.lightDevices.length === 0
            width: parent.width
            text: !root.adapter ? "No Bluetooth adapter"
                : !root.adapter.enabled ? "Turn Bluetooth on to find your light"
                : root.adapter.discovering ? "Scanning for Ulanzi lights…"
                : "No Ulanzi light found. Power it on and scan."
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
          }
        }
      }
    }
  }

  // Section header with the current value on the right, then the control.
  component SliderRow: Column {
    id: sliderRow
    property string title: ""
    property string valueText: ""
    spacing: Style.space(6)

    Item {
      width: parent.width
      implicitHeight: sliderTitle.implicitHeight

      PanelSectionHeader {
        id: sliderTitle
        anchors.left: parent.left
        text: sliderRow.title
        foreground: root.foreground
        fontFamily: root.fontFamily
      }

      Text {
        textFormat: Text.PlainText
        anchors.right: parent.right
        anchors.verticalCenter: sliderTitle.verticalCenter
        text: sliderRow.valueText
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
        font.bold: true
      }
    }
  }

  component LightRow: CursorSurface {
    id: row
    required property var dev
    readonly property bool isSelected: dev.address === root.address

    current: isSelected
    hasCursor: rowMouse.containsMouse
    foreground: root.foreground
    fill: root.hoverFill
    currentFill: root.selectedFill
    implicitHeight: rowContent.implicitHeight + Style.spacing.rowPaddingX

    readonly property string caption: {
      var parts = []
      if (isSelected) parts.push(root.lightOn ? "Selected · On" : "Selected")
      if (dev.connected) parts.push("Connected")
      else if (!dev.known) parts.push("Not seen")
      parts.push(dev.address)
      return parts.join(" · ")
    }

    MouseArea {
      id: rowMouse
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: row.isSelected ? Qt.ArrowCursor : Qt.PointingHandCursor
      onClicked: root.selectLight(row.dev.address)
    }

    PanelToolTip {
      visible: rowMouse.containsMouse && !row.isSelected
      text: "Use this light"
      fontFamily: root.fontFamily
    }

    Item {
      id: rowContent
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(10)
      implicitHeight: Math.max(rowIcon.implicitHeight, info.implicitHeight)

      Text {
        id: rowIcon
        textFormat: Text.PlainText
        text: row.isSelected && root.lightOn ? "󰛨" : "󰌶"
        color: row.isSelected ? root.foreground : root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.heading
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
      }

      Column {
        id: info
        spacing: Style.space(1)
        anchors.left: rowIcon.right
        anchors.leftMargin: Style.space(10)
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter

        Text {
          textFormat: Text.PlainText
          text: row.dev.name
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          elide: Text.ElideRight
          width: parent.width
        }
        Text {
          textFormat: Text.PlainText
          text: row.caption
          color: row.isSelected ? root.foreground : root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
          width: parent.width
        }
      }
    }
  }
}
