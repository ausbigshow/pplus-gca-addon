#!/usr/bin/env python3
"""Run the real compiled add-on code on an emulated PowerPC with a mock Brawl.

Loads the built BOOST.GCT at 0x80550010, fakes the Brawl SDK functions the
driver calls, simulates an IPC/USB stack with a WUP-028 adapter attached
(both /dev/usb/hid v4 and v5), and checks:
  1. The C0 entry preserves every register the codehandler might rely on.
  2. gca_install patches exactly the `bl PADRead` in updateLowGC and every
     `bl PADControlMotor`, nothing else, and is idempotent.
  3. Without adapter data, gca_PADRead falls through to the real PADRead.
  4. With an adapter, input is decoded and origin-calibrated correctly.
  5. Short presses between game reads are latched for exactly one read, and
     a held button is released on time (no phantom holds).
  6. Rumble state reaches the mock adapter as 0x11 + 4 bytes (v4 INTERRUPT_OUT
     and v5 INTERRUPT ioctlv), one message in flight, alongside polls.
  7. __VIResetSIIdle is called on input change only, and never if its code
     doesn't look right; PADControlMotor isn't hooked if its signature is off.
  8. Unplugging, or never having, the adapter falls back to stock behaviour.
  9. Real reports captured from an adapter (tests/data/*.gcap) replayed at
     their recorded timing against a 59.94 Hz game: every press is seen,
     no phantom holds, sticks rest at 0 after calibration.
"""
import os
import random
import struct
import sys
from pathlib import Path

from unicorn import Uc, UC_ARCH_PPC, UC_MODE_PPC32, UC_MODE_BIG_ENDIAN, UC_HOOK_CODE
from unicorn import ppc_const as P

ROOT = Path(__file__).resolve().parent.parent
DIST = Path(os.environ.get("GCA_DIST", ROOT / "dist"))  # tests/check_asm.py points this elsewhere
GCT = (DIST / "BOOST.GCT").read_bytes()
REPORT = dict(l.split(": ", 1) for l in (DIST / "build-report.txt").read_text().splitlines())
BLOB = int(REPORT["blob_addr"], 16)
GCA_PADREAD = int(REPORT["gca_PADRead"], 16)
GCA_MOTOR = int(REPORT["gca_PADControlMotor"], 16)
DIAG = int(REPORT["gca_diag"], 16)
DATA = Path(__file__).resolve().parent / "data"

# Brawl addresses (must match src/gca.c)
UPDATE_LOW_GC, PADREAD, PADMOTOR = 0x80029578, 0x80215FC4, 0x802162A4
IOS_OPEN_ASYNC, IOS_CLOSE_ASYNC = 0x80212290, 0x802124C8
IOS_IOCTL_ASYNC, IOS_IOCTLV_ASYNC = 0x80212C08, 0x80212FAC
IPC_HI, IPC_LO = 0x80211AE0, 0x80211AE8
DCFLUSH, ICINVAL = 0x801D76E8, 0x801D77CC
VI_RF_IDLE, VI_SI_IDLE = 0x801EA078, 0x801EA088

STOP = 0x80001000          # return address that ends an emulated call
STACK = 0x817F0000
R13 = 0x805A8000           # game SDA base
IPC_HI_VAR = R13 - 0x1000  # what IPCGetBufferHi loads
MEM2_LO, MEM2_HI = 0x933E8000, 0x933F0000
STATUS = 0x81000000        # PADStatus[4] the "game" passes
PADREAD_MARKER = 0x00C0FFEE
TB_PER_MS = 60750

BLR = 0x4E800020
MOTOR_SIG = [0x3C608000, 0x880330E3, 0x540006B5, 0x41820008]
# Fake vi.c idle resets: li r0,0; stw r0,d(r13); nop; blr (differ in d only).
RF_IDLE = [0x38000000, 0x900DA000, 0x60000000, BLR]
SI_IDLE = [0x38000000, 0x900DA004, 0x60000000, BLR]
# Callers of PADControlMotor planted around gfPadSystem / gfRumble.
MOTOR_SITES = [0x8002A9C0, 0x8002AAB0, 0x8002ABA0, 0x8002C200]
REG = {i: getattr(P, f"UC_PPC_REG_{i}") for i in range(32)}


def bl(src, dst):
    return 0x48000001 | ((dst - src) & 0x03FFFFFC)


class Brawl:
    def __init__(self, hid_version, adapter=True, motor_sig=True, si_shape=True):
        self.hid_version = hid_version
        self.uc = uc = Uc(UC_ARCH_PPC, UC_MODE_PPC32 | UC_MODE_BIG_ENDIAN)
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
        # PADControlMotor: 0xB8 bytes, SDK signature at +0x64, blr at the end.
        motor = [0x60000000] * (0xB8 // 4)
        motor[0x64 // 4:0x64 // 4 + 4] = MOTOR_SIG if motor_sig else [0x60000000] * 4
        motor[-1] = BLR
        uc.mem_write(PADMOTOR, struct.pack(">%dI" % len(motor), *motor))
        # Calls to it, plus near-misses that must stay untouched.
        for a in MOTOR_SITES:
            self.w32(a, bl(a, PADMOTOR))
        self.decoys = {0x8002A000: bl(0x8002A000, PADMOTOR + 4),          # wrong target
                       0x8002A010: bl(0x8002A010, PADMOTOR) & ~1,         # b, not bl
                       0x80100000: bl(0x80100000, 0x80215B18)}           # other fn
        for a, v in self.decoys.items():
            self.w32(a, v)
        self.uc.mem_write(VI_RF_IDLE, struct.pack(">4I", *RF_IDLE))
        si = SI_IDLE if si_shape else [0x38000000, 0x4800000D, 0x60000000, BLR]
        self.uc.mem_write(VI_SI_IDLE, struct.pack(">4I", *si))
        # Entry stubs (blr) for every mocked function; hooks run before them.
        for a in (0x80021000, 0x80215B18, PADREAD, IOS_OPEN_ASYNC, IOS_CLOSE_ASYNC,
                  IOS_IOCTL_ASYNC, IOS_IOCTLV_ASYNC, IPC_LO, DCFLUSH, ICINVAL, STOP):
            self.w32(a, BLR)
        # IPCGetBufferHi is real code: lwz r3,-0x1000(r13); blr
        self.w32(IPC_HI, 0x806D0000 | (-0x1000 & 0xFFFF))
        self.w32(IPC_HI + 4, BLR)
        self.w32(IPC_HI_VAR, MEM2_HI)

        self.queue = []
        self.probed = False
        self.padread_calls = self.motor_calls = self.si_idle_calls = 0
        self.fd_open = None
        self.devchange_calls = 0
        self.adapter_present = adapter
        self.replug = False
        self.packets = []
        self.dev_id = 0x1234 if hid_version == 4 else 0x5678
        self.tb = 0                      # virtual time base
        self.timed = False               # polls complete only via deliver()
        self.pending_poll = None
        self.rumble_msgs = []            # payloads seen on the OUT endpoint
        self.rumble_hold = False         # keep rumble replies pending
        self.rumble_pending = []
        self.polls_issued = 0

        mocks = {PADREAD: self.m_padread, IOS_OPEN_ASYNC: self.m_open,
                 IOS_CLOSE_ASYNC: self.m_close, IOS_IOCTL_ASYNC: self.m_ioctl,
                 IOS_IOCTLV_ASYNC: self.m_ioctlv, IPC_LO: self.m_ipclo,
                 PADMOTOR: self.m_motor, VI_SI_IDLE: self.m_si_idle}
        for a, f in mocks.items():
            uc.hook_add(UC_HOOK_CODE, lambda uc, addr, size, _, f=f: f(), begin=a, end=a)
        # Emulate mftb with the virtual time base (Unicorn's isn't controllable).
        blob = GCT[BLOB - 0x80550010:]
        for off in range(0, int(REPORT["blob_size"]), 4):
            w = struct.unpack(">I", blob[off:off + 4])[0]
            if w & 0xFC1FFFFF == 0x7C0C42E6:
                rd = (w >> 21) & 31
                uc.hook_add(UC_HOOK_CODE, lambda uc, addr, size, _, rd=rd: self.m_mftb(addr, rd),
                            begin=BLOB + off, end=BLOB + off)

    # ---- memory helpers
    def w32(self, a, v): self.uc.mem_write(a, struct.pack(">I", v & 0xFFFFFFFF))
    def r32(self, a): return struct.unpack(">I", self.uc.mem_read(a, 4))[0]
    def reg(self, r): return self.uc.reg_read(REG[r] if isinstance(r, int) and r < 32 else r)
    def args(self, n): return [self.uc.reg_read(REG[3 + i]) for i in range(n)]
    def ret(self, v): self.uc.reg_write(REG[3], v & 0xFFFFFFFF)
    def diag(self): return struct.unpack(">12I", self.uc.mem_read(DIAG, 48))

    # ---- mocks
    def m_mftb(self, addr, rd):
        self.uc.reg_write(REG[rd], self.tb & 0xFFFFFFFF)
        self.uc.reg_write(P.UC_PPC_REG_PC, addr + 4)

    def m_padread(self):
        self.padread_calls += 1
        r3 = self.args(1)[0]
        for i in range(4):  # stock: no controllers on a Wii U
            self.uc.mem_write(r3 + i * 12, bytes(10) + b"\xff\x00")
        self.ret(PADREAD_MARKER)

    def m_motor(self):
        self.motor_calls += 1
        self.last_motor = tuple(self.args(2))

    def m_si_idle(self):
        self.si_idle_calls += 1

    def m_open(self):
        path, _, cb, usr = self.args(4)
        assert bytes(self.uc.mem_read(path, 16)).split(b"\0")[0] == b"/dev/usb/hid"
        self.fd_open = 7
        self.queue.append((cb, 7, usr))
        self.ret(0)

    def m_close(self):
        self.fd_open = None
        self.ret(0)

    def m_ipclo(self):
        self.ret(MEM2_LO)

    def packet(self):
        return self.packets.pop(0) if len(self.packets) > 1 else self.packets[0]

    def poll_reply(self, cb, usr, ptr):
        self.polls_issued += 1
        if self.timed:
            assert self.pending_poll is None, "two polls in flight"
            self.pending_poll = (cb, usr, ptr)
        elif self.adapter_present:
            self.uc.mem_write(ptr, self.packet())
            self.queue.append((cb, 0x25, usr))
        else:
            self.queue.append((cb, -7, usr))

    def rumble(self, payload, cb, usr):
        assert len(payload) == 5 and payload[0] == 0x11, payload.hex()
        assert not self.rumble_pending, "second rumble message while one is in flight"
        self.rumble_msgs.append(bytes(payload[1:]))
        if self.rumble_hold:
            self.rumble_pending.append((cb, 5, usr))
        else:
            self.queue.append((cb, 5 if self.adapter_present else -7, usr))

    def m_ioctl(self):
        fd, num, inp, inlen, out, outlen, cb, usr = self.args(8)
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
            assert (dev, ep) == (self.dev_id, 2), (hex(dev), ep)
            data = bytes(self.uc.mem_read(ptr, ln))
            if data[0] == 0x13:
                assert ln == 1
                res = 1
            else:
                self.rumble(data, cb, usr)
        elif num == 3 and v == 4:                               # v4 INTERRUPT_IN
            dev, ep, ln, ptr = struct.unpack(">IIII", self.uc.mem_read(inp + 16, 16))
            assert (dev, ep, ln) == (self.dev_id, 0x81, 0x25)
            self.poll_reply(cb, usr, ptr)
        elif num == 1 and v == 5:                               # v5 DEVICE_CHANGE
            assert out >> 28 == 9
            self.devchange_calls += 1
            if self.devchange_calls == 1:
                ent = (self.dev_id, 0x057E0337, 0) if self.adapter_present else (0, 0, 0)
                self.uc.mem_write(out, struct.pack(">III", *ent))
                res = 1 if self.adapter_present else 0
        elif num == 6 and v == 5:  res = 0                      # ATTACH_FINISH
        elif num == 16 and v == 5:                              # SET_RESUME
            assert self.r32(inp) == self.dev_id; res = 0
        elif num == 3 and v == 5:  res = 0                      # GET_DEVICE_PARAMS
        else:
            raise AssertionError(f"unexpected ioctl {num} v{v}")
        if res is not None:
            self.queue.append((cb, res, usr))
        self.ret(0)

    def m_ioctlv(self):
        fd, num, nin, nout, argv, cb, usr = self.args(7)
        assert fd == 7 and num == 19 and self.hid_version == 5
        a0d, a0l, a1d, a1l = struct.unpack(">IIII", self.uc.mem_read(argv, 16))
        assert a0d >> 28 == 9 and a0l == 0x40, "v5 header not in MEM2"
        if nin == 2 and nout == 0:      # OUT: init or rumble
            assert self.r32(a0d) == self.dev_id, "OUT header lacks device id"
            data = bytes(self.uc.mem_read(a1d, a1l))
            if data[0] == 0x13:
                assert a1l == 1
                self.queue.append((cb, 1, usr))
            else:
                self.rumble(data, cb, usr)
        else:                            # IN: poll
            assert (nin, nout, a1l) == (1, 1, 0x25)
            assert self.r32(a0d) == self.dev_id, "poll header lacks device id"
            if not self.adapter_present and not self.timed:
                self.queue.append((cb, -7, usr))
            else:
                self.poll_reply(cb, usr, a1d)
        self.ret(0)

    # ---- running code
    def call(self, fn, *args, regs=None):
        uc = self.uc
        for i, a in enumerate(args):
            uc.reg_write(REG[3 + i], a & 0xFFFFFFFF)
        uc.reg_write(P.UC_PPC_REG_1, STACK)
        uc.reg_write(P.UC_PPC_REG_13, R13)
        uc.reg_write(P.UC_PPC_REG_LR, STOP)
        if regs:
            for k, v in regs.items(): uc.reg_write(k, v)
        uc.emu_start(fn, STOP, count=20_000_000)
        return self.uc.reg_read(REG[3])

    def pump(self, limit=8):
        """Deliver pending IOS completions (as the IPC interrupt would)."""
        n = 0
        while self.queue and n < limit:
            cb, res, usr = self.queue.pop(0)
            self.call(cb, res, usr)
            n += 1
        return n

    def deliver(self, report):
        """Timed mode: complete the outstanding poll with this report."""
        cb, usr, ptr = self.pending_poll
        self.pending_poll = None
        self.uc.mem_write(ptr, report)
        self.call(cb, 0x25, usr)
        self.pump(4)

    def fail_poll(self):
        """Timed mode: complete the outstanding poll with an IOS error."""
        cb, usr, _ = self.pending_poll
        self.pending_poll = None
        self.call(cb, -7, usr)
        self.pump(4)

    def read(self):
        r = self.call(GCA_PADREAD, STATUS)
        return r, self.read_status()

    def read_status(self):
        out = []
        for i in range(4):
            b = self.uc.mem_read(STATUS + i * 12, 12)
            btn, sx, sy, cx, cy, tl, tr, aa, ab, err = struct.unpack(">Hbbbb BBBB b", bytes(b[:11]))
            out.append(dict(btn=btn, sx=sx, sy=sy, cx=cx, cy=cy, tl=tl, tr=tr, err=err))
        return out

    def install_and_connect(self, packets):
        self.call(BLOB)
        self.packets = list(packets)
        self.call(GCA_PADREAD, STATUS)
        self.pump(limit=12)


def pkt(port0=None, port1=None):
    p = bytearray(0x25)
    p[0] = 0x21
    if port0:
        p[1:10] = bytes(port0)
    if port1:
        p[10:19] = bytes(port1)
    return bytes(p)


def with_buttons(base, b1=0, b2=0):
    p = list(base)
    p[1], p[2] = b1, b2
    return p


REST = [0x10, 0x00, 0x00, 130, 126, 128, 128, 30, 35]
PRESSED = [0x10, 0x01, 0x08, 200, 50, 128, 128, 200, 35]  # A, L click, stick, L analog
A, B = 0x0100, 0x0200

failed = False


def check(cond, msg):
    global failed
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        failed = True


def run_basic(version):
    print(f"\n== /dev/usb/hid v{version}: install, decode, fallback ==")
    g = Brawl(version)
    uc = g.uc

    # 1+2: C0 entry preserves registers and patches the call sites once.
    sentinel = {REG[i]: 0x11110000 + i for i in list(range(3, 13)) + list(range(14, 32))}
    sentinel[P.UC_PPC_REG_CTR] = 0xC7C7C7C7
    g.call(BLOB, regs=sentinel)
    kept = all(g.uc.reg_read(k) == v for k, v in sentinel.items())
    check(kept, "C0 entry preserves r3-r12, r14-r31 and CTR")
    words = struct.unpack(">%dI" % (0x1D8 // 4), uc.mem_read(UPDATE_LOW_GC, 0x1D8))
    diffs = [i for i, (a, b) in enumerate(zip(words, g.lowgc_orig)) if a != b]
    check(diffs == [0x10], f"only the bl PADRead was patched (changed words: {diffs})")
    check(words[0x10] == bl(UPDATE_LOW_GC + 0x40, GCA_PADREAD), "patched bl targets gca_PADRead")
    sites_ok = all(g.r32(a) == bl(a, GCA_MOTOR) for a in MOTOR_SITES)
    check(sites_ok, f"all {len(MOTOR_SITES)} bl PADControlMotor redirected to gca_PADControlMotor")
    check(all(g.r32(a) == v for a, v in g.decoys.items()), "near-miss branches left alone")
    d = g.diag()
    check(d[8] == len(MOTOR_SITES) and d[9] == 1, f"diag: motor sites {d[8]}, SI idle {d[9]}")
    g.call(BLOB)
    words2 = struct.unpack(">%dI" % (0x1D8 // 4), uc.mem_read(UPDATE_LOW_GC, 0x1D8))
    check(words2 == words and all(g.r32(a) == bl(a, GCA_MOTOR) for a in MOTOR_SITES),
          "re-running the C0 code changes nothing")

    # 3: first read with no data yet goes to the real PADRead and opens USB.
    g.packets = [pkt(REST), pkt(PRESSED)]
    r = g.call(GCA_PADREAD, STATUS)
    check(r == PADREAD_MARKER and g.padread_calls == 1, "no adapter data yet: real PADRead used")
    check(g.fd_open == 7, "opened /dev/usb/hid")

    # Deliver USB completions: open, version, device change, (v5 attach/resume/params), init, polls.
    g.pump(limit=12)
    r, s = g.read()
    check(r == 0 and g.padread_calls == 1, "adapter live: real PADRead no longer called")
    check(s[0]["err"] == 0 and all(x["err"] == -1 for x in s[1:]),
          f"port 1 connected, ports 2-4 empty (errs {[x['err'] for x in s]})")
    exp = dict(btn=0x0100 | 0x0040, sx=70, sy=-76, cx=0, cy=0, tl=170, tr=0)
    got = {k: s[0][k] for k in exp}
    check(got == exp, f"decoded + calibrated input {got}")

    d = g.diag()
    check(d[0] == 0x47434131 and d[1] == 7 and d[2] == 0, f"diagnostics {[hex(x) for x in d[:4]]}")
    check(d[4] >= 2 and d[5] == 2, f"poll counter {d[4]}, PADRead counter {d[5]}")
    check(g.si_idle_calls == 1, f"input changed: __VIResetSIIdle called ({g.si_idle_calls})")
    g.read()
    check(g.si_idle_calls == 1, "same input again: __VIResetSIIdle not called")

    # 8: unplug -> polls fail -> fall back to the real PADRead.
    g.adapter_present = False
    g.pump(limit=6)
    calls = g.padread_calls
    r, s = g.read()
    check(r == PADREAD_MARKER and g.padread_calls == calls + 1, "adapter unplugged: back to real PADRead")
    check(not g.queue and g.diag()[2] == 11, "unplugged: polling stops after the retries")
    g.call(GCA_MOTOR, 0, 1)
    g.pump(4)
    check(g.motor_calls == 1 and g.last_motor == (0, 1),
          "unplugged: real PADControlMotor still called with the game's arguments")


def run_latch(version):
    print(f"\n== /dev/usb/hid v{version}: press latch ==")
    g = Brawl(version)
    g.timed = True
    g.call(BLOB)
    g.call(GCA_PADREAD, STATUS)
    g.pump(limit=12)
    g.deliver(pkt(REST, REST))
    _, s = g.read()
    check(s[0]["btn"] == 0 and s[1]["btn"] == 0, "rest: no buttons")

    # Tap A on port 1 entirely between two reads.
    g.deliver(pkt(with_buttons(REST, 0x01), REST))
    g.deliver(pkt(REST, REST))
    _, s = g.read()
    check(s[0]["btn"] == A and s[1]["btn"] == 0, f"tap between reads is reported ({s[0]['btn']:#x})")
    _, s = g.read()
    check(s[0]["btn"] == 0, "and only for one read")
    check(g.diag()[7] == 1, f"latch counter = {g.diag()[7]}")

    # Hold B across a read, release before the next one: released on time.
    g.deliver(pkt(with_buttons(REST, 0x02), REST))
    _, s = g.read()
    check(s[0]["btn"] == B, "held B reported")
    g.deliver(pkt(with_buttons(REST, 0x02), REST))
    g.deliver(pkt(REST, REST))
    _, s = g.read()
    check(s[0]["btn"] == 0, "B released before the read: not held over (no phantom hold)")

    # Release and re-press between reads: a new press, so it's reported.
    g.deliver(pkt(with_buttons(REST, 0x02), REST))
    _, s = g.read()
    g.deliver(pkt(REST, REST))
    g.deliver(pkt(with_buttons(REST, 0x02), REST))
    g.deliver(pkt(REST, REST))
    _, s = g.read()
    check(s[0]["btn"] == B, "release + re-press between reads shows the re-press")

    # Port 2 tap, then port 2 unplugged before the read: nothing reported.
    g.deliver(pkt(REST, with_buttons(REST, 0x01)))
    g.deliver(pkt(REST, None))
    _, s = g.read()
    check(s[1]["err"] == -1 and s[1]["btn"] == 0, "controller unplugged: its latch is dropped")

    # Hot-plug: the adapter's first report for a new controller has all analog
    # bytes 0 (real Mayflash capture). Don't calibrate on it.
    g.deliver(pkt(REST, [0x14, 0x02, 0, 0, 0, 0, 0, 0, 0]))
    _, s = g.read()
    check(s[1]["err"] == -1, "hot-plug report with zeroed analog: port not connected yet")
    g.deliver(pkt(REST, [0x14, 0, 0, 0x81, 0x81, 0x86, 0x86, 0x20, 0x20]))
    _, s = g.read()
    check(s[1]["err"] == 0 and (s[1]["sx"], s[1]["sy"], s[1]["tl"]) == (0, 0, 0),
          f"next report calibrates to 0 ({s[1]})")

    # One failed poll (USB glitch) must not drop the adapter.
    g.fail_poll()
    check(g.pending_poll is not None, "poll error: driver polls again")
    g.deliver(pkt(REST, REST))
    r, _ = g.read()
    check(r == 0, "after a one-off poll error the adapter is still used")

    # Adapter goes stale (no reports for >1.5 s): stock path, latch cleared.
    g.deliver(pkt(with_buttons(REST, 0x01), REST))
    g.deliver(pkt(REST, REST))
    g.tb += 2000 * TB_PER_MS
    r, _ = g.read()
    check(r == PADREAD_MARKER, "stale adapter: real PADRead used")
    g.deliver(pkt(REST, REST))
    _, s = g.read()
    check(s[0]["btn"] == 0, "latched press from before the stall is not replayed later")


def run_rumble(version):
    print(f"\n== /dev/usb/hid v{version}: rumble ==")
    g = Brawl(version)
    g.timed = True
    g.call(BLOB)
    g.call(GCA_PADREAD, STATUS)
    g.pump(limit=12)
    g.deliver(pkt(REST))
    g.read()
    check(g.rumble_msgs == [], "no rumble message before the game asks")

    g.call(GCA_MOTOR, 0, 1)
    check(g.motor_calls == 1 and g.last_motor == (0, 1), "real PADControlMotor still called")
    g.deliver(pkt(REST))
    check(g.rumble_msgs == [bytes([1, 0, 0, 0])], f"port 1 on -> 11 01 00 00 00 ({[m.hex() for m in g.rumble_msgs]})")
    check(g.pending_poll is not None, "poll issued alongside the rumble message")

    # One in flight: hold the reply, change state twice, only latest goes out.
    g.rumble_hold = True
    g.call(GCA_MOTOR, 0, 0)
    g.deliver(pkt(REST))            # sends 00 00 00 00, reply held
    g.call(GCA_MOTOR, 2, 1)
    g.call(GCA_MOTOR, 3, 1)
    g.deliver(pkt(REST))
    g.deliver(pkt(REST))
    check(len(g.rumble_msgs) == 2, "while a rumble reply is pending, nothing else is sent")
    g.rumble_hold = False
    cb, res, usr = g.rumble_pending.pop()
    g.call(cb, res, usr)
    g.deliver(pkt(REST))
    check(g.rumble_msgs[-1] == bytes([0, 0, 1, 1]), f"after the reply, latest state sent ({g.rumble_msgs[-1].hex()})")

    g.call(GCA_MOTOR, 2, 2)          # STOP_HARD
    g.call(GCA_MOTOR, 3, 0)
    g.call(GCA_MOTOR, 7, 1)          # not a GC port: ignored
    g.deliver(pkt(REST))
    check(g.rumble_msgs[-1] == bytes([0, 0, 0, 0]), "STOP_HARD stops; out-of-range port ignored")
    n = len(g.rumble_msgs)
    for _ in range(5):
        g.deliver(pkt(REST))
    check(len(g.rumble_msgs) == n, "no messages while the state is unchanged")

    # A lost reply doesn't wedge rumble forever.
    g.rumble_hold = True
    g.call(GCA_MOTOR, 1, 1)
    g.deliver(pkt(REST))
    g.rumble_pending.clear()        # reply never comes
    g.rumble_hold = False
    g.call(GCA_MOTOR, 1, 0)
    for _ in range(70):
        g.deliver(pkt(REST))
    check(g.rumble_msgs[-1] == bytes([0, 0, 0, 0]) and len(g.rumble_msgs) == n + 2,
          "lost rumble reply: retried after the stall timeout")
    d = g.diag()
    check(d[6] == len(g.rumble_msgs) and d[10] == g.motor_calls,
          f"diag: rumble sends {d[6]}, motor calls {d[10]}")


def run_absent(version):
    print(f"\n== /dev/usb/hid v{version}: no adapter, bad signatures ==")
    g = Brawl(version, adapter=False)
    g.call(BLOB)
    for _ in range(3):
        r = g.call(GCA_PADREAD, STATUS)
        g.pump(limit=12)
    check(r == PADREAD_MARKER and g.padread_calls == 3, "no adapter: every read uses the real PADRead")
    g.call(GCA_MOTOR, 1, 1)
    g.pump(4)
    check(g.motor_calls == 1 and g.rumble_msgs == [], "no adapter: rumble goes only to the real PADControlMotor")
    check(g.si_idle_calls == 0, "no adapter: __VIResetSIIdle never called")

    g = Brawl(version, motor_sig=False, si_shape=False)
    g.call(BLOB)
    check(all(g.r32(a) == bl(a, PADMOTOR) for a in MOTOR_SITES),
          "PADControlMotor signature mismatch: no call site patched")
    words = struct.unpack(">%dI" % (0x1D8 // 4), g.uc.mem_read(UPDATE_LOW_GC, 0x1D8))
    check(words[0x10] == bl(UPDATE_LOW_GC + 0x40, GCA_PADREAD), "...but PADRead is still hooked")
    d = g.diag()
    check(d[8] == 0xEE and d[9] == 0xEE, f"diag reports both mismatches ({d[8]:#x}, {d[9]:#x})")
    g.packets = [pkt(REST), pkt(PRESSED)]
    g.call(GCA_PADREAD, STATUS)
    g.pump(limit=12)
    g.read()
    g.read()
    check(g.si_idle_calls == 0, "SI idle shape mismatch: never called")


def load_capture(path):
    raw = path.read_bytes()
    assert raw[:8] == b"GCAP\x01\x00\x00\x00", "bad capture header"
    recs = []
    for i in range(8, len(raw), 45):
        t, rep = struct.unpack(">Q", raw[i:i + 8])[0], raw[i + 8:i + 45]
        recs.append((t / 1e6, rep))
    return recs


BITS = [(0, 0x01, 0x0100), (0, 0x02, 0x0200), (0, 0x04, 0x0400), (0, 0x08, 0x0800),
        (0, 0x10, 0x0001), (0, 0x20, 0x0002), (0, 0x40, 0x0004), (0, 0x80, 0x0008),
        (1, 0x01, 0x1000), (1, 0x02, 0x0010), (1, 0x04, 0x0020), (1, 0x08, 0x0040)]


def pad_buttons(rep, port):
    d = rep[1 + 9 * port: 10 + 9 * port]
    if d[0] >> 4 not in (1, 2):
        return None
    return sum(m for byte, bit, m in BITS if d[1 + byte] & bit)


def run_replay(recs, version, phase):
    g = Brawl(version)
    g.timed = True
    g.call(BLOB)
    g.call(GCA_PADREAD, STATUS)
    g.pump(limit=12)
    period = 1000 / 59.94
    t_read = recs[0][0] + phase
    reads = []          # (time, status list)
    edges_since = [0] * 4
    prev = [0] * 4
    phantom = []
    for t, rep in recs:
        while t_read < t:
            g.tb = int(t_read * TB_PER_MS)
            r, s = g.read()
            if r == 0:
                for p in range(4):
                    # Allowed: what the latest report holds, plus presses
                    # that started since the previous read.
                    allowed = (pad_buttons(last_rep, p) or 0) | edges_since[p]
                    if s[p]["err"] == 0 and s[p]["btn"] & ~allowed:
                        phantom.append((t_read, p, s[p]["btn"], allowed))
                reads.append((t_read, s))
            edges_since = [0] * 4
            t_read += period
        g.tb = int(t * TB_PER_MS)
        g.deliver(rep)
        last_rep = rep
        for p in range(4):
            b = pad_buttons(rep, p) or 0
            edges_since[p] |= b & ~prev[p]
            prev[p] = b
    # Every press in the capture shows up in some read during its press window
    # or the read right after it.
    missed = []
    for p in range(4):
        on = {}
        for t, rep in recs + [(recs[-1][0] + 1, bytes(37))]:
            b = pad_buttons(rep, p) or 0
            for _, _, m in BITS:
                if b & m and m not in on:
                    on[m] = t
                elif not b & m and m in on:
                    start = on.pop(m)
                    seen = any(start <= tr <= t + period and s[p]["btn"] & m for tr, s in reads)
                    if not seen:
                        missed.append((p, hex(m), round(start), round(t - start)))
    return reads, missed, phantom, g


def run_captures():
    caps = sorted(DATA.glob("*.gcap"))
    if not caps:
        print("\n(no captures in tests/data; skipping replay)")
        return
    for path in caps:
        print(f"\n== replay {path.name} (real adapter reports) ==")
        recs = load_capture(path)
        presses = 0
        for p in range(4):
            prev = 0
            for _, rep in recs:
                b = pad_buttons(rep, p) or 0
                presses += bin(b & ~prev).count("1")
                prev = b
        rnd = random.Random(1)
        # Full rate as captured, and every other report (a console poll loop
        # running at half the adapter's rate).
        for label, sub in (("as captured", recs), ("half poll rate", recs[::2])):
            worst_missed, phantoms, reads_total = [], [], 0
            for version in (4, 5):
                for k in range(4):
                    phase = rnd.uniform(0, 1000 / 59.94)
                    reads, missed, phantom, g = run_replay(sub, version, phase)
                    reads_total += len(reads)
                    phantoms += phantom
                    if len(missed) > len(worst_missed):
                        worst_missed = missed
            check(reads_total > 0, f"{label}: {presses} presses, adapter live for {reads_total} game reads")
            check(not worst_missed, f"{label}: every press reaches the game (8 runs; missed {worst_missed[:5]})")
            check(not phantoms, f"{label}: no phantom buttons ({phantoms[:3]})")
        # Calibration: once connected and at rest, sticks read ~0.
        rest = [s[p] for _, s in reads[:20] for p in range(4) if s[p]["err"] == 0]
        if rest:
            worst = max(max(abs(x["sx"]), abs(x["sy"]), abs(x["cx"]), abs(x["cy"])) for x in rest)
            check(worst <= 2, f"sticks at rest after calibration: max |axis| = {worst}")


for v in (4, 5):
    run_basic(v)
    run_latch(v)
    run_rumble(v)
    run_absent(v)
run_captures()
print("\nRESULT:", "FAILED" if failed else "all checks passed")
sys.exit(1 if failed else 0)
