#!/usr/bin/env python3
"""Check dist/USBGameCubeAdapter.asm with the real GCTRealMate.

1. Assembles it the way the Project+ codeset does (.include from a codeset
   .txt) and checks the PULSE body is byte-for-byte the PIC build
   (build/pic/gca.bin), so GCTRM encoded every instruction as intended.
2. Appends that GCTRM-built C0 code to the stock BOOST.GCT at four
   addresses (every 8-byte offset from a 32-byte boundary) and runs
   tests/emulate.py on each, so self-relocation and buffer alignment are
   exercised wherever GCTRM could put it.

Usage: python3 tests/check_asm.py <GCTRealMate binary> <stock BOOST.GCT>
Run build.py first.
"""
import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import build  # noqa: E402

gctrm, stock = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).read_bytes()
asm = ROOT / "dist" / "USBGameCubeAdapter.asm"
pic = (ROOT / "build" / "pic" / "gca.bin").read_bytes()
pic += b"\0" * (-len(pic) % 8)

with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    (d / "USBGameCubeAdapter.asm").write_text(asm.read_text())
    (d / "T.txt").write_text("check\n.include USBGameCubeAdapter.asm\n")
    r = subprocess.run([str(gctrm), "-q", "T.txt"], cwd=d, capture_output=True, text=True)
    gct = (d / "T.GCT").read_bytes() if (d / "T.GCT").exists() else b""
    if r.returncode or not gct:
        sys.exit(f"GCTRealMate failed:\n{r.stdout}{r.stderr}")

    assert gct[:8] == bytes.fromhex("00D0C0DE00D0C0DE"), "not a GCT"
    op, lines = struct.unpack(">II", gct[8:16])
    assert op == 0xC0000000, f"first code is {op:08x}, not C0"
    body = gct[16:16 + lines * 8]
    assert gct[16 + lines * 8:] == build.GCT_END, "unexpected trailing codes"
    bad = [o for o in range(0, max(len(body), len(pic)), 4) if body[o:o + 4] != pic[o:o + 4]]
    for o in bad[:20]:
        print(f"  +{o:#06x}: gctrm {body[o:o + 4].hex()} want {pic[o:o + 4].hex()}")
    if bad or len(body) != len(pic):
        sys.exit(f"FAIL: {len(bad)} words differ (sizes {len(body)} vs {len(pic)})")
    print(f"ok: GCTRM output matches the PIC build ({len(body)} bytes)")

    # Where build.py puts it (just before the stock GCT's end code), then
    # shifted by 8, 16 and 24 bytes so the code sits at every 8-byte offset
    # GCTRM can give it relative to a 32-byte boundary.
    syms = {}
    for line in subprocess.run([build.CROSS + "nm", str(ROOT / "build" / "pic" / "gca.elf")],
                               capture_output=True, text=True, check=True).stdout.splitlines():
        a, _, n = line.split()
        syms[n] = int(a, 16)
    insert_at = len(stock) - 8
    failed = 0
    for pad in (0, 8, 16, 24):
        blob_addr = build.BOOST_LOAD + insert_at + pad + 8
        (d / "BOOST.GCT").write_bytes(stock[:insert_at] + bytes(pad) + gct[8:16 + lines * 8]
                                      + build.GCT_END)
        report = {"blob_addr": blob_addr, "blob_size": len(body)}
        for k in ("gca_PADRead", "gca_PADControlMotor", "gca_diag"):
            report[k] = blob_addr + syms[k]
        (d / "build-report.txt").write_text(
            "".join(f"{k}: {v if k == 'blob_size' else hex(v)}\n" for k, v in report.items()))
        r = subprocess.run([sys.executable, str(ROOT / "tests" / "emulate.py")],
                           env={**os.environ, "GCA_DIST": str(d)}, capture_output=True, text=True)
        print(f"code at {blob_addr:#x} (32-byte offset {blob_addr % 32:2}): "
              + ("ok" if r.returncode == 0 else "FAIL"))
        if r.returncode:
            print(r.stdout[-3000:], r.stderr[-3000:])
            failed += 1
    sys.exit(1 if failed else 0)
