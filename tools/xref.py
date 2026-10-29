"""Find every call to the named functions (direct or through the PLT) in the library.

Usage: .venv/bin/python tools/xref.py <lib.so> <function> [...]
Prints the calling function (nearest exported symbol before the call) and address.
"""
import sys

from capstone import CS_ARCH_ARM, CS_MODE_ARM, Cs

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from disasm import load  # noqa: E402

path, names = sys.argv[1], sys.argv[2:]
funcs, by_addr, plt_names, vaddr_to_off, cstring, word, data = load(path)
from elftools.elf.elffile import ELFFile  # noqa: E402

text = ELFFile(open(path, "rb")).get_section_by_name(".text")
targets = {}
for n in names:
    if n in funcs:
        targets[funcs[n][0] & ~1] = n
    for a, pn in plt_names.items():
        if pn == n:
            targets[a] = n
# PLT stubs for our own exported functions are named in plt_names too.
starts = sorted(by_addr)
md = Cs(CS_ARCH_ARM, CS_MODE_ARM)
md.skipdata = True
for ins in md.disasm(data[text["sh_offset"]:text["sh_offset"] + text["sh_size"]], text["sh_addr"]):
    if ins.mnemonic in ("bl", "b", "blx") and ins.op_str.startswith("#"):
        t = int(ins.op_str[1:], 16)
        if t in targets:
            caller = max((s for s in starts if s <= ins.address), default=None)
            print(f"{targets[t]:28} called from {by_addr.get(caller, '?')} @ {ins.address:#x}")
