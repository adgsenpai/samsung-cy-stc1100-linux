"""Disassemble functions from Samsung's ARM camera library, with calls and strings labelled.

Usage: .venv/bin/python tools/disasm.py <lib.so> <function|0xADDR[:SIZE]> [...]

Reads the library as data only (it is ARM code and is never executed here).
Calls through the PLT are named after the imported symbol, direct calls after
the exported function at that address, and literal-pool loads that point at a
C string are shown with the string.
"""

import struct
import sys

from capstone import CS_ARCH_ARM, CS_MODE_ARM, CS_MODE_THUMB, Cs
from elftools.elf.elffile import ELFFile
from elftools.elf.relocation import RelocationSection


def load(path):
    f = open(path, "rb")
    elf = ELFFile(f)
    data = open(path, "rb").read()

    funcs = {}
    by_addr = {}
    dynsym = elf.get_section_by_name(".dynsym")
    for s in dynsym.iter_symbols():
        if s["st_info"]["type"] == "STT_FUNC" and s["st_shndx"] != "SHN_UNDEF":
            funcs[s.name] = (s["st_value"], s["st_size"])
            by_addr[s["st_value"] & ~1] = s.name

    # PLT stubs: map each stub address to the imported symbol it jumps through.
    plt_names = {}
    relplt = elf.get_section_by_name(".rel.plt")
    plt = elf.get_section_by_name(".plt")
    if isinstance(relplt, RelocationSection) and plt:
        syms = elf.get_section(relplt["sh_link"])
        got_to_name = {r["r_offset"]: syms.get_symbol(r["r_info_sym"]).name for r in relplt.iter_relocations()}
        # ARM PLT: 20-byte header, then 12-byte entries in GOT order.
        for i, (got, name) in enumerate(sorted(got_to_name.items())):
            plt_names[plt["sh_addr"] + 20 + 12 * i] = name

    def vaddr_to_off(addr):
        for seg in elf.iter_segments():
            if seg["p_type"] == "PT_LOAD" and seg["p_vaddr"] <= addr < seg["p_vaddr"] + seg["p_filesz"]:
                return addr - seg["p_vaddr"] + seg["p_offset"]
        return None

    def cstring(addr):
        off = vaddr_to_off(addr)
        if off is None:
            return None
        end = data.find(b"\0", off, off + 200)
        if end <= off:
            return None
        s = data[off:end]
        if all(32 <= c < 127 or c in (9, 10) for c in s) and len(s) >= 3:
            return s.decode()
        return None

    def word(addr):
        off = vaddr_to_off(addr)
        return None if off is None else struct.unpack_from("<I", data, off)[0]

    return funcs, by_addr, plt_names, vaddr_to_off, cstring, word, data


def disasm(path, names):
    funcs, by_addr, plt_names, vaddr_to_off, cstring, word, data = load(path)
    for name in names:
        if name.startswith("0x"):
            # Unexported code: "0xADDR" or "0xADDR:SIZE" (default 0x400 bytes).
            a, _, n = name.partition(":")
            addr, size = int(a, 16), int(n, 16) if n else 0x400
        elif name not in funcs:
            print(f"; {name}: not exported")
            continue
        else:
            addr, size = funcs[name]
        thumb = addr & 1
        addr &= ~1
        md = Cs(CS_ARCH_ARM, CS_MODE_THUMB if thumb else CS_MODE_ARM)
        off = vaddr_to_off(addr)
        # Position-independent code loads "base = pc + lit" once, then reaches
        # strings as "rX = lit; rX = base + rX". Track literal values and bases.
        lits, bases = {}, {}
        print(f"\n; ===== {name} @ {addr:#x} size {size} {'thumb' if thumb else 'arm'}")
        for ins in md.disasm(data[off:off + size], addr):
            note = ""
            if ins.mnemonic in ("bl", "blx", "b") and ins.op_str.startswith("#"):
                tgt = int(ins.op_str[1:], 16)
                note = plt_names.get(tgt) or by_addr.get(tgt) or ""
            elif ins.mnemonic == "ldr" and "[pc, #" in ins.op_str:
                disp = int(ins.op_str.split("#")[1].rstrip("]"), 16)
                lit = (ins.address + (4 if thumb else 8)) & ~3 if thumb else ins.address + 8
                val = word(lit + disp)
                if val is not None:
                    lits[ins.op_str.split(",")[0]] = val
                    s = cstring(val)
                    note = f"={val:#x}" + (f' "{s}"' if s else "")
            elif ins.mnemonic == "add":
                ops = [o.strip() for o in ins.op_str.split(",")]
                if len(ops) == 3 and ops[1] == "pc" and ops[2] in lits:
                    bases[ops[0]] = ins.address + 8 + lits[ops[2]]
                    note = f"base={bases[ops[0]]:#x}"
                elif len(ops) == 3 and ops[1] in bases and ops[2] in lits:
                    target = (bases[ops[1]] + lits[ops[2]]) & 0xFFFFFFFF
                    s = cstring(target)
                    if s:
                        note = f'"{s}"'
            print(f"  {ins.address:08x}: {ins.mnemonic:8} {ins.op_str}" + (f"   ; {note}" if note else ""))


if __name__ == "__main__":
    disasm(sys.argv[1], sys.argv[2:])
