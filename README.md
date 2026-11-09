# Orange Pi Zero 2W + PCA9685 web PWM control

FastAPI service + single-page web UI (and a REST API) for driving a PCA9685 16-channel PWM board from an Orange Pi Zero 2W.

Features:
- 16 channels, each with a slider in servo pulse width (µs), angle in degrees (calibrated) or duty (%)
- per-channel **calibration** (min / max pulse, angle range in degrees, name, invert)
- **workflows**: named sequences of waypoints (start → middle → end) with move/hold times and repeat
- adjustable PWM frequency and an "All off" button
- runs as a systemd service (auto-start, auto-restart)

Contents: [Wiring](#wiring) · [Powering servos](#powering-servos) · [Enable I2C](#enable-i2c-on-the-pi) · [Run](#run) · [Service](#run-as-a-linux-service-systemd) · [PWM basics](#pwm-basics) · [Calibration](#calibration) · [Workflows](#workflows) · [API](#api) · [Troubleshooting](#troubleshooting)

## Wiring

| PCA9685 | Orange Pi Zero 2W (40-pin header) |
|---|---|
| VCC | pin 1 (3.3V) |
| SDA | pin 3 |
| SCL | pin 5 |
| GND | pin 6 |
| V+ / GND screw terminal | **separate 5–6V supply** for servos/motors (its ground is common with the Pi via pin 6) |

Servo on channel 0: signal (yellow/orange) → top pin, V+ (red) → middle pin, GND (black/brown) → bottom pin.
Do not join the VCC pin (3.3V logic) to V+. Verify pins 3/5 are I2C in the Orange Pi manual.

## Powering servos

The PCA9685 does not generate power: the V+ rail on the board is only an input that is passed to the servo connectors. Something has to feed it.

- **Recommended: separate 5–6V supply** on the blue `V+ / GND` screw terminal, with its ground common with the Pi (the GND wire on header pin 6 already does this). Required for several servos or large ones (MG996R etc.).
- **One small servo (SG90 / MG90S) only**: connect the Pi's 5V pin (header pin 2 or 4) to the PCA9685 **V+ header pin** (next to `VCC`). Use a good USB-C supply (5V/3A), because servo current spikes (0.5–1 A when moving or stalled) can brown out and reboot the Pi. Add a 470–1000 µF capacitor across V+ and GND on the PCA9685.

If the servo does not move at all, check first that the V+ rail is actually powered.

## Enable I2C on the Pi

Run `sudo orangepi-config` → System → Hardware, enable the I2C overlay for the header pins you wired (or add it to `overlays=` in `/boot/orangepiEnv.txt`), reboot, then:

```bash
sudo apt install -y i2c-tools python3-venv
ls /dev/i2c-*
sudo i2cdetect -y 1   # try each bus number; PCA9685 shows up at 0x40 (and 0x70)
```

Use the bus number that shows `40` as `PWM_I2C_BUS`.

Notes:
- If `/dev/i2c-0` does not exist, I2C is not enabled for those pins yet. List the available overlays with `ls /boot/dtb/allwinner/overlay/ | grep -i i2c`, then add the matching name (without the `sun50i-h616-` prefix and `.dtbo` suffix, e.g. `ph-i2c3`) to `overlays=` in `/boot/orangepiEnv.txt` and reboot.
- Extra buses such as `/dev/i2c-20` and `/dev/i2c-21` are usually internal (HDMI/DDC etc.). On the tested board the PCA9685 was on `/dev/i2c-1`.

## Run

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
PWM_I2C_BUS=0 uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open `http://<pi-ip>:8000`. For development without hardware: `PWM_MOCK=1`.

Env vars: `PWM_I2C_BUS` (0), `PWM_I2C_ADDR` (0x40), `PWM_FREQ` (50), `PWM_DATA_DIR` (`./data`: calibration and workflows), `PWM_MOCK`.

## Run as a Linux service (systemd)

```bash
sudo ./install.sh
```

This installs the app to `/opt/orangepi-pwm`, creates an unprivileged `pwm` user with access to `/dev/i2c-*`, and enables the `pwm-web` service: it starts at boot and restarts automatically if it crashes (`Restart=always`). On stop, all PWM outputs are switched off.

Settings are in `/etc/default/pwm-web` (`PWM_I2C_BUS`, `PWM_I2C_ADDR`, `PWM_FREQ`, `PWM_PORT`). Set the right I2C bus there, then:

```bash
sudo systemctl restart pwm-web
sudo systemctl status pwm-web
journalctl -u pwm-web -f      # logs
sudo systemctl stop pwm-web   # stop
sudo systemctl disable pwm-web  # don't start at boot
```

To update after changing the code, run `sudo ./install.sh` again (your `/etc/default/pwm-web` is kept).

The web UI listens on port **8000** by default (`PWM_PORT`). It binds to all interfaces. The unprivileged service user cannot bind ports below 1024, so for port 80 use a reverse proxy.

## PWM basics

PWM switches an output on and off very fast. Three numbers describe it:

- **`hz` (frequency)**: on/off cycles per second. Servos expect 50 Hz (a 20 ms cycle); DC motors and LEDs usually use 500–1500 Hz. The PCA9685 supports ~24–1526 Hz and one frequency applies to all 16 channels, so don't mix servos and 1 kHz devices on one board.
- **`duty`**: the fraction of each cycle the output is on, 0…1. For a DC motor via an H-bridge this is speed; for an LED, brightness. `0` turns the output off.
- **`pulse_us`**: the same as duty, but as on-time in microseconds per cycle: `pulse_us = duty × 1,000,000 / hz`. Convenient for servos: at 50 Hz, 1500 µs = 7.5 % duty. Typical mapping: ~1000 µs one end, 1500 µs centre, ~2000 µs other end (many servos accept 500–2500 µs, but extremes can strain the servo against its end stops).

Changing the frequency keeps each channel's pulse width constant.

## Calibration

Every servo has slightly different end points and travel. Per channel you store a name, `min_us`, `max_us`, an **angle range** in degrees and an invert flag (saved to `calibration.json`; `/var/lib/pwm-web` when run as the service).

- **0°** is the `min_us` pulse, and the **range** (default 180°) is the `max_us` pulse. Angles in between are linear in pulse width.
- Angles are only as accurate as your range setting: if the servo really travels 160° between your min and max, enter 160.

In the UI: switch to **Servo (µs)** mode, move a servo to a safe limit, expand **Calibrate** on its card and press **Set min** (the servo's 0° end) or **Set max**, then enter the **Range** in degrees. Press the min/max buttons to jump there and check. Then use **Angle (°)** mode: the slider runs from 0 to the range and the servo never leaves the calibrated limits. **Invert direction** flips which end is 0°.

If you change min, max or range later, angles stored in workflows keep their number but map to the new pulse widths.

## Workflows

A workflow is a named list of steps, e.g. **start → middle → end**. Each step stores an **angle in degrees** for each selected channel (0° = calibrated min, range = calibrated max), plus:

- **move** (ms): how long the servos take to travel there, in milliseconds (1000 ms = 1 s). The servo glides from its current position at a steady rate; 0 = jump.
- **hold** (ms): pause after arriving, before the next step.

The workflow has a **repeat** count (`0` = loop until stopped). Only one workflow runs at a time; manual slider moves during a run are allowed but get overridden by the next step. "All off" and "Stop" abort a run, and stopping leaves the servos where they are.

**Building one in the UI** (Workflows section):
1. **Pick the channels** the workflow controls with the numbered buttons (hover for the channel name). Channels you don't select are never touched. Selecting a channel later adds it to every existing step at its current angle; deselecting removes it from every step.
2. Move the servos where you want them (Angle mode) and press **+ Add step from current positions**. This records the *selected* channels. A selected channel that is switched off is recorded at mid-range, and a message tells you. If no channel is selected yet, the channels that are currently on are selected for you. The first step is labelled "start"; rename as you like.
3. Repeat for each point. Every recorded angle is shown in the step and can be edited by hand.
4. Name the workflow and **Save** / **▶ Run**. Per step: ▶ runs only that step, ⟳ re-records it from the current positions, ↑/↓ reorder, ✕ delete.

Errors (missing name, no steps, a run already in progress, validation problems, a hardware error during a run) are shown in red next to the Run button; each step is also logged (`journalctl -u pwm-web -f`).

Calibrate the servos first, since angles are relative to the calibration. A channel that was off when a step starts jumps straight to its target; channels that are on glide from where they are. If all steps hold the same angle for a channel, that channel won't move — check the numbers in the steps.

Saved to `workflows.json` next to `calibration.json`. Workflows saved by an earlier version (calibrated −100…100 % positions in `targets`) are converted to angles automatically.

## API

Interactive docs: `http://<pi-ip>:8000/docs`

- `GET /api/state`
- `POST /api/channel/{0-15}` with exactly one of `{"angle": 90}` (degrees, calibrated), `{"pulse_us": 1500}`, `{"duty": 0.5}` (0 = output off) or the legacy `{"position": -1..1}`. `GET /api/state` returns each channel's `angle` (null when off).
- `GET /api/calibration`, `PUT /api/calibration/{0-15}` body `{"name":"Pan","min_us":1000,"max_us":2000,"range_deg":180,"invert":false}`
- `GET /api/workflows`, `PUT /api/workflows/{name}`, `DELETE /api/workflows/{name}`
- `POST /api/workflows/{name}/run` (optional `?step=N` runs a single step, 0-based), `POST /api/workflows/stop`, `GET /api/workflows/status`
- `POST /api/frequency` body `{"hz": 50}`
- `POST /api/off`

```bash
curl -X POST http://<pi-ip>:8000/api/channel/0 -H 'Content-Type: application/json' -d '{"angle": 90}'

# define and run a workflow
curl -X PUT http://<pi-ip>:8000/api/workflows/sweep -H 'Content-Type: application/json' -d '{
  "repeat": 1,
  "steps": [
    {"label": "start",  "angles": {"0": 0},   "duration_ms": 0},
    {"label": "middle", "angles": {"0": 90},  "duration_ms": 1000, "hold_ms": 500},
    {"label": "end",    "angles": {"0": 180}, "duration_ms": 1000}
  ]}'
curl -X POST http://<pi-ip>:8000/api/workflows/sweep/run
```

## Troubleshooting

Check the service first:

```bash
sudo systemctl status pwm-web --no-pager
sudo journalctl -u pwm-web -n 30 --no-pager
```

| Symptom | Cause / fix |
|---|---|
| `FileNotFoundError ... /dev/i2c-0` | The bus is not enabled or has another number. See [Enable I2C](#enable-i2c-on-the-pi), then set `PWM_I2C_BUS` in `/etc/default/pwm-web` and restart. |
| `cannot open PCA9685 ... Remote I/O error` / `No such device` | The bus exists but the chip does not answer. Check wiring (SDA/SCL swapped? GND? VCC?) with `sudo i2cdetect -y N`. The app exits at startup if the PCA9685 is not found, so the page never comes up. |
| `Permission denied` on `/dev/i2c-N` | Run `ls -l /dev/i2c-N`: the group should be `i2c`. Run `sudo udevadm trigger` or reboot. |
| Servo does not move, UI works | The V+ rail is not powered, wrong channel, or the signal/power/ground wires are in the wrong order. |
| Servo buzzes or twitches at an end | The limit is beyond its mechanical range. Recalibrate min/max. |
| Pi reboots when a servo moves | Power dip. Use a separate supply, or a bigger supply plus a capacitor. |
| Workflow runs but nothing moves | All steps have the same value for the channel (check the numbers in each step), or the channel isn't selected. Remember to hard-refresh the page after updating the service so you get the current UI. |
| Page unreachable | `curl localhost:8000/api/state` on the Pi; if that works, check you are on the same subnet, that the router has no AP/client isolation, and that no VPN captures the subnet (`route -n get <pi-ip>` on a Mac). |

## Security

No authentication — anyone on the network can move your motors. Keep it on a trusted LAN or put it behind a reverse proxy with auth.

## Notes

- Servos: 50 Hz, ~500–2500 µs (most use 1000–2000 µs; narrow the slider range if yours binds).
- DC motors via an H-bridge/ESC: use "Duty" mode, usually with a higher frequency (e.g. 1000 Hz).
