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
    *(.rodata .rodata.*)
  }
  .data ALIGN(32) : {
    *(.data .data.*)
    *(.sdata .sdata.*)
    *(.sbss .sbss.*)
    *(.bss .bss.*)
    *(COMMON)
    LONG(0x47434145) /* 'GCAE' end marker, keeps .data PROGBITS to the end */
    . = ALIGN(8);
  }
  /DISCARD/ : { *(.comment) *(.note*) *(.eh_frame*) *(.gnu*) *(.got*) *(.plt*) }
}
