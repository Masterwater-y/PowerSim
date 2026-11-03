/* Architectural data check for all gem5 cacheable NT-store fallbacks. */
.section .data
.balign 64
pattern: .quad 0x1122334455667788, 0x8877665544332211
.balign 64
output: .zero 64
output_p: .zero 64
expected:
    .quad 0x1122334455667788, 0x8877665544332211
    .quad 0x1122334455667788, 0x8877665544332211
    .quad 0x1122334455667788, 0x8877665544332211
    .quad 0x1122334455667788, 0x1122334455667788
.section .text
.global _start
_start:
    movabs $0x1122334455667788, %rax
    movdqa pattern(%rip), %xmm0
    movq %rax, %mm0
    lea output(%rip), %rdi
#ifdef FAULT_PROBE
    xor %edi, %edi
#endif
    mov $2048, %ecx
1:
    movntdq %xmm0, 0(%rdi)
    movntps %xmm0, 16(%rdi)
    movntpd %xmm0, 32(%rdi)
    movnti %rax, 48(%rdi)
    movntq %mm0, 56(%rdi)
    /* Exercise the RIP-relative decoder forms as well. */
    movntdq %xmm0, output_p(%rip)
    movntps %xmm0, output_p+16(%rip)
    movntpd %xmm0, output_p+32(%rip)
    movnti %rax, output_p+48(%rip)
    movntq %mm0, output_p+56(%rip)
    sfence
    dec %ecx
    jne 1b
    emms
    mov $2, %r8d
5:
    lea expected(%rip), %rsi
    mov $8, %ecx
2:
    mov (%rsi), %rdx
    cmp %rdx, (%rdi)
    jne 3f
    add $8, %rdi
    add $8, %rsi
    dec %ecx
    jne 2b
    dec %r8d
    jne 5b
    xor %edi, %edi
    jmp 4f
3:
    mov $1, %edi
4:
    mov $60, %eax
    syscall
.section .note.GNU-stack,"",@progbits
