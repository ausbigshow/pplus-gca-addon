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
 *  - No rumble (PADControlMotor's address isn't confirmed for this build).
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
/* PAD */
#define ADDR_PADRead          0x80215FC4
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

/*============================================================================*/
/* Constants                                                                  */
/*============================================================================*/

#define GCN_CONTROLLER_COUNT 4
/* Time base runs at bus clock / 4 = 60.75 MHz on Wii. */
#define TB_PER_MS (243000 / 4)
#define GCN_TIMEOUT (1500 * TB_PER_MS)
#define RECAL_HOLD (3000 * TB_PER_MS)

#define WUP_028_CMD_INIT 0x13
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

/* Diagnostics, readable with a memory viewer: magic, stage, last error. */
volatile u32 gca_diag[4] = { 0x47434131 /* 'GCA1' */, 0, 0, 0 };
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

static int sendPoll(void) {
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
  if (ret >= 0) { DIAG_STAGE(7); ret = sendPoll(); }
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
        if (type != 1 && type != 2) {
          s->err = PAD_ERR_NO_CONTROLLER;
          origin_valid[i] = 0;
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
  int use_adapter = adapter_live && !usb_failed &&
                    (mftb() - gcn_data_written) <= GCN_TIMEOUT;
  if (use_adapter) {
    for (int i = 0; i < GCN_CONTROLLER_COUNT; i++) result[i] = gcn_data[i];
    isr_restore(isr);
    return 0;
  }
  isr_restore(isr);
  /* No adapter data: behave exactly like stock Brawl. */
  return RealPADRead(result);
}

/* Called every frame from the C0 code; patches the call site once. */
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
}
