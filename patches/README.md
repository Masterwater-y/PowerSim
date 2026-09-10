# Canonical TaoTrace gem5 patch

`gem5-taotrace-fastsim.patch` is the only maintained gem5 producer patch. It
applies to upstream gem5 commit
`c8222cc67a399bfc01e8658dd14b30d5bfd634f9` and materializes the complete
TaoTrace state used by this repository.

The patch includes:

- user-only and opt-in mixed-CPL FST v7 production;
- syscall, privilege, PTE, virtual-page and address-space evidence;
- ASID-scoped `.fst.imap` static geometry and architectural operand masks;
- native Ruby lifecycle/hierarchy accounting and bounded online summaries;
- exact-window frontend and retired branch-prediction diagnostics.

The `.imap` extension is a local implementation of the future
address-space-scoped direction identified by `origin/FastSim@cf346fd`; that
remote revision still used a PC-only reader. The FST v7 hot record remains
unchanged.

Apply and build through the maintained entrypoint:

```bash
python -m tools.taotrace_fst.build --apply --jobs 16
```

`--apply` requires a clean checkout at the exact base commit. Without
`--apply`, the command requires the target tree to match the canonical patch
exactly and only performs the build. The build uses
`tools/taotrace_fst/build_opts/X86_TAOTRACE_FST`, the workspace Python 3.11
toolchain and GCC 11 RUNPATH.

TCSim consumer/plumbing patches are not part of the current QEMU-FST
comparison workflow. TaoTrace reference collection is driven by
`tools.taotrace_fst.collect`.
