"""Read-only probe for the Samsung CY-STC1100 TV camera (USB 04e8:2058, Maxim/Mobilygen MG3500).

Reads a few chip registers through the camera's bootloader, the way Samsung's
TV library (libMoIPCamera_2058.so) does, to confirm the protocol. It writes
nothing to the camera.

Protocol (from libMoIPCamera_2058.so):
    host register read = vendor control IN, bRequest 0xB9, wIndex = reg >> 1, 2 bytes back (LE)
    CSR read           = vendor control IN, bRequest 0xB5,
                         wValue = block | (size << 8), wIndex = register, 4 bytes back.

Usage: sudo .venv/bin/python tools/probe.py
"""

import sys

import usb.core

VID, PID = 0x04E8, 0x2058
REQ_CSR_READ = 0xB5
REQ_HOST_READ = 0xB9
CTRL_IN_VENDOR = 0xC0
TIMEOUT_MS = 1000


def csr_read(dev, block: int, reg: int, size: int = 4) -> bytes:
    return bytes(dev.ctrl_transfer(CTRL_IN_VENDOR, REQ_CSR_READ, block | (size << 8), reg, size, TIMEOUT_MS))


def host_read(dev, reg: int) -> int:
    raw = bytes(dev.ctrl_transfer(CTRL_IN_VENDOR, REQ_HOST_READ, 0, reg >> 1, 2, 5000))
    return raw[0] | raw[1] << 8


def main() -> int:
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        print("Camera 04e8:2058 not found. Is it plugged in?")
        return 1
    print(f"Found {dev.manufacturer} {dev.product} on bus {dev.bus} address {dev.address}")

    # Host-interface registers, as qhalpm_reset / qhalpm_setclock read them.
    for reg, label in [(0x38, "chip ID"), (0x36, "clock config"), (0x34, "clock config"), (0x64, "host control")]:
        try:
            print(f"host reg {reg:#04x}: {host_read(dev, reg):#06x}   ({label})")
        except usb.core.USBError as e:
            print(f"host reg {reg:#04x}: error {e}   ({label})")

    # (block, register, what the TV library labels it)
    for block, reg, label in [
        (1, 0x14, "QMM part ID (qmmldGetQMMPartID)"),
        (1, 0x10, "CSR.01:0x10"),
        (3, 0x10, "CSR.03:0x10"),
    ]:
        try:
            raw = csr_read(dev, block, reg)
            print(f"block {block} reg {reg:#06x}: {raw.hex(' ')}   ({label})")
        except usb.core.USBError as e:
            print(f"block {block} reg {reg:#06x}: error {e}   ({label})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
