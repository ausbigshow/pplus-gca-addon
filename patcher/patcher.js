/**
 * Browser- and Node-compatible pure JS patcher for Project+ USB GameCube Adapter.
 *
 * Implements CRC32, standard 7z archive generation with non-solid LZMA streams,
 * and stock BOOST.GCT verification and patching.
 */
(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) {
    module.exports = factory();
  }
  if (typeof root !== "undefined") {
    const exports = factory();
    for (const key of Object.keys(exports)) {
      root[key] = exports[key];
    }
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  // Precomputed CRC32 table (IEEE 802.3 polynomial 0xEDB88320)
  const CRC_TABLE = (function () {
    const table = new Uint32Array(256);
    for (let i = 0; i < 256; i++) {
      let c = i;
      for (let k = 0; k < 8; k++) {
        c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
      }
      table[i] = c >>> 0;
    }
    return table;
  })();

  function crc32(bytes) {
    const data = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
    let c = 0xFFFFFFFF;
    for (let i = 0; i < data.length; i++) {
      c = CRC_TABLE[(c ^ data[i]) & 0xFF] ^ (c >>> 8);
    }
    return (c ^ 0xFFFFFFFF) >>> 0;
  }

  /**
   * Encodes a 64-bit integer into 7z variable-length UINT64 format.
   *
   * 0xxxxxxx                        -> 1 byte
   * 10xxxxxx BYTE y[1]             -> 2 bytes
   * 110xxxxx BYTE y[2]             -> 3 bytes
   * ...
   * 11111111 BYTE y[8]             -> 9 bytes
   */
  function encodeUint64(val) {
    const v = BigInt(val);
    for (let i = 0; i < 8; i++) {
      const bits = 7 + 7 * i;
      if (v < (1n << BigInt(bits))) {
        const highMask = i === 0 ? 0 : ((0xFF << (8 - i)) & 0xFF);
        const highVal = Number(v >> BigInt(8 * i)) | highMask;
        const res = [highVal];
        for (let j = 0; j < i; j++) {
          res.push(Number((v >> BigInt(8 * j)) & 0xFFn));
        }
        return res;
      }
    }
    const res = [0xFF];
    for (let j = 0; j < 8; j++) {
      res.push(Number((v >> BigInt(8 * j)) & 0xFFn));
    }
    return res;
  }

  function getLzma() {
    if (typeof globalThis !== "undefined" && globalThis.LZMA) {
      return globalThis.LZMA;
    }
    if (typeof LZMA !== "undefined") {
      return LZMA;
    }
    throw new Error("LZMA encoder not found. Ensure lzma-c-min.js is loaded.");
  }

  /**
   * Writes a standard 7z archive from an array of files.
   *
   * @param {Array<{name: string, data: Uint8Array|ArrayBuffer|Buffer}>} files
   * @returns {Uint8Array}
   */
  function write7z(files) {
    const lzma = getLzma();
    lzma.disableEndMark = true;

    const packedFiles = files.map(function (f) {
      const uncompressedData = f.data instanceof Uint8Array ? f.data : new Uint8Array(f.data);
      const uncompressedCrc = crc32(uncompressedData);
      // Synchronous LZMA compression (2 arguments: data, mode 7)
      const comp = lzma.compress(uncompressedData, 7);
      const compBytes = new Uint8Array(comp.map(function (x) { return x & 0xFF; }));
      // 5 props bytes, 8-byte LE uncompressed size, then raw stream
      const props = compBytes.slice(0, 5);
      const rawStream = compBytes.slice(13);
      return {
        name: f.name,
        uncompressedSize: uncompressedData.length,
        uncompressedCrc: uncompressedCrc,
        props: props,
        packedData: rawStream,
      };
    });

    let totalPackedSize = 0;
    for (let i = 0; i < packedFiles.length; i++) {
      totalPackedSize += packedFiles[i].packedData.length;
    }

    const packedStreamsBuf = new Uint8Array(totalPackedSize);
    let packOffset = 0;
    for (let i = 0; i < packedFiles.length; i++) {
      packedStreamsBuf.set(packedFiles[i].packedData, packOffset);
      packOffset += packedFiles[i].packedData.length;
    }

    // Build plain Header
    const h = [];
    function pushByte(b) { h.push(b & 0xFF); }
    function pushBytes(arr) {
      for (let i = 0; i < arr.length; i++) {
        h.push(arr[i] & 0xFF);
      }
    }
    function pushU64(v) { pushBytes(encodeUint64(v)); }

    pushByte(0x01); // kHeader
    pushByte(0x04); // kMainStreamsInfo

    // PackInfo (0x06)
    pushByte(0x06);
    pushU64(0); // PackPos = 0
    pushU64(packedFiles.length); // NumPackStreams
    pushByte(0x09); // kSize
    for (let i = 0; i < packedFiles.length; i++) {
      pushU64(packedFiles[i].packedData.length);
    }
    pushByte(0x00); // kEnd (PackInfo)

    // UnPackInfo (0x07)
    pushByte(0x07);
    pushByte(0x0B); // kFolder
    pushU64(packedFiles.length); // NumFolders
    pushByte(0x00); // External = 0
    for (let i = 0; i < packedFiles.length; i++) {
      pushU64(1); // NumCoders = 1
      pushByte(0x23); // Flags: method ID size 3, simple coder, attributes defined
      pushBytes([0x03, 0x01, 0x01]); // LZMA method ID
      pushU64(5); // Properties size = 5
      pushBytes(packedFiles[i].props);
    }
    pushByte(0x0C); // kCodersUnPackSize
    for (let i = 0; i < packedFiles.length; i++) {
      pushU64(packedFiles[i].uncompressedSize);
    }
    pushByte(0x00); // kEnd (UnPackInfo)

    // SubStreamsInfo (0x08)
    pushByte(0x08);
    pushByte(0x0A); // kCRC
    pushByte(0x01); // All defined = 1
    for (let i = 0; i < packedFiles.length; i++) {
      const c = packedFiles[i].uncompressedCrc;
      pushBytes([c & 0xFF, (c >>> 8) & 0xFF, (c >>> 16) & 0xFF, (c >>> 24) & 0xFF]);
    }
    pushByte(0x00); // kEnd (SubStreamsInfo)

    pushByte(0x00); // kEnd (MainStreamsInfo)

    // FilesInfo (0x05)
    pushByte(0x05);
    pushU64(packedFiles.length); // NumFiles
    pushByte(0x11); // kName

    // UTF-16LE file names, NUL-terminated
    const namePayload = [0x00]; // External = 0
    for (let i = 0; i < packedFiles.length; i++) {
      const name = packedFiles[i].name;
      for (let j = 0; j < name.length; j++) {
        const code = name.charCodeAt(j);
        namePayload.push(code & 0xFF, (code >>> 8) & 0xFF);
      }
      namePayload.push(0x00, 0x00); // NUL terminator
    }
    pushU64(namePayload.length);
    pushBytes(namePayload);
    pushByte(0x00); // kEnd (FilesInfo)

    pushByte(0x00); // kEnd (Header)

    const headerBytes = new Uint8Array(h);
    const headerCrc = crc32(headerBytes);

    // Signature Header (32 bytes)
    const sig = new Uint8Array(32);
    const sigView = new DataView(sig.buffer);
    sig[0] = 0x37; sig[1] = 0x7A; sig[2] = 0xBC; sig[3] = 0xAF; sig[4] = 0x27; sig[5] = 0x1C;
    sig[6] = 0x00; sig[7] = 0x04;
    sigView.setBigUint64(12, BigInt(totalPackedSize), true);
    sigView.setBigUint64(20, BigInt(headerBytes.length), true);
    sigView.setUint32(28, headerCrc, true);
    const startHeaderCrc = crc32(sig.subarray(12, 32));
    sigView.setUint32(8, startHeaderCrc, true);

    const archive = new Uint8Array(32 + totalPackedSize + headerBytes.length);
    archive.set(sig, 0);
    archive.set(packedStreamsBuf, 32);
    archive.set(headerBytes, 32 + totalPackedSize);
    return archive;
  }

  function base64ToBytes(b64) {
    if (typeof atob === "function") {
      const bin = atob(b64);
      const bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) {
        bytes[i] = bin.charCodeAt(i);
      }
      return bytes;
    }
    if (typeof Buffer !== "undefined") {
      return new Uint8Array(Buffer.from(b64, "base64"));
    }
    throw new Error("No base64 decoding implementation available");
  }

  function bufToHex(buf) {
    const bytes = new Uint8Array(buf);
    let hex = "";
    for (let i = 0; i < bytes.length; i++) {
      hex += bytes[i].toString(16).padStart(2, "0");
    }
    return hex;
  }

  function containsGCA1(bytes) {
    for (let i = 0; i <= bytes.length - 4; i++) {
      if (bytes[i] === 0x47 && bytes[i + 1] === 0x43 && bytes[i + 2] === 0x41 && bytes[i + 3] === 0x31) {
        return true;
      }
    }
    return false;
  }

  /**
   * Verifies the stock BOOST.GCT, appends the adapter code, verifies the output,
   * and packs the launcher add-on .7z archive.
   *
   * @param {Uint8Array} stockBytes
   * @param {Object} cfg
   * @returns {Promise<{ok: boolean, error?: string, archive?: Uint8Array, gct?: Uint8Array}>}
   */
  async function buildAddon(stockBytes, cfg) {
    const bytes = stockBytes instanceof Uint8Array ? stockBytes : new Uint8Array(stockBytes);

    if (containsGCA1(bytes)) {
      return {
        ok: false,
        error: "This file already contains the adapter code (bytes 'GCA1'). Please restore the original stock Project+ 3.2.1 Wii BOOST.GCT (from \"Project+/BOOST.GCT\", MD5 a8b8be65aa8c63625235476df542d2e3) before applying this patch.",
      };
    }

    if (bytes.length !== cfg.stockSize) {
      return {
        ok: false,
        error: "Unexpected file size (" + bytes.length.toLocaleString() + " bytes). Expected stock Project+ 3.2.1 Wii BOOST.GCT (from \"Project+/BOOST.GCT\", MD5 a8b8be65aa8c63625235476df542d2e3, " + cfg.stockSize.toLocaleString() + " bytes).",
      };
    }

    const subtle = (typeof crypto !== "undefined" && crypto.subtle) ||
                   (typeof globalThis !== "undefined" && globalThis.crypto && globalThis.crypto.subtle);
    if (!subtle) {
      return { ok: false, error: "Crypto Subtle API is not supported in this environment." };
    }

    const stockHash = await subtle.digest("SHA-256", bytes);
    const stockSha256 = bufToHex(stockHash);
    if (stockSha256 !== cfg.stockSha256) {
      return {
        ok: false,
        error: "Checksum mismatch (" + stockSha256 + "). Expected stock Project+ 3.2.1 Wii BOOST.GCT (from \"Project+/BOOST.GCT\", MD5 a8b8be65aa8c63625235476df542d2e3).",
      };
    }

    const tailBytes = base64ToBytes(cfg.tailB64);
    const insertAt = bytes.length - 8;
    const gct = new Uint8Array(insertAt + tailBytes.length);
    gct.set(bytes.subarray(0, insertAt), 0);
    gct.set(tailBytes, insertAt);

    const gctHash = await subtle.digest("SHA-256", gct);
    const gctSha256 = bufToHex(gctHash);
    if (gctSha256 !== cfg.outSha256) {
      return {
        ok: false,
        error: "Patched GCT checksum mismatch (" + gctSha256 + "). Expected " + cfg.outSha256 + ".",
      };
    }

    const xmlBytes = new TextEncoder().encode(cfg.infoXml);
    const archive = write7z([
      { name: "info.xml", data: xmlBytes },
      { name: "BOOST.GCT", data: gct },
    ]);

    return {
      ok: true,
      archive: archive,
      gct: gct,
    };
  }

  return {
    crc32: crc32,
    encodeUint64: encodeUint64,
    write7z: write7z,
    buildAddon: buildAddon,
  };
});
