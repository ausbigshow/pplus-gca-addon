# Entry point of the C0 (execute) code. The Gecko codehandler calls this every
# frame; it must preserve everything the codehandler relies on, so save all
# GPRs, CR and LR, run gca_install (no-op after the first call), and return.
    .section .text.entry, "ax"
    .globl _entry
_entry:
    stwu  1, -0xA0(1)                     # new 0xA0-byte stack frame
    mflr  0
    stw   0, 0xA4(1)                      # save the return address (LR)
    mfcr  0
    stw   0, 0x90(1)                      # save the condition register
    mfctr 0
    stw   0, 0x94(1)                      # save the count register
    stmw  3, 0x10(1)                      # save r3-r31
#ifdef GCA_PIC
# Position-independent (movable) build, linked at 0. The compiler leaves
# every absolute address in .got2 or in a word that .fixup lists, so add the
# distance moved since the last run to each of them.
    bcl   20, 31, 1f                      # LR = address of the next instruction
1:  mflr  4                               # r4 = run-time address of label 1
    addi  3, 4, _entry - 1b               # r3 = where we run now
    addis 5, 4, (gca_reloc_base - 1b)@ha
    addi  5, 5, (gca_reloc_base - 1b)@l
    lwz   6, 0(5)
    subf. 6, 6, 3                         # r6 = delta since last relocation
    beq   5f
    stw   3, 0(5)                         # remember where we are now
    addis 7, 4, (__got2_start - 1b)@ha    # r7..r8 = the address table (.got2)
    addi  7, 7, (__got2_start - 1b)@l
    addis 8, 4, (__got2_end - 1b)@ha
    addi  8, 8, (__got2_end - 1b)@l
2:  cmplw 7, 8
    bge   3f
    lwz   9, 0(7)                         # each table entry is an address:
    add   9, 9, 6                         # move it by the same distance
    stw   9, 0(7)
    addi  7, 7, 4
    b     2b
3:  addis 7, 4, (__fixup_start - 1b)@ha  # r7..r8 = list of words holding addresses
    addi  7, 7, (__fixup_start - 1b)@l
    addis 8, 4, (__fixup_end - 1b)@ha
    addi  8, 8, (__fixup_end - 1b)@l
4:  cmplw 7, 8
    bge   5f
    lwz   9, 0(7)                         # link address of a word to fix
    add   9, 9, 3                         # linked at 0: + base = its address now
    lwz   10, 0(9)
    add   10, 10, 6
    stw   10, 0(9)
    addi  7, 7, 4
    b     4b
5:
#endif
    bl    gca_install                     # patch the game's calls (first run only)
    lmw   3, 0x10(1)                      # restore r3-r31, CTR, CR and LR
    lwz   0, 0x94(1)
    mtctr 0
    lwz   0, 0x90(1)
    mtcr  0
    lwz   0, 0xA4(1)
    mtlr  0
    addi  1, 1, 0xA0                      # drop the stack frame
    blr                                   # back to the codehandler

#ifdef GCA_PIC
    .section .data
    .balign 4
gca_reloc_base:
    .long 0                               # link base; set to the run address
#endif
