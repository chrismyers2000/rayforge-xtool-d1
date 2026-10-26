# xTool D1 driver for Rayforge

Two drivers (serial and Wi-Fi) and device profiles that let Rayforge 1.13 run an
xTool D1 / D1 10W diode laser from Linux.

**Status: experimental. Written with AI assistance and tested on one
machine** (a D1 10W, firmware 40.30.002.01 B1, Rayforge 1.13.0). It has
not been reviewed line by line by a person who knows the Rayforge
internals. A laser can start fires and damage eyes. Use it at your own
risk.

## Verified on that machine

- Connect (`$I` handshake, then `?` position polling).
- Jog X and Y, and a 50 mm square returning to (0, 0).
- A real job: a 20 mm line at 10% power, laser off afterwards.
- Long moves (140 mm at 1200 mm/min) run to the end without being cut
  off or stalling, and a 60 mm cut at 10 mm/s (6 s, twice the D1's 3 s
  limit) came out the full length, smooth, with the laser off at the end.
- A 19 mm square contour at 10 mm/s (four corners, 8 s) closed cleanly
  with sharp corners.
- A raster engrave (100 rows of 12 mm, 606 commands, 42 s) streamed
  without stalls or errors and ended with the laser off. The power
  level was constant in that test, so variable-power raster output is
  not verified, and dense fills burn deep very quickly: use much lower
  power than for a single line.

Everything else (variable-power rasters, Z, rotary, settings, curved paths,
other firmware versions) is untested.

## Wi-Fi driver (upload only)

`XToolD1WifiDriver` uploads the job to the D1 over HTTP (port 8080,
`POST /cnc/data?filetype=1`) and you start it with the machine's button.
Verified: connect, upload, a pop-up confirming the upload, a laser-off
19 mm square run from the button, a laser-on 19 mm square at 10%
and 10 mm/s (clean, closed, laser off afterwards), and the button stopping a run in
progress. It has no position, jog, progress or software stop. The D1 does
not announce itself over mDNS (on my network), so enter its IP by hand.
A copy of each uploaded job is saved to
`xtool-d1-last-job.gcode` in Rayforge's config folder. Framing works: Rayforge's
Frame sends a laser-off outline job, and the driver turns the crosshair
(`M106 S1`) on for it. Variable-power rasters work over Wi-Fi: with Rayforge's
**Power Levels at 25** a five-band test image collapsed to two powers; at **100** it produced five (the laser was seen dimming five times per pass). The
lowest levels at a 10% maximum were too faint to mark cardboard.
`auto_start` (`GET /read?file=/tmp.gcode`) is undocumented and off by
default; it started a frame job without a button press in testing, so
only enable it when nothing laser-on is queued. Rayforge's Frame Repeat
Count defaults to 20 passes (the profiles set 5).

## Safety

- **The power switch is the only verified way to stop the machine.**
  The D1 acknowledges a command when it queues it, not when the move
  finishes. Rayforge's Cancel stops the driver sending more lines and
  asks for `M5`, but a move already queued still runs to its end
  (an `M5` sent mid-move is acknowledged without stopping it), and the
  machine's own button did nothing during a serial-streamed move in
  testing (it does stop a stored job). The driver
  keeps the queue short (about 0.5 s of motion plus the segment being
  run), so a cancel should be late by roughly a second of motion.
- The D1 cannot home. It treats wherever the head is at power-on as
  (0, 0). Park the head in the upper-left corner before powering it,
  and power-cycle it if you move the head by hand.
- Keep soft limits on. With a wrong zero point they are the only guard
  against driving the head into the frame.
- Check where the job starts (the first `G0`) before sending it.
  Imported designs may sit at X=0.
- Wear laser-rated eye protection and keep your hand on the switch.

## Install

Tested with Rayforge 1.13.0 on Linux.

1. Close Rayforge.
2. Copy this folder (the one containing `rayforge-addon.yaml`) to
   `~/.config/rayforge/addons/xtool_d1`. The folder must be named
   `xtool_d1`. If you set `RAYFORGE_CONFIG_DIR`, use that folder's
   `addons` directory instead. (For development, symlink it instead of
   copying.)
3. Start Rayforge. It loads the addon, registers the two drivers and
   copies the device profiles into `~/.config/rayforge/devices`. A
   "No stored version ... UnknownVersion" warning in the log is
   harmless.
4. Add a machine (the add-machine wizard) and pick **xTool D1 10W
   (Wi-Fi)** or **xTool D1 10W** (USB). The hardware details come
   pre-filled. For Wi-Fi, enter the D1's IP address and port 8080. For USB
   pick the port, or enter `1a86:7523` in the "Match by USB VID:PID"
   entry so it survives a power cycle.

Rayforge 1.13 never calls the `register_machines` hook, so `backend.py`
registers the drivers in `rayforge_init` instead.

## Known quirks

- Power-cycling the D1 re-enumerates its CH340 serial adapter, often as
  `/dev/ttyUSB1` instead of `/dev/ttyUSB0`. Rayforge keeps the stale
  port until you change it in the machine settings.
- Rayforge sends `S102` for a 10% setting. I don't know why it isn't
  `S100`.
- The D1 has no jog command (`$J`), so jogging sends `G91`, a relative
  `G0`, then `G90`.
- **The D1 abandons any single move after about 3 s**, leaving the head
  short of the target. The driver splits long G0/G1 moves into segments
  of at most 1 s to avoid this.
- **The D1 stalls between queued moves unless the port stays busy.**
  The driver polls position every 0.2 s during a job to keep it moving.
  I don't know why; this is from testing, not from firmware.

## Layout

- `rayforge-addon.yaml`: addon manifest.
- `xtool_d1/backend.py`: registers both drivers and installs the profiles.
- `xtool_d1/driver.py`: the serial driver.
- `xtool_d1/pacing.py`: move pacing and splitting for the serial driver.
- `xtool_d1/wifi_driver.py`: the Wi-Fi (upload-only) driver.
- `xtool_d1/profile/xtool-d1/`, `xtool-d1-wifi/`: device and G-code
  dialect definitions, one set per driver.
- `tests/`: run with
  `pixi run python -m unittest discover -s <addon>/tests` from the Rayforge
  checkout (the Wi-Fi tests need Rayforge's environment).

## Which driver?

- **Wi-Fi:** no cable, a pop-up confirms the upload, and you start and
  stop jobs with the machine's button. No position, jogging or progress.
  Enter the D1's IP by hand (it doesn't announce itself over mDNS here).
- **Serial (USB):** live position, jogging and a streamed job, but the
  only verified stop is the power switch.
