#!/usr/bin/env python3
"""Inventory verified guest scheduler metadata, refusing collector enablement.

Requires pyelftools. DWARF from an external debug image is accepted only with
the same GNU build ID. Function/data symbols are candidates, not a proof of an
ABI, task identity, or a committed FST boundary. A small exact-build machine
code audit is included for the supplied gem5 5.4.49 kernel; partial evidence
does not make a collector usable. Default exit status is 2 until collection
is implemented and verified. --inventory-only permits diagnostic inventory.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

from elftools.elf.elffile import ELFFile
from elftools.dwarf.descriptions import describe_form_class
from elftools.common.exceptions import ELFError


HOOKS = ("__schedule", "__switch_to_asm", "__switch_to", "finish_task_switch",
         "try_to_wake_up", "trace_sched_wakeup", "futex_wait_queue_me", "futex_wake",
         "__tracepoint_sched_switch", "__tracepoint_sched_wakeup",
         "__tracepoint_sched_wakeup_new", "current_task")
MEMBERS = {"task_struct": ("state", "pid", "tgid", "mm", "start_boottime"),
           "mm_struct": ("pgd",)}
AUDITED_SHA256 = "da3d5e7dbf696b977f635d1123e06b324af873183d90111101ced005dc64e174"
AUDITED_BUILD_ID = "69d094cffd9ca1e8754d0ddf82836073ddc0d96f"


def build_id(elf):
    identifiers = set()
    for section in elf.iter_sections():
        if section["sh_type"] != "SHT_NOTE":
            continue
        for note in section.iter_notes():
            if note["n_type"] == "NT_GNU_BUILD_ID" and note["n_name"] == "GNU":
                value = note["n_desc"]
                identifiers.add(value if isinstance(value, str) else value.hex())
    if len(identifiers) != 1:
        return None
    return identifiers.pop()


def type_size(die, seen=None):
    seen = set() if seen is None else seen
    if die.offset in seen:
        return None
    seen.add(die.offset)
    if "DW_AT_byte_size" in die.attributes:
        return int(die.attributes["DW_AT_byte_size"].value)
    if die.tag == "DW_TAG_pointer_type":
        return die.cu["address_size"]
    if "DW_AT_type" in die.attributes:
        return type_size(die.get_DIE_from_attribute("DW_AT_type"), seen)
    return None


def dwarf_types(elf, rejections):
    if elf.get_section_by_name(".debug_info") is None:
        rejections.append("missing_dwarf_type_information")
        return {}
    dwarf = elf.get_dwarf_info()
    types = {}
    for cu in dwarf.iter_CUs():
        for die in cu.iter_DIEs():
            if die.tag != "DW_TAG_structure_type" or "DW_AT_name" not in die.attributes:
                continue
            name = die.attributes["DW_AT_name"].value.decode("utf-8")
            if name not in MEMBERS or "DW_AT_byte_size" not in die.attributes:
                continue
            size = int(die.attributes["DW_AT_byte_size"].value)
            members = {}
            for member in die.iter_children():
                if member.tag != "DW_TAG_member" or "DW_AT_name" not in member.attributes:
                    continue
                key = member.attributes["DW_AT_name"].value.decode("utf-8")
                if key not in MEMBERS[name]:
                    continue
                location = member.attributes.get("DW_AT_data_member_location")
                if location is None or describe_form_class(location.form) != "constant":
                    rejections.append(f"nonconstant_member_location:{name}.{key}")
                    continue
                if "DW_AT_type" not in member.attributes or "DW_AT_bit_size" in member.attributes:
                    rejections.append(f"unsupported_member_type:{name}.{key}")
                    continue
                width = type_size(member.get_DIE_from_attribute("DW_AT_type"))
                offset = int(location.value)
                if width is None or width < 1 or offset < 0 or offset + width > size:
                    rejections.append(f"invalid_member_extent:{name}.{key}")
                    continue
                members[key] = {"offset_bytes": offset, "size_bytes": width,
                                "evidence": "dwarf_constant_member_location"}
            record = {"size_bytes": size, "members": members}
            if name in types and types[name] != record:
                rejections.append(f"conflicting_type_definitions:{name}")
                types[name] = {"size_bytes": None, "members": {}}
            else:
                types[name] = record
    return types


def audited_machine_code(kernel, elf):
    """Return only manually audited partial evidence for this exact ELF.

    These offsets were checked in independent scheduler switch and wakeup
    trace writers, plus state access in __schedule and its state translator.
    There is no generalized pattern inference or fallback to these offsets.
    """
    if kernel["sha256"] != AUDITED_SHA256 or kernel["build_id"] != AUDITED_BUILD_ID:
        return {"status": "unrecognized_build", "members": {}, "boundaries": {}}
    # Exact instruction bytes, not a decoder pattern that could match an
    # unrelated load. The ELF SHA/build guard also binds surrounding control flow.
    definitions = (
        ("prev_pid_load", 0xffffffff8106874d, "418b842458070000"),
        ("next_pid_load", 0xffffffff8106878f, "418b8658070000"),
        ("wake_pid_load", 0xffffffff81067110, "8b9358070000"),
        ("schedule_state_load", 0xffffffff81517803, "488b4310"),
        ("trace_state_load", 0xffffffff8106869f, "488b4f10"),
        ("current_task_gs_load", 0xffffffff810597f1, "65488b3c25005d0100"),
        ("preempt_saved_r15d", 0xffffffff81517757, "440fb6ff"),
        ("switch_arguments_and_call", 0xffffffff81517aef, "4c89ee4889df498b85d80f00008a00e86d860e00"),
        ("switch_entry", 0xffffffff81600170, "55534154415541564157"),
        ("wakeup_entry", 0xffffffff810683b3, "8b050f6bec00"),
        ("self_wake_state_zero_and_call", 0xffffffff81069aef, "49c74710000000004c89ffe8b4e8ffff"),
        ("queued_wake_state_zero_and_call", 0xffffffff8106911f, "49c7442410000000004c89e7e883f2ffff"),
        ("schedule_entry", 0xffffffff81517740, "554889e5"),
        ("dequeue_sleep_call_and_return", 0xffffffff81517841, "ba090000004889de4c89e7e85717b5fff6831807000002"),
        ("futex_plist_enqueue_and_task", 0xffffffff810a0215, "e84177460048896b28"),
        ("futex_selected_unqueue", 0xffffffff8109f973, "4889f7e888ffffff48c7433000000000"),
        ("futex_selected_task", 0xffffffff8109f943, "488b6e28"),
    )
    proofs = []
    for name, pc, expected in definitions:
        raw = None
        size = len(bytes.fromhex(expected))
        for segment in elf.iter_segments():
            if segment["p_type"] == "PT_LOAD" and segment["p_vaddr"] <= pc and pc + size <= segment["p_vaddr"] + segment["p_filesz"]:
                start = pc - segment["p_vaddr"]
                raw = segment.data()[start:start + size]
                break
        proofs.append({"name": name, "pc": hex(pc), "expected_bytes": expected,
                       "bytes_verified": raw == bytes.fromhex(expected)})
    symtab = elf.get_section_by_name(".symtab")
    symbols = {s.name: s["st_value"] for s in symtab.iter_symbols()} if symtab else {}
    symbols_match = all(symbols.get(name) == pc for name, pc in (
        ("__switch_to_asm", 0xffffffff81600170),
        ("trace_sched_wakeup", 0xffffffff810683b3), ("current_task", 0x15d00)))
    if not symbols_match or not all(p["bytes_verified"] for p in proofs):
        return {"status": "exact_build_proof_failed", "members": {}, "boundaries": {}, "proofs": proofs}
    return {
        "status": "partial_exact_build_audit",
        "current_task_gs_displacement": 0x15d00,
        "proofs": proofs,
        "members": {
            "task_struct.pid": {"offset_bytes": 0x758, "size_bytes": 4,
                "evidence_pcs": ["0xffffffff8106874d", "0xffffffff8106878f", "0xffffffff81067110"]},
            "task_struct.state": {"offset_bytes": 0x10, "size_bytes": 8,
                "evidence_pcs": ["0xffffffff81517803", "0xffffffff8106869f"]},
        },
        "boundaries": {
            "schedule_entry": {"pc": "0xffffffff81517740", "stage": "invocation_reset"},
            "schedule_state": {"pc": "0xffffffff81517803", "task_pointer_register": "rbx", "state_register": "rax", "stage": "after_load"},
            "dequeue_sleep": {"pc": "0xffffffff81517851", "task_pointer_register": "rbx", "flags": 9, "stage": "after_deactivate_task_return"},
            "futex_enqueue": {"pc": "0xffffffff810a021a", "queue_pointer_register": "rbx", "task_pointer_register": "rbp", "stage": "after_plist_add_and_task_store"},
            "futex_selected": {"pc": "0xffffffff8109f97b", "queue_pointer_register": "rbx", "task_pointer_register": "rbp", "stage": "after_unqueue_and_lock_ptr_clear"},
            "switch": {"pc": "0xffffffff81600170", "symbol": "__switch_to_asm",
                "prev_pointer_register": "rdi", "next_pointer_register": "rsi",
                "preempt_register": "r15d", "stage": "assembly_entry_before_stack_switch",
                "evidence_pcs": ["0xffffffff81517757", "0xffffffff81517aef", "0xffffffff81517af2", "0xffffffff81517afe"]},
            "wakeup": {"pc": "0xffffffff810683b3", "symbol": "trace_sched_wakeup",
                "target_pointer_register": "rdi", "stage": "wakeup_trace_function_entry"},
        },
        "limitations": ["tgid_not_verified", "task_lifetime_not_verified",
            "address_space_identity_not_verified", "committed_boundary_not_verified",
            "wake_cause_not_verified", "state_at_switch_hook_can_race_wakeup",
            "switch_boundary_is_not_perf_scheduler_tracepoint", "cpu_irq_unhalted_scope_not_verified"],
    }


def generate(kernel_path, debug_path=None):
    rejections = []
    kernel_bytes = kernel_path.read_bytes()
    with kernel_path.open("rb") as stream:
        elf = ELFFile(stream)
        identity = {"path": str(kernel_path), "sha256": hashlib.sha256(kernel_bytes).hexdigest(),
                    "build_id": build_id(elf), "machine": elf["e_machine"],
                    "elf_class": elf.elfclass, "little_endian": elf.little_endian}
        if elf["e_machine"] != "EM_X86_64" or elf.elfclass != 64 or not elf.little_endian:
            rejections.append("unsupported_kernel_architecture")
        if elf["e_type"] != "ET_EXEC":
            rejections.append("kernel_must_be_nonrelocatable_elf_executable")
        if identity["build_id"] is None:
            rejections.append("missing_or_ambiguous_kernel_build_id")
        hooks = {}
        symtab = elf.get_section_by_name(".symtab")
        if symtab is not None:
            for symbol in symtab.iter_symbols():
                if symbol.name not in HOOKS or symbol["st_shndx"] == "SHN_UNDEF":
                    continue
                if symbol.name == "current_task":
                    classification = "per_cpu_data_candidate"
                elif symbol.name.startswith("__tracepoint_"):
                    classification = "data_tracepoint_not_executable"
                else:
                    classification = "function_symbol_candidate"
                    section = elf.get_section(symbol["st_shndx"]) if isinstance(symbol["st_shndx"], int) else None
                    if symbol["st_info"]["type"] != "STT_FUNC" or section is None or not section["sh_flags"] & 4:
                        rejections.append(f"not_executable_function:{symbol.name}")
                hooks[symbol.name] = {"address": hex(symbol["st_value"]),
                                      "size_bytes": symbol["st_size"], "classification": classification}
        for hook in HOOKS:
            if hook not in hooks:
                rejections.append(f"missing_symbol:{hook}")
        if debug_path:
            with debug_path.open("rb") as debug_stream:
                debug = ELFFile(debug_stream)
                if not identity["build_id"] or build_id(debug) != identity["build_id"]:
                    rejections.append("debug_image_build_id_mismatch")
                    types = {}
                elif (debug["e_machine"], debug.elfclass, debug.little_endian) != (elf["e_machine"], elf.elfclass, elf.little_endian):
                    rejections.append("debug_image_architecture_mismatch")
                    types = {}
                else:
                    types = dwarf_types(debug, rejections)
        else:
            types = dwarf_types(elf, rejections)
        machine_evidence = audited_machine_code(identity, elf)
    for name, members in MEMBERS.items():
        for member in members:
            if member not in types.get(name, {}).get("members", {}):
                rejections.append(f"missing_member:{name}.{member}")
    metadata_status = "unavailable" if rejections else "verified"
    rejections.append("committed_scheduler_collector_unimplemented")
    return {"schema": "fastsim-guest-scheduler-profile-v1", "kernel": identity,
            "debug_image": str(debug_path) if debug_path else None,
            "metadata_status": metadata_status, "collector_status": "unimplemented",
            "can_emit_state_facts": False, "hooks": hooks, "types": types,
            "machine_code_evidence": machine_evidence,
            "rejections": sorted(set(rejections))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kernel", type=Path, required=True)
    parser.add_argument("--debug-image", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--inventory-only", action="store_true",
                        help="write diagnostic metadata without authorizing collection")
    args = parser.parse_args()
    try:
        profile = generate(args.kernel.resolve(), args.debug_image.resolve() if args.debug_image else None)
    except (OSError, ValueError, ELFError) as error:
        parser.exit(2, f"scheduler profile rejected: {error}\n")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(profile, indent=2, sort_keys=True) + "\n")
    identity_bad = any(reason in profile["rejections"] for reason in (
        "unsupported_kernel_architecture", "kernel_must_be_nonrelocatable_elf_executable",
        "missing_or_ambiguous_kernel_build_id", "debug_image_build_id_mismatch",
        "debug_image_architecture_mismatch"))
    if identity_bad or not args.inventory_only:
        print("scheduler collection unavailable: " + "; ".join(profile["rejections"]), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
