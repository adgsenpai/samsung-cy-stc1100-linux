"""Run Samsung's ARM camera library (libMoIPCamera_2058.so) in an emulator, against the real camera.

Rather than re-implementing the MG3500 boot logic (PLL maths, firmware image
parsing, mailbox protocol) by hand, this loads Samsung's own code into Unicorn
and gives it a tiny C library. Its USB calls (the libusb-0.1 style
usb_control_msg / usb_bulk_write / usb_bulk_read it carries internally) are
routed to the real camera through pyusb. The emulated code can reach nothing
on this machine except the functions provided here.

Library usage:
    emu = Emulator(lib_path, firmware_dir)
    emu.call("qhalpm_open", 48_000_000)
"""

import math
import os
import struct
import sys
import time

import usb.core
import usb.util
from elftools.elf.elffile import ELFFile
from elftools.elf.relocation import RelocationSection
from unicorn import UC_ARCH_ARM, UC_HOOK_CODE, UC_HOOK_MEM_INVALID, UC_MODE_ARM, Uc, UcError
from unicorn.arm_const import (
    UC_ARM_REG_C1_C0_2, UC_ARM_REG_FPEXC,
    UC_ARM_REG_CPSR, UC_ARM_REG_LR, UC_ARM_REG_PC, UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3,
    UC_ARM_REG_SP,
)

BASE = 0x0100_0000          # where the library is loaded
STUBS = 0x0800_0000         # one 4-byte "bx lr" per imported function
HEAP = 0x2000_0000
HEAP_SIZE = 0x0800_0000     # 128 MiB
STACK_TOP = 0x7F00_0000
STACK_SIZE = 0x0010_0000
SCRATCH = 0x6000_0000       # errno, misc
END = 0x0F00_0000           # return address that stops emulation
BX_LR = struct.pack("<I", 0xE12FFF1E)

ARG_REGS = [UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3]


class Camera:
    """The real USB camera, in the shape of libusb-0.1 calls."""

    REQ_SOFT_REBOOT = 0xBE  # handled by the USB bridge; the TV's usbRenumDevice

    def __init__(self, vid=0x04E8, pid=0x2058, log=print, reboot=False):
        self.log = log
        self.vid, self.pid = vid, pid
        self.dev = usb.core.find(idVendor=vid, idProduct=pid)
        if self.dev is None:
            raise SystemExit(f"camera {vid:04x}:{pid:04x} not found")
        if reboot:
            self._soft_reboot()
        self._claim()

    def _soft_reboot(self):
        """Reboot the camera back to its bootloader, as the TV does, and wait for it to return."""
        try:
            self.dev.ctrl_transfer(0x40, self.REQ_SOFT_REBOOT, 0, 0, b"\0" * 8, 1000)
        except usb.core.USBError as e:
            self.log(f"soft reboot request: {e} (continuing)")
        usb.util.dispose_resources(self.dev)
        time.sleep(3)
        for _ in range(20):
            self.dev = usb.core.find(idVendor=self.vid, idProduct=self.pid)
            if self.dev is not None:
                return
            time.sleep(0.5)
        raise SystemExit("camera did not come back after soft reboot")

    def _claim(self):
        try:
            if self.dev.is_kernel_driver_active(0):
                self.dev.detach_kernel_driver(0)
        except (usb.core.USBError, NotImplementedError):
            pass
        self.dev.set_configuration()
        usb.util.claim_interface(self.dev, 0)

    def control(self, rtype, req, value, index, data_or_len, timeout):
        return self.dev.ctrl_transfer(rtype, req, value, index, data_or_len, timeout or 1000)

    def bulk_write(self, ep, data, timeout):
        return self.dev.write(ep, data, timeout or 1000)

    def bulk_read(self, ep, size, timeout):
        return bytes(self.dev.read(ep, size, timeout or 1000))


class Emulator:
    def __init__(self, lib_path, firmware_dir, camera=None, verbose_usb=False, quiet=False, out_dir=None):
        self.lib_path = lib_path
        self.firmware_dir = firmware_dir
        self.out_dir = out_dir
        self.threads = []       # (start_routine, arg) captured from pthread_create
        self.files = {}         # emulated FILE* -> python file
        self.cam = camera
        self.verbose_usb = verbose_usb
        self.quiet = quiet
        self.uc = Uc(UC_ARCH_ARM, UC_MODE_ARM)
        # The library is built for VFPv3; the emulated FPU starts disabled.
        self.uc.reg_write(UC_ARM_REG_C1_C0_2, self.uc.reg_read(UC_ARM_REG_C1_C0_2) | (0xF << 20))
        self.uc.reg_write(UC_ARM_REG_FPEXC, 0x40000000)
        self.heap_next = HEAP
        self.fds = {}
        self.next_fd = 10
        self.usb_log = []
        self.unknown_imports = set()
        self._load()
        self._install_hooks()
        self._run_constructors()

    # ------------------------------------------------------------------ loading

    def _load(self):
        uc = self.uc
        f = open(self.lib_path, "rb")
        elf = ELFFile(f)
        image = open(self.lib_path, "rb").read()

        top = 0
        for seg in elf.iter_segments():
            if seg["p_type"] == "PT_LOAD":
                top = max(top, seg["p_vaddr"] + seg["p_memsz"])
        size = (top + 0xFFFF) & ~0xFFFF
        uc.mem_map(BASE, size)
        for seg in elf.iter_segments():
            if seg["p_type"] == "PT_LOAD":
                uc.mem_write(BASE + seg["p_vaddr"], image[seg["p_offset"]:seg["p_offset"] + seg["p_filesz"]])

        uc.mem_map(STUBS, 0x10000)
        uc.mem_map(HEAP, HEAP_SIZE)
        uc.mem_map(STACK_TOP - STACK_SIZE, STACK_SIZE)
        uc.mem_map(SCRATCH, 0x10000)
        uc.mem_map(END, 0x1000)
        uc.mem_write(END, BX_LR)

        dynsym = elf.get_section_by_name(".dynsym")
        self.exports = {}
        for s in dynsym.iter_symbols():
            if s["st_shndx"] != "SHN_UNDEF" and s.name:
                self.exports[s.name] = BASE + s["st_value"]

        self.stub_names = {}
        stub_for = {}

        def stub(name):
            if name not in stub_for:
                addr = STUBS + 4 * len(stub_for)
                uc.mem_write(addr, BX_LR)
                stub_for[name] = addr
                self.stub_names[addr] = name
            return stub_for[name]

        for sec in elf.iter_sections():
            if not isinstance(sec, RelocationSection):
                continue
            symtab = elf.get_section(sec["sh_link"])
            for r in sec.iter_relocations():
                where = BASE + r["r_offset"]
                rtype = r["r_info_type"]
                sym = symtab.get_symbol(r["r_info_sym"]) if r["r_info_sym"] else None
                if rtype == 23:  # R_ARM_RELATIVE
                    old = struct.unpack("<I", uc.mem_read(where, 4))[0]
                    uc.mem_write(where, struct.pack("<I", (old + BASE) & 0xFFFFFFFF))
                    continue
                if sym is None:
                    continue
                if sym["st_shndx"] != "SHN_UNDEF":
                    target = BASE + sym["st_value"]
                else:
                    target = stub(sym.name)
                if rtype == 2:  # R_ARM_ABS32: REL-style, addend already in place
                    old = struct.unpack("<I", uc.mem_read(where, 4))[0]
                    uc.mem_write(where, struct.pack("<I", (old + target) & 0xFFFFFFFF))
                elif rtype in (21, 22):  # GLOB_DAT, JUMP_SLOT
                    uc.mem_write(where, struct.pack("<I", target))

        self.errno_addr = SCRATCH
        init_array = elf.get_section_by_name(".init_array")
        self.init_array = (init_array["sh_addr"], init_array["sh_size"] // 4) if init_array else None

    def _run_constructors(self):
        """Run the library's static constructors, as the dynamic loader would."""
        if not self.init_array:
            return
        start, n = self.init_array
        for i in range(n):
            fn = self.u32(BASE + start + 4 * i)
            if fn and fn != 0xFFFFFFFF:
                self.call_addr(fn)

    # ------------------------------------------------------------------ helpers

    def reg(self, r):
        return self.uc.reg_read(r)

    def arg(self, i):
        if i < 4:
            return self.uc.reg_read(ARG_REGS[i])
        sp = self.uc.reg_read(UC_ARM_REG_SP)
        return struct.unpack("<I", self.uc.mem_read(sp + 4 * (i - 4), 4))[0]

    def read(self, addr, n):
        return bytes(self.uc.mem_read(addr, n))

    def write(self, addr, data):
        self.uc.mem_write(addr, bytes(data))

    def u32(self, addr):
        return struct.unpack("<I", self.read(addr, 4))[0]

    def cstr(self, addr, limit=4096):
        out = bytearray()
        while addr and len(out) < limit:
            b = self.read(addr, 1)[0]
            if b == 0:
                break
            out.append(b)
            addr += 1
        return out.decode("latin-1")

    def malloc(self, n):
        n = (max(n, 1) + 15) & ~15
        addr = self.heap_next
        self.heap_next += n + 16
        if self.heap_next > HEAP + HEAP_SIZE:
            raise MemoryError("emulated heap exhausted")
        self.write(addr, b"\0" * n)
        self.write(addr - 4, struct.pack("<I", n)) if addr > HEAP else None
        return addr

    def out(self, text):
        if not self.quiet:
            sys.stdout.write(text)
            sys.stdout.flush()

    # ------------------------------------------------------------------ calling

    def call_addr(self, addr, *args, timeout_s=600):
        uc = self.uc
        # Set the CPU mode first: ARM banks SP and LR per mode, so writing them
        # before switching would leave the user-mode stack pointer at 0.
        uc.reg_write(UC_ARM_REG_CPSR, 0x10 | (0x20 if addr & 1 else 0))
        sp = STACK_TOP - 0x100
        extra = list(args[4:])
        if extra:
            sp -= 4 * len(extra)
            sp &= ~7
            for i, a in enumerate(extra):
                uc.mem_write(sp + 4 * i, struct.pack("<I", a & 0xFFFFFFFF))
        uc.reg_write(UC_ARM_REG_SP, sp)
        for i, a in enumerate(args[:4]):
            uc.reg_write(ARG_REGS[i], a & 0xFFFFFFFF)
        uc.reg_write(UC_ARM_REG_LR, END)
        try:
            uc.emu_start(addr, END, timeout=int(timeout_s * 1e6))
        except UcError as e:
            pc = uc.reg_read(UC_ARM_REG_PC)
            raise RuntimeError(f"emulation fault {e} at pc={pc:#x} (lib offset {pc - BASE:#x}), lr={uc.reg_read(UC_ARM_REG_LR):#x}")
        return uc.reg_read(UC_ARM_REG_R0)

    def call(self, name, *args, **kw):
        r = self.call_addr(self.exports[name], *args, **kw)
        return r - (1 << 32) if r & 0x80000000 else r

    def put_string(self, s):
        b = s.encode() + b"\0"
        a = self.malloc(len(b))
        self.write(a, b)
        return a

    # ------------------------------------------------------------------ hooks

    def _ret(self, value=0):
        """Return from an intercepted library function to its caller."""
        self.uc.reg_write(UC_ARM_REG_R0, value & 0xFFFFFFFF)
        self.uc.reg_write(UC_ARM_REG_PC, self.uc.reg_read(UC_ARM_REG_LR))

    def _install_hooks(self):
        uc = self.uc
        uc.hook_add(UC_HOOK_CODE, self._on_stub, begin=STUBS, end=STUBS + 0xFFFF)
        uc.hook_add(UC_HOOK_MEM_INVALID, self._on_bad_mem)
        # Library functions replaced outright (they wrap libusb or threads).
        self.overrides = {
            "getUsbHandle": lambda: self._ret(0x1),
            "usb_control_msg": self._usb_control_msg,
            "usb_bulk_write": self._usb_bulk_write,
            "usb_bulk_read": self._usb_bulk_read,
            "usb_interrupt_read": self._usb_bulk_read,
            "usb_strerror": lambda: self._ret(self.put_string("usb error")),
            "pctimer": self._pctimer,
        }
        for name, fn in self.overrides.items():
            if name in self.exports:
                a = self.exports[name] & ~1
                uc.hook_add(UC_HOOK_CODE, lambda uc, addr, size, ud, fn=fn: fn(), begin=a, end=a)

    def _on_bad_mem(self, uc, access, address, size, value, ud):
        pc = uc.reg_read(UC_ARM_REG_PC)
        print(f"\n!! bad memory access {address:#x} (size {size}) at pc={pc:#x} lib+{pc - BASE:#x}")
        return False

    # libusb-0.1: usb_control_msg(dev, requesttype, request, value, index, bytes, size, timeout)
    def _usb_control_msg(self):
        rtype, req, value, index = self.arg(1), self.arg(2), self.arg(3) & 0xFFFF, self.arg(4) & 0xFFFF
        buf, size, timeout = self.arg(5), self.arg(6), self.arg(7)
        try:
            if rtype & 0x80:
                data = bytes(self.cam.control(rtype, req, value, index, size, timeout))
                self.write(buf, data)
                n = len(data)
            else:
                data = self.read(buf, size) if size else b""
                n = self.cam.control(rtype, req, value, index, data, timeout)
            self._trace(f"CTRL {rtype:02x} {req:02x} v={value:04x} i={index:04x} len={size} -> {n}"
                        + (f" {data[:16].hex(' ')}" if size else ""))
            self._ret(n)
        except usb.core.USBError as e:
            self._trace(f"CTRL {rtype:02x} {req:02x} v={value:04x} i={index:04x} len={size} -> ERROR {e}")
            self._ret(-(e.errno or 5))

    # usb_bulk_write(dev, ep, bytes, size, timeout)
    def _usb_bulk_write(self):
        ep, buf, size, timeout = self.arg(1), self.arg(2), self.arg(3), self.arg(4)
        try:
            n = self.cam.bulk_write(ep, self.read(buf, size), timeout)
            self._trace(f"BULK OUT ep{ep:02x} {size} -> {n}")
            self._ret(n)
        except usb.core.USBError as e:
            self._trace(f"BULK OUT ep{ep:02x} {size} -> ERROR {e}")
            self._ret(-(e.errno or 5))

    def _usb_bulk_read(self):
        ep, buf, size, timeout = self.arg(1), self.arg(2), self.arg(3), self.arg(4)
        try:
            data = self.cam.bulk_read(ep, size, timeout)
            self.write(buf, data)
            self._trace(f"BULK IN  ep{ep:02x} {size} -> {len(data)}")
            self._ret(len(data))
        except usb.core.USBError as e:
            self._trace(f"BULK IN  ep{ep:02x} {size} -> ERROR {e}")
            self._ret(-(e.errno or 110))

    def _pctimer(self):
        # returns a double in r0:r1
        t = time.monotonic()
        lo, hi = struct.unpack("<II", struct.pack("<d", t))
        self.uc.reg_write(UC_ARM_REG_R1, hi)
        self._ret(lo)

    def _trace(self, line):
        self.usb_log.append(line)
        if self.verbose_usb:
            print("   usb:", line)

    # ------------------------------------------------------------------ libc

    def _on_stub(self, uc, addr, size, ud):
        name = self.stub_names.get(addr)
        fn = getattr(self, "c_" + (name or "").split("@")[0], None)
        if fn is None:
            if name not in self.unknown_imports:
                self.unknown_imports.add(name)
                print(f"   [stub] {name}() -> 0")
            uc.reg_write(UC_ARM_REG_R0, 0)
            return
        r = fn()
        if isinstance(r, tuple):  # 64-bit result (double) in r0:r1
            uc.reg_write(UC_ARM_REG_R1, r[1] & 0xFFFFFFFF)
            r = r[0]
        uc.reg_write(UC_ARM_REG_R0, (r or 0) & 0xFFFFFFFF)

    def c_malloc(self):
        return self.malloc(self.arg(0))

    def c_free(self):
        return 0

    def c_realloc(self):
        old, n = self.arg(0), self.arg(1)
        new = self.malloc(n)
        if old:
            oldn = self.u32(old - 4) if old > HEAP else n
            self.write(new, self.read(old, min(oldn, n)))
        return new

    def c_memcpy(self):
        d, s, n = self.arg(0), self.arg(1), self.arg(2)
        if n:
            self.write(d, self.read(s, n))
        return d

    c_memmove = c_memcpy

    def c_memset(self):
        d, v, n = self.arg(0), self.arg(1), self.arg(2)
        if n:
            self.write(d, bytes([v & 0xFF]) * n)
        return d

    def c_strlen(self):
        return len(self.cstr(self.arg(0)).encode("latin-1"))

    def c_strcpy(self):
        d, s = self.arg(0), self.arg(1)
        self.write(d, self.cstr(s).encode("latin-1") + b"\0")
        return d

    def c_strncpy(self):
        d, s, n = self.arg(0), self.arg(1), self.arg(2)
        b = self.cstr(s).encode("latin-1")[:n]
        self.write(d, b + b"\0" * (n - len(b)))
        return d

    def c_strcat(self):
        d, s = self.arg(0), self.arg(1)
        self.write(d + len(self.cstr(d)), self.cstr(s).encode("latin-1") + b"\0")
        return d

    def c_strcmp(self):
        a, b = self.cstr(self.arg(0)), self.cstr(self.arg(1))
        return (a > b) - (a < b)

    def c_strcasecmp(self):
        a, b = self.cstr(self.arg(0)).lower(), self.cstr(self.arg(1)).lower()
        return (a > b) - (a < b)

    # math (softfp ABI: doubles in register pairs, floats in one register)
    def _dbl(self, lo, hi):
        return struct.unpack("<d", struct.pack("<II", lo, hi))[0]

    def _ret_dbl(self, v):
        return struct.unpack("<II", struct.pack("<d", v))

    def c_pow(self):
        try:
            v = math.pow(self._dbl(self.arg(0), self.arg(1)), self._dbl(self.arg(2), self.arg(3)))
        except (OverflowError, ValueError):
            v = float("nan")
        return self._ret_dbl(v)

    def c_sincos(self):
        x = self._dbl(self.arg(0), self.arg(1))
        self.write(self.arg(2), struct.pack("<d", math.sin(x)))
        self.write(self.arg(3), struct.pack("<d", math.cos(x)))
        return 0

    def c_log10f(self):
        x = struct.unpack("<f", struct.pack("<I", self.arg(0)))[0]
        v = math.log10(x) if x > 0 else float("-inf")
        return struct.unpack("<I", struct.pack("<f", v))[0]

    def c___errno_location(self):
        return self.errno_addr

    def c_usleep(self):
        time.sleep(self.arg(0) / 1e6)
        return 0

    def c_sleep(self):
        time.sleep(self.arg(0))
        return 0

    def c_gettimeofday(self):
        tv = self.arg(0)
        t = time.time()
        if tv:
            self.write(tv, struct.pack("<II", int(t), int((t % 1) * 1e6)))
        return 0

    def c_time(self):
        t = int(time.time())
        if self.arg(0):
            self.write(self.arg(0), struct.pack("<I", t))
        return t

    for _n in ["pthread_mutex_init", "pthread_mutex_lock", "pthread_mutex_unlock", "pthread_cond_init",
               "pthread_cond_signal", "pthread_attr_init", "pthread_attr_destroy", "pthread_setschedparam",
               "fflush", "fsync", "sync", "_ZN9__gnu_cxx18__exchange_and_addEPVii"]:
        locals()["c_" + _n] = lambda self: 0
    del _n

    # files: only the firmware image is visible to the emulated code
    def _host_path(self, path):
        name = os.path.basename(path)
        local = os.path.join(self.firmware_dir, name)
        return local if os.path.isfile(local) else None

    def c___xstat(self):
        path, buf = self.cstr(self.arg(1)), self.arg(2)
        local = self._host_path(path)
        if not local:
            self.write(self.errno_addr, struct.pack("<I", 2))
            return 0xFFFFFFFF
        st = bytearray(88)
        struct.pack_into("<I", st, 0x2C, os.path.getsize(local))
        struct.pack_into("<I", st, 0x10, 0o100644)
        self.write(buf, st)
        return 0

    def c_open(self):
        path, flags = self.cstr(self.arg(0)), self.arg(1)
        if flags & 3:  # O_WRONLY / O_RDWR: only into out_dir, by basename
            if not self.out_dir:
                self.write(self.errno_addr, struct.pack("<I", 13))
                return 0xFFFFFFFF
            os.makedirs(self.out_dir, exist_ok=True)
            local = os.path.join(self.out_dir, os.path.basename(path) or "out.bin")
            f = open(local, "ab" if flags & 0o2000 else "wb")
        else:
            local = self._host_path(path)
            if not local:
                self.write(self.errno_addr, struct.pack("<I", 2))
                return 0xFFFFFFFF
            f = open(local, "rb")
        fd = self.next_fd
        self.next_fd += 1
        self.fds[fd] = f
        print(f"   [file] open {path} -> {local}")
        return fd

    def c_write(self):
        fd, buf, n = self.arg(0), self.arg(1), self.arg(2)
        if fd in (1, 2):
            self.out(self.read(buf, n).decode("latin-1"))
            return n
        f = self.fds.get(fd)
        if not f or not n:
            return 0 if f else 0xFFFFFFFF
        f.write(self.read(buf, n))
        f.flush()
        return n


    # pthreads: the emulator is single-threaded, so threads are recorded and
    # run explicitly by the caller (Emulator.run_thread).
    def c_pthread_create(self):
        tid_ptr, start, arg = self.arg(0), self.arg(2), self.arg(3)
        self.threads.append((start, arg))
        if tid_ptr:
            self.write(tid_ptr, struct.pack("<I", len(self.threads)))
        print(f"   [thread] created #{len(self.threads)} start=lib+{start - BASE:#x} arg={arg:#x}")
        return 0

    def override(self, name, fn):
        """Replace a library function with a Python one (fn reads args, calls _ret)."""
        a = self.exports[name] & ~1
        self.uc.hook_add(UC_HOOK_CODE, lambda uc, addr, size, ud: fn(), begin=a, end=a)

    def trace_calls(self, names):
        """Count (and print the first few) calls to these library functions."""
        self.call_counts = {n: 0 for n in names}
        for n in names:
            if n not in self.exports:
                continue
            a = self.exports[n] & ~1

            def hit(uc, addr, size, ud, n=n):
                self.call_counts[n] += 1
                if self.call_counts[n] <= 3:
                    print(f"   [call] {n}({', '.join(hex(self.arg(i)) for i in range(4))})")
            self.uc.hook_add(UC_HOOK_CODE, hit, begin=a, end=a)

    def run_thread(self, index, seconds):
        start, arg = self.threads[index]
        try:
            self.call_addr(start, arg, timeout_s=seconds)
        except RuntimeError as e:
            print(f"   [thread] #{index + 1} stopped: {e}")

    # stdio files: writes go to out_dir (by basename) only; reads only the firmware.
    def c_fopen(self):
        path, mode = self.cstr(self.arg(0)), self.cstr(self.arg(1))
        if any(c in mode for c in "wa+"):
            if not self.out_dir:
                return 0
            os.makedirs(self.out_dir, exist_ok=True)
            local = os.path.join(self.out_dir, os.path.basename(path) or "out.bin")
            f = open(local, "ab" if "a" in mode else "wb")
        else:
            local = self._host_path(path)
            if not local:
                return 0
            f = open(local, "rb")
        handle = self.malloc(16)
        self.files[handle] = f
        print(f"   [file] fopen {path} ({mode}) -> {local}")
        return handle

    def c_fwrite(self):
        ptr, size, n, fh = self.arg(0), self.arg(1), self.arg(2), self.arg(3)
        f = self.files.get(fh)
        if not f or not size * n:
            return 0
        f.write(self.read(ptr, size * n))
        f.flush()
        return n

    def c_fread(self):
        ptr, size, n, fh = self.arg(0), self.arg(1), self.arg(2), self.arg(3)
        f = self.files.get(fh)
        if not f or not size:
            return 0
        data = f.read(size * n)
        self.write(ptr, data)
        return len(data) // size

    def c_fclose(self):
        f = self.files.pop(self.arg(0), None)
        if f:
            f.close()
        return 0

    def c_read(self):
        fd, buf, n = self.arg(0), self.arg(1), self.arg(2)
        f = self.fds.get(fd)
        if not f:
            return 0xFFFFFFFF
        data = f.read(n)
        self.write(buf, data)
        return len(data)

    def c_lseek(self):
        fd, off, whence = self.arg(0), self.arg(1), self.arg(2)
        f = self.fds.get(fd)
        if not f:
            return 0xFFFFFFFF
        return f.seek(struct.unpack("<i", struct.pack("<I", off))[0], whence)

    def c_close(self):
        f = self.fds.pop(self.arg(0), None)
        if f:
            f.close()
        return 0

    # output
    def c_puts(self):
        self.out(self.cstr(self.arg(0)) + "\n")
        return 1

    def c_putchar(self):
        self.out(chr(self.arg(0) & 0xFF))
        return self.arg(0)

    def c_fputc(self):
        self.out(chr(self.arg(0) & 0xFF))
        return self.arg(0)

    def c_printf(self):
        self.out(self._format(self.cstr(self.arg(0)), 1))
        return 0

    def c_fprintf(self):
        self.out(self._format(self.cstr(self.arg(1)), 2))
        return 0

    def c_sprintf(self):
        s = self._format(self.cstr(self.arg(1)), 2).encode("latin-1", "replace")
        self.write(self.arg(0), s + b"\0")
        return len(s)

    def c_snprintf(self):
        n = self.arg(1)
        s = self._format(self.cstr(self.arg(2)), 3).encode("latin-1", "replace")
        if n:
            self.write(self.arg(0), s[: n - 1] + b"\0")
        return len(s)

    def _format(self, fmt, first_arg):
        """Minimal printf: ARM EABI varargs (doubles 8-byte aligned)."""
        out, i, slot = [], 0, first_arg

        def take():
            nonlocal slot
            v = self.arg(slot)
            slot += 1
            return v

        def take_double():
            nonlocal slot
            if slot % 2:
                slot += 1
            lo, hi = self.arg(slot), self.arg(slot + 1)
            slot += 2
            return struct.unpack("<d", struct.pack("<II", lo, hi))[0]

        while i < len(fmt):
            c = fmt[i]
            if c != "%":
                out.append(c)
                i += 1
                continue
            j = i + 1
            while j < len(fmt) and fmt[j] in "-+ #0123456789.*hlLqjzt":
                j += 1
            if j >= len(fmt):
                break
            spec, conv = fmt[i:j], fmt[j]
            flags = spec[1:].replace("l", "").replace("h", "").replace("q", "").replace("z", "")
            i = j + 1
            if conv == "%":
                out.append("%")
            elif conv in "di":
                v = take()
                v = v - (1 << 32) if v & 0x80000000 else v
                out.append(("%" + flags + "d") % v)
            elif conv in "uxXo":
                out.append(("%" + flags + conv) % take())
            elif conv == "c":
                out.append(chr(take() & 0xFF))
            elif conv == "s":
                out.append(("%" + flags + "s") % self.cstr(take()))
            elif conv == "p":
                out.append("%#x" % take())
            elif conv in "feEgG":
                v = take_double()
                out.append(("%" + flags + conv) % v if math.isfinite(v) else str(v))
            else:
                out.append(spec + conv)
        return "".join(out)


def default_paths():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    lib = os.path.join(root, "research/emp/x10p/libMoIPCamera_2058.so")
    fw = os.path.join(root, "research/emp/x10p")
    return lib, fw
