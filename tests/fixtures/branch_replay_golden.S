/* Minimal x86 SE workload for gem5 Tournament BPU consistency checks. */
.global _start
.text
_start:
    xorl %r12d, %r12d
.Lloop:
    incl %r12d
    mfence
    cmpl $8, %r12d
    jl .Lloop

    mfence
    call replay_function
    mfence
    leaq .Lindirect_target(%rip), %rax
    jmp *%rax

    /* Keep the indirect target non-sequential without adding wrong-path
       control flow that would complicate the committed golden stream. */
    .rept 64
    nop
    .endr

.Lindirect_target:
    mfence
    movl $60, %eax
    xorl %edi, %edi
    syscall

replay_function:
    mfence
    xorl %edx, %edx
    testl %edx, %edx
    jne .Lconditional_target
    .rept 16
    nop
    .endr
.Lconditional_target:
    mfence
    ret
