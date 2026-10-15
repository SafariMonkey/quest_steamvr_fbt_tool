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
  - `q`: quit.
  - Any other text marks the recording.
- **Tuning:**
  - `--predict-ms N` (default 30) extrapolates the trackers ahead to hide Wi-Fi and VRChat delay. Lower it if the trackers jitter or overshoot; raise it if they lag.
  - `--head-lead-ms N` (default 40) is how far VRChat's head pose runs ahead of real time.

Every session is recorded to `recordings/` (add `--no-record` to skip this). Send the file back if anything looks wrong.
