import QtQuick
import Quickshell
import Quickshell.Io
import qs.Ui

// Reads the state file the daemon writes on every transition, so the bar
// reflects what the assistant is doing without polling a process.
BarWidget {
  id: root
  moduleName: "voice.indicator"

  property string status: "stopped"
  property string label: ""

  readonly property var icons: ({
    "stopped":   "󰍭",
    "idle":      "󰍬",
    "listening": "󰍬",
    "thinking":  "󱚟",
    "acting":    "󱐋",
    "confirm":   "󰀦",
    "error":     "󰍭",
    // No API key yet. A fresh install lands here rather than looking broken.
    "unconfigured": "󰍭"
  })

  FileView {
    id: state
    path: Quickshell.env("XDG_RUNTIME_DIR") + "/omarchy-voice/state.json"
    watchChanges: true
    onFileChanged: reload()
    onLoaded: {
      try {
        const parsed = JSON.parse(state.text())
        root.status = parsed.status || "idle"
        root.label = parsed.text || ""
      } catch (e) {
        root.status = "error"
        root.label = ""
      }
    }
    onLoadFailed: {
      root.status = "stopped"
      root.label = ""
    }
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.icons[root.status] || root.icons["idle"]
    active: root.status === "listening" || root.status === "thinking"
             || root.status === "acting" || root.status === "confirm"
    tooltipText: root.label !== ""
                 ? root.status + " — " + root.label
                 : "Voice control: " + root.status
    onPressed: function(b) {
      if (root.status === "confirm")
        root.bar.run("omarchy-voice listen confirm")
      else
        root.bar.run("omarchy-voice listen toggle")
    }
  }
}
