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


def build_blob(base: int, build: Path) -> tuple[bytes, dict]:
    build.mkdir(parents=True, exist_ok=True)
    objs = []
    for src in ("entry.S", "gca.c"):
        obj = build / (src.split(".")[0] + ".o")
        run([CROSS + "gcc", *CFLAGS, "-c", str(SRC / src), "-o", str(obj)])
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
    (build / "gca.dis").write_text(run([CROSS + "objdump", "-d", str(elf)]))
    return blob, syms


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("boost", help="stock Project+ 3.2.1 BOOST.GCT")
    ap.add_argument("-o", "--out", default=str(HERE / "dist"))
    ap.add_argument("--version", default=VERSION)
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

    lines = len(blob) // 8
    code = struct.pack(">II", 0xC0000000, lines) + blob
    out_gct = stock[:insert_at] + code + GCT_END

    end = BOOST_LOAD + len(out_gct)
    if end > CODELIST_START:
        sys.exit(f"BOOST.GCT would overrun the codelist: ends {end:#x}")

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "BOOST.GCT").write_bytes(out_gct)
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

    # Browser-based patcher: inlines LZMA, patcher logic, and config into template.
    tail = out_gct[insert_at:]
    cfg = {
        "version": args.version,
        "stockSize": len(stock),
        "stockSha256": hashlib.sha256(stock).hexdigest(),
        "tailB64": base64.b64encode(tail).decode("ascii"),
        "outSha256": hashlib.sha256(out_gct).hexdigest(),
        "outMd5": md5,
        "infoXml": info_xml_content,
    }
    template = (HERE / "patcher" / "template.html").read_text()
    lzma_js = (HERE / "patcher" / "vendor" / "lzma-c-min.js").read_text()
    patcher_js = (HERE / "patcher" / "patcher.js").read_text()
    config_js = "const CFG = " + json.dumps(cfg, indent=2) + ";"

    patcher_html = (template
                    .replace("/*@@LZMA@@*/", lzma_js)
                    .replace("/*@@PATCHER@@*/", patcher_js)
                    .replace("/*@@CONFIG@@*/", config_js))
    patcher_path = outdir / "gca-patcher.html"
    patcher_path.write_text(patcher_html)
    # GitHub Pages serves docs/ from main: https://ausbigshow.github.io/pplus-gca-addon/
    (HERE / "docs").mkdir(exist_ok=True)
    (HERE / "docs" / "index.html").write_text(patcher_html)
    (outdir / "patcher-config.json").write_text(json.dumps(cfg, indent=2) + "\n")

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


if __name__ == "__main__":
    main()
