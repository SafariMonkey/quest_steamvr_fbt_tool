# Quest SteamVR FBT bridge

This tool forwards SteamVR Lighthouse trackers to VRChat running standalone on a Quest. It calibrates them to the Quest's own tracking space, so a second person can use Lighthouse full-body tracking on a standalone Quest while sharing a PC with a PCVR player.

It started as a fork of [takana-v/quest_steamvr_fbt_tool](https://github.com/takana-v/quest_steamvr_fbt_tool) (MIT) and has been rewritten. The debugging tools (hardware probe, fake VRChat, recording replay) and the original investigation notes are on the `probe` branch.

## How it works

- **Trackers:** the bridge reads Person B's trackers from SteamVR's *raw* tracking space. That space is fixed to the base stations, so the PCVR player's room setup and playspace moves don't affect it.
- **Head pose:** the bridge advertises itself over OSCQuery, and VRChat on the Quest then sends it the headset pose.
- **Calibration:** a Lighthouse tracker mounted on the Quest links the two tracking systems.
  - The bridge solves the tracker's mounting offset on the headset once, saves it to `calibration.json`, and refines it during use.
  - While that tracker is on, the bridge keeps re-estimating the Lighthouse-to-Quest alignment. This includes after the Quest re-localises, for example after the headset is taken off and put back on.
- **Forwarding:** the other trackers go to VRChat as OSC trackers, extrapolated slightly ahead to hide Wi-Fi delay.

## Setup

Do this once, on the PC the trackers are connected to.

1. **Install uv:** `winget install --id=astral-sh.uv -e`
2. **Get the code:** `git clone -b bridge <fork URL>`, then `cd quest_steamvr_fbt_tool`, then `uv sync`. To update later: `git pull`, then `uv sync`.
3. **Network:**
   - The PC's network profile must be **Private**.
   - When Windows asks, allow Python through the firewall on private networks.
   - The Quest and the PC must be on the same LAN, with no AP or client isolation.
4. **Config:**
   1. Start SteamVR and run `uv run python -m qsft.identify`.
   2. Shake each of Person B's trackers (or turn them on one at a time) to see its serial.
   3. Copy `qsft.example.toml` to `qsft.toml` and fill in `trackers` (VRChat's tracker numbers follow this order) and `calibration_tracker`.
   4. Setting `quest_ip` is recommended.
5. **Mount** the calibration tracker on the Quest.

**VRChat on the Quest:**
- Turn on OSC: Action Menu → Options → OSC.
- In **Tracking & IK**:
  - Turn on **Allow Sending Head and Wrist VR Tracking OSC Data**.
  - Set **Avatar Measurement** to **Height**, then stand straight and use **Set Standing Height**.

**VRChat on the PC (Person A):** add `--ignore-trackers=LHR-…,LHR-…` to the launch options, listing all of Person B's serials, including the calibration tracker.

## Use

1. Start SteamVR, with the calibration tracker on the headset, and start VRChat on the Quest.
2. Run `uv run python -m qsft.bridge`.
3. **First time only:** move your head around (turn, nod, tilt) until it prints `mount offset solved`. This takes about 10–20 s.
4. Once the status shows `sending to …`, run VRChat's full-body calibration.

**While playing:**
- **Calibration tracker on the headset:** the alignment survives the Quest re-localising.
- **To take it off:** type `p` then Enter to pause, or switch the tracker off. The last alignment is kept until the Quest re-localises.
- **Console commands:**
  - `p`: pause or resume calibration.
  - `m`: re-solve the mounting offset, e.g. after re-mounting the tracker differently.
  - `q`: quit.
- **Options:**
  - `--predict-ms N` (default 30): how far ahead to extrapolate the trackers.
  - `--head-lead-ms N` (default 40): how far VRChat's head pose runs ahead of real time.
  - `--record`: saves the session to `recordings/`, for diagnosing problems. Any text you type marks the recording.

## Troubleshooting

**Trackers sit a constant distance below or above your controllers, or your feet are in the floor:**
- **Likely cause:** VRChat's **Avatar Measurement** is set to **Arm Span**. VRChat then moves the world's floor up or down (about 20 cm in testing) and reports your headset and controllers relative to it. But it places incoming trackers relative to the real floor.
- **Fix:** set Avatar Measurement to **Height**, then use **Set Standing Height**.

**Trackers jump or turn after taking the headset off and on:** the Quest re-localised its tracking space.
- With the calibration tracker on the headset, the bridge re-aligns within a couple of seconds.
- Otherwise, put the tracker back on and move your head for a few seconds.

**No head pose (the status says so, or shows a HINT):**
- In VRChat, turn "Allow Sending Head and Wrist VR Tracking OSC Data" off and on, or restart VRChat.
- Check the PC's network profile is Private, and the PC and Quest are on the same network.

**A tracker freezes:** it has lost sight of the base stations. VRChat keeps its last pose.

**Person A's VRChat uses Person B's trackers:** add the `--ignore-trackers` launch option.

## VRChat behaviour this relies on or works around

- **Head pose:** sent on `/tracking/vrsystem/head/pose` at about 50 Hz, only to apps discovered over OSCQuery. It runs about 40 ms ahead of real time, because it's display-predicted.
- **Time tags:** the fraction of a second in the head pose's bundle time tags is scaled by 5×10⁹ instead of 2³². The bridge decodes it.
- **Discovery:** VRChat ignored an OSCQuery service re-advertised under the same name with new ports, so each run uses a fresh name.
- **Head alignment:** sending VRChat a head alignment (`/tracking/trackers/head/*`) made it scale the tracker space by about 0.87. The bridge doesn't send one.

## Development

Run the tests with `uv run pytest`.

## Licence

MIT; see `LICENSE`. The original tool is by takana-v.
