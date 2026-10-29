"""Boot the CY-STC1100 with Samsung's own code running in the emulator (see emu.py).

Follows CMoIPCameraInitThread::t_Main from libMoIPCamera_2058.so:
    qhalpm_open(48 MHz) -> qhalpm_reset -> qhalpm_setclock(600 MHz)
    -> qmmldSetSystemData x7 -> qmmldLoadAndRunFile(mgfw_2058.bin)

Usage: sudo .venv/bin/python tools/boot.py [--until reset|clock|sysdata|load] [-v]
Everything written goes to the camera's RAM/registers; unplugging resets it.
"""
import argparse
import os
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from emu import Camera, Emulator, default_paths  # noqa: E402
from qbox2h264 import boxes, nal_units  # noqa: E402

STEPS = ["reset", "clock", "sysdata", "load", "bringup", "stream"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--until", choices=STEPS, default="stream")
    ap.add_argument("--seconds", type=float, default=10, help="how long to stream")
    ap.add_argument("--out", default="capture", help="directory for the recorded stream")
    ap.add_argument("--fps", type=int, default=30, help="request this frame rate (camera max 30); 0 = leave default")
    ap.add_argument("--live", action="store_true",
                    help="write the 720p stream as raw H.264 to stdout (pipe into ffplay); logs go to stderr")
    ap.add_argument("-v", "--verbose", action="store_true", help="log every USB transfer")
    a = ap.parse_args()
    stop = STEPS.index(a.until)
    video_out = None
    if a.live:
        # Keep a handle on the real stdout for video and send everything else to stderr.
        video_out = os.fdopen(os.dup(1), "wb", buffering=0)
        os.dup2(2, 1)
        sys.stdout = sys.stderr

    lib, fw = default_paths()
    # Start from the bootloader even if a previous run left firmware running.
    emu = Emulator(lib, fw, Camera(reboot=True), verbose_usb=a.verbose, out_dir=a.out)

    h = emu.call("qhalpm_open", 48_000_000)
    print(f"qhalpm_open -> {h}")
    r = emu.call("qhalpm_reset", h)
    print(f"qhalpm_reset -> {r}")
    if r < 0 or stop == 0:
        return r
    r = emu.call("qhalpm_setclock", h, 600_000_000)
    print(f"qhalpm_setclock -> {r}")
    if r < 0 or stop == 1:
        return r
    for key, value in [(0xB, 600_000_000), (0xC, 200_000_000), (0xD, 48_000_000), (0xE, 0), (0xF, 0), (8, 3), (0x10, 0)]:
        emu.call("qmmldSetSystemData", key, value)
    print("qmmldSetSystemData x7 done")
    if stop == 2:
        return 0
    path = emu.put_string("/mtd_rwcommon/moip/bin/mgfw_2058.bin")
    r = emu.call("qmmldLoadAndRunFile", 0, path)
    print(f"qmmldLoadAndRunFile -> {r}")
    if r < 0 or stop == 3:
        return r

    # Firmware bring-up, as CMoIPCameraInitThread::t_Main continues.
    mw = emu.call("MWare_Create", 0, 0, 0)
    print(f"MWare_Create -> {mw:#x}")
    if not mw:
        return -1
    gpb = emu.call("MWare_GetGPB", mw) & 0xFFFFFFFF
    sc = emu.call("SystemControlAPI_Create", 0, mw, emu.u32(gpb + 8)) & 0xFFFFFFFF
    print(f"SystemControlAPI_Create -> {sc:#x}")
    if not sc:
        return -1
    emu.call("SystemControlAPI_DirectCommandWriteAlloc", sc, 0x80, 1)
    emu.call("SystemControlAPI_DirectCommandWriteStart", sc)
    for key, value in [(0x25, 6), (0x16, 24_000_000), (0xF, 0x41249), (0x1A, 6_144_000), (0x22, 6_144_000)]:
        print(f"SystemControlAPI_Configure({key:#x}, {value}) -> {emu.call('SystemControlAPI_Configure', sc, key, value)}")
    print(f"sensorinit -> {emu.call('sensorinit', sc)}")
    print(f"wm8750linit -> {emu.call('wm8750linit', sc)}")
    print(f"mg3500init -> {emu.call('mg3500init', sc, 0, 0)}")
    print(f"MWare_Run -> {emu.call('MWare_Run', mw, 0)}")
    if stop == 4:
        return 0

    # CMoIPCamera::Play in "Skype call mode": start the encoders, then let
    # the firmware event thread (created by MWare_Run) move the bitstream.
    print(f"vcmd_estart -> {emu.call('vcmd_estart', 1)}")
    if a.fps:
        # "Skype call mode" runs at 15 fps with adaptive frame rate; ask for more.
        print(f"vcmd_EnableAFR(0) -> {emu.call('vcmd_EnableAFR', 0)}")
        print(f"vcmd_videofr({a.fps}) -> {emu.call('vcmd_videofr', a.fps)}")
    # The firmware's bitstream reaches cbQBoxDemuxer(data, length) as QBox
    # blocks; on the TV it goes on to the camera object. Save it instead.
    os.makedirs(a.out, exist_ok=True)
    qbox_path = os.path.join(a.out, "stream.qbx")
    # Live viewing streams for as long as you like; don't fill the disk with it.
    qbox = open(os.devnull if a.live else qbox_path, "wb")
    stats = {"blocks": 0, "bytes": 0}

    def on_qbox():
        data, length = emu.arg(0), emu.arg(1)
        block = emu.read(data, length)
        qbox.write(block)
        stats["blocks"] += 1
        stats["bytes"] += length
        if video_out:
            for stype, sid, _, payload in boxes(block):
                if stype == 2 and sid == 2:
                    try:
                        video_out.write(nal_units(payload))
                    except BrokenPipeError:
                        raise SystemExit("viewer closed")
        emu._ret(0)

    emu.override("cbQBoxDemuxer", on_qbox)
    print(f"running the event thread for {a.seconds:.0f}s ...")
    emu.run_thread(len(emu.threads) - 1, a.seconds)
    qbox.close()
    print(f"saved {stats['blocks']} QBox blocks, {stats['bytes']} bytes -> {qbox_path}")
    return 0


if __name__ == "__main__":
    sys.exit(0 if (main() or 0) >= 0 else 1)
