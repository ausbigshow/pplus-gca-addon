#!/usr/bin/env python3
"""Build the Project+ USB GameCube adapter add-on.

Takes the stock Project+ 3.2.1 Wii BOOST.GCT, appends one Gecko C0 code whose
body is the compiled driver (linked for the exact address it lands at once
BOOST.GCT is loaded to 0x80550010), and packages it as a launcher add-on.

Usage: python3 build.py <stock BOOST.GCT> [-o out_dir]
"""
import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "src"
# Any 32-bit PowerPC GCC works (we pass -mcpu=750). Override with GCA_CROSS,
# e.g. GCA_CROSS=/path/to/bin/powerpc-buildroot-linux-gnu- or devkitPPC's
# powerpc-eabi-.
CROSS = os.environ.get("GCA_CROSS")
if not CROSS:
    CROSS = next((c for c in ("powerpc-linux-gnu-", "powerpc-eabi-", "powerpc-linux-")
                  if shutil.which(c + "gcc")), "powerpc-linux-gnu-")

BOOST_LOAD = 0x80550010          # MultiGCT.asm: BOOST.GCT is read to here
CODELIST_START = 0x80566528      # gc.txt: main codelist begins here
GCT_END = bytes.fromhex("F000000000000000")
VERSION = "1.2"                  # add-on version shown in the launcher
STOCK_BOOST_MD5 = "a8b8be65aa8c63625235476df542d2e3"  # Project+ 3.2.1 Wii

CFLAGS = [
    "-mcpu=750", "-m32", "-mbig-endian", "-O2", "-ffreestanding", "-fno-builtin",
    "-fno-tree-loop-distribute-patterns", "-nostdlib", "-fno-pic", "-fno-PIC",
    "-mno-relocatable", "-msdata=none", "-G0", "-fno-asynchronous-unwind-tables",
    "-fno-exceptions", "-fno-stack-protector", "-mno-altivec", "-msoft-float",
    "-Wall", "-Wextra", "-Wno-unused-parameter",
]

INFO_XML = """<?xml version="1.0" encoding="UTF-8"?>
<addon>
  <title>USB GameCube Adapter (Wii U)</title>
  <code>WUPGCA</code>
  <version>{version}</version>
  <files>
    <file
      source="BOOST.GCT"
      destination="sd:/Project+/BOOST.GCT"
      md5="{md5}" />
  </files>
</addon>
"""


def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode:
        sys.exit(f"command failed: {' '.join(map(str, cmd))}\n{r.stdout}{r.stderr}")
    return r.stdout


def build_blob(base: int, build: Path, pic: bool = False) -> tuple[bytes, dict]:
    build.mkdir(parents=True, exist_ok=True)
    cflags = CFLAGS + ["-g"]  # -g only feeds objdump -S (the .asm's C comments)
    if pic:  # relocates itself on first run (see entry.S)
        cflags = [f for f in cflags if f not in ("-fno-pic", "-fno-PIC", "-mno-relocatable")]
        cflags += ["-fPIC", "-mrelocatable", "-DGCA_PIC"]
    objs = []
    for src in ("entry.S", "gca.c"):
        obj = build / (src.split(".")[0] + ".o")
        run([CROSS + "gcc", *cflags, "-c", str(SRC / src), "-o", str(obj)])
        objs.append(str(obj))
    elf = build / "gca.elf"
    run([CROSS + "ld", "-m", "elf32ppc", "-T", str(SRC / "link.ld"),
         f"--defsym=BASE={base:#x}", "--no-undefined", "-e", "_entry",
         "-Map", str(build / "gca.map"), *objs, "-o", str(elf)])
    # Nothing may be left undefined or relocated at runtime.
    if run([CROSS + "nm", "-u", str(elf)]).strip():
        sys.exit("undefined symbols in blob")
    syms = {}
    for line in run([CROSS + "nm", str(elf)]).splitlines():
        a, t, n = line.split()
        syms[n] = int(a, 16)
    binf = build / "gca.bin"
    run([CROSS + "objcopy", "-O", "binary", "-j", ".text", "-j", ".data", str(elf), str(binf)])
    blob = binf.read_bytes()
    blob += b"\0" * (-len(blob) % 8)
    (build / "gca.dis").write_text(run([CROSS + "objdump", "-d", "-S", str(elf)]))
    return blob, syms


# Mnemonics GCTRealMate v0.2.6 can't encode (some silently become garbage), so
# write them as `word`. tests/check_asm.py proves each .asm assembles to its blob.
GCTRM_UNSUPPORTED = {"clrlwi", "clrrwi", "mftb", "mtmsr", "mfmsr", "bcl", "cmpdi"}

ASM_HEADER = """\
#####################################################################
USB GameCube Adapter (Wii U) v{version}{title} [Chadderz, wilm0x42, ausbigshow]
#####################################################################
# Lets the official Wii U GameCube Controller Adapter (WUP-028), or a clone
# switched to "Wii U" mode, work in game in Super Smash Bros. Brawl NTSC-U
# (RSBE01) and mods built on it, on a Wii U's vWii or a Wii. All four ports,
# analog sticks and triggers, rumble. Normal GameCube/Wii controllers keep
# working whenever the adapter isn't sending input.
#
# {placement}
# Load only one copy of this driver (one of the two .asm builds, or another
# codeset that already contains it).
#
# Credits:
#   Alex "Chadderz" Chadwick - WUP-028 BrainSlug module, the USB/IOS driver
#     this code is adapted from (github.com/Chadderz121/wup-028-bslug)
#   wilm0x42 - wii-gc-adapter-inject notes, which helped find Brawl's pad code
#   ausbigshow - this port: freestanding C0 code, game hooks, calibration,
#     press latch, rumble, screen-dimming fix
#   Brawl addresses checked against the doldecomp/brawl decompilation.
# Source: https://github.com/ausbigshow/pplus-gca-addon (gca.c, entry.S).
# This file is generated from that C and assembly: the "#|" lines are the C
# source of the instructions after them (the compiler reorders code, so a C
# line can appear more than once or out of order). Change the C and rebuild
# rather than editing instructions here.
#
# ---- How it works --------------------------------------------------
# This is one C0 (PULSE) code. The Gecko codehandler runs it every frame.{reloc}
# - On its first run gca_install finds the `bl PADRead` in Brawl's
#   gfPadSystem::updateLowGC (0x80029578) and changes it to call
#   gca_PADRead instead. It does the same to every `bl PADControlMotor` in
#   the game (rumble). It checks the code around each call first and patches
#   nothing it doesn't recognise. After that the C0 code does nothing.
# - The first time the game reads the pads, gca_PADRead opens IOS's
#   /dev/usb/hid. From then on everything runs in IOS callbacks (interrupt
#   context): find the HID interface version (v4, or v5 on the vWii), wait
#   for the adapter (USB 057e:0337), send it 0x13 to start reports, then keep
#   one 37-byte interrupt read ("poll") outstanding forever. Each reply is
#   decoded into four PADStatus records (onDevUsbPoll) and the next poll is
#   sent at once, ~125 times a second.
# - gca_PADRead gives the game those records while reports are fresh (less
#   than 1.5 s old) and otherwise calls the real PADRead.
# - Each port is calibrated when a controller appears: its first stick and
#   trigger readings count as centre/released, like a real GameCube. Hold
#   X+Y+Start for 3 s to recalibrate.
# - Presses shorter than one game frame are latched so the game sees them.
# - Rumble: gca_PADControlMotor records what the game wants per port (and
#   still calls the real PADControlMotor); the poll loop sends 0x11 + four
#   motor bytes to the adapter whenever that changes.
# - Adapter input resets the SDK's screen-dimming timer (__VIResetSIIdle).
# - Nothing ever waits: on any failure the driver stops using the adapter,
#   notes why in gca_diag, and the game keeps reading the pads normally.
#
# ---- Brawl NTSC-U addresses used -----------------------------------
{addrs}
#
# ---- Status block for memory viewers -------------------------------
# gca_diag is at {diag}: twelve words.
#   [0] 'GCA1'  [1] stage: 1 opening, 2 opened, 4 HID v4, 5 HID v5,
#       6 adapter found, 7 polling, 0x10 installed, 0xEE PADRead call not found
#   [2] error site, [3] error code (from the last failure)
#   [4] adapter reports received   [5] game pad reads
#   [6] rumble messages sent       [7] reads where a latched press was added
#   [8] PADControlMotor calls patched (0xEE = not patched: no rumble)
#   [9] dimming fix: 1 on, 0xEE off   [10] PADControlMotor calls by the game
#   [11] rumble message errors
# Size: {size:#x} bytes.
#
# ---- Licences ------------------------------------------------------
# Copyright (c) 2017 Alex Chadwick; Copyright (c) 2026 ausbigshow.
# Permission is hereby granted, free of charge, to any person obtaining a
# copy of this software and associated documentation files (the "Software"),
# to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense,
# and/or sell copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following conditions:
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.
"""

FIXED = dict(title="", placement="""Fixed-address build: the code is linked to run at one address, so the
# C0 line must land at exactly {c0:#x}. That is where GCTRM puts it as the
# last line of the stock Project+ 3.2.1 BOOST.txt, with nothing else changed
# (it then builds the same file as the Project+ web patcher). Anywhere else,
# use the movable build (USBGameCubeAdapter-Movable.asm) instead.""",
             reloc="", diag="{diag:#x}")
MOVABLE = dict(title=" movable", placement="""Movable build: works wherever GCTRM places it, in any codeset, before
# or after other codes. Include it like any other code, e.g.
#     .include Source/Extras/USBGameCubeAdapter-Movable.asm""", reloc="""
# - Its first lines (entry.S) make it movable: the code finds its own run
#   address and adds the distance it moved to every absolute address it
#   uses (the address table .got2 and the words listed in .fixup). IOS
#   buffers are aligned to 32 bytes at run time for the same reason.""",
               diag="code body + {diag:#x}\n# (the C0 line + 8 + {diag:#x})")


def func_docs(src: str) -> dict:
    """The comment block above each C function definition, by name. objdump -S
    loses most of these because -O2 reorders functions."""
    lines, docs = src.splitlines(), {}
    for i, line in enumerate(lines):
        m = re.match(r"[A-Za-z][\w\s*]*?\b(\w+)\(", line)
        if not m or line.startswith("typedef") or not line.rstrip().endswith("{"):
            continue
        j = i
        while j > 0 and lines[j - 1].startswith("#define"):  # e.g. MAX_MOTOR_SITES
            j -= 1
        k = j
        while k > 0 and lines[k - 1].rstrip().endswith("*/"):
            k -= 1
            while "/*" not in lines[k]:
                k -= 1
        docs[m[1]] = lines[k:j]
    return docs


def write_asm(blob: bytes, syms: dict, dis: str, path: Path, version: str, kind: dict):
    """Write the blob as a GCTRM PULSE code with the C source as comments."""
    insn = re.compile(r"^\s*([0-9a-f]+):\t((?:[0-9a-f]{2} ){4})\s*\t?(\S*)\s*(.*)$")
    names = {}
    for n, a in sorted(syms.items(), key=lambda kv: kv[1]):
        if not n.startswith("__") and n != "BASE":
            names.setdefault(a, n)
    label = lambda a: "GCA_" + re.sub(r"\W", "_", names.get(a, f"L{a:04x}"))
    base = syms["_entry"]
    code_end = syms["__rodata_start"]
    targets = set()
    for line in dis.splitlines():
        m = insn.match(line)
        if m and m[3].startswith("b") and (t := re.search(r"([0-9a-f]+) <[^>]*>$", m[4])):
            targets.add(int(t[1], 16))

    fill = {k: v.format(c0=base - 8, diag=syms["gca_diag"]) for k, v in kind.items()}
    addrs = "\n".join(f"#   {a}  {n}" for n, a in re.findall(
        r"#define (?:ADDR|TEXT)_(\w+)\s+(0x[0-9A-F]+)", (SRC / "gca.c").read_text()))
    out = [ASM_HEADER.format(version=version, size=len(blob), addrs=addrs, **fill),
           "PULSE", "{"]
    docs = func_docs((SRC / "gca.c").read_text())
    func, note = None, ""
    for line in dis.splitlines():
        m = insn.match(line)
        if not m:
            if h := re.match(r"^[0-9a-f]+ <(.*)>:", line):
                func = h[1]
            if not line.strip() or h or "file format" in line or line.startswith("Disassembly"):
                continue
            # entry.S is already assembly: keep only its comments, not each line
            # twice. A comment after an instruction goes on that instruction.
            if func == "_entry":
                if "#" not in line or re.match(r"#\s*(if|else|endif)", line.lstrip()):
                    continue
                if not line.lstrip().startswith("#"):
                    note = line[line.index("#"):].rstrip()
                    continue
                line = line[line.index("#"):]
            out.append("  #| " + line.rstrip())
            continue
        a = int(m[1], 16)
        if a >= code_end:
            break
        word = int(m[2].replace(" ", ""), 16)
        if a in names:
            out += ["", f"# ---- {names[a]} " + "-" * max(0, 60 - len(names[a]))]
            out += ["  #| " + d for d in docs.get(names[a], [])]
        if a in names or a in targets:
            out.append(f"{label(a)}:")
        ops = m[4]
        t = re.search(r"([0-9a-f]+) <[^>]*>$", ops)
        if m[3] in GCTRM_UNSUPPORTED or not m[3] or "*" in ops or (t and not m[3].startswith("b")):
            text = f"word 0x{word:08X}"
            ops = f"# {m[3]} {ops}".rstrip()
        else:
            mn, op, bo = m[3], word >> 26, (word >> 21) & 31
            if op in (16, 19) and mn.startswith("b") and bo & 0x14 != 0x14:
                # GCTRM's hint suffix means "predict taken" (no suffix = "-"),
                # so turn the hint bit back into what GCTRM will encode.
                taken = bool(word & 0x00200000) != (op == 16 and bool(word & 0x8000))
                mn = mn.rstrip("+-") + ("+" if taken else "")
            if t:
                ops = ops[:t.start()] + label(int(t[1], 16))
            text, ops = f"{mn} {ops}".rstrip(), ""
        ops, note = f"{ops} {note}".strip(), ""
        out.append(f"  {text:34} {ops}".rstrip())

    data = {a: n for a, n in names.items() if a >= code_end}
    out += ["", "# ---- read-only data, variables and buffers (zero = bss) ----------"]
    for a in range(code_end, base + len(blob), 4):
        if a in data:
            out.append(f"{label(a)}:")
        w = blob[a - base:a - base + 4]
        note = ""
        v = struct.unpack(">I", w)[0]
        if syms["__got2_start"] <= a < syms["__got2_end"]:
            note = f"# address of {names.get(v, '+0x%x' % v)}"
        elif syms["__fixup_start"] <= a < syms["__fixup_end"]:
            note = "# fixup: this word holds an address"
        elif base and v in names:
            note = f"# address of {names[v]}"
        elif a < syms["__data_start"] and any(w) and all(32 <= c < 127 or c == 0 for c in w):
            note = "# '" + w.decode("ascii").replace("\0", "\\0") + "'"
        text = f"word 0x{w.hex().upper()}"
        out.append(f"  {text:34} {note}".rstrip())
    out.append("}")
    path.write_text("\n".join(out) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("boost", help="stock Project+ 3.2.1 BOOST.GCT")
    ap.add_argument("-o", "--out", default=str(HERE / "dist"))
    ap.add_argument("--version", default=VERSION)
    ap.add_argument("--release", action="store_true",
                    help="add this build to patcher/versions/ and the published patcher (docs/)")
    args = ap.parse_args()

    stock = Path(args.boost).read_bytes()
    # The blob address and the published patcher are pinned to this exact file.
    if hashlib.md5(stock).hexdigest() != STOCK_BOOST_MD5:
        sys.exit(f"input is not the stock Project+ 3.2.1 Wii BOOST.GCT (md5 {STOCK_BOOST_MD5})")

    insert_at = len(stock) - 8
    c0_addr = BOOST_LOAD + insert_at
    blob_addr = c0_addr + 8
    blob, syms = build_blob(blob_addr, HERE / "build")
    assert syms["_entry"] == blob_addr, "entry is not at the start of the C0 body"
    # Same driver, position-independent, for anywhere in any codeset.
    pic, pic_syms = build_blob(0, HERE / "build" / "pic", pic=True)

    lines = len(blob) // 8
    code = struct.pack(">II", 0xC0000000, lines) + blob
    out_gct = stock[:insert_at] + code + GCT_END

    end = BOOST_LOAD + len(out_gct)
    if end > CODELIST_START:
        sys.exit(f"BOOST.GCT would overrun the codelist: ends {end:#x}")

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "BOOST.GCT").write_bytes(out_gct)
    write_asm(blob, syms, (HERE / "build" / "gca.dis").read_text(),
              outdir / "USBGameCubeAdapter.asm", args.version, FIXED)
    write_asm(pic, pic_syms, (HERE / "build" / "pic" / "gca.dis").read_text(),
              outdir / "USBGameCubeAdapter-Movable.asm", args.version, MOVABLE)
    md5 = hashlib.md5(out_gct).hexdigest()

    # Launcher add-on: non-solid 7z, one LZMA1 stream per file (see README).
    addon = outdir / "USBGameCubeAdapter.7z"
    addon.unlink(missing_ok=True)
    stage = HERE / "build" / "stage"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    info_xml_content = INFO_XML.format(version=args.version, md5=md5)
    (stage / "info.xml").write_text(info_xml_content)
    (stage / "BOOST.GCT").write_bytes(out_gct)
    run(["7z", "a", "-t7z", "-m0=LZMA", "-ms=off", str(addon), "info.xml", "BOOST.GCT"],
        cwd=stage)

    # Browser patcher: one entry per add-on version (only our appended bytes).
    # patcher/versions/ holds the released, console-tested versions; the dist/
    # page also offers this build, the docs/ (published) page only released ones.
    entry = {
        "version": args.version,
        "tailB64": base64.b64encode(out_gct[insert_at:]).decode("ascii"),
        "outSha256": hashlib.sha256(out_gct).hexdigest(),
        "outMd5": md5,
        "infoXml": info_xml_content,
    }
    versions_dir = HERE / "patcher" / "versions"
    if args.release:
        (versions_dir / f"{args.version}.json").write_text(json.dumps(entry, indent=2) + "\n")
    released = {e["version"]: e for e in
                (json.loads(p.read_text()) for p in versions_dir.glob("*.json"))}

    def patcher_page(versions):
        cfg = {
            "stockSize": len(stock),
            "stockSha256": hashlib.sha256(stock).hexdigest(),
            # Newest first; the page selects the first one by default.
            "versions": sorted(versions.values(), reverse=True,
                               key=lambda e: tuple(map(int, e["version"].split(".")))),
        }
        template = (HERE / "patcher" / "template.html").read_text()
        html = (template
                .replace("/*@@LZMA@@*/", (HERE / "patcher" / "vendor" / "lzma-c-min.js").read_text())
                .replace("/*@@PATCHER@@*/", (HERE / "patcher" / "patcher.js").read_text())
                .replace("/*@@CONFIG@@*/", "const CFG = " + json.dumps(cfg, indent=2) + ";"))
        return html, cfg

    patcher_html, cfg = patcher_page({**released, args.version: entry})
    patcher_path = outdir / "gca-patcher.html"
    patcher_path.write_text(patcher_html)
    (outdir / "patcher-config.json").write_text(json.dumps(cfg, indent=2) + "\n")
    # GitHub Pages serves docs/ from main: https://ausbigshow.github.io/pplus-gca-addon/
    (HERE / "docs").mkdir(exist_ok=True)
    (HERE / "docs" / "index.html").write_text(patcher_page(released)[0])

    report = {
        "stock_size": len(stock),
        "stock_md5": hashlib.md5(stock).hexdigest(),
        "c0_addr": hex(c0_addr),
        "blob_addr": hex(blob_addr),
        "blob_size": len(blob),
        "gca_PADRead": hex(syms["gca_PADRead"]),
        "gca_PADControlMotor": hex(syms["gca_PADControlMotor"]),
        "gca_install": hex(syms["gca_install"]),
        "gca_diag": hex(syms["gca_diag"]),
        "new_size": len(out_gct),
        "boost_end": hex(end),
        "headroom": CODELIST_START - end,
        "md5": md5,
        "patcher": patcher_path.name,
    }
    for k, v in report.items():
        print(f"{k:20} {v}")
    (outdir / "build-report.txt").write_text(
        "\n".join(f"{k}: {v}" for k, v in report.items()) + "\n")

    # GCTRM encodes some instructions wrongly without an error, so the .asm is
    # only trustworthy once it has been assembled and compared.
    if os.environ.get("GCTRM"):
        print(run([sys.executable, str(HERE / "tests" / "check_asm.py"),
                   os.environ["GCTRM"], args.boost]).rstrip())
    else:
        print("WARNING: the .asm files are unverified; set GCTRM=<GCTRealMate binary>"
              " (runs tests/check_asm.py)")


if __name__ == "__main__":
    main()
