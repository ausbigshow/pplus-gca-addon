/* Contiguous blob: entry first, then code, then data and zero-filled bss.
   BASE is supplied on the command line with --defsym. Code and data are in
   separate output sections so the 32-byte alignment the USB buffers need
   doesn't move the entry point; objcopy -O binary fills the gap. */
SECTIONS
{
  . = BASE;
  .text BASE : {
    KEEP(*(.text.entry))
    *(.text .text.*)
    __rodata_start = .;    /* where the .asm stops writing instructions */
    *(.rodata .rodata.*)
  }
  .data ALIGN(32) : {
    __data_start = .;
    *(.data .data.*)
    *(.sdata .sdata.*)
    *(.sbss .sbss.*)
    *(.bss .bss.*)
    *(COMMON)
    LONG(0x47434145) /* 'GCAE' end marker, keeps .data PROGBITS to the end */
    . = ALIGN(8);
    __got2_start = .; *(.got2) __got2_end = .;    /* PIC build only */
    __fixup_start = .; *(.fixup) __fixup_end = .;
    . = ALIGN(8);
  }
  /DISCARD/ : { *(.comment) *(.note*) *(.eh_frame*) *(.gnu*) *(.got*) *(.plt*) }
}
