"""Convert the camera's QBox stream into raw H.264 (Annex-B) and PCM audio.

QBox (Mobilygen's container) as produced by the CY-STC1100, all big-endian:
    u32 box size (incl. header) | "qbox" | u8 version, u24 flags
    u16 stream type | u16 stream id | u32 sample flags | u64 timestamp
    payload (28 bytes in)
Stream type 2 = H.264, payload is NAL units each prefixed with a u32 length.
Stream type 3 = PCM audio, payload starts with a "qmed" sub-header.
Stream ids seen: 2 = main video (1280x720), 4 = second video, 1 = audio.

Usage: python3 tools/qbox2h264.py capture/stream.qbx [out_prefix]
Writes <prefix>_<id>.h264 per video stream and <prefix>_audio.pcm.
"""

import struct
import sys

HEADER = 28
H264, PCM = 2, 3


def boxes(data):
    off = 0
    while off + HEADER <= len(data):
        size, tag = struct.unpack_from(">I4s", data, off)
        if tag != b"qbox" or size < HEADER or off + size > len(data):
            break
        stype, sid, sflags = struct.unpack_from(">HHI", data, off + 12)
        yield stype, sid, sflags, data[off + HEADER: off + size]
        off += size


def nal_units(payload):
    """Length-prefixed NAL units -> Annex-B."""
    out, i = bytearray(), 0
    while i + 4 <= len(payload):
        (n,) = struct.unpack_from(">I", payload, i)
        i += 4
        if n == 0 or i + n > len(payload):
            break
        out += b"\0\0\0\1" + payload[i:i + n]
        i += n
    return bytes(out)


def pcm(payload):
    """Skip the qmed sub-header (its own size is its first u32)."""
    if payload[4:8] == b"qmed":
        (n,) = struct.unpack_from(">I", payload, 0)
        return payload[n:]
    return payload


def main():
    src = sys.argv[1]
    prefix = sys.argv[2] if len(sys.argv) > 2 else src.rsplit(".", 1)[0]
    data = open(src, "rb").read()
    outs = {}
    for stype, sid, _, payload in boxes(data):
        if stype == H264:
            key, chunk = f"{prefix}_{sid}.h264", nal_units(payload)
        elif stype == PCM:
            key, chunk = f"{prefix}_audio.pcm", pcm(payload)
        else:
            continue
        if key not in outs:
            outs[key] = open(key, "wb")
        outs[key].write(chunk)
    for name, f in outs.items():
        f.close()
        print(name)


if __name__ == "__main__":
    main()
