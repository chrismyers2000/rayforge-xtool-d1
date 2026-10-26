# xTool D1 driver for Rayforge

Serial (USB) and Wi-Fi drivers, with device profiles, that let Rayforge 1.13
run an xTool D1 / D1 10W diode laser from Linux.

**Experimental.** Written with AI assistance and tested on one machine (a
D1 10W, firmware 40.30.002.01 B1, Rayforge 1.13.0). It has not been reviewed
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
| Start a job | the D1's button: press 1 frames the job, press 2 runs it | from Rayforge |
| Pause / stop | button press 3 pauses; power switch stops | power switch only |
| Position, jogging | no | yes |

## Safety

- **Wi-Fi jobs may fire the cutting laser even when the file has no `M3`.**
  See "Supported". Keep eye protection on and material under the head.
- **Know your stop.** On Wi-Fi, a third button press pauses the job;
  the power switch stops it. On USB, only the power switch is verified:
  Rayforge's Cancel and the D1's button do not stop a streamed job.
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
- Framing (an outline job). **Known problem:** over Wi-Fi the D1 has been
  seen switching its cutting laser on (about 10%) and its framing light on
  while running an uploaded job that contains no `M3` and no `M106 S1`.
  Treat every Wi-Fi job, including frames, as laser-on until this is
  understood. The cause is unknown.
- Circles, curves and text. The D1 ignores `G2`/`G3` arcs, so Rayforge
  converts them to short straight moves.

Not tested: Z axis, rotary, other firmware versions and other D1 models.

## Tips

- **Raster engraving:** set **Power Levels to 100**. At Rayforge's default
  of 25, a five-shade image came out with only two power levels.
- **Min/Max Power are percentages of the step's Power**, not of the laser.
- **Frame Repeat Count:** Rayforge defaults to 20 passes; the profiles set 5.
- **Dense fills burn deep.** Use much lower power than for a single line.
- **Auto start** (Wi-Fi) starts the uploaded job without a button press. It
  uses an undocumented call, is off by default, and will start a laser-on
  job immediately. Only enable it if you accept that.
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
- Rayforge sends `S102` for a 10% setting. I don't know why it isn't `S100`.
- Power-cycling the D1 re-enumerates its USB adapter (often
  `/dev/ttyUSB1` instead of `0`). Use the VID:PID entry above.

## Development

- `rayforge-addon.yaml`: addon manifest.
- `xtool_d1/backend.py`: registers the drivers and installs the profiles.
  Rayforge 1.13 never calls the `register_machines` hook, so this happens
  in `rayforge_init`.
- `xtool_d1/driver.py`, `pacing.py`: the USB driver and its move pacing.
- `xtool_d1/wifi_driver.py`: the Wi-Fi driver (HTTP API on port 8080).
- `xtool_d1/profile/`: device and G-code dialect definitions.
- Tests: from a Rayforge checkout, run
  `pixi run python -m unittest discover -s <addon>/tests` (the Wi-Fi tests
  need Rayforge's environment).
- For development, symlink this folder into the addons folder.

MIT licensed.
