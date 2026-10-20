#!/usr/bin/env python3
"""Talk to a WUP-028 (or Mayflash in Wii U mode) adapter from a PC over libusb.

  measure  Send init (0x13), read interrupt IN reports for N seconds, print the
           report rate and inter-arrival jitter, and optionally save a capture
           that tests/emulate.py can replay through its mock adapter.
  rumble   Send the rumble command (0x11 + one byte per port). Needs the grey
           (power) cable plugged in.
  show     Print decoded reports live, to check which port/button is which.

Capture format (tests/data/*.gcap): 8-byte header b"GCAP\\x01\\x00\\x00\\x00", then
records of <u64 big-endian ns since start><37-byte report>.

Requires: pip install pyusb, and read/write access to the device node (on
systemd distros the logged-in user normally has it via uaccess; otherwise add a
udev rule). Nothing else may have the interface claimed (Dolphin, Steam,
Wine...).
"""
import argparse
import statistics
import struct
import sys
import time

import usb.core
import usb.util

VID, PID = 0x057E, 0x0337
EP_IN, EP_OUT = 0x81, 0x02
REPORT = 37
MAGIC = b"GCAP\x01\x00\x00\x00"


def open_adapter():
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        sys.exit("adapter 057e:0337 not found")
    try:
        if dev.is_kernel_driver_active(0):
            dev.detach_kernel_driver(0)
    except (NotImplementedError, usb.core.USBError):
        pass
    try:
        dev.set_configuration()
    except usb.core.USBError:
        pass  # already configured, or busy; claim below gives the real error
    usb.util.claim_interface(dev, 0)
    return dev


def init(dev):
    dev.write(EP_OUT, b"\x13", timeout=1000)
    # Drain anything queued from before init.
    t = time.monotonic() + 0.05
    while time.monotonic() < t:
        try:
            dev.read(EP_IN, REPORT, timeout=20)
        except usb.core.USBTimeoutError:
            break


def decode(rep):
    out = []
    for i in range(4):
        d = rep[1 + 9 * i: 10 + 9 * i]
        typ = d[0] >> 4
        out.append(dict(type=typ, b1=d[1], b2=d[2], sx=d[3], sy=d[4], cx=d[5],
                        cy=d[6], l=d[7], r=d[8]))
    return out


def pct(sorted_vals, p):
    if not sorted_vals:
        return float("nan")
    k = min(len(sorted_vals) - 1, max(0, round(p / 100 * (len(sorted_vals) - 1))))
    return sorted_vals[k]


def cmd_measure(a):
    dev = open_adapter()
    init(dev)
    recs = []
    t0 = time.perf_counter_ns()
    end = t0 + int(a.seconds * 1e9)
    timeouts = bad = 0
    while time.perf_counter_ns() < end:
        try:
            rep = bytes(dev.read(EP_IN, REPORT, timeout=100))
        except usb.core.USBTimeoutError:
            timeouts += 1
            continue
        t = time.perf_counter_ns() - t0
        if len(rep) != REPORT or rep[0] != 0x21:
            bad += 1
            continue
        recs.append((t, rep))
    usb.util.release_interface(dev, 0)

    n = len(recs)
    if n < 2:
        sys.exit(f"only {n} reports ({timeouts} timeouts, {bad} bad)")
    dts = sorted((recs[i + 1][0] - recs[i][0]) / 1e6 for i in range(n - 1))
    span = (recs[-1][0] - recs[0][0]) / 1e9
    changes = sum(1 for i in range(1, n) if recs[i][1] != recs[i - 1][1])
    print(f"reports        {n} in {span:.2f} s  ->  {(n - 1) / span:.1f} Hz")
    print(f"interval ms    mean {statistics.mean(dts):.3f}  median {pct(dts, 50):.3f}  "
          f"stdev {statistics.pstdev(dts):.3f}")
    print(f"               min {dts[0]:.3f}  p1 {pct(dts, 1):.3f}  p99 {pct(dts, 99):.3f}  "
          f"max {dts[-1]:.3f}")
    buckets = {}
    for d in dts:
        b = round(d)
        buckets[b] = buckets.get(b, 0) + 1
    print("histogram      " + "  ".join(f"{k}ms:{v}" for k, v in sorted(buckets.items())))
    print(f"read timeouts  {timeouts}   bad reports {bad}   reports that changed {changes}")
    print("ports          " + "  ".join(
        f"{i + 1}:type{p['type']}" for i, p in enumerate(decode(recs[-1][1]))))
    if a.save:
        with open(a.save, "wb") as f:
            f.write(MAGIC)
            for t, rep in recs:
                f.write(struct.pack(">Q", t) + rep)
        print(f"saved          {a.save}")


def cmd_rumble(a):
    dev = open_adapter()
    init(dev)
    ports = [0, 0, 0, 0]
    for p in a.ports:
        ports[p - 1] = 1
    print(f"rumble on {ports} for {a.seconds}s")
    dev.write(EP_OUT, bytes([0x11] + ports), timeout=1000)
    end = time.monotonic() + a.seconds
    while time.monotonic() < end:  # keep reading so the adapter stays happy
        try:
            dev.read(EP_IN, REPORT, timeout=100)
        except usb.core.USBTimeoutError:
            pass
    dev.write(EP_OUT, bytes([0x11, 0, 0, 0, 0]), timeout=1000)
    # Report whether the adapter sees external power (bit 2 of the port type
    # byte is set when the grey cable powers rumble).
    rep = bytes(dev.read(EP_IN, REPORT, timeout=200))
    print("power bits     " + " ".join(f"{(rep[1 + 9 * i] >> 2) & 1}" for i in range(4)))
    usb.util.release_interface(dev, 0)


def cmd_show(a):
    dev = open_adapter()
    init(dev)
    last = None
    end = time.monotonic() + a.seconds
    while time.monotonic() < end:
        try:
            rep = bytes(dev.read(EP_IN, REPORT, timeout=100))
        except usb.core.USBTimeoutError:
            continue
        if rep != last:
            print(" | ".join(f"t{p['type']:x} {p['b1']:02x}{p['b2']:02x} "
                             f"{p['sx']:3} {p['sy']:3} {p['l']:3} {p['r']:3}"
                             for p in decode(rep)))
            last = rep
    usb.util.release_interface(dev, 0)


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    m = sp.add_parser("measure")
    m.add_argument("-s", "--seconds", type=float, default=10)
    m.add_argument("--save")
    r = sp.add_parser("rumble")
    r.add_argument("ports", type=int, nargs="*", default=[1])
    r.add_argument("-s", "--seconds", type=float, default=1)
    s = sp.add_parser("show")
    s.add_argument("-s", "--seconds", type=float, default=10)
    a = ap.parse_args()
    {"measure": cmd_measure, "rumble": cmd_rumble, "show": cmd_show}[a.cmd](a)


if __name__ == "__main__":
    main()
