# xTool D1 driver for Rayforge

Serial (USB) and Wi-Fi drivers, with device profiles, that let Rayforge 1.13
run an xTool D1 / D1 10W diode laser from Linux.

**Experimental.** Written with AI assistance and tested on one machine (a
D1 10W on firmware 40.30.002.01 B1 and then 40.30.010.01 B2, Rayforge
1.13.0; see [Firmware](#firmware)). It has not been reviewed
line by line by someone who knows Rayforge's internals. A laser can start
fires and damage eyes. Use it at your own risk.

## Install

1. In Rayforge, open Settings > Addons, install a new addon, click
   **Install from URL...** and paste
   `https://github.com/chrismyers2000/rayforge-xtool-d1`.
2. **Restart Rayforge once.** The device profiles are only copied in at
   startup.
3. Add a machine and pick **xTool D1 10W (Wi-Fi)** or **xTool D1 10W**
   (USB). The hardware details come pre-filled.
   - Wi-Fi: enter the D1's IP address and port `8080`. The D1 doesn't
     announce itself, so Rayforge can't find it automatically.
   - USB: pick the port, or enter `1a86:7523` under "Match by USB VID:PID"
     so it survives a power cycle.

A "No stored version ... UnknownVersion" warning in the log is harmless.

**Manual install:** close Rayforge, copy this folder to
`~/.config/rayforge/addons/xtool_d1`, and start Rayforge.

## Which driver?

| | Wi-Fi | USB |
|---|---|---|
| Cable | none | required |
| Start a job | the D1's button (see [Firmware](#firmware) for what each press does) | from Rayforge |
| Pause / stop | Rayforge's Pause and Cancel on firmware 40.30.010.01 or newer; the button pauses on 40.30.002.01; power switch | power switch only |
| Job running / idle in Rayforge | 40.30.010.01 or newer | no |
| Position, jogging | no | yes |

## Firmware

The drivers read the D1's firmware version when they connect (shown in
Rayforge's log) and only use features that version has. Older firmware
keeps working, without the newer features.

| Feature | 40.30.002.01 B1 | 40.30.010.01 B2 |
|---|---|---|
| USB: connect, jog, stream jobs | verified | connect verified; jobs not re-tested |
| Wi-Fi: upload, start with the button | verified | verified |
| Wi-Fi: Pause, Resume, Cancel from Rayforge | not available (the firmware has no such call) | verified with the laser off |
| Wi-Fi: job running / idle shown in Rayforge | not available | verified (Pause becomes clickable while a job runs) |
| Laser-on lines and contours (Wi-Fi) | verified | verified: a 20 mm line and a Rayforge-made 22 × 20 mm square at 10%, 600 mm/min |
| Rasters, crosshair (`M106`), auto start | verified | not re-tested |
| The D1's button on an uploaded job | press 1 frames, press 2 runs, press 3 pauses | press 1 runs the job (observed once) |

- On 40.30.010.01, Cancel sends the D1's stop call and then `M5`; what the
  laser does after a stop has not been checked, so the `M5` is a
  precaution.
- A paused job still reports "running" on the D1; the driver remembers
  that it paused it.
- On 40.30.010.01 the crosshair light stays on while the D1 is idle.
  What a later button press does during a job is not yet known.
- Unknown or unreadable version: treated like 40.30.002.01.
- Updating: xTool's own tools (the mobile app, xTool Creative Space) are
  the supported route. On one D1 the mobile app's Wi-Fi update failed from
  40.30.002.01; it was updated over USB with `esptool` instead (only the
  app partition at `0x10000` written, Wi-Fi settings kept). Back up the
  flash first if you try that.

## Safety

- **Auto start can fire the cutting laser** (about 10%) even when the file
  has no `M3`. Started from the D1's button, the same file did not. Don't
  use auto start without eye protection and material under the head.
- **Know your stop.** On Wi-Fi with firmware 40.30.010.01 or newer,
  Rayforge's Cancel stops the job (verified with the laser off). On
  40.30.002.01, a third button press pauses and the power switch stops it.
  **On 40.30.010.01 the first button press runs the job straight away, with
  no framing pass.** On
  USB, only the power switch is verified: Rayforge's Cancel and the D1's
  button do not stop a streamed job.
- **The D1 cannot home.** It treats wherever the head is at power-on as
  (0, 0). Park the head in the upper-left corner before powering on, and
  power-cycle after moving it by hand.
- **Wi-Fi has no position feedback**, so nothing warns you if the zero
  point is wrong. Keep soft limits on with USB.
- Check where a job starts (the first `G0`) before running it. Imported
  designs may sit at X=0.
- Wear laser-rated eye protection and keep a hand near the power switch.

## Supported

- Cutting and engraving, including variable-power rasters (tested over
  Wi-Fi).
- Framing: Rayforge's Frame sends an outline job, and the Wi-Fi driver
  keeps the D1's crosshair light on for it (`M106 S1`). On 40.30.002.01 the
  D1 also frames on the first button press; on 40.30.010.01 it does not,
  so use Rayforge's Frame first.

Not tested: Z axis, rotary, firmware versions other than the two above,
and other D1 models.

## Tips

- **Raster engraving:** set **Power Levels to 100**. At Rayforge's default
  of 25, a five-shade image came out with only two power levels.
- **Min/Max Power are percentages of the step's Power**, not of the laser.
- **Frame Repeat Count:** Rayforge defaults to 20 passes; the profiles set 5.
- **Dense fills burn deep.** Use much lower power than for a single line.
- **Auto start** (Wi-Fi) starts the uploaded job without a button press,
  skipping the D1's own frame step. It uses an undocumented call, is off by
  default, and has been seen to switch the cutting laser on. Prefer the
  button.
- Each Wi-Fi upload is saved as `xtool-d1-last-job.gcode` in Rayforge's
  config folder, so you can read it before running it.

## Known quirks

- The D1 boots in "app mode", where its button ignores an uploaded job
  until it receives `$I`. Both drivers send it for you. A button stop drops
  the D1 back to app mode, so the Wi-Fi driver sends it again after each
  upload.
- The D1 abandons any single move after about 3 s. The USB driver splits long
  moves into segments of at most 1 s.
- The D1 stalls between streamed moves unless the port stays busy, so the
  USB driver polls position every 0.2 s during a job.
- The D1 has no jog command, so USB jogging sends `G91`, a relative `G0`,
  then `G90`.
- Power-cycling the D1 re-enumerates its USB adapter (often
  `/dev/ttyUSB1` instead of `0`). Use the VID:PID entry above.

## Development

- `rayforge-addon.yaml`: addon manifest.
- `xtool_d1/backend.py`: registers the drivers and installs the profiles.
  Rayforge 1.13 never calls the `register_machines` hook, so this happens
  in `rayforge_init`.
- `xtool_d1/driver.py`, `pacing.py`: the USB driver and its move pacing.
- `xtool_d1/wifi_driver.py`: the Wi-Fi driver (HTTP API on port 8080).
- `xtool_d1/firmware.py`: firmware version parsing and which version has
  what.
- `xtool_d1/profile/`: device and G-code dialect definitions.
- Tests: from a Rayforge checkout, run
  `pixi run python -m unittest discover -s <addon>/tests` (the Wi-Fi tests
  need Rayforge's environment).
- For development, symlink this folder into the addons folder.

MIT licensed.
