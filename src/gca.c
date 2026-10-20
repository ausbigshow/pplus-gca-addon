/*
 * Project+ USB GameCube Adapter (WUP-028) support for vWii / Wii
 * Target: Super Smash Bros. Brawl NTSC-U (RSBE01), as used by Project+ v3.2.x
 *
 * Adapted from "WUP-028 adapter BrainSlug module" by Alex "Chadderz" Chadwick
 * https://github.com/Chadderz121/wup-028-bslug  (BSD/MIT-style licence below)
 *
 * Changes from the original:
 *  - Freestanding build that lives inside BOOST.GCT; calls Brawl's own SDK
 *    functions at fixed NTSC-U addresses (verified against doldecomp/brawl).
 *  - Installs itself by redirecting the single `bl PADRead` inside
 *    gfPadSystem::updateLowGC, and falls back to the real PADRead whenever no
 *    adapter is delivering data.
 *  - Origin calibration (like the real PADRead), X+Y+Start recalibration,
 *    digital L/R from the adapter's own bits.
 *  - v5 MEM2 buffers are claimed from the top of the IPC buffer region at
 *    runtime instead of before _start.
 *  - Fixed: error path closed fd -1 instead of the open handle.
 *  - Latches short button presses between game reads (see press_latch).
 *  - Rumble: redirects every `bl PADControlMotor` in Brawl's code after
 *    checking the function's signature; keeps one desired state per port and
 *    sends it alongside a poll, at most one rumble message in flight.
 *  - Resets the SDK's SI idle (screen dimming) timer on adapter input, after
 *    checking __VIResetSIIdle's code shape.
 *
 * Original licence:
 *  Copyright (C) 2017, Alex Chadwick
 *  Permission is hereby granted, free of charge, to any person obtaining a copy
 *  of this software and associated documentation files (the "Software"), to deal
 *  in the Software without restriction, including without limitation the rights
 *  to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 *  copies of the Software, and to permit persons to whom the Software is
 *  furnished to do so, subject to the following conditions:
 *  The above copyright notice and this permission notice shall be included in
 *  all copies or substantial portions of the Software.
 *  THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND.
 */

typedef unsigned char u8;
typedef signed char s8;
typedef unsigned short u16;
typedef unsigned int u32;
typedef signed int s32;

#define NULL ((void *)0)
#define IOS_ALIGN __attribute__((aligned(32)))

/*============================================================================*/
/* Brawl NTSC-U (RSBE01) addresses                                            */
/*============================================================================*/

/* Wii SDK IPC client (ipcclt.c), in source order. */
#define ADDR_IOS_OpenAsync    0x80212290
#define ADDR_IOS_CloseAsync   0x802124C8
#define ADDR_IOS_IoctlAsync   0x80212C08
#define ADDR_IOS_IoctlvAsync  0x80212FAC
/* ipcMain.c */
#define ADDR_IPCGetBufferHi   0x80211AE0
#define ADDR_IPCGetBufferLo   0x80211AE8
/* OS */
#define ADDR_DCFlushRange     0x801D76E8
#define ADDR_ICInvalidateRange 0x801D77CC
/* PAD (pad.c, in SDK order: PADRead 0x2E0, PADControlMotor 0xB8, SPEC0/1/2...) */
#define ADDR_PADRead          0x80215FC4
#define ADDR_PADControlMotor  0x802162A4
#define SIZE_PADControlMotor  0xB8
/* vi.c: VIResetDimmingCount 0x14, __VIResetRFIdle 0x10, __VIResetSIIdle 0x10,
   WaitMicroTime 0x98, matching MKW/SPM symbol maps. */
#define ADDR_VIResetRFIdle    0x801EA078
#define ADDR_VIResetSIIdle    0x801EA088
/* Brawl's main .text, scanned once for calls to PADControlMotor. */
#define TEXT_START            0x80004000
#define TEXT_END              0x80406500
/* gfPadSystem::updateLowGC(gfPadStatus*) */
#define ADDR_updateLowGC      0x80029578
#define SIZE_updateLowGC      0x1D8

typedef struct {
  void *data;
  u32 len;
} ioctlv;

typedef s32 (*ios_cb_t)(s32 ret, void *usr);

typedef struct {
  u16 button;
  s8 stickX, stickY, substickX, substickY;
  u8 triggerL, triggerR, analogA, analogB;
  s8 err;
  u8 pad;
} PADStatus;

#define PAD_MOTOR_STOP 0
#define PAD_MOTOR_RUMBLE 1
#define PAD_MOTOR_STOP_HARD 2

#define PAD_ERR_NONE 0
#define PAD_ERR_NO_CONTROLLER -1

#define PAD_BUTTON_LEFT  0x0001
#define PAD_BUTTON_RIGHT 0x0002
#define PAD_BUTTON_DOWN  0x0004
#define PAD_BUTTON_UP    0x0008
#define PAD_TRIGGER_Z    0x0010
#define PAD_TRIGGER_R    0x0020
#define PAD_TRIGGER_L    0x0040
#define PAD_BUTTON_A     0x0100
#define PAD_BUTTON_B     0x0200
#define PAD_BUTTON_X     0x0400
#define PAD_BUTTON_Y     0x0800
#define PAD_BUTTON_START 0x1000

#define IOS_OpenAsync   ((s32 (*)(const char *, u32, ios_cb_t, void *))ADDR_IOS_OpenAsync)
#define IOS_CloseAsync  ((s32 (*)(s32, ios_cb_t, void *))ADDR_IOS_CloseAsync)
#define IOS_IoctlAsync  ((s32 (*)(s32, s32, void *, s32, void *, s32, ios_cb_t, void *))ADDR_IOS_IoctlAsync)
#define IOS_IoctlvAsync ((s32 (*)(s32, s32, s32, s32, ioctlv *, ios_cb_t, void *))ADDR_IOS_IoctlvAsync)
#define IPCGetBufferLo  ((void *(*)(void))ADDR_IPCGetBufferLo)
#define DCFlushRange    ((void (*)(void *, u32))ADDR_DCFlushRange)
#define ICInvalidateRange ((void (*)(void *, u32))ADDR_ICInvalidateRange)
#define RealPADRead     ((u32 (*)(PADStatus *))ADDR_PADRead)
#define RealPADControlMotor ((void (*)(s32, u32))ADDR_PADControlMotor)
#define VIResetSIIdle   ((void (*)(void))ADDR_VIResetSIIdle)

/*============================================================================*/
/* Constants                                                                  */
/*============================================================================*/

#define GCN_CONTROLLER_COUNT 4
/* Time base runs at bus clock / 4 = 60.75 MHz on Wii. */
#define TB_PER_MS (243000 / 4)
#define GCN_TIMEOUT (1500 * TB_PER_MS)
#define RECAL_HOLD (3000 * TB_PER_MS)

#define WUP_028_CMD_INIT 0x13
#define WUP_028_CMD_RUMBLE 0x11
#define WUP_028_ID 0x057e0337
#define WUP_028_POLL_SIZE 0x25
#define DEV_USB_HID_PATH "/dev/usb/hid"

/*============================================================================*/
/* State                                                                      */
/*============================================================================*/

static s32 dev_usb_hid_fd = -1;
static s8 started = 0;
static s8 version = 0;
static volatile s8 usb_failed = 0;
static PADStatus gcn_data[GCN_CONTROLLER_COUNT];
static volatile u32 gcn_data_written;
static volatile u32 gcn_adapter_id = (u32)-1;
static volatile u8 adapter_live = 0;

/* Origin calibration, per port. */
static u8 origin_valid[GCN_CONTROLLER_COUNT];
static u8 origin[GCN_CONTROLLER_COUNT][6]; /* sx, sy, cx, cy, l, r */
static u32 recal_start[GCN_CONTROLLER_COUNT];

/* Press latch, per port: buttons that went from released to pressed in any
 * adapter report since the game's last read. The adapter reports at ~125 Hz
 * and the game reads at ~60 Hz, so a short tap can start and end between two
 * reads; the latch makes it show up for one read. Only rising edges are
 * latched, so a button held across a read and then released is reported
 * released on time (no phantom hold). */
static u16 prev_btn[GCN_CONTROLLER_COUNT];
static u16 press_latch[GCN_CONTROLLER_COUNT];

/* Diagnostics, readable with a memory viewer:
 *  [0] magic 'GCA1'   [1] stage   [2] error site   [3] error code
 *  [4] adapter reports received (poll rate = delta over time)
 *  [5] gca_PADRead calls (game read rate)
 *  [6] rumble commands sent
 *  [7] reads where the latch added a press the snapshot had missed
 *  [8] PADControlMotor calls redirected (0xEE: signature mismatch, none)
 *  [9] SI idle reset: 1 = verified and in use, 0xEE = shape mismatch, off
 *  [10] PADControlMotor calls from the game
 *  [11] rumble message errors */
volatile u32 gca_diag[12] = { 0x47434131 /* 'GCA1' */ };
#define DIAG_STAGE(x) (gca_diag[1] = (x))
#define DIAG_ERR(m, e) (gca_diag[2] = (m), gca_diag[3] = (u32)(e))

static u8 init_msg_buffer[32] IOS_ALIGN = { WUP_028_CMD_INIT };
static u8 poll_msg_buffer[64] IOS_ALIGN;

/*============================================================================*/
/* CPU helpers                                                                */
/*============================================================================*/

static inline u32 mftb(void) {
  u32 r;
  __asm__ volatile("mftb %0" : "=r"(r));
  return r;
}
static inline u32 isr_disable(void) {
  u32 isr, tmp;
  __asm__ volatile("mfmsr %0; rlwinm %1, %0, 0, 17, 15; mtmsr %1"
                   : "=r"(isr), "=r"(tmp));
  return isr;
}
static inline void isr_restore(u32 isr) {
  u32 tmp;
  __asm__ volatile("mfmsr %0; rlwimi %0, %1, 0, 16, 16; mtmsr %0"
                   : "=&r"(tmp) : "r"(isr));
}

static void mem_zero(void *p, u32 n) {
  u8 *b = (u8 *)p;
  while (n--) *b++ = 0;
}

/*============================================================================*/
/* /dev/usb/hid version 4                                                     */
/*============================================================================*/

#define WUP_028_DESCRIPTOR_SIZE 0x44
#define WUP_028_ENDPOINT_OUT 0x2
#define WUP_028_ENDPOINT_IN 0x81
#define HID4_DEVICE_CHANGE_SIZE 0x180 /* words */
#define HID4_IOCTL_GET_DEVICE_CHANGE 0
#define HID4_IOCTL_INTERRUPT_IN 3
#define HID4_IOCTL_INTERRUPT_OUT 4
#define HID4_IOCTL_GET_VERSION 6
#define HID4_VERSION 0x00040001

static u32 hid4_devices[HID4_DEVICE_CHANGE_SIZE] IOS_ALIGN;

struct interrupt_msg4 {
  u8 padding[16];
  u32 device;
  u32 endpoint;
  u32 length;
  void *ptr;
};

static struct interrupt_msg4 init_msg4 IOS_ALIGN = {
  { 0 }, (u32)-1, WUP_028_ENDPOINT_OUT, 1, init_msg_buffer
};
static struct interrupt_msg4 poll_msg4 IOS_ALIGN = {
  { 0 }, (u32)-1, WUP_028_ENDPOINT_IN, WUP_028_POLL_SIZE, poll_msg_buffer
};

/*============================================================================*/
/* /dev/usb/hid version 5                                                     */
/*============================================================================*/

#define HID5_DEVICE_CHANGE_SIZE 0x20 /* entries */
#define HID5_TMP_BUFFER_SIZE 0x20    /* words */
#define HID5_IOCTL_GET_VERSION 0
#define HID5_IOCTL_GET_DEVICE_CHANGE 1
#define HID5_IOCTL_GET_DEVICE_PARAMETERS 3
#define HID5_IOCTL_ATTACH_FINISH 6
#define HID5_IOCTL_SET_RESUME 16
#define HID5_IOCTL_INTERRUPT 19
#define HID5_VERSION 0x00050001

struct hid5_device {
  u32 id;
  u32 vid_pid;
  u32 unknown8;
};

/* These two MUST be in MEM2; claimed at runtime from the IPC buffer region. */
static struct hid5_device *hid5_devices;
static u32 *hid5_buffer;
static ioctlv hid5_argv[2] IOS_ALIGN;
static ioctlv hid5_poll_argv[2] IOS_ALIGN;

/*
 * Claim MEM2 for v5 from the top of the SDK's IPC buffer region. Libraries
 * claim from the bottom (IPCSetBufferLo), so the top is free slack after boot.
 * We lower the SDK's own "current hi" pointer so nothing later collides.
 * IPCGetBufferHi is `lwz r3, d(r13); blr`; decode it to find that pointer.
 */
static int claim_mem2(void) {
  const u32 *fn = (const u32 *)ADDR_IPCGetBufferHi;
  u32 need = sizeof(struct hid5_device) * HID5_DEVICE_CHANGE_SIZE +
             HID5_TMP_BUFFER_SIZE * 4 * 4; /* devices + 4x tmp (generous) */
  if ((fn[0] & 0xFFFF0000) != 0x806D0000 || fn[1] != 0x4E800020) return 0;
  u32 sda;
  __asm__ volatile("mr %0, 13" : "=r"(sda));
  u8 **hi_ptr = (u8 **)(sda + (s32)(short)(fn[0] & 0xFFFF));
  u8 *hi = *hi_ptr;
  u8 *lo = (u8 *)IPCGetBufferLo();
  /* Must be MEM2 (0x90000000-0x93FFFFFF cached) with room to spare. */
  if (((u32)hi & 0xF0000000) != 0x90000000) return 0;
  if ((u32)hi < (u32)lo || (u32)hi - (u32)lo < need + 0x100) return 0;
  u8 *base = (u8 *)(((u32)hi - need) & ~0x1F);
  *hi_ptr = base;
  hid5_devices = (struct hid5_device *)base;
  hid5_buffer = (u32 *)(base + sizeof(struct hid5_device) * HID5_DEVICE_CHANGE_SIZE);
  mem_zero(base, need);
  DCFlushRange(base, need);
  return 1;
}

/*============================================================================*/
/* Request builders                                                           */
/*============================================================================*/

static s32 onDevUsbInit(s32 ret, void *usr);
static s32 onDevUsbPoll(s32 ret, void *usr);
static s32 callbackIgnore(s32 ret, void *usr) { (void)ret; (void)usr; return 0; }

static int checkVersion4(ios_cb_t cb) {
  return IOS_IoctlAsync(dev_usb_hid_fd, HID4_IOCTL_GET_VERSION, NULL, 0, NULL, 0, cb, NULL);
}
static int getDeviceChange4(ios_cb_t cb) {
  return IOS_IoctlAsync(dev_usb_hid_fd, HID4_IOCTL_GET_DEVICE_CHANGE, NULL, 0,
                        hid4_devices, sizeof(hid4_devices), cb, NULL);
}
static int sendInit4(ios_cb_t cb) {
  init_msg4.device = gcn_adapter_id;
  return IOS_IoctlAsync(dev_usb_hid_fd, HID4_IOCTL_INTERRUPT_OUT,
                        &init_msg4, sizeof(init_msg4), NULL, 0, cb, NULL);
}
static int sendPoll4(ios_cb_t cb) {
  poll_msg4.device = gcn_adapter_id;
  DCFlushRange(poll_msg_buffer, sizeof(poll_msg_buffer));
  return IOS_IoctlAsync(dev_usb_hid_fd, HID4_IOCTL_INTERRUPT_IN,
                        &poll_msg4, sizeof(poll_msg4), NULL, 0, cb, NULL);
}

static int checkVersion5(ios_cb_t cb) {
  return IOS_IoctlAsync(dev_usb_hid_fd, HID5_IOCTL_GET_VERSION, NULL, 0,
                        hid5_buffer, 0x20, cb, NULL);
}
static int getDeviceChange5(ios_cb_t cb) {
  return IOS_IoctlAsync(dev_usb_hid_fd, HID5_IOCTL_GET_DEVICE_CHANGE, NULL, 0,
                        hid5_devices, sizeof(struct hid5_device) * HID5_DEVICE_CHANGE_SIZE,
                        cb, NULL);
}
static int sendAttach5(ios_cb_t cb, void *usr) {
  return IOS_IoctlAsync(dev_usb_hid_fd, HID5_IOCTL_ATTACH_FINISH, NULL, 0, NULL, 0, cb, usr);
}
static int sendResume5(ios_cb_t cb) {
  for (int i = 0; i < 8; i++) hid5_buffer[i] = 0;
  hid5_buffer[0] = gcn_adapter_id;
  hid5_buffer[2] = 1;
  return IOS_IoctlAsync(dev_usb_hid_fd, HID5_IOCTL_SET_RESUME, hid5_buffer, 0x20,
                        NULL, 0, cb, NULL);
}
static int sendParams5(ios_cb_t cb) {
  return IOS_IoctlAsync(dev_usb_hid_fd, HID5_IOCTL_GET_DEVICE_PARAMETERS,
                        hid5_buffer, 0x20, hid5_buffer + 8, 0x60, cb, NULL);
}
static int sendInit5(ios_cb_t cb) {
  hid5_argv[0].data = hid5_buffer;
  hid5_argv[0].len = 0x40;
  hid5_argv[1].data = init_msg_buffer;
  hid5_argv[1].len = 1;
  return IOS_IoctlvAsync(dev_usb_hid_fd, HID5_IOCTL_INTERRUPT, 2, 0, hid5_argv, cb, NULL);
}
static int sendPoll5(ios_cb_t cb) {
  hid5_poll_argv[0].data = hid5_buffer + 0x10;
  hid5_poll_argv[0].len = 0x40;
  hid5_poll_argv[1].data = poll_msg_buffer;
  hid5_poll_argv[1].len = WUP_028_POLL_SIZE;
  return IOS_IoctlvAsync(dev_usb_hid_fd, HID5_IOCTL_INTERRUPT, 1, 1, hid5_poll_argv, cb, NULL);
}

/*============================================================================*/
/* Rumble                                                                     */
/*============================================================================*/

/* The game sets rumble_want; the poll loop sends it when it differs from what
 * the adapter last accepted. Only one rumble message is in flight at a time,
 * and it is issued together with a poll, as Chadderz's module does (separate
 * IN/OUT traffic was seen to lock up). Intermediate on/off toggles faster than
 * the poll loop collapse into the latest state. */
#define RUMBLE_STALL_POLLS 64 /* reply lost: retry after ~0.5 s of polls */

static u8 rumble_msg_buffer[32] IOS_ALIGN = { WUP_028_CMD_RUMBLE };
static volatile u8 rumble_want[GCN_CONTROLLER_COUNT];
static u8 rumble_acked[GCN_CONTROLLER_COUNT];
static u8 rumble_pending[GCN_CONTROLLER_COUNT];
static u8 rumble_inflight;
static u8 rumble_token;
static u8 rumble_stall;
static ioctlv hid5_rumble_argv[2] IOS_ALIGN;
static struct interrupt_msg4 rumble_msg4 IOS_ALIGN = {
  { 0 }, (u32)-1, WUP_028_ENDPOINT_OUT, 1 + GCN_CONTROLLER_COUNT, rumble_msg_buffer
};

static s32 onRumble(s32 ret, void *usr) {
  if ((u8)(u32)usr != rumble_token || !rumble_inflight) return 0; /* stale */
  rumble_inflight = 0;
  /* Accept the state even on error so a refusing adapter isn't spammed; the
     next change retries. */
  if (ret < 0) { gca_diag[11]++; DIAG_ERR(13, ret); }
  for (int i = 0; i < GCN_CONTROLLER_COUNT; i++) rumble_acked[i] = rumble_pending[i];
  return 0;
}

static int sendRumble(void) {
  for (int i = 0; i < GCN_CONTROLLER_COUNT; i++) {
    rumble_pending[i] = rumble_want[i];
    rumble_msg_buffer[1 + i] = rumble_pending[i];
  }
  DCFlushRange(rumble_msg_buffer, sizeof(rumble_msg_buffer));
  void *token = (void *)(u32)++rumble_token;
  if (version == 5) {
    /* Words 0-15 of hid5_buffer still hold the OUT header used for init. */
    hid5_rumble_argv[0].data = hid5_buffer;
    hid5_rumble_argv[0].len = 0x40;
    hid5_rumble_argv[1].data = rumble_msg_buffer;
    hid5_rumble_argv[1].len = 1 + GCN_CONTROLLER_COUNT;
    return IOS_IoctlvAsync(dev_usb_hid_fd, HID5_IOCTL_INTERRUPT, 2, 0, hid5_rumble_argv,
                           onRumble, token);
  }
  if (version == 4) {
    rumble_msg4.device = gcn_adapter_id;
    return IOS_IoctlAsync(dev_usb_hid_fd, HID4_IOCTL_INTERRUPT_OUT,
                          &rumble_msg4, sizeof(rumble_msg4), NULL, 0, onRumble, token);
  }
  return -1;
}

static void rumble_reset(void) {
  /* New adapter: motors are off, and any reply still in flight is stale. */
  for (int i = 0; i < GCN_CONTROLLER_COUNT; i++) rumble_acked[i] = 0;
  rumble_inflight = 0;
  rumble_token++;
}

static void maybeSendRumble(void) {
  if (rumble_inflight) {
    if (++rumble_stall < RUMBLE_STALL_POLLS) return;
    /* Lost reply: the adapter may or may not have applied it, so resend. */
    rumble_inflight = 0;
    rumble_token++;
    for (int i = 0; i < GCN_CONTROLLER_COUNT; i++) rumble_acked[i] = 0xFF;
  }
  int dirty = 0;
  for (int i = 0; i < GCN_CONTROLLER_COUNT; i++)
    if (rumble_want[i] != rumble_acked[i]) dirty = 1;
  if (!dirty) return;
  rumble_inflight = 1;
  rumble_stall = 0;
  if (sendRumble()) { rumble_inflight = 0; gca_diag[11]++; }
  else gca_diag[6]++;
}

static int sendPoll(void) {
  maybeSendRumble();
  if (version == 5) return sendPoll5(onDevUsbPoll);
  if (version == 4) return sendPoll4(onDevUsbPoll);
  return -1;
}

static void onFatal(int method, s32 err) {
  DIAG_ERR(method, err);
  usb_failed = 1;
  adapter_live = 0;
  if (dev_usb_hid_fd >= 0) {
    s32 fd = dev_usb_hid_fd;
    dev_usb_hid_fd = -1;
    IOS_CloseAsync(fd, callbackIgnore, NULL);
  }
}

/*============================================================================*/
/* Callback chain                                                             */
/*============================================================================*/

static s32 onDevGetVersion4(s32 ret, void *usr);
static s32 onDevGetVersion5(s32 ret, void *usr);
static s32 onDevUsbChange4(s32 ret, void *usr);
static s32 onDevUsbChange5(s32 ret, void *usr);
static s32 onDevUsbAttach5(s32 ret, void *usr);
static s32 onDevUsbResume5(s32 ret, void *usr);
static s32 onDevUsbParams5(s32 ret, void *usr);

static s32 onDevOpen(s32 fd, void *usr) {
  (void)usr;
  if (fd < 0) { onFatal(1, fd); return 0; }
  dev_usb_hid_fd = fd;
  DIAG_STAGE(2);
  s32 ret = checkVersion4(onDevGetVersion4);
  if (ret) onFatal(2, ret);
  return 0;
}

static s32 onDevGetVersion4(s32 ret, void *usr) {
  (void)usr;
  if (ret == HID4_VERSION) {
    version = 4;
    DIAG_STAGE(4);
    ret = getDeviceChange4(onDevUsbChange4);
  } else if (claim_mem2()) {
    ret = checkVersion5(onDevGetVersion5);
  } else {
    DIAG_ERR(3, ret);
    ret = -1;
  }
  if (ret) onFatal(3, ret);
  return 0;
}

static s32 onDevGetVersion5(s32 ret, void *usr) {
  (void)usr;
  if (ret == 0 && hid5_buffer[0] == HID5_VERSION) {
    version = 5;
    DIAG_STAGE(5);
    ret = getDeviceChange5(onDevUsbChange5);
  } else if (ret == 0) {
    ret = (s32)hid5_buffer[0];
  }
  if (ret) onFatal(4, ret);
  return 0;
}

static s32 onDevUsbChange4(s32 ret, void *usr) {
  (void)usr;
  if (ret >= 0) {
    int found = 0;
    for (int i = 0; i < HID4_DEVICE_CHANGE_SIZE - 4 && hid4_devices[i] != 0 &&
                    hid4_devices[i] < sizeof(hid4_devices);
         i += hid4_devices[i] / 4) {
      u32 device_id = hid4_devices[i + 1];
      if (hid4_devices[i] == WUP_028_DESCRIPTOR_SIZE && hid4_devices[i + 4] == WUP_028_ID) {
        found = 1;
        if (gcn_adapter_id != device_id) {
          gcn_adapter_id = device_id;
          DIAG_STAGE(6);
          if (sendInit4(onDevUsbInit)) gcn_adapter_id = (u32)-1;
        }
        break;
      }
    }
    if (!found) { gcn_adapter_id = (u32)-1; adapter_live = 0; }
    ret = getDeviceChange4(onDevUsbChange4);
  }
  if (ret) onFatal(5, ret);
  return 0;
}

static s32 onDevUsbChange5(s32 ret, void *usr) {
  (void)usr;
  if (ret >= 0) ret = sendAttach5(onDevUsbAttach5, (void *)ret);
  if (ret) onFatal(6, ret);
  return 0;
}

static s32 onDevUsbAttach5(s32 ret, void *vcount) {
  if (ret == 0) {
    int found = 0;
    int count = (int)vcount;
    for (int i = 0; i < HID5_DEVICE_CHANGE_SIZE && i < count; i++) {
      u32 device_id = hid5_devices[i].id;
      if (hid5_devices[i].vid_pid == WUP_028_ID) {
        found = 1;
        if (gcn_adapter_id != device_id) {
          gcn_adapter_id = device_id;
          DIAG_STAGE(6);
          if (sendResume5(onDevUsbResume5)) found = 0;
        }
        break;
      }
    }
    if (!found) { gcn_adapter_id = (u32)-1; adapter_live = 0; }
    ret = getDeviceChange5(onDevUsbChange5);
  }
  if (ret) onFatal(7, ret);
  return 0;
}

static s32 onDevUsbResume5(s32 ret, void *usr) {
  (void)usr;
  if (ret == 0) ret = sendParams5(onDevUsbParams5);
  if (ret) { DIAG_ERR(8, ret); gcn_adapter_id = (u32)-1; }
  return 0;
}

static s32 onDevUsbParams5(s32 ret, void *usr) {
  (void)usr;
  if (ret == 0) {
    /* Words 0-7 still hold the resume block. */
    for (int i = 8; i < 32; i++) hid5_buffer[i] = 0;
    hid5_buffer[16] = gcn_adapter_id;
    ret = sendInit5(onDevUsbInit);
  }
  if (ret) { DIAG_ERR(9, ret); gcn_adapter_id = (u32)-1; }
  return 0;
}

static s32 onDevUsbInit(s32 ret, void *usr) {
  (void)usr;
  if (ret >= 0) { DIAG_STAGE(7); rumble_reset(); ret = sendPoll(); }
  if (ret) { DIAG_ERR(10, ret); gcn_adapter_id = (u32)-1; }
  return 0;
}

static inline s8 clamp_s8(int v) {
  return (s8)(v < -128 ? -128 : v > 127 ? 127 : v);
}
static inline u8 clamp_u8(int v) {
  return (u8)(v < 0 ? 0 : v > 255 ? 255 : v);
}

static s32 onDevUsbPoll(s32 ret, void *usr) {
  (void)usr;
  if (ret >= 0) {
    if (poll_msg_buffer[0] == 0x21) {
      u32 now = mftb();
      u32 isr = isr_disable();
      for (int i = 0; i < GCN_CONTROLLER_COUNT; i++) {
        const u8 *d = poll_msg_buffer + (i * 9 + 1);
        PADStatus *s = &gcn_data[i];
        u8 type = d[0] >> 4;
        /* On hot-plug the adapter's first report for a port has every analog
           byte 0 (seen on a Mayflash); a resting stick is never 0, so wait for
           real values before calibrating. */
        int analog_zero = !(d[3] | d[4] | d[5] | d[6] | d[7] | d[8]);
        if ((type != 1 && type != 2) || (!origin_valid[i] && analog_zero)) {
          mem_zero(s, sizeof(*s));
          s->err = PAD_ERR_NO_CONTROLLER;
          origin_valid[i] = 0;
          prev_btn[i] = 0;
          press_latch[i] = 0;
          continue;
        }
        u16 b = 0;
        if (d[1] & 0x01) b |= PAD_BUTTON_A;
        if (d[1] & 0x02) b |= PAD_BUTTON_B;
        if (d[1] & 0x04) b |= PAD_BUTTON_X;
        if (d[1] & 0x08) b |= PAD_BUTTON_Y;
        if (d[1] & 0x10) b |= PAD_BUTTON_LEFT;
        if (d[1] & 0x20) b |= PAD_BUTTON_RIGHT;
        if (d[1] & 0x40) b |= PAD_BUTTON_DOWN;
        if (d[1] & 0x80) b |= PAD_BUTTON_UP;
        if (d[2] & 0x01) b |= PAD_BUTTON_START;
        if (d[2] & 0x02) b |= PAD_TRIGGER_Z;
        if (d[2] & 0x04) b |= PAD_TRIGGER_R;
        if (d[2] & 0x08) b |= PAD_TRIGGER_L;

        /* Origin: captured on connect, re-captured after X+Y+Start for 3s. */
        const u16 recal = PAD_BUTTON_X | PAD_BUTTON_Y | PAD_BUTTON_START;
        if ((b & recal) == recal) {
          if (recal_start[i] == 0) recal_start[i] = now | 1;
          else if (now - recal_start[i] > RECAL_HOLD) {
            origin_valid[i] = 0;
            recal_start[i] = now | 1;
          }
        } else {
          recal_start[i] = 0;
        }
        if (!origin_valid[i]) {
          for (int k = 0; k < 6; k++) origin[i][k] = d[3 + k];
          origin_valid[i] = 1;
        }

        press_latch[i] |= b & ~prev_btn[i];
        prev_btn[i] = b;

        s->button = b;
        s->stickX = clamp_s8((int)d[3] - origin[i][0]);
        s->stickY = clamp_s8((int)d[4] - origin[i][1]);
        s->substickX = clamp_s8((int)d[5] - origin[i][2]);
        s->substickY = clamp_s8((int)d[6] - origin[i][3]);
        s->triggerL = clamp_u8((int)d[7] - origin[i][4]);
        s->triggerR = clamp_u8((int)d[8] - origin[i][5]);
        s->analogA = 0;
        s->analogB = 0;
        s->err = PAD_ERR_NONE;
      }
      gcn_data_written = now;
      adapter_live = 1;
      gca_diag[4]++;
      isr_restore(isr);
    }
    ret = sendPoll();
  }
  if (ret) { DIAG_ERR(11, ret); gcn_adapter_id = (u32)-1; adapter_live = 0; }
  return 0;
}

/*============================================================================*/
/* Game-facing entry points                                                   */
/*============================================================================*/

static u8 si_idle_ok;
static PADStatus last_read[GCN_CONTROLLER_COUNT];

static inline int absdiff(int a, int b) { return a > b ? a - b : b - a; }

/* Same test the SDK's PADRead uses before calling __VIResetSIIdle. */
static int input_changed(const PADStatus *a, const PADStatus *b) {
  if (a->err != b->err || a->button != b->button) return 1;
  return absdiff(a->stickX, b->stickX) >= 3 || absdiff(a->stickY, b->stickY) >= 3 ||
         absdiff(a->substickX, b->substickX) >= 3 ||
         absdiff(a->substickY, b->substickY) >= 3 ||
         absdiff(a->triggerL, b->triggerL) >= 3 || absdiff(a->triggerR, b->triggerR) >= 3;
}

/* Replaces the `bl PADRead` in gfPadSystem::updateLowGC. */
u32 gca_PADRead(PADStatus *result) {
  u32 isr = isr_disable();
  if (!started) {
    started = 1;
    for (int i = 0; i < GCN_CONTROLLER_COUNT; i++) gcn_data[i].err = PAD_ERR_NO_CONTROLLER;
    gcn_data_written = mftb();
    DIAG_STAGE(1);
    if (IOS_OpenAsync(DEV_USB_HID_PATH, 0, onDevOpen, NULL) != 0) onFatal(12, -1);
  }
  gca_diag[5]++;
  int use_adapter = adapter_live && !usb_failed &&
                    (mftb() - gcn_data_written) <= GCN_TIMEOUT;
  for (int i = 0; i < GCN_CONTROLLER_COUNT; i++) {
    if (use_adapter) {
      result[i] = gcn_data[i];
      u16 missed = press_latch[i] & ~result[i].button;
      if (missed && result[i].err == PAD_ERR_NONE) {
        result[i].button |= missed;
        gca_diag[7]++;
      }
    }
    press_latch[i] = 0;
  }
  if (use_adapter) {
    int changed = 0;
    for (int i = 0; i < GCN_CONTROLLER_COUNT; i++) {
      if (result[i].err == PAD_ERR_NONE && input_changed(&result[i], &last_read[i]))
        changed = 1;
      last_read[i] = result[i];
    }
    isr_restore(isr);
    /* No SI controller is pressing anything on a Wii U, so without this the
       screen dims after a few minutes of adapter-only play. */
    if (changed && si_idle_ok) VIResetSIIdle();
    return 0;
  }
  isr_restore(isr);
  /* No adapter data: behave exactly like stock Brawl. */
  return RealPADRead(result);
}

/* Replaces every `bl PADControlMotor`. Stock behaviour is kept (the real
   function still runs for SI controllers); the adapter gets the state too. */
void gca_PADControlMotor(s32 chan, u32 cmd) {
  gca_diag[10]++;
  if ((u32)chan < GCN_CONTROLLER_COUNT) rumble_want[chan] = (cmd == PAD_MOTOR_RUMBLE);
  RealPADControlMotor(chan, cmd);
}

static int bl_target(u32 a, u32 v, u32 *dest) {
  if ((v & 0xFC000003) != 0x48000001) return 0;
  s32 off = (s32)(v & 0x03FFFFFC);
  if (off & 0x02000000) off -= 0x04000000;
  *dest = a + off;
  return 1;
}

static int patch_bl(u32 *ins, u32 target) {
  s32 noff = (s32)(target - (u32)ins);
  if (noff < -0x02000000 || noff >= 0x02000000) return 0;
  *ins = 0x48000001 | ((u32)noff & 0x03FFFFFC);
  DCFlushRange(ins, 4);
  ICInvalidateRange(ins, 4);
  return 1;
}

/* PADControlMotor: the SDK signature BrainSlug matches (pad.xml), at +0x64:
   lis r3,0x8000; lbz r0,0x30E3(r3); rlwinm. r0,r0,0,26,26; beq +8 */
static int motor_signature_ok(void) {
  static const u32 sig[4] = { 0x3C608000, 0x880330E3, 0x540006B5, 0x41820008 };
  const u32 *f = (const u32 *)ADDR_PADControlMotor;
  for (int k = 0; k < 4; k++)
    if (f[0x64 / 4 + k] != sig[k]) return 0;
  return f[SIZE_PADControlMotor / 4 - 1] == 0x4E800020; /* ends in blr */
}

#define MAX_MOTOR_SITES 32
static void install_rumble(void) {
  if (!motor_signature_ok()) { gca_diag[8] = 0xEE; return; }
  u32 target = (u32)gca_PADControlMotor;
  u32 *sites[MAX_MOTOR_SITES];
  int n = 0;
  for (u32 a = TEXT_START; a < TEXT_END; a += 4) {
    u32 dest;
    if (!bl_target(a, *(u32 *)a, &dest) || dest != ADDR_PADControlMotor) continue;
    if (n == MAX_MOTOR_SITES) { gca_diag[8] = 0xEE; return; } /* implausible */
    sites[n++] = (u32 *)a;
  }
  int patched = 0;
  for (int k = 0; k < n; k++) patched += patch_bl(sites[k], target);
  gca_diag[8] = patched ? (u32)patched : 0xEE;
}

/* __VIResetSIIdle and __VIResetRFIdle are adjacent 4-instruction leaf
   functions that each clear one idle flag: same instructions, different
   variable. Only call it if the code looks exactly like that. */
static int leaf_store_only(const u32 *f) {
  if (f[3] != 0x4E800020) return 0; /* blr */
  for (int k = 0; k < 3; k++) {
    u32 op = f[k] >> 26;
    /* li/lis/addi(s), ori, andi., rlwinm, lwz/lbz/lhz, stw/stb/sth: no
       branches, no SPR or MSR access. */
    if (op != 14 && op != 15 && op != 24 && op != 28 && op != 21 && op != 32 &&
        op != 34 && op != 40 && op != 36 && op != 38 && op != 44) return 0;
  }
  return 1;
}
static void install_si_idle(void) {
  const u32 *si = (const u32 *)ADDR_VIResetSIIdle;
  const u32 *rf = (const u32 *)ADDR_VIResetRFIdle;
  int ok = leaf_store_only(si) && leaf_store_only(rf);
  int differs = 0, stores = 0;
  for (int k = 0; ok && k < 3; k++) {
    if ((si[k] ^ rf[k]) & 0xFFFF0000) ok = 0; /* same opcode and registers */
    if (si[k] != rf[k]) differs = 1;
    u32 op = si[k] >> 26;
    if (op == 36 || op == 38 || op == 44) stores++;
  }
  si_idle_ok = ok && differs && stores;
  gca_diag[9] = si_idle_ok ? 1 : 0xEE;
}

/* Called every frame from the C0 code; patches the call sites once. */
void gca_install(void) {
  static u8 installed = 0;
  if (installed) return;
  installed = 1;

  u32 target = (u32)gca_PADRead;
  int patched = 0;
  for (u32 a = ADDR_updateLowGC; a < ADDR_updateLowGC + SIZE_updateLowGC; a += 4) {
    u32 *ins = (u32 *)a;
    u32 v = *ins;
    if ((v & 0xFC000003) != 0x48000001) continue; /* bl only */
    s32 off = (s32)(v & 0x03FFFFFC);
    if (off & 0x02000000) off -= 0x04000000;
    u32 dest = a + off;
    if (dest != ADDR_PADRead && dest != target) continue;
    s32 noff = (s32)(target - a);
    if (noff < -0x02000000 || noff >= 0x02000000) break;
    *ins = 0x48000001 | ((u32)noff & 0x03FFFFFC);
    DCFlushRange(ins, 4);
    ICInvalidateRange(ins, 4);
    patched++;
  }
  gca_diag[1] = patched ? 0x10 : 0xEE; /* 0xEE: PADRead call not found */
  if (!patched) return; /* adapter not in use: leave everything else stock */
  install_rumble();
  install_si_idle();
}
