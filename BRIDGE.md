# Using the bridge (experimental)

The bridge forwards Person B's Lighthouse trackers to the Quest's VRChat. While the calibration tracker is on the headset, it keeps them aligned with the Quest's tracking space.

## Setup

Setup is the same as for the probe ([PROBE.md](PROBE.md), one-time setup):
- `qsft.toml` lists Person B's `trackers` and the `calibration_tracker`. Trackers are numbered for VRChat in the order they're listed.
- On the Quest, VRChat has OSC on and **Allow Sending Head and Wrist VR Tracking OSC Data** on.
- Person A's VRChat has `--ignore-trackers=…` with all of Person B's serials.

## Run it

1. Start SteamVR, with the calibration tracker on the Quest Pro.
2. Run `uv run python -m qsft.bridge`.
3. **First time only:** move your head around (turn, nod, tilt) until it prints `mount offset solved`. This takes about 10–20 s. The result is saved to `calibration.json`, so later sessions skip this step.
4. Once the status shows `sending to …`, run VRChat's normal full-body calibration (T-pose).

## While playing

- **Calibration tracker on the headset:** the alignment keeps itself up to date. If the Quest's tracking space moves (recentre, relocalisation), it re-aligns within a second or two. You shouldn't need to redo VRChat's calibration.
- **To take it off:** type `p` then Enter to pause, or just switch the tracker off. The last alignment is kept. If the Quest recentres while it's off, the trackers will drift out of place until you put it back on.
- **Console commands:**
  - `p`: pause or resume calibration.
  - `m`: re-solve the mount offset, e.g. after re-mounting the tracker differently.
  - `+` / `-`: raise or lower the trackers 1 cm.
  - `h` (or `h 20` for a 20 s countdown): one VRChat head alignment after a 10 s countdown, so you can get into the play space first (experiment; see Height below).
  - `q`: quit.
  - Any other text marks the recording.
- **Height:** VRChat draws incoming trackers about 21 cm below the coordinates it reports the headset and controllers in. That gap is the same as the height of its reported floor above the real floor. The bridge raises the trackers by that gap, measured from SteamVR's floor, and the status line shows it as `height lift`. If trackers and controllers ever disagree in height, `send-test` wrist mode (PROBE.md) measures the gap directly.
  - **Adjusting in VR:** on one hand, hold grip and trigger, then lift your thumb off the controller and put it back 10 times within 8 s. VRChat sees this as alternating Fist and ThumbsUp.
    - The trackers bob twice: you're in adjust mode.
    - Make a fist and move that hand up or down: the whole tracker space moves with it. Open your hand to let go.
    - Repeat the 10 thumb lifts to finish; the trackers bob once. Adjust mode also ends after a minute without dragging.
    - The result is saved in `vrchat_offset.json` and used from then on, instead of the SteamVR-floor estimate.
    - Known limitation: this relies on the controller's capacitive thumb sensing, which often fails on worn controllers. It's fine on Quest Pro controllers for now, but needs a sturdier trigger long term.
  - **From the PC:** `+` and `-` (then Enter) raise or lower the trackers 1 cm (saved).
  - **Experiment:** type `h` at the PC, then within 10 s put the headset on, stand upright and look ahead. The trackers bob at 3, 2 and 1 seconds to go; two bobs mean it was sent, one long bob means it was refused (your head wasn't at least 1 m up). It sends VRChat one head alignment, taken from the head pose VRChat reports, and turns the height correction off for the session. This replaces any alignment VRChat may have cached. Afterwards, check whether a controller and a tracker held together line up, are still about 21 cm apart, or are now some other distance apart.
  - To fine-tune by a fixed amount, set `height_offset_m` in `qsft.toml`.
  - To turn the SteamVR-floor estimate off, set `align_floor = false`.
- **Tuning:**
  - `--predict-ms N` (default 30) extrapolates the trackers ahead to hide Wi-Fi and VRChat delay. Lower it if the trackers jitter or overshoot; raise it if they lag.
  - `--head-lead-ms N` (default 40) is how far VRChat's head pose runs ahead of real time.

Every session is recorded to `recordings/` (add `--no-record` to skip this). Send the file back if anything looks wrong.
