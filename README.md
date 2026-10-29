# Samsung CY-STC1100 TV camera on Linux

Goal: use the Samsung CY-STC1100 Skype TV camera (USB `04e8:2058`, "MoIP_HD")
as a webcam on Linux.

## Findings at a glance

- **It works.** The camera boots, encodes and streams on a stock Linux PC, with
  no Samsung TV: H.264 at 1280×720 and 640×480, plus 24 kHz PCM from its
  microphones, viewable live in ffplay.
- **No reimplementation needed.** Samsung's own TV driver
  (`libMoIPCamera_2058.so`, ARM) runs unmodified in the Unicorn emulator, with
  its USB calls routed to the real camera through pyusb.
- **The MG3500 bootloader protocol is fully mapped**: eight vendor control
  requests plus two bulk endpoints (table below), the soft reboot (`0xBE`),
  and the boot order the TV used.
- **The output is QBox** (Mobilygen's container) wrapping length-prefixed
  H.264 NAL units; `tools/qbox2h264.py` turns it into Annex-B H.264.
- **15 fps, not 30.** Stream timestamps show a steady 15 fps (frames 6000 ticks
  apart on a 90 kHz clock); requesting 30 via `vcmd_videofr` changes nothing.
  Most likely the OV2640's limit at full resolution.
- **Image quality is poor.** The OV2640 is a small 2 MP phone-class sensor, so
  low light gives grain and a purple cast. Autofocus (`SetAutoFocus` /
  `AFSetup`) and bitrate (`vcmd_vbitrate`, about 2.8 Mbit/s by default) are not
  tuned yet. Expect a 2011 Skype TV camera, not a modern webcam.

Not done: autofocus, bitrate tuning, a 30 fps sensor mode, and exposing it as
a `/dev/video` device through v4l2loopback.

## Why it doesn't just work

The camera holds only a bootloader. A Samsung TV uploads firmware into the
camera's RAM every time it is plugged in, then drives it with Samsung's own
library. Skype on Samsung TVs ended in 2016, so nothing does that any more.

[samsung-tvcam-fwloader](https://github.com/ondrej-zary/samsung-tvcam-fwloader)
(Ondrej Zary, 2025) makes Samsung's later cameras (VG-STC2000…5000) work: once
their firmware is loaded they become ordinary UVC webcams. It lists the
CY-STC1100 as **not supported**: it is built on a different chip — a
Maxim/Mobilygen **MG3500** H.264 codec SoC with an OmniVision OV2640 sensor
and a Wolfson WM8750L audio codec — and is probably not UVC even with firmware
loaded. So it needs its own userspace driver. That is what this repo is for.

## What we know (from Samsung's TV library)

Samsung's TV camera module (`empCamera`, still downloadable — see
`research/README.md`) contains `libMoIPCamera_2058.so`, the TV's driver for
this camera. It is ARM code, but its function names are exported, so the
protocol can be read from it with `tools/disasm.py`.

### USB requests (all vendor control transfers on endpoint 0)

| Request | Dir | Meaning | Details |
|---|---|---|---|
| `0xB5` | IN  | CSR read | `wValue = block \| size<<8`, `wIndex = reg`, 4 bytes back |
| `0xB6` | OUT | CSR write | 8-byte packet: reg (2, BE), value (4, LE), block, op |
| `0xB7` | OUT | bitstream write | then data on bulk EP `0x02` |
| `0xB8` | OUT | memory write | 9-byte header: addr (4, BE), len (4, BE), flag; data on bulk EP `0x02` in 64 KiB chunks |
| `0xB9` | IN  | host register read | `wIndex = reg >> 1`, 2 bytes back (LE) |
| `0xBA` | OUT | host register write | `wValue = value`, `wIndex = reg >> 1` |
| `0xBC` | OUT | chip reset | no data |
| `0xBD` | OUT | memory read | 9-byte header as `0xB8`; data from bulk EP `0x86` |

### Boot sequence the TV runs (`CMoIPCameraInitThread::t_Main`)

1. open USB `04e8:2058` (optionally a "renumerate" control request + reopen)
2. `qhalpm_reset` — `0xBC`, check chip ID (host reg `0x38`, high byte must be 3), host reg `0x64 = 0x10`
3. `qhalpm_setclock` — PLL setup via host regs `0x34`/`0x36` and CSR writes
4. `qmmldSetSystemData` ×7
5. `qmmldLoadAndRunFile("mgfw_2058.bin")` — parses the `MBY0` image: CSR-write sections and memory-load sections, then runs it
6. firmware bring-up: `MWare_Create`, `SystemControlAPI_*`, OV2640 sensor init, WM8750L init, `mg3500init` (H.264 1280×720 @ 30 fps)
7. `MWare_Run` — video arrives on bulk EP `0x86` in Mobilygen **QBox** containers wrapping H.264

## Status

- [x] Device identified; firmware (`mgfw_2058.bin`) and Samsung's driver library located
- [x] USB protocol decoded from the library
- [x] **Read-only probe works** (`tools/probe.py`): chip ID `0x0303` — matches what Samsung's code expects
- [x] Reset + clock setup (600 MHz PLL locks)
- [x] Firmware load and run — every section verified by read-back
- [x] Sensor (OV2640), audio codec (WM8750L) and encoder bring-up
- [x] **Video!** QBox stream captured, demuxed to H.264: 1280×720 and 640×480 Baseline, ~2.8 Mbit/s, plus 24 kHz PCM audio
- [x] **Live view**: `--live` streams the 720p H.264 to stdout for ffplay
- [ ] 30 fps: the encoder runs at a steady 15 fps (QBox timestamps 6000 ticks apart at 90 kHz), most likely the OV2640's limit at full resolution; `vcmd_videofr(30)` has no effect. Try a smaller sensor mode.
- [ ] Expose as a webcam device (v4l2loopback)

## Watch it live

```bash
sudo .venv/bin/python tools/boot.py --live --fps 0 --seconds 3600 2>live.log \
  | ffplay -f h264 -framerate 15 -fflags nobuffer -flags low_delay -framedrop -window_title "CY-STC1100" -i -
```

Only the camera side runs as root; ffplay runs as you, so it can open a window.

## Record a clip

```bash
sudo .venv/bin/python tools/boot.py --seconds 10     # reboot camera, load firmware, record capture/stream.qbx
python3 tools/qbox2h264.py capture/stream.qbx capture/cam   # -> capture/cam_2.h264 (720p), cam_4.h264, cam_audio.pcm
ffmpeg -r 30 -f h264 -i capture/cam_2.h264 -c copy capture/cam.mp4
```

### How it runs

`tools/boot.py` does not re-implement the camera's boot logic. It runs Samsung's
own `libMoIPCamera_2058.so` (ARM) inside the Unicorn emulator (`tools/emu.py`),
giving it a minimal C library and routing its USB calls to the real camera
through pyusb. The emulated code can only reach the functions `emu.py`
provides: it can read the firmware file and write only into the output folder.
The steps follow the TV's `CMoIPCameraInitThread::t_Main` and `CMoIPCamera::Play`:
soft reboot (`0xBE`) → reset → 600 MHz clock → system data → load and run
`mgfw_2058.bin` → MWare / SystemControl → OV2640 → WM8750L → `mg3500init` →
`MWare_Run` → `vcmd_estart(1)`, then the firmware's event thread (captured
from `pthread_create`) is run to move the bitstream, which arrives at
`cbQBoxDemuxer` and is saved.

## Tools

```bash
python3 -m venv .venv && .venv/bin/pip install capstone pyelftools pyusb unicorn

# disassemble functions (or 0xADDR[:SIZE]) from Samsung's library, calls and strings labelled
.venv/bin/python tools/disasm.py research/emp/x10p/libMoIPCamera_2058.so qhalqcc_read
# who calls a function
.venv/bin/python tools/xref.py research/emp/x10p/libMoIPCamera_2058.so qmmldLoadAndRunFile

# read-only probe of the camera (needs USB access; see below)
sudo .venv/bin/python tools/probe.py
```

The camera's USB node is root-only by default. Run tools with `sudo`, or add a
udev rule giving your user access to `04e8:2058`.

## Not in git

Samsung's binaries (`research/emp/`) and the upstream loader
(`third_party/`) are not committed; `research/README.md` says how to fetch them.
