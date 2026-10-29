/* Cold NT stream: CDA; stfp; stfp. No initialization stores touch the region.
 * SINGLE provides the empty-SQ scalar demand control. No libc/startup traffic.
 */
.global _start
.text
_start:
    lea region(%rip), %rdi
#ifdef SINGLE
    movq $1, (%rdi)
#else
    pxor %xmm0, %xmm0
    mov $256, %ecx
1:
    movntdq %xmm0, (%rdi)
    add $16, %rdi
    dec %ecx
    jnz 1b
#endif
    sfence
    mov $60, %eax
    xor %edi, %edi
    syscall
/* Materialized data avoids gem5 ELF-loader rejection of an empty segment. */
.data
.balign 4096
region:
    .zero 16384
.section .note.GNU-stack,"",@progbits
