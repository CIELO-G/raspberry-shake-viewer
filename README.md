# Raspberry Shake Live Viewer

A real-time waveform & spectrogram viewer for Raspberry Shake seismometers, plus a "Shake Challenge" game for outreach events. (Read-only)

![channels: EHZ / EHN / EHE](https://img.shields.io/badge/channels-EHZ%20%7C%20EHN%20%7C%20EHE-blue)

## Features

- Live scrolling waveforms for the Z / N / E channels (one-minute window)
- Toggle to a live spectrogram of the vertical channel
- Signal-strength meter (Quiet → Strong)
- "Shake Challenge" mini-game with intensity tiers (Sleeping Cat → Mega Earthquake)

## Install

Clone the repo first:

```bash
git clone https://github.com/CIELO-G/raspberry-shake-viewer.git
cd raspberry-shake-viewer
```

### Conda (recommended)

```bash
conda env create -f environment.yml
conda activate shake-viewer
```

## Run

```bash
python viewer.py --host <shake-ip> --station <station-code>
```

Find your Shake's IP from the `rs.local` web UI, your router's DHCP table, or `arp -a`. The station code is shown on the front panel of the Shake and in the web UI.

### All options

| Flag | Default | Description |
|---|---|---|
| `--host` | `169.254.33.45` | IP / hostname of the Shake |
| `--port` | `18000` | SeedLink port |
| `--station` | `RBCD4` | Station code |
| `--network` | `AM` | Network code |
| `--full-scale` | `100000` | RMS counts that map to 100% on the meter |

### Tuning the meter

`--full-scale` controls when the signal-strength meter and game pegs at 100%. Reasonable starting points:

- Desk tap nearby: ~500 – 2 000 counts RMS
- A group of kids jumping next to the sensor: ~50 000 – 200 000 counts RMS

If the meter pegs too easily, raise it; if it never moves, lower it.

## Requirements

- Python ≥ 3.9
- A Raspberry Shake on the same network with SeedLink enabled (default on RS4D / RS3D / RS1D)
