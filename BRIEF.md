# Project brief: SteamVR tracker bridge for standalone Quest VRChat

## Goal

Extend [quest_steamvr_fbt_tool](https://github.com/takana-v/quest_steamvr_fbt_tool) to support two people playing VRChat simultaneously with full-body tracking, using only one PC.

The project already forwards SteamVR tracker poses to VRChat over OSC. We want to build on it rather than start from scratch.

The main missing pieces are compatibility with our shared-PC arrangement and proper calibration between the two tracking spaces.

## Hardware and setup

**Person A — PCVR**

- Bigscreen Beyond connected to the PC.
- SteamVR running normally, with Lighthouse base stations.
- PC VRChat using Lighthouse full-body trackers and Index controllers.

**Person B — standalone Quest**

- Quest Pro running standalone Android VRChat over the same local network.
- Separate Lighthouse full-body trackers, whose dongles connect to Person A's PC.
- Tracker poses forwarded from SteamVR to Quest VRChat over OSC.
- A Lighthouse tracker can temporarily be attached to the Quest Pro using an existing mount.

Both sets of Lighthouse trackers must coexist in the PC's SteamVR environment, without Person A's VRChat accidentally adopting Person B's trackers.

VRChat has an official `--ignore-trackers=serial1,serial2` launch option for excluding selected SteamVR trackers from its own FBT handling.

The Beyond remains the active SteamVR HMD. The Quest does **not** connect to SteamVR as a second HMD.

## Starting repository

https://github.com/takana-v/quest_steamvr_fbt_tool

The upstream is a small Python project, last updated in November 2022, under the MIT licence.

It already:

- Reads tracker poses through OpenVR.
- Selects devices by serial number.
- Forwards position and rotation to VRChat's OSC tracker endpoints.
- Supports up to eight trackers, with some basic configuration and height-offset handling.

Its README assumes SteamVR is running with a dummy HMD driver. That shouldn't be necessary when the Beyond is already active, but needs testing.

**Inspect the implementation before assuming its outputs are correct.** In particular, check coordinate conversions and Euler angle conventions: the current code appears to calculate angles in radians, whereas VRChat's OSC tracker API expects degrees.

The aim is to reuse what's useful, not preserve every implementation decision.

## The main challenge: tracker-space calibration

Lighthouse tracking and Quest inside-out tracking have different coordinate origins and orientations.

OpenVR Space Calibrator ordinarily solves this by comparing devices tracked in both spaces. However, the Quest here is standalone, so it cannot simply be selected as another SteamVR-tracked headset.

We need an equivalent calibration process that works across the network.

### Preferred user experience

We have a physical Lighthouse tracker mount for the Quest Pro.

Ideally:

1. Attach the tracker to the Quest Pro.
2. Perform calibration using the Quest's head pose and the Lighthouse tracker's pose.
3. Remove the tracker to save weight.
4. Continue using full-body tracking with the saved calibration.

Leaving the tracker mounted should also be a possible basis for continuous alignment, but continuous tracking is not a requirement for the initial workflow.

**The mounting offset matters.** The Lighthouse tracker isn't at the Quest's reported head origin and might not share its orientation. Calibration needs to account for this automatically, rather than assuming both devices occupy the same point.

This is closely related to the problems already solved by OpenVR Space Calibrator. Investigate its calibration methods and mathematical approach before inventing a new one.

The one-time and continuous cases may be able to share the same underlying calibration mechanism.

We expect some Quest tracking drift over time, and Quest recentering or tracking-origin changes may invalidate a saved alignment. These behaviours should be investigated rather than assumed.

### VRChat OSC capabilities

Official documentation: https://docs.vrchat.com/docs/osc-trackers

VRChat accepts:

- `/tracking/trackers/1..8/position`
- `/tracking/trackers/1..8/rotation`
- `/tracking/trackers/head/position`
- `/tracking/trackers/head/rotation`

The head messages help VRChat align an external tracking space. Its head-position alignment targets the avatar's head bone, not exactly the HMD's tracked origin, so this doesn't automatically solve the tracker mounting offset.

VRChat 2026.1.2 also introduced single-pulse head-position alignment, complementing the existing one-shot rotation alignment:

https://docs.vrchat.com/docs/vrchat-202612

This may be useful for one-time calibration.

### Receiving the Quest's head pose

VRChat can expose headset tracking data through OSCQuery, including:

`/tracking/vrsystem/head/pose`

The user must enable **Allow Sending Head and Wrist VR Tracking OSC Data** in VRChat's Tracking & IK settings.

SlimeVR already uses this integration to obtain the standalone Quest's head pose, build its body tracking relative to that head pose, and send OSC tracker data back to VRChat.

Relevant implementation:

- https://github.com/SlimeVR/SlimeVR-Server/blob/main/server/core/src/main/java/dev/slimevr/osc/VRCOSCHandler.kt
- https://github.com/SlimeVR/SlimeVR-Server/blob/main/server/core/src/main/java/dev/slimevr/osc/VRCOSCQueryHandler.kt

SlimeVR's body tracking differs from our absolute Lighthouse tracker tracking, but its OSCQuery integration should provide a useful reference.

There was a June 2026 VRChat bug report about unreliable Quest head/wrist pose output:

https://feedback.vrchat.com/bug-reports/p/osc-head-and-wrist-tracking-not-working-inconsistent

Given that SlimeVR uses this feature, it's plausible that the issue was situational, but we should verify that the head pose actually arrives reliably before depending on it.

## Things to investigate and verify

- Whether the upstream bridge works correctly with an active Beyond and multiple sets of Lighthouse trackers.
- Whether tracker serial selection reliably separates the two players' devices.
- Whether OSC tracker positions and rotations arrive correctly in standalone Quest VRChat.
- Whether the Quest provides usable outgoing head-pose data over OSCQuery.
- How best to calibrate the Lighthouse-to-Quest transform, including the physical headset-to-tracker offset.
- Whether a saved calibration remains accurate enough after the calibration tracker is removed, and what events require recalibration.

The eventual goal is a practical tool, not merely a calibration demonstration. It should forward the selected trackers correctly throughout a normal VRChat session without disrupting the PCVR player.

## Development environment and GitHub rules

Development will take place **on my own PC, accessed through a remote desktop session**. The actual VR hardware is connected to a friend's PC.

I don't want to run a coding agent on my friend's computer.

I'll create a personal fork and clone it. We can exchange changes through that repository and run builds on my friend's PC for hardware testing.

GitHub access is read-only except for making commits to my personal forks of non-Khronos repositories. Do not create forks, PRs, issues, comments, reviews, reactions, stars, or otherwise interact with GitHub on my behalf. In particular, do not interact with upstream repositories.

## Design latitude

This brief describes the problem and the approaches we've discussed, **not a fixed implementation specification**.

Please inspect the existing repository and relevant implementations, identify the gaps, and propose a sensible path forward.

Don't assume decisions about programming language, architecture, dependencies, UI, packaging, calibration algorithm, or other implementation details that we haven't discussed.

Prefer finding and adapting existing working solutions where practical.

Start by establishing what the existing code already handles, what needs to change for our setup, and what we need to test on the real hardware before committing to an implementation approach.
