# Entry point of the C0 (execute) code. The Gecko codehandler calls this every
# frame; it must preserve everything the codehandler relies on, so save all
# GPRs, CR and LR, run gca_install (no-op after the first call), and return.
    .section .text.entry, "ax"
    .globl _entry
_entry:
    stwu  1, -0xA0(1)
    mflr  0
    stw   0, 0xA4(1)
    mfcr  0
    stw   0, 0x90(1)
    mfctr 0
    stw   0, 0x94(1)
    stmw  3, 0x10(1)
    bl    gca_install
    lmw   3, 0x10(1)
    lwz   0, 0x94(1)
    mtctr 0
    lwz   0, 0x90(1)
    mtcr  0
    lwz   0, 0xA4(1)
    mtlr  0
    addi  1, 1, 0xA0
    blr
