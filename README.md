# USB GameCube Adapter add-on for Project+ 3.2.1 (Wii U / vWii)

Lets the official Wii U GameCube Controller Adapter (WUP-028, or a clone in
"Wii U" mode) work **in game** in Project+ 3.2.1 on a Wii U's vWii.

**Status (v1.2):** v1.0 is confirmed working on a vWii (disc boot, Loader
IOS 251 = d2x base 58, so the `/dev/usb/hid` v5 path) with a Mayflash adapter
in Wii U mode, all four ports. v1.1 adds press latching, rumble and the
screen-dimming fix, all confirmed on the same console, and is installed with
the web patcher. v1.2 keeps the adapter working through one-off USB read
errors (it used to stop until the adapter was replugged), also confirmed on
console. See "How it was verified".

## Install

1. Open the patcher at **https://ausbigshow.github.io/pplus-gca-addon/**, choose a
   version (the newest is selected), and pick your
   stock `BOOST.GCT` (`sd:/Project+/BOOST.GCT` before any add-on replaced it,
   or `Project+/BOOST.GCT` from the Project+ 3.2.1 Wii download). It checks
   the file, adds the driver and saves `USBGameCubeAdapter-<version>.7z`.
   Everything happens in the browser; the file isn't uploaded anywhere.
   (Confirmed: the launcher accepts the patcher's `.7z` on a real console.)
2. Copy that `.7z` to `sd:/Project+/launcher/addons/`.
3. Start the Project+ launcher, open **Addons**, and turn on
   **USB GameCube Adapter (Wii U)**. The launcher backs up your original
   `BOOST.GCT` and swaps in the patched one. Turning the add-on off restores it.
4. Plug the adapter's **black** cable into the Wii U. Plug the **grey** cable
   in too if you want rumble (it only powers the motors).
5. Launch the game as usual (disc).

Requires the stock Project+ **3.2.1** Wii `BOOST.GCT`
(md5 `a8b8be65aa8c63625235476df542d2e3`). If another add-on also replaces
`BOOST.GCT`, they can't both be on.

## Building it into Project+ (GCTRM source)

`build.py` also writes `dist/USBGameCubeAdapter.asm`, the same driver as one
commented GCTRealMate `PULSE` code, to put straight into the Project+
codeset:

1. Copy it to `Source/Extras/USBGameCubeAdapter.asm`.
2. Add `.include Source/Extras/USBGameCubeAdapter.asm` on its own line in
   `BOOST.txt`. The stock file has no newline at the end, so check the line
   above stays separate.
3. Compile `BOOST.txt` as usual. Checked with GCTRealMate v0.2.6: stock
   `BOOST.txt` plus this line builds, with 18144 bytes left before the
   codelist at `0x80566528`.

This build is position-independent: on its first run it adds its own address
to the addresses in its address table (entry.S), so it works wherever GCTRM
places it. The C source lines are comments next to the instructions. Mnemonics
GCTRM can't encode are written as `word` with the instruction in a comment.
Don't also turn on the launcher add-on.

## Using it

- Controllers on the adapter show up as GameCube controllers in ports 1-4.
- With no adapter plugged in (or if it stops responding for 1.5 s), the game
  behaves exactly like stock, so Wii Remotes and Classic Controllers work as
  normal.
- Like a real GameCube, stick and trigger resting positions are calibrated
  when a controller connects, so don't touch the sticks or triggers while
  plugging in. Hold **X + Y + Start for 3 seconds** to recalibrate.
- Adapter and controller hot-plugging work.
- Quick taps aren't lost: the adapter reports about twice per game frame, and
  a button pressed and released between two game frames is still seen for one
  frame. Holding and releasing a button behaves exactly as before.
- Rumble works when the game turns it on (grey cable needed).
- Adapter input keeps the screen from dimming, like a real GameCube controller.

## If something goes wrong

- **Crash screen:** turn the add-on off in the launcher to get back to normal,
  and send a photo of the crash screen.
- **No input from the adapter:** try a different **Loader IOS** in the launcher
  settings (249, 250 and 251 use different USB interfaces). Note which one you
  tried and what happened.
- Keep a Wii Remote connected while testing so you can always navigate.

For anyone with a memory viewer: the driver writes a status block of twelve
32-bit words at the `gca_diag` address in `build-report.txt`:

| Word | Meaning |
|---|---|
| 0 | magic `GCA1` |
| 1 | stage: `0x10` installed, `7` polling the adapter, `0xEE` `PADRead` call not found (nothing patched) |
| 2, 3 | last error site and code (`11` = adapter reads failed 3 times in a row, polling stopped; `13` = a rumble message failed) |
| 4 | adapter reports received. Read it twice a known time apart for the console's poll rate (the PC sees 125/s) |
| 5 | game reads (`PADRead` calls), about 60/s |
| 6 | rumble messages sent |
| 7 | game reads where the latch added a press that would otherwise have been missed |
| 8 | number of `PADControlMotor` calls redirected; `0xEE` = its code didn't match, rumble off |
| 9 | `1` = screen-dimming reset in use; `0xEE` = its code didn't match, off |
| 10 | rumble requests from the game |
| 11 | rumble message errors |

## How it works

- The add-on appends one Gecko `C0` code to `BOOST.GCT`, which Project+ loads
  to `0x80550010` at boot. The code's body is the driver itself, compiled for
  exactly that address.
- Each frame the `C0` entry calls `gca_install`, which on its first run finds
  the single `bl PADRead` inside `gfPadSystem::updateLowGC` (`0x80029578`) and
  redirects it to the driver. If that call isn't where it's expected, it
  patches nothing.
- **Latch:** each adapter report ORs newly pressed buttons (released → pressed
  since the previous report) into a per-port latch; the next game read adds
  them and clears it. Only new presses are latched, so a button held across a
  read and released before the next is reported released on time. Unplugged
  ports, and fallback to the stock `PADRead`, clear the latch.
- **Rumble:** `PADControlMotor` isn't named in the symbol map. `0x802162A4`
  (size `0xB8`) is the function right after `PADRead` and matches the Wii SDK's
  pad.c layout exactly (`PADRead 0x2E0`, `PADControlMotor 0xB8`, `SPEC0/1
  0x118`, `SPEC2 0x580`, ...; checked against the Mario Kart Wii and Super
  Paper Mario symbol maps). The decomp has no code for `gfPadSystem`'s motor
  functions, so at runtime the driver checks the function's SDK signature
  (the same 4 words at `+0x64` that Chadderz's module matches) and that it ends
  in `blr`, then redirects every `bl` to it in Brawl's `.text`. If the
  signature doesn't match, nothing is patched. The redirect still calls the
  real function, records the wanted state per port, and the poll loop sends
  `0x11` + one byte per port (v4 `INTERRUPT_OUT` / v5 `INTERRUPT` ioctlv)
  alongside a poll, with at most one rumble message in flight. A lost reply
  is retried after 64 polls.
- **Screen dimming:** the SDK's `PADRead` calls `__VIResetSIIdle` when input
  changes; with only adapter input, nothing does. `0x801EA088` is
  `__VIResetSIIdle` by the same size/order match against vi.c
  (`VIResetDimmingCount 0x14`, `__VIResetRFIdle 0x10`, `__VIResetSIIdle 0x10`,
  `WaitMicroTime 0x98`). The driver calls it on input change (same thresholds
  as the SDK), only after checking it is a branch-free 4-instruction leaf with
  the same shape as its twin `__VIResetRFIdle`.
- **Hot-plug:** when a controller is plugged into the adapter, the first
  report for that port has every analog byte 0. The driver waits for the next
  report before calibrating.
- The driver talks to the adapter through IOS `/dev/usb/hid`, using either the
  v4 interface (IOS 37-57 based) or the v5 interface (IOS 58 based), with the
  same asynchronous flow as Chadderz's BrainSlug module. The v5 interface
  needs a few buffers in MEM2; these are claimed at runtime from the top of
  the SDK's IPC buffer region, and the v5 path is skipped if there isn't room.
- Brawl addresses were checked against the doldecomp/brawl symbol map
  (`RSBE01_02`), the same build Project+ targets. Project+'s own file-loading
  calls land on the same named functions there.

## How it was verified

`tests/emulate.py` runs the real compiled code with Unicorn (PowerPC 32-bit,
big-endian) against a mock Brawl and checks that:

- the `C0` entry preserves every register the codehandler relies on
- only the `bl PADRead` and the `bl PADControlMotor`s are patched (near-miss
  branches aren't), and re-running changes nothing
- the real `PADRead` is used until adapter data arrives
- input is decoded and origin-calibrated correctly on both USB interface versions
- taps between game reads are latched for exactly one read; held buttons
  release on time; unplugged or stale ports don't replay old presses
- rumble requests reach the mock adapter as `11 xx xx xx xx` on v4 and v5, one
  message in flight, latest state wins, lost replies retried
- a wrong `PADControlMotor` signature or `__VIResetSIIdle` shape patches or
  calls nothing; with no adapter everything goes to the stock functions
- unplugging the adapter falls back to the real `PADRead`
- **real adapter data:** reports captured from a Mayflash adapter on a PC
  (`tests/data/*.gcap`) are replayed at their recorded timing against a
  59.94 Hz game, at full rate and at half rate. Every press reaches the game,
  no button is ever reported that wasn't held or newly pressed, and sticks rest
  at 0 after calibration. Without the latch, the same data loses a 16 ms tap.

PC measurements (Mayflash, Wii U mode, `tools/adapter_probe.py`): 125.0
reports/s, 8.000 ms median interval, 0.014 ms standard deviation, no timeouts,
over 30 s. Sending `11 01 01 01 01` from the PC rumbled the controller
(grey cable in). The fastest real taps were 16 ms (two reports), shorter than one
60 Hz frame (16.7 ms). The console's own rate is in `gca_diag` word 4.

It cannot check real IOS behaviour, timing on the console, or Brawl's handling
of the data. That needs a console. The rumble and dimming addresses are checked
by code signature at runtime, because no Brawl binary was available to trace
the callers.

## Building

```
sudo apt install gcc-powerpc-linux-gnu p7zip-full
pip install unicorn py7zr pyusb
python3 build.py path/to/stock/BOOST.GCT
python3 tests/emulate.py
node tests/test_patcher.mjs path/to/stock/BOOST.GCT /tmp/out.7z
python3 tests/check_asm.py path/to/GCTRealMate path/to/stock/BOOST.GCT
```

`check_asm.py` assembles `dist/USBGameCubeAdapter.asm` with GCTRealMate,
checks the bytes match the compiled PIC build, then runs the emulator tests
on it.

`build.py` writes `dist/USBGameCubeAdapter.7z` (contains the patched
`BOOST.GCT`, for your own testing only) and the patcher page (contains only
the driver). The page has a version picker:

- `dist/gca-patcher.html` offers every released version plus the build you
  just made, for testing.
- `docs/index.html` (published by GitHub Pages from `main`) offers only the
  released versions in `patcher/versions/`.

To release a version after testing it on a console, run
`python3 build.py path/to/stock/BOOST.GCT --release`, then commit
`patcher/versions/` and `docs/index.html`.

Any 32-bit PowerPC GCC works. `build.py` looks for `powerpc-linux-gnu-`,
`powerpc-eabi-` (devkitPPC) or `powerpc-linux-`; set `GCA_CROSS` to a full
prefix otherwise, e.g. a Bootlin `powerpc-e300c3` toolchain:
`GCA_CROSS=/path/to/bin/powerpc-buildroot-linux-gnu-`. The stock
`BOOST.GCT` isn't in this repository (it isn't ours to redistribute).

PC tools (adapter on the PC, nothing else claiming it):

```
python3 tools/adapter_probe.py measure -s 10 --save tests/data/new.gcap
python3 tools/adapter_probe.py show
python3 tools/adapter_probe.py rumble 1 -s 1
```

## AI disclosure

This project was built with heavy use of AI coding assistants, directed and
reviewed by the maintainer:

- The driver, build script, emulator tests, PC tools and this README were
  largely written by Anthropic's Claude (in Claude Code). Google's Gemini (via
  Antigravity) wrote parts of the web patcher, which Claude then reviewed and
  tested.
- AI-found facts (Brawl addresses, SDK function identities) were checked
  against the doldecomp/brawl symbol map and other decompilations. Where they
  couldn't be fully confirmed, the driver checks the real code at runtime and
  patches nothing if it doesn't match.
- Testing on real hardware (vWii, Mayflash adapter) and the decision to release
  were done by a human.

## Credits and licence

MIT licence, see `LICENSE`. This repository and the patcher page contain no
Project+ or Nintendo files: the patcher adds the driver to your own copy of
Project+'s `BOOST.GCT`. The patcher bundles LZMA-JS by Nathan Rugg (MIT,
`patcher/vendor/LICENSE`).

Driver adapted from the WUP-028 BrainSlug module by Alex "Chadderz" Chadwick
(MIT-style licence, reproduced in `src/gca.c`). Brawl addresses were checked
against doldecomp/brawl. The add-on format follows 2gifts' reverse-engineered
notes on the Project+ launcher, and wilm0x42's wii-gc-adapter-inject notes
helped locate the pad code.
