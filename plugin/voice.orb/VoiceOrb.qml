import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import qs.Commons

// A glow at the bottom of the screen while voice control is awake, breathing
// with whoever is talking to it — either of them.
//
// Two files, both written by the daemon:
//   state.json  status + transcript, a few changes a minute
//   level       two loudnesses, "<you> <oma>", sixteen a second while awake
//
// They are separate on purpose — a watcher on the status file should not have
// to wake for every audio frame.
//
// The levels are the *same* audio the daemon is sending and playing, not a
// second capture. So the orb cannot move while the mic is muted: muting kills
// the recorder, no frames exist, and nothing writes a level. What you see is
// what is heard.
//
// ---------------------------------------------------------------------------
// Why there are two levels, and why the ring is drawn the way it is
//
// The conversation is half duplex: the daemon holds the microphone shut for
// the whole of every reply, so her own voice cannot come back in as yours.
// That pins the input meter to zero for seconds at a time, and an orb reading
// only that number goes dead still exactly when the exchange is liveliest —
// it looks like it stopped. So the daemon publishes her loudness too, and the
// orb breathes with whoever currently has the floor.
//
// The ring around the core is the last six seconds of that, as a timeline
// sweeping clockwise from twelve o'clock. Which of the two was speaking is
// shown by the DIRECTION each bar grows, not by its colour:
//
//     you  ──▶  bars grow inward, toward the core      (it is taking this in)
//     oma  ──▶  bars grow outward, away from the core  (it is putting this out)
//
// Colour would have been the obvious choice and it does not survive contact
// with the themes: the foundational palette is foreground / background /
// accent / urgent / muted, and in the default theme `accent` and `foreground`
// are the same value, so a two-colour scheme is invisible there. Direction is
// legible in every theme, at any size, and to anyone who cannot separate the
// two hues anyway.
//
// Everything is painted from the live Omarchy palette (Color.accent, .urgent,
// .foreground). Switching theme re-tints it on the next repaint — there are no
// colours of our own anywhere in here.
Item {
  id: root

  property string status: "stopped"
  property string label: ""
  // Loudness 0..1 of each side of the conversation, straight off the daemon.
  property real youLevel: 0
  property real omaLevel: 0
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
  readonly property real voice: status === "listening" ? youLevel : 0

  // Her level needs no such guard. She speaks across "thinking" and "acting"
  // as readily as anywhere else, and the daemon only ever books audio it is
  // actually about to play — a barge-in clears the booking, so this falls to
  // zero the moment a reply is cut off rather than finishing a sentence the
  // user never heard.
  readonly property real speech: omaLevel


  // ---- the rolling timeline -------------------------------------------------
  // Six seconds of who-was-talking, sampled at the poll rate. Plain arrays and
  // an index rather than shift() on every frame: this is touched sixteen times
  // a second for as long as the orb is up.
  readonly property int historySize: 96
  property var youHistory: new Array(96).fill(0)
  property var omaHistory: new Array(96).fill(0)
  property int historyHead: 0

  function recordSample() {
    youHistory[historyHead] = voice
    omaHistory[historyHead] = speech
    historyHead = (historyHead + 1) % historySize
    ring.requestPaint()
  }

  function clearHistory() {
    youHistory = new Array(historySize).fill(0)
    omaHistory = new Array(historySize).fill(0)
    historyHead = 0
    ring.requestPaint()
  }

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

  // Polled rather than watched. The level file is rewritten many times a second
  // by atomic replace, which swaps the inode out from under an inotify watch;
  // a fixed 60 ms poll of a small file on tmpfs is cheaper and steadier than
  // re-arming a watch that often. It runs only while the orb is up.
  FileView {
    id: levelFile
    path: Quickshell.env("XDG_RUNTIME_DIR") + "/omarchy-voice/level"
    onLoaded: {
      // "<you> <oma>". A file written by an older daemon has one field; the
      // second reads as NaN and settles to zero, which is exactly the old
      // behaviour — the orb simply never hears about her side.
      const parts = levelFile.text().split(" ")
      const you = parseFloat(parts[0])
      const oma = parseFloat(parts[1])
      root.youLevel = isFinite(you) ? Math.max(0, Math.min(1, you)) : 0
      root.omaLevel = isFinite(oma) ? Math.max(0, Math.min(1, oma)) : 0
    }
    onLoadFailed: {
      root.youLevel = 0
      root.omaLevel = 0
    }
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
    onTriggered: {
      levelFile.reload()
      root.recordSample()
    }
    onRunningChanged: {
      if (!running) {
        root.youLevel = 0
        root.omaLevel = 0
        // A new session should not open on the tail of the last one.
        root.clearHistory()
      }
    }
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
      // not fight the idle animation for the same property. Her voice moves it
      // less than yours: she is heard through the speakers already, and an orb
      // that lunges on every syllable of a reply is exhausting to sit next to.
      Item {
        id: body
        anchors.fill: parent
        scale: 1 + root.voice * 0.42 + root.speech * 0.22
        // Levels land in steps; without easing the orb would tick like a VU
        // meter. Attack is quick so a word lands with the voice, release slower
        // so it settles instead of snapping back between syllables.
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

        // ---- the conversation ring -----------------------------------------
        // The last six seconds of the exchange, sweeping clockwise from twelve
        // o'clock. Your voice grows inward, hers outward — see the note at the
        // top for why this is direction rather than colour.
        Canvas {
          id: ring
          anchors.centerIn: parent
          // Sized off the geometry below rather than by eye: the widest thing
          // drawn is an outward bar at full amplitude, and a canvas that ends
          // before it does clips the loudest moment of every reply.
          width: (root.coreSize / 2 + 22 + 17 + 4) * 2
          height: (root.coreSize / 2 + 22 + 17 + 4) * 2
          renderStrategy: Canvas.Cooperative
          onPaint: {
            const ctx = getContext("2d")
            ctx.reset()
            const c = width / 2
            const reach = 17
            // Far enough out that a full-amplitude INWARD bar still stops clear
            // of the core. At the first attempt the ring sat at core + 14 with
            // the same reach, so the loudest thing the user said drew a spike
            // three pixels inside the sphere.
            const baseline = root.coreSize / 2 + 22
            const step = (Math.PI * 2) / root.historySize

            ctx.lineWidth = 1.6
            ctx.lineCap = "round"

            for (let i = 0; i < root.historySize; i++) {
              // Oldest sample first, so the newest lands just behind twelve
              // o'clock and the ring reads as a clock hand that has just passed.
              const slot = (root.historyHead + i) % root.historySize
              const you = root.youHistory[slot]
              const oma = root.omaHistory[slot]
              if (you <= 0 && oma <= 0) continue

              // Oldest faintest, so the ring decays into the past rather than
              // ending in a hard edge where the buffer wraps. `i` counts up
              // from the oldest sample, so this rises with recency.
              const recency = i / root.historySize
              const angle = -Math.PI / 2 + i * step
              const cos = Math.cos(angle)
              const sin = Math.sin(angle)

              if (oma > you) {
                // Hers: outward.
                const len = reach * oma
                ctx.strokeStyle = root.rgba(root.tint, 0.30 + recency * 0.45)
                ctx.beginPath()
                ctx.moveTo(c + cos * baseline, c + sin * baseline)
                ctx.lineTo(c + cos * (baseline + len), c + sin * (baseline + len))
                ctx.stroke()
              } else {
                // Yours: inward.
                const len = reach * you
                ctx.strokeStyle = root.rgba(root.tint, 0.45 + recency * 0.5)
                ctx.beginPath()
                ctx.moveTo(c + cos * baseline, c + sin * baseline)
                ctx.lineTo(c + cos * (baseline - len), c + sin * (baseline - len))
                ctx.stroke()
              }
            }

            // A hairline the bars sit on, so an empty ring is still a ring and
            // the orb does not look broken during a silence.
            ctx.lineWidth = 1
            ctx.strokeStyle = root.rgba(root.tint, 0.12)
            ctx.beginPath()
            ctx.arc(c, c, baseline, 0, Math.PI * 2)
            ctx.stroke()
          }
          Connections {
            target: root
            function onTintChanged() { ring.requestPaint() }
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
        // Your voice, gathering inward. Painted once at full strength and
        // revealed by opacity: fading a ready layer costs a blend, repainting
        // the gradient per frame would not.
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

      // ---- her voice, leaving ----------------------------------------------
      // Outside `body`, so an emitted ring keeps travelling at its own size
      // instead of being dragged by the orb's own swell.
      //
      // One canvas draws both rings — the second half a cycle behind the first,
      // so they read as a train rather than a double line. A Repeater with two
      // delegates was the obvious shape and is the wrong one twice over: a
      // delegate reaching `root` is unscoped access (qmllint says so), and
      // seeding a per-delegate phase through Component.onCompleted fights the
      // NumberAnimation that owns the property. One animator, two arcs, no
      // scoping question.
      //
      // It runs only while she is actually making sound, so nothing animates
      // through a silence — and a barge-in stops it within a frame, because the
      // daemon drops her booked audio the moment a reply is cut off.
      Canvas {
        id: emission
        anchors.centerIn: parent
        width: root.haloSize
        height: root.haloSize
        renderStrategy: Canvas.Cooperative

        property real travel: 0
        readonly property real span: root.haloSize / 2 - 6
        readonly property real start: root.coreSize / 2 + 4

        visible: root.speech > 0.02
        opacity: 0.5 * Math.min(1, root.speech * 1.6)
        Behavior on opacity {
          NumberAnimation { duration: 160; easing.type: Easing.OutCubic }
        }

        onPaint: {
          const ctx = getContext("2d")
          ctx.reset()
          const c = width / 2
          ctx.lineWidth = 1.5
          for (let i = 0; i < 2; i++) {
            // Half a cycle apart, wrapping.
            const phase = (travel + i * 0.5) % 1
            ctx.strokeStyle = root.rgba(root.tint, 1 - phase)
            ctx.beginPath()
            ctx.arc(c, c, start + span * phase, 0, Math.PI * 2)
            ctx.stroke()
          }
        }
        onTravelChanged: requestPaint()

        NumberAnimation on travel {
          running: emission.visible
          loops: Animation.Infinite
          from: 0; to: 1
          duration: 1600
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
