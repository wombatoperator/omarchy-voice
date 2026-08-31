import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import qs.Commons

// A glow at the bottom of the screen while voice control is awake, breathing
// with whoever is talking to it.
//
// Two files, both written by the daemon:
//   state.json  status + transcript, a few changes a minute
//   level       microphone loudness 0..1, ten a second while listening
//
// They are separate on purpose — a watcher on the status file should not have
// to wake for every audio frame.
//
// The level is the *same* audio being sent upstream, not a second capture. So
// the orb cannot move while the mic is muted: muting kills the recorder, no
// frames exist, and nothing writes a level. What you see is what is heard.
//
// Everything is painted from the live Omarchy palette (Color.accent, .urgent,
// .foreground). Switching theme re-tints it on the next repaint — there are no
// colours of our own anywhere in here.
Item {
  id: root

  property string status: "stopped"
  property string label: ""
  property real level: 0
  property real updatedAt: 0
  // Bumped by a slow timer so the staleness check below re-evaluates.
  property real nowSeconds: Date.now() / 1000

  // A clean shutdown writes "idle" and the orb goes away. A killed daemon
  // writes nothing, leaving "listening" in the file forever — and this is a
  // fullscreen overlay, so a stuck orb sits on top of everything with no
  // obvious way to clear it. Treat a very old status as no status.
  //
  // The window is deliberately generous: status is written on transitions, not
  // on a heartbeat, so a genuinely long listening session can leave `updated`
  // minutes behind without anything being wrong.
  readonly property bool fresh: updatedAt > 0 && (nowSeconds - updatedAt) < 900

  readonly property bool active: status === "listening" || status === "thinking"
                              || status === "acting" || status === "confirm"
                              || status === "error"
  readonly property bool awake: active && fresh

  readonly property color tint: {
    if (status === "confirm" || status === "error") return Color.urgent
    if (status === "acting") return Color.foreground
    return Color.accent
  }

  readonly property real coreSize: 54
  readonly property real haloSize: 190

  // Only speech should move it. The daemon already gates room tone to zero;
  // this keeps the orb still during "thinking", when the mic is open but the
  // user has stopped talking and a twitching orb would read as mishearing.
  readonly property real voice: status === "listening" ? level : 0

  // Canvas gradients want CSS colour strings, not QML color values.
  function rgba(c, a) {
    return "rgba(" + Math.round(c.r * 255) + "," + Math.round(c.g * 255)
         + "," + Math.round(c.b * 255) + "," + a + ")"
  }

  FileView {
    id: stateFile
    path: Quickshell.env("XDG_RUNTIME_DIR") + "/omarchy-voice/state.json"
    watchChanges: true
    onFileChanged: reload()
    onLoaded: {
      try {
        const parsed = JSON.parse(stateFile.text())
        root.status = parsed.status || "idle"
        root.label = parsed.text || ""
        root.updatedAt = Number(parsed.updated) || 0
      } catch (e) {
        root.status = "error"
        root.label = ""
        root.updatedAt = 0
      }
    }
    onLoadFailed: {
      root.status = "stopped"
      root.label = ""
      root.updatedAt = 0
    }
  }

  // Polled rather than watched. The level file is rewritten ten times a second
  // by atomic replace, which swaps the inode out from under an inotify watch;
  // a fixed 60 ms poll of a five-byte file on tmpfs is cheaper and steadier
  // than re-arming a watch that often. It runs only while the orb is up.
  FileView {
    id: levelFile
    path: Quickshell.env("XDG_RUNTIME_DIR") + "/omarchy-voice/level"
    onLoaded: {
      const v = parseFloat(levelFile.text())
      root.level = isFinite(v) ? Math.max(0, Math.min(1, v)) : 0
    }
    onLoadFailed: root.level = 0
  }

  Timer {
    running: true
    interval: 30000
    repeat: true
    onTriggered: root.nowSeconds = Date.now() / 1000
  }

  Timer {
    running: root.awake
    interval: 60
    repeat: true
    onTriggered: levelFile.reload()
    onRunningChanged: if (!running) root.level = 0
  }

  PanelWindow {
    id: panel
    visible: root.awake
    anchors { top: true; bottom: true; left: true; right: true }
    color: "transparent"
    WlrLayershell.namespace: "omarchy-voice-orb"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.None
    exclusionMode: ExclusionMode.Ignore
    // Visual only. An empty input region means clicks land on whatever is
    // underneath — the orb must never eat a click meant for the desktop.
    mask: Region {}

    Item {
      id: stage
      width: root.haloSize
      height: root.haloSize
      anchors.horizontalCenter: parent.horizontalCenter
      anchors.bottom: parent.bottom
      anchors.bottomMargin: 74

      opacity: root.awake ? 1 : 0
      Behavior on opacity { NumberAnimation { duration: 260; easing.type: Easing.OutCubic } }

      // Idle breathing. Slow and small, so it reads as alive rather than busy.
      SequentialAnimation {
        running: root.awake
        loops: Animation.Infinite
        NumberAnimation {
          target: stage; property: "scale"
          from: 0.94; to: 1.06
          duration: root.status === "acting" ? 340 : 1250
          easing.type: Easing.InOutSine
        }
        NumberAnimation {
          target: stage; property: "scale"
          from: 1.06; to: 0.94
          duration: root.status === "acting" ? 340 : 1250
          easing.type: Easing.InOutSine
        }
      }

      // Voice rides on top of the breath, on its own transform, so speech does
      // not fight the idle animation for the same property.
      Item {
        id: body
        anchors.fill: parent
        scale: 1 + root.voice * 0.42
        // Levels land in 100 ms steps; without easing the orb would tick like
        // a VU meter. Attack is quick so a word lands with the voice, release
        // slower so it settles instead of snapping back between syllables.
        Behavior on scale {
          NumberAnimation { duration: 130; easing.type: Easing.OutCubic }
        }

        // ---- halo ---------------------------------------------------------
        Canvas {
          id: halo
          anchors.fill: parent
          renderStrategy: Canvas.Cooperative
          onPaint: {
            const ctx = getContext("2d")
            ctx.reset()
            const c = width / 2
            const g = ctx.createRadialGradient(c, c, 0, c, c, c)
            g.addColorStop(0.00, root.rgba(root.tint, 0.66))
            g.addColorStop(0.35, root.rgba(root.tint, 0.30))
            g.addColorStop(0.70, root.rgba(root.tint, 0.09))
            g.addColorStop(1.00, root.rgba(root.tint, 0.0))
            ctx.fillStyle = g
            ctx.fillRect(0, 0, width, height)
          }
          Connections {
            target: root
            function onTintChanged() { halo.requestPaint() }
          }
        }

        // ---- rotating arc, only while thinking -----------------------------
        Canvas {
          id: arc
          anchors.centerIn: parent
          width: root.coreSize + 20
          height: root.coreSize + 20
          visible: root.status === "thinking"
          renderStrategy: Canvas.Cooperative
          onPaint: {
            const ctx = getContext("2d")
            ctx.reset()
            const c = width / 2
            ctx.lineWidth = 2
            ctx.lineCap = "round"
            ctx.strokeStyle = root.rgba(root.tint, 1.0)
            ctx.beginPath()
            ctx.arc(c, c, c - 2, -Math.PI / 2, Math.PI * 0.35)
            ctx.stroke()
          }
          Connections {
            target: root
            function onTintChanged() { arc.requestPaint() }
          }
          RotationAnimator on rotation {
            running: arc.visible
            loops: Animation.Infinite
            from: 0; to: 360; duration: 1400
          }
        }

        // ---- core ----------------------------------------------------------
        Canvas {
          id: core
          anchors.centerIn: parent
          width: root.coreSize
          height: root.coreSize
          renderStrategy: Canvas.Cooperative
          onPaint: {
            const ctx = getContext("2d")
            ctx.reset()
            const c = width / 2
            // Offset centre gives the sphere a light source, not a flat disc.
            const g = ctx.createRadialGradient(c, c * 0.72, c * 0.05, c, c, c)
            g.addColorStop(0.00, root.rgba(root.tint, 1.0))
            g.addColorStop(0.55, root.rgba(root.tint, 0.92))
            g.addColorStop(1.00, root.rgba(root.tint, 0.40))
            ctx.fillStyle = g
            ctx.beginPath()
            ctx.arc(c, c, c, 0, Math.PI * 2)
            ctx.fill()
          }
          Connections {
            target: root
            function onTintChanged() { core.requestPaint() }
          }
        }

        // ---- speech bloom ---------------------------------------------------
        // Painted once at full strength and revealed by opacity. Fading a ready
        // layer costs a blend; repainting the gradient per frame would not.
        Canvas {
          id: bloom
          anchors.centerIn: parent
          width: root.coreSize * 2.1
          height: root.coreSize * 2.1
          opacity: root.voice * 0.85
          Behavior on opacity {
            NumberAnimation { duration: 130; easing.type: Easing.OutCubic }
          }
          renderStrategy: Canvas.Cooperative
          onPaint: {
            const ctx = getContext("2d")
            ctx.reset()
            const c = width / 2
            const g = ctx.createRadialGradient(c, c, 0, c, c, c)
            g.addColorStop(0.00, root.rgba(root.tint, 0.55))
            g.addColorStop(0.45, root.rgba(root.tint, 0.22))
            g.addColorStop(1.00, root.rgba(root.tint, 0.0))
            ctx.fillStyle = g
            ctx.fillRect(0, 0, width, height)
          }
          Connections {
            target: root
            function onTintChanged() { bloom.requestPaint() }
          }
        }
      }

      // ---- what it heard ---------------------------------------------------
      // Outside `body`, so the caption stays put while the orb pulses.
      Text {
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.top: parent.bottom
        anchors.topMargin: -28
        width: 380
        horizontalAlignment: Text.AlignHCenter
        wrapMode: Text.WordWrap
        elide: Text.ElideRight
        maximumLineCount: 2
        text: root.label
        visible: root.label !== ""
        color: Color.foreground
        opacity: 1.0
        font.pixelSize: 13
      }
    }
  }
}
