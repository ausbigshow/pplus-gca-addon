#!/usr/bin/env python3
"""Run the real compiled add-on code on an emulated PowerPC with a mock Brawl.

Loads the built BOOST.GCT at 0x80550010, fakes the Brawl SDK functions the
driver calls, simulates an IPC/USB stack with a WUP-028 adapter attached
(both /dev/usb/hid v4 and v5), and checks:
  1. The C0 entry preserves every register the codehandler might rely on.
  2. gca_install patches exactly the `bl PADRead` in updateLowGC, nothing else,
     and is idempotent.
  3. Without an adapter, gca_PADRead falls through to the real PADRead.
  4. With an adapter, input is decoded and origin-calibrated correctly.
  5. Unplugging the adapter falls back to the real PADRead.
"""
import struct
import sys
from pathlib import Path

from unicorn import Uc, UC_ARCH_PPC, UC_MODE_PPC32, UC_MODE_BIG_ENDIAN, UC_HOOK_CODE
from unicorn.ppc_const import *

ROOT = Path(__file__).resolve().parent.parent
GCT = (ROOT / "dist" / "BOOST.GCT").read_bytes()
REPORT = dict(l.split(": ", 1) for l in (ROOT / "dist" / "build-report.txt").read_text().splitlines())
BLOB = int(REPORT["blob_addr"], 16)
GCA_PADREAD = int(REPORT["gca_PADRead"], 16)

# Brawl addresses (must match src/gca.c)
UPDATE_LOW_GC, PADREAD = 0x80029578, 0x80215FC4
IOS_OPEN_ASYNC, IOS_CLOSE_ASYNC = 0x80212290, 0x802124C8
IOS_IOCTL_ASYNC, IOS_IOCTLV_ASYNC = 0x80212C08, 0x80212FAC
IPC_HI, IPC_LO = 0x80211AE0, 0x80211AE8
DCFLUSH, ICINVAL = 0x801D76E8, 0x801D77CC

STOP = 0x80001000          # return address that ends an emulated call
STACK = 0x817F0000
R13 = 0x805A8000           # game SDA base
IPC_HI_VAR = R13 - 0x1000  # what IPCGetBufferHi loads
MEM2_LO, MEM2_HI = 0x933E8000, 0x933F0000
STATUS = 0x81000000        # PADStatus[4] the "game" passes
PADREAD_MARKER = 0x00C0FFEE

BLR = 0x4E800020


def bl(src, dst):
    return 0x48000001 | ((dst - src) & 0x03FFFFFC)


class Brawl:
    def __init__(self, hid_version):
        self.hid_version = hid_version
        self.uc = Uc(UC_ARCH_PPC, UC_MODE_PPC32 | UC_MODE_BIG_ENDIAN)
        uc = self.uc
        uc.mem_map(0x80000000, 0x01800000)
        uc.mem_map(0x90000000, 0x04000000)
        uc.mem_write(0x80550010, GCT)
        # Fake updateLowGC: nops with a few unrelated bls and one bl PADRead.
        body = [0x60000000] * (0x1D8 // 4)
        body[3] = bl(UPDATE_LOW_GC + 12, 0x80021000)      # unrelated call
        body[0x10] = bl(UPDATE_LOW_GC + 0x40, PADREAD)    # the one to patch
        body[0x20] = bl(UPDATE_LOW_GC + 0x80, 0x80215B18) # another PAD fn
        body[-1] = BLR
        self.lowgc_orig = body
        uc.mem_write(UPDATE_LOW_GC, struct.pack(">%dI" % len(body), *body))
        for a in (0x80021000, 0x80215B18, PADREAD, IOS_OPEN_ASYNC, IOS_CLOSE_ASYNC,
                  IOS_IOCTL_ASYNC, IOS_IOCTLV_ASYNC, IPC_LO, DCFLUSH, ICINVAL, STOP):
            self.w32(a, BLR)
        # IPCGetBufferHi is real code: lwz r3,-0x1000(r13); blr
        self.w32(IPC_HI, 0x806D0000 | (-0x1000 & 0xFFFF))
        self.w32(IPC_HI + 4, BLR)
        self.w32(IPC_HI_VAR, MEM2_HI)
        self.queue = []
        self.probed = False
        self.padread_calls = 0
        self.fd_open = None
        self.devchange_calls = 0
        self.adapter_present = True
        self.packets = []
        self.dev_id = 0x1234 if hid_version == 4 else 0x5678
        uc.hook_add(UC_HOOK_CODE, self.on_code)

    # ---- memory helpers
    def w32(self, a, v): self.uc.mem_write(a, struct.pack(">I", v & 0xFFFFFFFF))
    def r32(self, a): return struct.unpack(">I", self.uc.mem_read(a, 4))[0]
    def reg(self, r): return self.uc.reg_read(r)

    # ---- mocks
    def ret(self, v): self.uc.reg_write(UC_PPC_REG_3, v & 0xFFFFFFFF)

    def on_code(self, uc, addr, size, _):
        r = [self.reg(getattr(__import__("unicorn.ppc_const", fromlist=["x"]), f"UC_PPC_REG_{i}")) for i in range(3, 11)]
        if addr == PADREAD:
            self.padread_calls += 1
            for i in range(4):  # stock: no controllers on a Wii U
                uc.mem_write(r[0] + i * 12, bytes(10) + b"\xff\x00")
            self.ret(PADREAD_MARKER)
        elif addr == IOS_OPEN_ASYNC:
            path = bytes(uc.mem_read(r[0], 16)).split(b"\0")[0]
            assert path == b"/dev/usb/hid", path
            self.fd_open = 7
            self.queue.append((r[2], 7, r[3]))
            self.ret(0)
        elif addr == IOS_CLOSE_ASYNC:
            self.fd_open = None
            self.ret(0)
        elif addr == IOS_IOCTL_ASYNC:
            self.ioctl(*r)
        elif addr == IOS_IOCTLV_ASYNC:
            self.ioctlv(*r[:7])
        elif addr == IPC_LO:
            self.ret(MEM2_LO)

    def packet(self):
        return self.packets.pop(0) if len(self.packets) > 1 else self.packets[0]

    def ioctl(self, fd, num, inp, inlen, out, outlen, cb, usr):
        assert fd == 7
        v = self.hid_version
        res = None  # None = stays pending
        if num == 6 and not self.probed:                        # v4 GET_VERSION probe
            self.probed = True
            res = 0x00040001 if v == 4 else -6
        elif num == 0 and v == 5:                               # v5 GET_VERSION
            assert out >> 28 == 9, "v5 buffer not in MEM2"
            self.w32(out, 0x00050001); res = 0
        elif num == 0 and v == 4:                               # v4 DEVICE_CHANGE
            self.devchange_calls += 1
            if self.devchange_calls == 1 or self.replug:
                self.replug = False
                words = [0] * (outlen // 4)
                if self.adapter_present:
                    words[0], words[1], words[4] = 0x44, self.dev_id, 0x057E0337
                self.uc.mem_write(out, struct.pack(">%dI" % len(words), *words))
                res = 0
        elif num == 4 and v == 4:                               # v4 INTERRUPT_OUT
            dev, ep, ln, ptr = struct.unpack(">IIII", self.uc.mem_read(inp + 16, 16))
            assert (dev, ep, ln) == (self.dev_id, 2, 1) and self.uc.mem_read(ptr, 1)[0] == 0x13
            res = 1
        elif num == 3 and v == 4:                               # v4 INTERRUPT_IN
            dev, ep, ln, ptr = struct.unpack(">IIII", self.uc.mem_read(inp + 16, 16))
            assert (dev, ep, ln) == (self.dev_id, 0x81, 0x25)
            if self.adapter_present:
                self.uc.mem_write(ptr, self.packet()); res = 0x25
            else:
                res = -7
        elif num == 1 and v == 5:                               # v5 DEVICE_CHANGE
            assert out >> 28 == 9
            self.devchange_calls += 1
            if self.devchange_calls == 1:
                self.uc.mem_write(out, struct.pack(">III", self.dev_id, 0x057E0337, 0))
                res = 1
        elif num == 6 and v == 5:  res = 0                      # ATTACH_FINISH
        elif num == 16 and v == 5:                              # SET_RESUME
            assert self.r32(inp) == self.dev_id; res = 0
        elif num == 3 and v == 5:  res = 0                      # GET_DEVICE_PARAMS
        else:
            raise AssertionError(f"unexpected ioctl {num} v{v}")
        if res is not None:
            self.queue.append((cb, res, usr))
        self.ret(0)

    def ioctlv(self, fd, num, nin, nout, argv, cb, usr):
        assert fd == 7 and num == 19 and self.hid_version == 5
        a0d, a0l, a1d, a1l = struct.unpack(">IIII", self.uc.mem_read(argv, 16))
        assert a0d >> 28 == 9 and self.r32(a0d if nin == 2 else a0d) is not None
        if nin == 2 and nout == 0:      # init
            assert self.uc.mem_read(a1d, 1)[0] == 0x13
            self.queue.append((cb, 1, usr))
        else:                            # poll
            assert (nin, nout, a1l) == (1, 1, 0x25)
            self.uc.mem_write(a1d, self.packet())
            self.queue.append((cb, 0x25, usr))
        self.ret(0)

    # ---- running code
    def call(self, fn, *args, regs=None):
        uc = self.uc
        for i, a in enumerate(args):
            uc.reg_write(getattr(__import__("unicorn.ppc_const", fromlist=["x"]), f"UC_PPC_REG_{3+i}"), a)
        uc.reg_write(UC_PPC_REG_1, STACK)
        uc.reg_write(UC_PPC_REG_13, R13)
        uc.reg_write(UC_PPC_REG_LR, STOP)
        if regs:
            for k, v in regs.items(): uc.reg_write(k, v)
        uc.emu_start(fn, STOP, count=2_000_000)
        return self.reg(UC_PPC_REG_3)

    def pump(self, limit=8):
        """Deliver pending IOS completions (as the IPC interrupt would)."""
        n = 0
        while self.queue and n < limit:
            cb, res, usr = self.queue.pop(0)
            self.call(cb, res & 0xFFFFFFFF, usr)
            n += 1
        return n

    def read_status(self):
        out = []
        for i in range(4):
            b = self.uc.mem_read(STATUS + i * 12, 12)
            btn, sx, sy, cx, cy, tl, tr, aa, ab, err = struct.unpack(">Hbbbb BBBB b", bytes(b[:11]))
            out.append(dict(btn=btn, sx=sx, sy=sy, cx=cx, cy=cy, tl=tl, tr=tr, err=err))
        return out


def pkt(port0):
    p = bytearray(0x25)
    p[0] = 0x21
    if port0:
        p[1:10] = bytes(port0)
    return bytes(p)


REST = [0x10, 0x00, 0x00, 130, 126, 128, 128, 30, 35]
PRESSED = [0x10, 0x01, 0x08, 200, 50, 128, 128, 200, 35]  # A, L click, stick, L analog


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        global failed
        failed = True


failed = False


def run(version):
    print(f"\n== /dev/usb/hid v{version} ==")
    g = Brawl(version)
    g.replug = False
    uc = g.uc

    # 1+2: C0 entry preserves registers and patches the call site once.
    sentinel = {getattr(__import__("unicorn.ppc_const", fromlist=["x"]), f"UC_PPC_REG_{i}"): 0x11110000 + i
                for i in list(range(3, 13)) + list(range(14, 32))}
    sentinel[UC_PPC_REG_CTR] = 0xC7C7C7C7
    g.call(BLOB, regs=sentinel)
    kept = all(g.reg(k) == v for k, v in sentinel.items())
    check(kept, "C0 entry preserves r3-r12, r14-r31 and CTR")
    words = struct.unpack(">%dI" % (0x1D8 // 4), uc.mem_read(UPDATE_LOW_GC, 0x1D8))
    diffs = [i for i, (a, b) in enumerate(zip(words, g.lowgc_orig)) if a != b]
    check(diffs == [0x10], f"only the bl PADRead was patched (changed words: {diffs})")
    check(words[0x10] == bl(UPDATE_LOW_GC + 0x40, GCA_PADREAD), "patched bl targets gca_PADRead")
    g.call(BLOB)
    words2 = struct.unpack(">%dI" % (0x1D8 // 4), uc.mem_read(UPDATE_LOW_GC, 0x1D8))
    check(words2 == words, "re-running the C0 code changes nothing")

    # 3: first read with no data yet goes to the real PADRead and opens USB.
    g.packets = [pkt(REST), pkt(PRESSED)]
    r = g.call(GCA_PADREAD, STATUS)
    check(r == PADREAD_MARKER and g.padread_calls == 1, "no adapter data yet: real PADRead used")
    check(g.fd_open == 7, "opened /dev/usb/hid")

    # Deliver USB completions: open, version, device change, (v5 attach/resume/params), init, polls.
    g.pump(limit=12)
    st = g.read_status
    r = g.call(GCA_PADREAD, STATUS)
    s = st()
    check(r == 0 and g.padread_calls == 1, "adapter live: real PADRead no longer called")
    check(s[0]["err"] == 0 and all(x["err"] == -1 for x in s[1:]),
          f"port 1 connected, ports 2-4 empty (errs {[x['err'] for x in s]})")
    exp = dict(btn=0x0100 | 0x0040, sx=70, sy=-76, cx=0, cy=0, tl=170, tr=0)
    got = {k: s[0][k] for k in exp}
    check(got == exp, f"decoded + calibrated input {got}")

    diag = struct.unpack(">4I", uc.mem_read(int(REPORT["gca_diag"], 16), 16))
    check(diag[0] == 0x47434131 and diag[1] == 7 and diag[2] == 0, f"diagnostics {[hex(x) for x in diag]}")

    # 5: unplug -> polls fail -> fall back to the real PADRead.
    g.adapter_present = False
    if version == 4:
        g.pump(limit=4)
        r = g.call(GCA_PADREAD, STATUS)
        check(r == PADREAD_MARKER and g.padread_calls == 2, "adapter unplugged: back to real PADRead")


run(4)
run(5)
print("\nRESULT:", "FAILED" if failed else "all checks passed")
sys.exit(1 if failed else 0)
