# Hardware probe: test instructions

This guide is for whoever runs the hardware tests on the PC that has the VR hardware attached.

The probe is a command-line tool that answers the hardware questions in [INVESTIGATION.md](INVESTIGATION.md) §4 (H1–H5) before the real bridge is built. It changes nothing in SteamVR or VRChat.

Every command records what it sees to `recordings/` so the data can be analysed afterwards. Those files are the main output of each session; send them back after each one (see the end of this guide).

## One-time setup (the VR PC)

1. **Install uv**, which also installs the right Python version:
   `winget install --id=astral-sh.uv -e`
2. **Get the code and its dependencies:**
   `git clone -b probe <fork URL>`, then `cd quest_steamvr_fbt_tool`, then `uv sync`.
   To update later: `git pull`, then `uv sync`.
3. **Check the network** (on this PC):
   - The network profile must be **Private**: Settings → Network & Internet → (your connection) → Properties. Windows blocks mDNS discovery on Public networks.
   - The first time the probe runs, Windows Firewall may ask about Python. Allow it on **private networks**.
   - The Quest and this PC must be on the same LAN subnet. Wi-Fi "AP isolation" or "client isolation" on the router must be off.
4. **Fill in the config:**
   1. Start SteamVR with every tracker on.
   2. Run `uv run python -m qsft.probe devices` and note Person B's tracker serials.
   3. Copy `qsft.example.toml` to `qsft.toml` and fill those serials in.
   4. Also set `quest_ip` (on the Quest: Settings → Wi-Fi → your network's details). This makes the probe advertise on the network adapter that actually reaches the Quest, which matters if this PC has a VPN adapter (Tailscale, ZeroTier, etc.).
5. **Quest VRChat settings:**
   - Turn OSC on: Action Menu → Options → OSC → Enabled.
   - Turn on **Allow Sending Head and Wrist VR Tracking OSC Data** (Settings → Tracking & IK) and accept its notice.
   - For H4, set the tracker model to **System** (Tracking & IK) so the tracker models stay visible.
6. **Person A's PC VRChat:**
   - Add `--ignore-trackers=LHR-…,LHR-…` to its Steam launch options. List all of Person B's serials, comma-separated, including the calibration tracker.
   - Leave Person A's "Allow Sending Head and Wrist…" **off**. If it's on, Person A's head pose is sent to the probe too. The probe labels anything from this PC as `LOCAL`.

**Commands below are all `uv run python -m qsft.probe <command>`. While one is running:**
- Type a note and press Enter to add a timestamped mark to the recording, e.g. `recentre`, `sleep`, `tracker removed`.
- Type `q` and press Enter (or press Ctrl+C) to stop.
- Add `--help` to any command to see its options.

## H1 — PC side (no Quest needed)

**Coexistence**
- With the Beyond active, run `devices`.
- **Check:**
  - Both players' trackers are listed with distinct serials.
  - Every Lighthouse device shows the same universe number.

**Playspace independence**
1. Put one of Person B's trackers somewhere it can't move.
2. Run `pc-watch --serial LHR-…`.
3. Person A does each of these, typing a mark for each:
   - Moves their playspace with OVR Advanced Settings (Offsets, or space drag).
   - Runs SteamVR's room setup or quick calibration.
- **Check:** "raw moved" stays under about 1 mm and 0.1°. "std moved" changes. It also prints "Standing-universe offset changed".

**SteamVR restart**
1. With the tracker still in place, stop `pc-watch`.
2. Restart SteamVR and run `pc-watch` again for 30 s.
3. Comparing the two recordings shows whether raw space survives a restart.

**Ignore list**
- Start Person A's VRChat with the `--ignore-trackers` option, and open Person A's full-body calibration.
- **Check:** Person B's trackers don't appear.

## H2 — Does the Quest's head pose arrive?

**Run** `listen`.
- Add `--quest-ip <Quest IP>` if you know it. Otherwise the probe finds VRChat via mDNS.

**Then, in Quest VRChat**, make sure OSC and "Allow Sending Head and Wrist…" are on.

**Good signs, in order:**
1. `mDNS added: VRChat-Client-…` with the Quest's IP.
2. `OSCQuery request from <Quest IP>`: VRChat has found the probe.
3. `First /tracking/vrsystem/head/pose from <Quest IP>`.
4. Status lines show the head pose at a steady rate (expect roughly the Quest's frame rate, 72–90 Hz), with max gaps well under 100 ms.

**Leave it running for 30 minutes or more**, typing a mark before each of these:
- Change world.
- Restart VRChat.
- Take the headset off until it sleeps, then put it back on.
- Stop and restart the probe.

After each one, note whether data resumes by itself and how long it takes.

**If nothing arrives:**
1. Check the network items in setup step 3.
2. Turn VRChat's "Allow Sending…" option off and on again.
3. Restart VRChat.
4. Retry with `listen --tree bare`, which advertises the way SlimeVR does.

Note in the session which of these, if any, helped.

## H3 — What the head pose means

Keep `listen` running and do each of these, typing a mark first:

| Action | What it tells us |
|---|---|
| Move with the thumbstick, then snap-turn | Whether the pose is in real-world tracking space or VRChat world space |
| Change avatar, and change avatar scale or height | Whether the pose is scaled by the avatar |
| Nod, turn left and right, tilt the head side to side | Checks the sign conventions |
| Recentre (hold the Meta button), redraw the boundary, sleep and wake | How tracking-origin changes show up; look for `Head pose jump` lines |

## H4 — OSC trackers in Quest VRChat

**Run** `send-test`. It sends one synthetic tracker. It needs a working head pose (H2); if there is none, see the last mode in the table.

| Mode or command | Expected in VRChat |
|---|---|
| `f` (follow, default) | A tracker model about 40 cm in front of your face at head height, staying in front as you walk and turn (with some lag) |
| `x` | It pitches (nods) about its left-right axis |
| `y` | It turns left and right about its vertical axis |
| `z` | It rolls about the axis pointing at your face |
| `0` | Stops spinning |
| `a` (anchor) | It stays fixed in the room 50 cm in front of where your head was. Walk around it, and try thumbstick movement (mark what happens) |
| `rate 30`, `rate 60`, `rate 120`, `bundle` | Compare smoothness. `bundle` toggles between one combined packet and separate ones |
| `send-test --mode fixed --quest-ip <IP>` | Works without the head pose: a tracker at (0, 1, 0.5) in VRChat's playspace |

Note anything that doesn't match, for example "rotates the opposite way on `x`" or "appears behind me".

## H5 — Calibration recording

Mount the calibration tracker on the Quest Pro. Then run `record`, which uses the serials in `qsft.toml`.

**Check first:** every serial shows "valid" near 100%, and the head pose is arriving.

1. **About 5 min, natural movement:** walk around the play space and look around. Then type `deliberate`, spend a minute nodding, tilting and turning your head (±30–45°), and type `natural`.
2. **30–60 min of normal play,** with the tracker left on. Type a mark for every recentre, sleep/wake, boundary redraw, or anything else odd.
3. **Near the end:** remove and refit the tracker three times. Type `tracker removed` and `tracker refitted` each time, and move your head around for about 30 s after each refit.

## Sending recordings back

- Recordings are in `recordings/` as `<date>-<time>-<command>.jsonl.gz`. A `record` session at the default 120 Hz is roughly 100 MB per hour.
- GitHub rejects files over 100 MB, so use a file-sharing link rather than committing large recordings.
- Along with the files, send a short note of what happened and anything that looked wrong.
