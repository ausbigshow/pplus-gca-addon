# USB GameCube Adapter add-on for Project+ 3.2.1 (Wii U / vWii)

Lets the official Wii U GameCube Controller Adapter (WUP-028, or a clone in
"Wii U" mode) work **in game** in Project+ 3.2.1 on a Wii U's vWii.

**Status: experimental, not yet tested on real hardware.** The compiled code
has been run end to end on an emulated PowerPC CPU against a mock Brawl and
mock USB stack (both USB interface versions); see "How it was verified".

## Install

1. Copy `USBGameCubeAdapter.7z` to `sd:/Project+/launcher/addons/`.
2. Start the Project+ launcher, open **Addons**, and turn on
   **USB GameCube Adapter (Wii U)**. The launcher backs up your original
   `BOOST.GCT` and swaps in the patched one. Turning the add-on off restores it.
3. Plug the adapter's **black** cable into the Wii U. The grey cable only
   powers rumble, which this version doesn't use.
4. Launch the game as usual (disc).

Requires the stock Project+ **3.2.1** Wii `BOOST.GCT`
(md5 `a8b8be65aa8c63625235476df542d2e3`). If another add-on also replaces
`BOOST.GCT`, they can't both be on.

## Using it

- Controllers on the adapter show up as GameCube controllers in ports 1-4.
- With no adapter plugged in (or if it stops responding for 1.5 s), the game
  behaves exactly like stock, so Wii Remotes and Classic Controllers work as
  normal.
- Like a real GameCube, stick and trigger resting positions are calibrated
  when a controller connects, so don't touch the sticks or triggers while
  plugging in. Hold **X + Y + Start for 3 seconds** to recalibrate.
- Adapter and controller hot-plugging work.
- **No rumble** in this version.

## If something goes wrong

- **Crash screen:** turn the add-on off in the launcher to get back to normal,
  and send a photo of the crash screen.
- **No input from the adapter:** try a different **Loader IOS** in the launcher
  settings (249, 250 and 251 use different USB interfaces). Note which one you
  tried and what happened.
- Keep a Wii Remote connected while testing so you can always navigate.

For anyone with a memory viewer: the driver writes a status block at the
`gca_diag` address in `build-report.txt`: magic `GCA1`, stage, error site,
error code. Stage `0x10` means installed; `7` means polling the adapter;
`0xEE` means the expected `PADRead` call wasn't found and nothing was patched.

## How it works

- The add-on appends one Gecko `C0` code to `BOOST.GCT`, which Project+ loads
  to `0x80550010` at boot. The code's body is the driver itself, compiled for
  exactly that address.
- Each frame the `C0` entry calls `gca_install`, which on its first run finds
  the single `bl PADRead` inside `gfPadSystem::updateLowGC` (`0x80029578`) and
  redirects it to the driver. If that call isn't where it's expected, it
  patches nothing.
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
- only the `bl PADRead` is patched, and re-running changes nothing
- the real `PADRead` is used until adapter data arrives
- input is decoded and origin-calibrated correctly on both USB interface versions
- unplugging the adapter falls back to the real `PADRead`

It cannot check real IOS behaviour, timing, or Brawl's handling of the data.
That needs a console.

## Building

```
sudo apt install gcc-powerpc-linux-gnu p7zip-full
pip install unicorn py7zr
python3 build.py path/to/stock/BOOST.GCT
python3 tests/emulate.py
```

## Credits and licence

Driver adapted from the WUP-028 BrainSlug module by Alex "Chadderz" Chadwick
(MIT-style licence, reproduced in `src/gca.c`). Brawl addresses were checked
against doldecomp/brawl. The add-on format follows 2gifts' reverse-engineered
notes on the Project+ launcher, and wilm0x42's wii-gc-adapter-inject notes
helped locate the pad code.
