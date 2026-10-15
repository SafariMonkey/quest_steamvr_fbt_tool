# Investigation: SteamVR tracker bridge for standalone Quest VRChat

Response to [BRIEF.md](BRIEF.md). Research only; nothing is implemented yet. Researched 2026-10-15.

## Summary

- **Nothing already does this.** No fork of upstream fixes it, and no tool sends Lighthouse trackers to standalone Quest VRChat with a calibration between the two spaces. The pieces we need exist in separate projects, all under permissive licences:
  - **SlimeVR:** receives the Quest's head pose over OSCQuery and has Unity coordinate conversions.
  - **OpenVR Space Calibrator forks:** calibration maths for a tracker mounted on a headset, plus continuous calibration.
- **Upstream is a thin, broken proof of concept.**
  - Rotation is effectively never sent: it sends radians where VRChat expects degrees, so every value it sends is within ±1.6° of zero.
  - The send loop floods UDP with no rate limit.
  - Little is worth keeping, so in practice "extending" it means rewriting its one file in place.
- **Proposed design:**
  - Read the selected trackers in SteamVR's **raw** tracking universe, so PCVR playspace moves don't affect them.
  - Receive the Quest's head pose over OSCQuery.
  - Solve the Lighthouse→Quest transform and the mount offset together. This is the standard hand-eye calibration problem (AX = YB), which Space Calibrator also solves.
  - Forward the transformed poses without sending head-alignment messages.
  - The first approach is **pausable continuous calibration** (your call): it solves from natural head movement whenever the mounted tracker is present. Pausing it, or removing the tracker, keeps the transform fixed. There are no special calibration poses.
- **Two risks decide the design, and both need the real hardware:**
  1. **Does the Quest's head pose arrive reliably, and in which space and scale?** An open June 2026 bug report, with no developer response, says it often doesn't arrive on Quest.
  2. **Does a fixed calibration survive a session once the tracker is removed?** Every other mixed Quest+Lighthouse project has moved to continuous calibration with a tracker left on the headset. Users report short bursts of 3–6 cm error, and recalibration after recentring or sleep.
- **Recommended first step:** a small probe/recorder tool that answers both questions. It records sessions on the friend's PC so the solver can be developed offline, on the development PC, with no VR hardware attached.

## 1. What the upstream code does

[quest_steamvr_fbt_tool.py](quest_steamvr_fbt_tool.py) is 233 lines. It:

1. Opens OpenVR as an overlay app.
2. Looks up the configured serials once, at startup.
3. Loops, sending `/tracking/trackers/N/position` and `/rotation` for each tracker to a configured IP.
4. Can optionally shift heights so a reference tracker sits `delta` metres above the floor.

The author's article ([Qiita](https://qiita.com/taka7n/items/bbff147e253e64813cfb)) says they had no physical trackers, never tested `/tracking/trackers/head`, and relied on VRChat's "Auto-center OSC Trackers" button for alignment.

| Problem | Where | Effect |
|---|---|---|
| Euler angles sent in radians; VRChat expects degrees | [L65-73](quest_steamvr_fbt_tool.py#L65-L73), [L122-129](quest_steamvr_fbt_tool.py#L122-L129) | Every value sent is within ±1.571, read as degrees, so the trackers never visibly rotate |
| Wrong Euler order: decomposes as Rx·Ry·Rz, but VRChat applies Z, X, Y (Unity `Quaternion.Euler`, i.e. Ry·Rx·Rz) | [L65-73](quest_steamvr_fbt_tool.py#L65-L73) | Wrong angles even once converted to degrees |
| `atan` instead of `atan2` | [L68-71](quest_steamvr_fbt_tool.py#L68-L71) | Loses the quadrant: a 180° yaw is sent as 0 |
| Gimbal-lock branch tests `rot_y != 0`; the correct test is cos(rot_y) ≈ 0 | [L67](quest_steamvr_fbt_tool.py#L67) | Whenever rot_y is exactly 0 (e.g. a pure roll of 30°), it sets Z to 0 |
| Position mirrors X (`-pos_x`), but rotation is not mirrored | [L117](quest_steamvr_fbt_tool.py#L117) | Position and rotation are in different handednesses |
| Pose validity never checked | [L108-113](quest_steamvr_fbt_tool.py#L108-L113) | An all-zero pose raises `ZeroDivisionError`, and any exception exits the app ([L130-132](quest_steamvr_fbt_tool.py#L130-L132)) |
| No sleep or rate limit; one unbundled message per value | [L102-129](quest_steamvr_fbt_tool.py#L102-L129) | Sends as fast as Python can loop, over Wi-Fi |
| Poses come from `VRCompositor().getLastPoses()` (standing universe) | [L93](quest_steamvr_fbt_tool.py#L93), [L108](quest_steamvr_fbt_tool.py#L108) | Moves whenever the PCVR player adjusts their playspace |
| Devices are looked up once at startup, and enumeration stops at the first empty serial | [L37-57](quest_steamvr_fbt_tool.py#L37-L57), [L81-85](quest_steamvr_fbt_tool.py#L81-L85) | A tracker switched on later is never found |
| Floor offset is measured once, at startup | [L90-100](quest_steamvr_fbt_tool.py#L90-L100) | Becomes unnecessary once the transform is calibrated properly |

**Numerical check.** I compared upstream's `get_euler_angle` with a reference OpenVR→Unity conversion over 10,000 random orientations:
- The upstream rotation is wrong by a median of 133° (maximum 180°).
- A 90° yaw is sent as `[0, 1.571, 0]`; the correct value is `[0, -90, 0]`.
- A zeroed matrix raises `ZeroDivisionError`.

**Probably fine, but untested:**
- `openvr.init(VRApplication_Overlay)` should work with the Beyond as the active HMD. The README's dummy-driver setup was only for running SteamVR with no headset.
- Selecting trackers by serial number is the right mechanism.

**Build and CI:**
- The project pins Python 3.8.10, which reached end of life in October 2024.
- [build.yml](.github/workflows/build.yml) uses the retired `windows-2019` runner and `actions/*@v2`, so it won't run as written.

## 2. What the research established

### VRChat OSC trackers (input to the Quest)

Source: [docs.vrchat.com/docs/osc-trackers](https://docs.vrchat.com/docs/osc-trackers) (page updated 2025-11).

**Format**
- Each message is 3 floats.
- Unity coordinates: "Left-handed", "+y is up", "1.0f = 1m in real-world space".
- "Euler angles are in degrees and will be applied in order of Z, X, Y".
- Up to 8 trackers.

**Head alignment**
- Sending head data is optional.
- `head/position` shifts the whole OSC space so that point "aligns with the avatar's head bone position (note this is at the root of the head, not the eye position)". It is applied every frame, with no smoothing.
- `head/rotation` is used for yaw alignment only:
  - A streamed head rotation lerps towards the target, with a 10 s timeout.
  - A single message (none other within 300 ms) snaps the yaw once.
- 2026.1.2 extends the single-pulse snap to position ([release notes](https://docs.vrchat.com/docs/vrchat-202612)).
- Because alignment is to the avatar's head-bone root, VRChat's head alignment can't stand in for calibrating to the headset itself.

**Not documented**
- **Which space trackers are in when no head data is sent.** The docs only say "world-space" and "similarly to our existing implementation for SteamVR trackers". Our design depends on this, so it's hardware test H4.
- Send rate. A 2022 staff post says trackers are read every frame. Bundles are accepted.

**Excluding Person B's trackers on the PC**
- `--ignore-trackers=serial1,serial2` takes a comma-separated list of serials, with `%20` replacing any spaces ([launch options](https://docs.vrchat.com/docs/launch-options)).
- It applies to PC VRChat only. "Oculus Quest does not support any of these launch options."

### Quest head pose (output from the Quest) and OSCQuery

**The feature**
- Added in [2023.3.1](https://docs.vrchat.com/docs/vrchat-202331). It requires the in-game option "Allow Sending Head and Wrist VR Tracking OSC Data", and the receiving app must use OSCQuery.
- [2025.3.3](https://docs.vrchat.com/docs/vrchat-202533) fixed a UI bug that stopped it being enabled on Quest.
- There is no dedicated docs page.

**Format**
- Defined only by VRChat's official sample ([vrc-oscquery-lib `HeadAndWristsReceiver.cs`](https://github.com/vrchat-community/vrc-oscquery-lib)):
  - Addresses: `/tracking/vrsystem/head/pose`, `/leftwrist/pose`, `/rightwrist/pose`.
  - Each message is 6 floats: position xyz, then Unity Euler angles xyz in degrees.
- Sent inside a timestamped bundle.
- Not documented: coordinate space, whether it is scaled by the avatar, and send rate.

**Discovery**
- The receiving app advertises `_oscjson._tcp` and `_osc._udp` over mDNS, and serves an address tree containing `/tracking/vrsystem`.
- VRChat discovers the app and starts sending, with a HUD notice.
- Quest→PC across the LAN is explicitly supported ([wiki](https://github.com/vrchat-community/osc/wiki/OSCQuery)).
- A standalone Quest has **no other way to send OSC output** to a PC, because launch options aren't supported there.

**Reliability** ([bug report](https://feedback.vrchat.com/bug-reports/p/osc-head-and-wrist-tracking-not-working-inconsistent))
- The report (June 2026, open, no developer response) says the Quest shows "Sending Data to IP:PORT" but nothing arrives. It "works but rarely", and SlimeVR and OSCQueryExplorer see nothing.
- Similar open GitHub issues exist (vrc-oscquery-lib #55, #66; osc #162).
- SlimeVR depends on this path, so it works at least sometimes.

**SlimeVR as the reference** (MIT/Apache-2.0)
- [VRCOSCHandler.kt](https://github.com/SlimeVR/SlimeVR-Server/blob/main/server/core/src/main/java/dev/slimevr/osc/VRCOSCHandler.kt) and [VRCOSCQueryHandler.kt](https://github.com/SlimeVR/SlimeVR-Server/blob/main/server/core/src/main/java/dev/slimevr/osc/VRCOSCQueryHandler.kt).
- Advertising: it advertises both services, with a bare `/tracking/vrsystem` node, on the PC's LAN IP.
- Discovery: it finds VRChat by browsing `_osc._udp` for instance names starting `VRChat-Client`.
- Conversion: position (x, y, −z); quaternion from YXZ Euler in degrees, then (w, −x, −y, z). This matches my reference conversion.
- Sending:
  - One bundle per tick.
  - It echoes head position every frame.
  - It sends a single yaw-only head-rotation pulse on reset.
- **A bug to avoid:** sending to two targets (a manual IP and an OSCQuery-discovered one) made VRChat treat the yaw pulses as a stream (SlimeVR #1309).

**Python options**
- [`python-oscquery`](https://github.com/fraklein/python-oscquery) (MIT, maintained, Python ≥3.10) can advertise on a chosen interface IP and discover VRChat.
- `tinyoscquery` hard-codes `127.0.0.1` in its mDNS records, so it can't work across a LAN.
- Raw `zeroconf` is also realistic: SlimeVR's OSCQuery side is about 100 lines.

### Calibration maths (OpenVR Space Calibrator)

**Sources**
- [pushrax](https://github.com/pushrax/OpenVR-SpaceCalibrator): the original.
- [bdunderscore](https://github.com/bdunderscore/OpenVR-SpaceCalibrator): added continuous mode.
- [hyblocker](https://github.com/hyblocker/OpenVR-SpaceCalibrator): maintained; its `develop` branch is a rewrite.
- All MIT.

**Rotation**
- Kabsch (SVD) alignment of the rotation axes from pairs of samples' world-frame delta rotations.
- The mount rotation cancels out, so rotation can be solved without knowing it.
- hyblocker's legacy branch has a yaw-only 2D variant.

**Translation**
- Linear least squares on pair differences.
- It doesn't assume the two devices share a point: the lever arm (mount translation) is recovered afterwards, and the mount rotation by averaging.

**Universe**
- Samples raw, pre-chaperone poses.
- OVR Advanced Settings' playspace mover only changes the standing-zero pose, so it doesn't affect raw space.
- Raw space does change if the Lighthouse universe is re-solved; `Prop_CurrentUniverseId_Uint64` shows when that happens.

**Continuous mode**
- Locks the mount offset, then re-estimates the space transform from a sliding window.
- Accepts a new transform only if its error is no worse than the current one's.
- Smooths changes in the driver.
- hyblocker's `HmdRelative` mode recomputes the transform each frame from the head and tracker, with latency prediction. Its latency estimator appears to have a bug that leaves confidence at 0.

**Not usable directly**
- It applies its correction inside the SteamVR driver, to *every* device of the target tracking system.
- That would move the PCVR player's Beyond-side trackers and controllers.
- We should apply the transform only to the poses we forward.

**Observability**
- Yaw-only motion can't separate the transform's yaw from the mount's yaw, or the mount's height from the transform's height.
- Calibration needs nods and tilts of about ±30° or more, plus some walking.

### Existing tools

| Tool | What it does | Why it isn't our solution |
|---|---|---|
| [Spazznyan's fork](https://github.com/Spazznyan/quest_steamvr_fbt_tool) of upstream | Per-axis OSC addresses | Not VRChat's message format; no calibration |
| [CreoleVR/QuestLHSync](https://github.com/CreoleVR/QuestLHSync) | Calibrates using the headset's own cameras | Needs a rooted Quest, moves every Lighthouse device, and switches itself off with a wired HMD such as the Beyond |
| [charliee1w/OpenVR-SpaceCalibrator-Quest](https://github.com/charliee1w/OpenVR-SpaceCalibrator-Quest) (MIT) | Space Calibrator tuned for Quest | PC only. Its jump-debounce heuristics (35 mm / 5°, 3 confirmations) are worth borrowing |
| [QuestCalibrator ("Nova")](https://github.com/VividNightmareUnleashed/QuestCalibrator) | Similar | PC only; its licence forbids reuse, so ideas only |
| SlimeVR Feeder App + SlimeVR Server | Can ingest Lighthouse trackers | Uses only their rotation and hangs the skeleton from the head, discarding absolute Lighthouse positions. A fallback at best |

### Quest tracking origin

Mostly anecdotal; to verify.

- **Recentring:** on Floor/Eye-level origins, recentring resets the origin. Driver4VR's instructions imply that recentring moves VRChat's space relative to OSC trackers.
- **Power and sleep:** LIV's guidance for standalone Quest plus Lighthouse says the Quest "rebuilds playspace coordinates" on every power-on or wake, and to never recentre after setup.
- **Drift with a tracker left on the headset:**
  - Quest Pro with continuous calibration: errors under a millimetre, with intermittent 3–6 cm bursts of about 5 s ([hyblocker #50](https://github.com/hyblocker/OpenVR-SpaceCalibrator/issues/50)).
  - Without continuous calibration, people report drift of centimetres over tens of minutes.

## 3. Proposed approach

```
SteamVR (raw universe) --select serials--> X (calibration) --> Unity conversion --> OSC bundle --> Quest:9000
Quest VRChat --/tracking/vrsystem/head/pose via OSCQuery--> calibrator --> X
```

### 3.1 Coordinates
- Poll `GetDeviceToAbsoluteTrackingPose(TrackingUniverseRawAndUncalibrated, prediction)`, keep only valid poses, and timestamp each sample.
- Prediction can be tuned to offset Wi-Fi latency.
- Converting OpenVR→Unity:
  - Position: `(x, y, −z)`.
  - Rotation: `S·R·S` with `S = diag(1, 1, −1)` (quaternion `(w, −x, −y, z)`), decomposed into YXZ Euler angles in degrees.
- Incoming head poses use the inverse conversion.
- Send all trackers in one bundle per tick, at about 60–120 Hz (H4 measures the right rate).

### 3.2 Calibration: pausable continuous

**Model**
- `H_i = X · T_i · M`, where:
  - `H` is the Quest head pose (VRChat space).
  - `T` is the mounted tracker's pose (Lighthouse raw).
  - `X` is the transform from Lighthouse to Quest space.
  - `M` is the mount offset from tracker to headset.

**When it runs**
- It runs automatically whenever the configured calibration tracker is on and tracking, and the head pose is arriving.
- It can be paused, which keeps X fixed. Removing the tracker, or losing either stream, also keeps X fixed.
- "One-shot" calibration is just this: run it for a while, then pause or remove the tracker.
- Person B needs no special poses, only enough natural head movement.

**Sample selection** (from natural motion, not prompts)
- **Time alignment.**
  - The two streams have an unknown latency between them: Wi-Fi plus VRChat.
  - Estimate it by cross-correlating angular speed |ω|. Angular speed is the same for rigidly attached bodies and doesn't depend on X or M.
  - Search ±300 ms, refining between samples. VRChat's bundle timestamps can remove arrival jitter.
  - Then interpolate the high-rate Lighthouse stream at each head sample's time, minus the latency.
- **Motion weighting.**
  - Prefer samples taken at low angular speed: down-weight fast motion, or keep only natural pauses.
  - That way a wrong latency estimate costs little.
- **Diversity gate** (as in hyblocker):
  - Reject samples whose orientation is within a few degrees of one already stored.
  - Keep a bounded window, with a longer history for M than for X.

**Solve**
1. **Rotation of X**, from delta-rotation axes as in Space Calibrator. Yaw only, since both spaces should be gravity-aligned. A full 3-DOF solve is a sanity check: pitch and roll should come out near 0.
2. **Translations**, by linear least squares for the mount translation `m` and X's translation `p`: `h_i − R_X t_i = R_X R_Ti m + p`. This needs no sample pairs.
3. **Mount rotation** `R_M`, as the average of `R_Tiᵀ R_Xᵀ R_Hi`.
4. **Refinement**: a robust nonlinear fit of everything, optionally including the latency.

**Diagnostic: scale**
- Also fit a scale factor. A value other than 1 would tell us VRChat scales the head pose by the avatar's size.

**Updating once M is known**
- Once M is well determined, lock it and re-estimate only X from the recent window. As in bdunderscore's lock-relative-position mode, this needs much less motion.
- Accept a new X only when it passes the gates and its error is no worse than the current X's.
- Ease towards it at a rate set by the size of the change, and snap on persistent jumps such as a Quest recentre.

**Gates and status**
- Gates: rotation spread (σ₂/σ₁ ≥ 0.15) and translation conditioning, as in Space Calibrator.
- Report the error left after fitting (target under about 1–2 cm and 1–2°).
- Status: "collecting", "need more head tilt", "calibrated", "paused".
- Observability still applies: until Person B has nodded and tilted about ±30° the solver can't separate M from X, so it should say so rather than guess.

**Mount offset reuse**
- M belongs to the physical mount. If the mount reattaches repeatably (H5 checks this), save M.
- After a re-clip, X then converges within a second or two of ordinary movement.

**Fallback**
- Guided static holds (about 20 poses, each averaged after both streams have been still for at least 300 ms).
- Use these only if natural motion proves too fast or too narrow, or if the latency estimate is unreliable.

### 3.3 Quest origin changes while paused
- **Idea to test (H5):** detect Quest origin jumps in the head-pose stream alone. A recentre shows up as a physically impossible discontinuity, so we could apply the jump to X (`X ← J·X`) without the tracker.
  - Sleep and wake can't be bridged this way, because the stream stops.
  - So those events need a quick re-clip (continuous calibration resumes automatically), or at least a warning to Person B.

### 3.4 Keeping the two players apart
- **Our tool:** forwards only an allowlist of Person B's serials, plus the calibration tracker's serial.
- **Person A's PC VRChat:** gets `--ignore-trackers=<all of B's serials>` in its Steam launch options.
- **OSCQuery cross-talk** (my inference; to verify in H2):
  - Person A's PC VRChat will also discover our service. If A has head/wrist output enabled, it will send A's head pose to us.
  - Our VRChat discovery will also find A's `VRChat-Client`.
  - Accept head poses only from the chosen Quest's source IP, and send only to that Quest.
- **Output:** goes only to the Quest's IP and port 9000. Person A's VRChat listens on the PC, so it never sees it.

### 3.5 Fallback if the head pose is unusable
- **Input-only path:** sending OSC *to* the Quest is the documented, reliable path.
- With the tracker mounted, send a single pulse each of `head/position` and `head/rotation`, computed from the tracker plus a configured offset from tracker to avatar head-bone root. VRChat snaps to it (2026.1.2+).
- **Weaker than calibrating to the headset itself, because:**
  - The offset to the head-bone root depends on the avatar.
  - Any error in the mount's yaw swings all the trackers around the head.
  - It can't detect drift.
- **Last resort:** upstream's approach, VRChat's Auto-center button.

### 3.6 Development loop
- The development PC has no VR hardware, so the tool should treat its inputs as interchangeable: live OpenVR and OSC, recorded files, or synthetic data.
- On the friend's PC it records sessions; here, the solver is developed and tested against those recordings and against synthetic data with known X, M, noise and latency.

## 4. Hardware tests (before committing to the full implementation)

One probe/recorder tool covers H1–H5. Order is by risk; H2 and H5 decide the design.

**H1. PC side** (no Quest needed)
- **Answers:** do both players' trackers coexist; are raw poses unaffected by PCVR playspace changes; does `--ignore-trackers` work?
- **How:**
  - With the Beyond active, list devices: serial, class, tracking system, universe ID.
  - Log a stationary Person B tracker in raw space while Person A moves their playspace (OVR Advanced Settings), runs room setup, and restarts SteamVR.
  - Run Person A's VRChat with `--ignore-trackers`.
- **Pass:**
  - The raw pose stays within 1 mm / 0.1° and the universe ID doesn't change.
  - Person B's trackers are absent from Person A's FBT calibration.

**H2. Quest head pose arrives**
- **Answers:** is the OSCQuery return path reliable enough?
- **How:**
  - Advertise `/tracking/vrsystem`. On the Quest, enable OSC and "Allow Sending Head and Wrist…".
  - Log source IP, message rate, gaps and bundle timestamps for 30 min or more, through a world change, a VRChat restart, Quest sleep/wake and a probe restart.
  - Check whether Person A's VRChat also sends to the probe.
- **Pass:**
  - A steady rate (expect roughly the frame rate) with no multi-second gaps.
  - Data resumes without manual steps after each interruption.
- **If it fails, check:**
  - The Windows network profile is set to Private.
  - The firewall rule for Python.
  - The router isn't blocking multicast, and AP isolation is off.

**H3. What the head pose means**
- **Answers:** which space and scale is it in, and what happens to it on recentre, sleep and guardian events?
- **How:**
  - Thumbstick-move and snap-turn: if the pose changes, it's world space, not tracking space, which would change the design.
  - Change avatar and avatar height.
  - Nod, turn and tilt to check signs.
  - Recentre, redraw the guardian, and sleep/wake, logging any jumps.
- **Pass:** it's in tracking space, the scale is understood, and the sign conventions are confirmed.

**H4. OSC trackers in Quest VRChat**
- **Answers:** does input arrive correctly; is the input space with no head data the same as the head-pose space; what send rate?
- **How:**
  - Send a synthetic tracker 40 cm in front of the received head pose, without head messages. Make the tracker models visible with "Tracker: System".
  - Rotate it about each axis in turn.
  - Try 30, 60, 90 and 120 Hz, and bundled vs separate messages.
- **Pass:** the model stays in front of the face as you move, and rotates the right way.

**H5. Calibration and drift**
- **Answers:**
  - Does the solver work on real data?
  - Is calibrate-then-remove good enough?
  - Which events break it, and can jumps be detected without the tracker?
  - Does the mount reattach repeatably?
  - What are the scale and the time offset?
- **How:**
  - Mount the tracker and record raw Lighthouse poses (about 250 Hz) plus head poses.
  - Play normally for 30–60 min with the tracker left on. Include a minute of deliberate nods and tilts, so we can compare it with natural motion alone.
  - Note the times of each recentre, sleep/wake and boundary redraw.
  - Remove and reattach the tracker a few times.
  - Analyse offline:
    - Replay the recording through the continuous solver.
    - Measure how quickly it converges from natural motion.
    - Measure the latency estimate.
    - Measure drift if X is frozen at various points.
- **Pass:**
  - The fit error is under about 1–2 cm / 1–2°.
  - We have a measured drift curve for a calibration fixed at the start.

H6, end-to-end in a normal VRChat session, comes after implementation.

## 5. Decisions needed

1. **Language.** I recommend Python: it's what upstream uses, the maths is a few NumPy calls, the load is tiny (8 trackers at about 100 Hz), and it can run from source on the friend's PC. The libraries are pyopenvr, numpy/scipy, python-osc and python-oscquery/zeroconf. The alternatives:
   - **C#:** VRChat's official OSCQuery library, but serving HTTP on the LAN through .NET's `HttpListener` needs an admin URL ACL.
   - **C++:** Space Calibrator's code could be reused directly, but iteration is slowest.
2. **Python version.** If Python: 3.12 or 3.13 (python-oscquery needs at least 3.10).
3. **Repository shape.** I recommend rewriting in place on the fork, keeping upstream's MIT notice, rather than starting a new repository.
4. **Getting recordings back.** How recordings reach the development side: a branch on the fork, or some other channel.
5. **First deliverable.** Go-ahead to build the H1–H5 probe/recorder first.

Decided:
- Calibration starts as pausable continuous calibration from natural motion.
- Guided poses are a fallback only.
- **Python 3.13**, managed with uv, so the VR PC needs no separate Python install.
- **In place on the fork:** the code is new, and the `qsft` package sits beside the upstream script until the forwarder replaces it.
- **Library choices:**
  - OSCQuery advertising uses `python-oscquery`; discovery uses `zeroconf` directly, so every event can be logged.
  - OSC parsing walks python-osc's `OscBundle` directly. The dispatcher would sleep until the Quest-clock time tags, and `OscPacket` replaces past time tags with the arrival time.
- **Probe built.** The H1–H5 tool is `qsft.probe`; instructions are in [PROBE.md](PROBE.md). It records sessions to `recordings/` for offline solver work.

For later:
- UI: tray, console or local web page.
- In-headset feedback for Person B:
  - The VRChat chatbox via OSC works but is visible to the whole instance.
  - Avatar parameters need avatar changes.
  - The PC speakers are another option.
- Packaging: fix CI on the fork, or keep running from source.

## Sources

- VRChat: [OSC trackers](https://docs.vrchat.com/docs/osc-trackers), [2026.1.2](https://docs.vrchat.com/docs/vrchat-202612), [2023.3.1](https://docs.vrchat.com/docs/vrchat-202331), [2025.3.3](https://docs.vrchat.com/docs/vrchat-202533), [launch options](https://docs.vrchat.com/docs/launch-options), [OSCQuery wiki](https://github.com/vrchat-community/osc/wiki/OSCQuery), [vrc-oscquery-lib](https://github.com/vrchat-community/vrc-oscquery-lib), [head/wrist bug report](https://feedback.vrchat.com/bug-reports/p/osc-head-and-wrist-tracking-not-working-inconsistent)
- SlimeVR: [VRCOSCHandler.kt](https://github.com/SlimeVR/SlimeVR-Server/blob/main/server/core/src/main/java/dev/slimevr/osc/VRCOSCHandler.kt), [VRCOSCQueryHandler.kt](https://github.com/SlimeVR/SlimeVR-Server/blob/main/server/core/src/main/java/dev/slimevr/osc/VRCOSCQueryHandler.kt), [oscquery-kt](https://github.com/SlimeVR/oscquery-kt), [OSC docs](https://docs.slimevr.dev/server/osc-information.html)
- Space Calibrator: [pushrax](https://github.com/pushrax/OpenVR-SpaceCalibrator), [bdunderscore](https://github.com/bdunderscore/OpenVR-SpaceCalibrator), [hyblocker](https://github.com/hyblocker/OpenVR-SpaceCalibrator), [charliee1w](https://github.com/charliee1w/OpenVR-SpaceCalibrator-Quest)
- Python OSCQuery: [python-oscquery](https://github.com/fraklein/python-oscquery), [tinyoscquery](https://github.com/cyberkitsune/tinyoscquery)
- Calibration theory: Shah 2013 (Kronecker-product AX=YB); Dornaika & Horaud 1998; Park & Martin 1994 (AX=XB)
