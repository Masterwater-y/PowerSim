import tempfile
import unittest
from pathlib import Path

from tools.drtrace.validation import (
    MatrixActionOptions,
    ValidationOptions,
    VALIDATION_MATRIX_PATH,
    convert_dr_fsts,
    convert_gem5_fsts,
    validate_dr_matrix,
    _load_matrix,
    _matrix_root,
    _selected_workloads,
    _workload_bin,
    _validate_dr_address_provenance,
    _validate_dr_trace_metadata,
    _validate_strict_fst_manifest,
)
import tools.drtrace.validation as validation
from tools.drtrace.dynamorio import UnsupportedConversion
from tools.drtrace.fst_compare import compare_fst_pairs
from tools.drtrace.projection import (
    DESTINATION_CLASS_MARKER,
    FEATURE_DESTINATION_CLASSES,
    FST_HEADER,
    FST_MAGIC,
    FST_RECORD,
    PHYSICAL_ADDRESS,
    VIRTUAL_PAGE_TOKEN,
    fst_info,
)
from tools.drtrace.replay_validation import (
    ReplayValidationOptions,
    _compare_replay_outputs,
    _resolved_replay_configs,
    validate_replay_matrix,
)


SYSCALL_OP_CLASS = -1


def write_fst(path: Path, core: int, records: list[tuple[int, ...]]) -> None:
    path.write_bytes(
        FST_HEADER.pack(
            FST_MAGIC, 6, FST_HEADER.size, FST_RECORD.size, core,
            len(records), FEATURE_DESTINATION_CLASSES, 0, 0, 0, 0,
        ) + b"".join(FST_RECORD.pack(*record) for record in records)
    )


def record(*, address: int, flags: int, page_token: int) -> tuple[int, ...]:
    return (
        0x400000, address, 0, 0, 0, 0, 0, 0, 1, flags, 0, 0,
        1, 0, 0, 0, 0, DESTINATION_CLASS_MARKER | page_token,
    )


def memory_record(
    *, address: int, page_token: int, pc: int = 0x400000
) -> tuple[int, ...]:
    values = list(record(
        address=address,
        flags=PHYSICAL_ADDRESS | VIRTUAL_PAGE_TOKEN | 2,
        page_token=page_token,
    ))
    values[0] = pc
    return tuple(values)


def record_at(pc: int) -> tuple[int, ...]:
    values = list(record(address=0, flags=0, page_token=0))
    values[0] = pc
    return tuple(values)


def syscall_record(number: int) -> tuple[int, ...]:
    values = list(record(address=number, flags=0, page_token=0))
    values[8] = 0
    values[10] = SYSCALL_OP_CLASS
    values[-1] = DESTINATION_CLASS_MARKER
    return tuple(values)


def write_manifest(root: Path, fst: Path) -> Path:
    manifest = root / "manifest.txt"
    manifest.write_text(f"0 fastsim-binary {fst.name}\n", encoding="utf-8")
    return manifest


class DrTraceFstCompareTest(unittest.TestCase):
    def test_missing_physical_address_flag_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gem5 = root / "gem5.fst"
            dr = root / "dr.fst"
            write_fst(gem5, 0, [memory_record(
                address=0x123456, page_token=12,
            )])
            write_fst(dr, 0, [record(
                address=0x7FFF123456, flags=VIRTUAL_PAGE_TOKEN | 2,
                page_token=99,
            )])
            self.assertEqual(fst_info(dr).records, 1)
            result = compare_fst_pairs([gem5], [dr])
            self.assertEqual(result["status"], "fail")
            self.assertIn(
                "flags",
                result["domains"]["core_reconstructable"]["field_counts"],
            )

    def test_different_page_and_token_identities_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            left = root / "left.fst"
            right = root / "right.fst"
            write_fst(left, 0, [
                memory_record(address=0x123456, page_token=12),
                memory_record(address=0x1234A0, page_token=12, pc=0x400004),
                memory_record(address=0x987010, page_token=13, pc=0x400008),
            ])
            write_fst(right, 0, [
                memory_record(address=0xABC456, page_token=99),
                memory_record(address=0xABC4A0, page_token=99, pc=0x400004),
                memory_record(address=0xDEF010, page_token=7, pc=0x400008),
            ])
            result = compare_fst_pairs([left], [right])
            self.assertEqual(result["status"], "pass")
            self.assertEqual(result["domains"]["core_reconstructable"]["status"], "pass")

    def test_page_reuse_difference_is_not_cross_producer_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            left = root / "left.fst"
            right = root / "right.fst"
            write_fst(left, 0, [
                memory_record(address=0x123010, page_token=1),
                memory_record(address=0x123020, page_token=1, pc=0x400004),
            ])
            write_fst(right, 0, [
                memory_record(address=0xABC010, page_token=1),
                memory_record(address=0xDEF020, page_token=1, pc=0x400004),
            ])
            result = compare_fst_pairs([left], [right])
            self.assertEqual(result["status"], "pass")

    def test_cache_line_reuse_difference_is_not_cross_producer_contract(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            left = root / "left.fst"
            right = root / "right.fst"
            write_fst(left, 0, [
                memory_record(address=0x123010, page_token=1),
                memory_record(address=0x123020, page_token=1, pc=0x400004),
            ])
            write_fst(right, 0, [
                memory_record(address=0xABC010, page_token=1),
                memory_record(address=0xABC060, page_token=1, pc=0x400004),
            ])
            result = compare_fst_pairs([left], [right])
            self.assertEqual(result["status"], "pass")

    def test_cache_line_offset_difference_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            left = root / "left.fst"
            right = root / "right.fst"
            write_fst(left, 0, [
                memory_record(address=0x123010, page_token=1),
            ])
            write_fst(right, 0, [
                memory_record(address=0xABC018, page_token=1),
            ])
            result = compare_fst_pairs([left], [right])
            self.assertEqual(result["status"], "fail")
            self.assertIn(
                "address_cache_line_offset",
                result["domains"]["core_reconstructable"]["field_counts"],
            )

    def test_virtual_token_reuse_difference_is_not_cross_producer_contract(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            left = root / "left.fst"
            right = root / "right.fst"
            write_fst(left, 0, [
                memory_record(address=0x123010, page_token=1),
                memory_record(address=0x123020, page_token=1, pc=0x400004),
            ])
            write_fst(right, 0, [
                memory_record(address=0xABC010, page_token=8),
                memory_record(address=0xABC020, page_token=9, pc=0x400004),
            ])
            result = compare_fst_pairs([left], [right])
            self.assertEqual(result["status"], "pass")

    def test_cross_core_page_ids_are_not_cross_producer_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gem5 = [root / "gem5-0.fst", root / "gem5-1.fst"]
            dr = [root / "dr-0.fst", root / "dr-1.fst"]
            write_fst(gem5[0], 0, [
                memory_record(address=0x123010, page_token=1),
            ])
            write_fst(gem5[1], 1, [
                memory_record(address=0x123020, page_token=1),
            ])
            write_fst(dr[0], 0, [
                memory_record(address=0xABC010, page_token=7),
            ])
            write_fst(dr[1], 1, [
                memory_record(address=0xDEF020, page_token=9),
            ])
            result = compare_fst_pairs(gem5, dr)
            self.assertEqual(result["status"], "pass")

    def test_functional_difference_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            left = root / "left.fst"
            right = root / "right.fst"
            write_fst(left, 0, [record(address=1, flags=0, page_token=1)])
            changed = list(record(address=2, flags=0, page_token=2))
            changed[8] = 2
            write_fst(right, 0, [tuple(changed)])
            self.assertEqual(compare_fst_pairs([left], [right])["status"], "fail")

    def test_syscall_number_remains_strict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            left = root / "left.fst"
            right = root / "right.fst"
            write_fst(left, 0, [syscall_record(202)])
            write_fst(right, 0, [syscall_record(231)])
            result = compare_fst_pairs([left], [right])
            self.assertEqual(result["status"], "fail")
            self.assertIn(
                "syscall_number",
                result["domains"]["core_reconstructable"]["field_counts"],
            )

    def test_syscall_address_is_not_memory_address(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            left = root / "left.fst"
            right = root / "right.fst"
            write_fst(left, 0, [syscall_record(0x100A)])
            write_fst(right, 0, [syscall_record(0x200A)])

            result = compare_fst_pairs([left], [right])

            self.assertEqual(result["status"], "fail")
            self.assertIn(
                "syscall_number",
                result["domains"]["core_reconstructable"]["field_counts"],
            )

    def test_syscall_manifest_rejects_memory_shape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fst = root / "core0.fst"
            syscall = list(syscall_record(202))
            syscall[8] = 8
            syscall[9] = PHYSICAL_ADDRESS | VIRTUAL_PAGE_TOKEN | 2
            syscall[-1] = DESTINATION_CLASS_MARKER | 1
            write_fst(fst, 0, [tuple(syscall)])

            with self.assertRaisesRegex(ValueError, "syscall carries memory flags"):
                _validate_strict_fst_manifest(write_manifest(root, fst))

    def test_memory_manifest_rejects_zero_physical_address(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fst = root / "core0.fst"
            write_fst(fst, 0, [memory_record(address=0, page_token=7)])

            with self.assertRaisesRegex(ValueError, "memory address is zero"):
                _validate_strict_fst_manifest(write_manifest(root, fst))

    def test_memory_manifest_rejects_missing_virtual_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fst = root / "core0.fst"
            record_values = list(memory_record(address=0x123456, page_token=7))
            record_values[9] &= ~VIRTUAL_PAGE_TOKEN
            record_values[-1] = DESTINATION_CLASS_MARKER
            write_fst(fst, 0, [tuple(record_values)])

            with self.assertRaisesRegex(ValueError, "memory lacks virtual-page token"):
                _validate_strict_fst_manifest(write_manifest(root, fst))

    def test_matching_roi_boundary_pcs_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gem5 = root / "gem5.fst"
            dr = root / "dr.fst"
            records = [record_at(0x4020BB), record_at(0x4021A9)]
            write_fst(gem5, 0, records)
            write_fst(dr, 0, records)

            result = compare_fst_pairs([gem5], [dr])

            self.assertEqual(result["status"], "pass")
            self.assertEqual(result["per_core"][0]["first_pc"], "0x4020bb")
            self.assertEqual(result["per_core"][0]["last_pc"], "0x4021a9")

    def test_roi_boundary_pc_difference_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gem5 = root / "gem5.fst"
            dr = root / "dr.fst"
            write_fst(gem5, 0, [record_at(0x4020BB), record_at(0x4021A9)])
            write_fst(dr, 0, [record_at(0x4020B6), record_at(0x4021A9)])

            result = compare_fst_pairs([gem5], [dr])

            self.assertEqual(result["status"], "fail")
            self.assertIn(
                "pc",
                result["domains"]["core_reconstructable"]["field_counts"],
            )

    def test_record_count_difference_is_structure_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            left = root / "left.fst"
            right = root / "right.fst"
            write_fst(left, 0, [record_at(0x400000), record_at(0x400004)])
            write_fst(right, 0, [record_at(0x400000)])

            result = compare_fst_pairs([left], [right])

            self.assertEqual(result["status"], "fail")
            self.assertIn(
                "record_count",
                result["domains"]["structure"]["field_counts"],
            )


class DrTraceMatrixTest(unittest.TestCase):
    def test_default_matrix_mirrors_yinhaolang_uarch_first(self) -> None:
        matrix = _load_matrix(VALIDATION_MATRIX_PATH)
        workloads = matrix["parsed_workloads"]
        self.assertEqual(matrix["schema"], "fastsim-dr-workload-matrix-v1")
        self.assertEqual(matrix["default_seed"], 0)
        self.assertEqual(matrix["default_cores"], 4)
        self.assertEqual(
            [workload.name for workload in workloads],
            [
                "v28_int_alu_dense",
                "v28_int_div_serial",
                "v28_fp_alu_dense",
                "v28_simd_sse_dense",
                "v28_cache_L1_mixed",
                "v28_cache_L2_mixed",
                "v28_memory_seq_moderate",
                "v28_memory_random_mlp",
                "v28_coh_readmostly_sparse",
                "v28_gofeed_base",
                "v28_pytorch_base",
                "v28_mysql_base",
            ],
        )
        self.assertEqual(
            {
                "v28_int_alu_dense": 7,
                "v28_int_div_serial": 41,
                "v28_fp_alu_dense": 23,
                "v28_simd_sse_dense": 23,
                "v28_cache_L1_mixed": 2,
                "v28_cache_L2_mixed": 2,
                "v28_memory_seq_moderate": 3,
                "v28_memory_random_mlp": 3,
                "v28_coh_readmostly_sparse": 2,
                "v28_gofeed_base": 2,
                "v28_pytorch_base": 2,
                "v28_mysql_base": 2,
            },
            {workload.name: workload.scale for workload in workloads},
        )

    def test_workload_filter_accepts_source_name_and_w_alias(self) -> None:
        matrix = _load_matrix(VALIDATION_MATRIX_PATH)
        selected = _selected_workloads(matrix, ["v28_int_alu_dense"])
        self.assertEqual([workload.name for workload in selected], [
            "v28_int_alu_dense"
        ])
        self.assertEqual([workload.scale for workload in selected], [7])
        alias = _selected_workloads(matrix, ["W_v28_int_alu_dense"])
        self.assertEqual([workload.name for workload in alias], ["v28_int_alu_dense"])

    def test_excitation_matrices_allow_family_binary_names(self) -> None:
        business = _load_matrix(
            Path("configs/workloads/business_excitation.json")
        )
        uarch = _load_matrix(Path("configs/workloads/uarch_excitation.json"))

        self.assertEqual(
            {workload.workload_dir for workload in business["parsed_workloads"]},
            {"business_excitation"},
        )
        self.assertEqual(
            {workload.workload_dir for workload in uarch["parsed_workloads"]},
            {"uarch_excitation"},
        )
        self.assertEqual(
            _workload_bin(business["parsed_workloads"][0], "gem5"),
            Path.cwd()
            / "workloads/business_excitation/bin/gem5/gofeed_fanout_wide",
        )
        self.assertEqual(
            _workload_bin(uarch["parsed_workloads"][0], "dr"),
            Path.cwd() / "workloads/uarch_excitation/bin/dynamoRIO/uarch_rob80",
        )
        self.assertEqual(
            business["parsed_workloads"][0].expected_profiles,
            ("core_width4", "rob96", "rob256", "iq32", "iq96"),
        )
        self.assertEqual(business["parsed_workloads"][0].domain, "business_core")
        self.assertIn("profiles", business)
        self.assertIn("common", business)
        self.assertEqual(uarch["parsed_workloads"][0].expected_profiles, ("rob96", "rob256"))
        self.assertEqual(uarch["parsed_workloads"][0].domain, "rob")

    def test_matrix_rejects_unknown_expected_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            matrix = root / "matrix.json"
            matrix.write_text(
                '{"schema":"test","workload_dir":"uarch_excitation",'
                '"profiles":[{"id":"baseline"}],"workloads":['
                '{"name":"uarch_rob80","group":"train","scale":1,'
                '"expected_profiles":["missing"]}]}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "unknown profile"):
                _load_matrix(matrix)

    def test_workload_matrices_do_not_duplicate_workload_names(self) -> None:
        matrices = [
            _load_matrix(VALIDATION_MATRIX_PATH),
            _load_matrix(Path("configs/workloads/business_excitation.json")),
            _load_matrix(Path("configs/workloads/uarch_excitation.json")),
        ]
        seen: set[str] = set()
        for matrix in matrices:
            for workload in matrix["parsed_workloads"]:
                normalized = workload.name.removeprefix("W_")
                self.assertNotIn(normalized, seen)
                seen.add(normalized)
        self.assertIn("v28_int_div_serial", seen)

    def test_gem5_domain_cfgs_use_canonical_directory(self) -> None:
        expected = {
            "v28_1-c04.cfg",
            "v28_1-time-epoch.cfg",
            "v28_1-c04-interval-bound.cfg",
            "v28_1-c04-interval-weave.cfg",
        }
        self.assertEqual(
            expected,
            {path.name for path in Path("configs/gem5").glob("*.cfg")},
        )
        for old_name in (
            "gem5-v28_1-c04.cfg",
            "gem5-v28_1-time-epoch.cfg",
            "gem5-v28_1-c04-interval-bound.cfg",
            "gem5-v28_1-c04-interval-weave.cfg",
        ):
            self.assertFalse(Path("configs", old_name).exists())

    def test_domain_config_directories_contain_only_cfg_files(self) -> None:
        for directory in (Path("configs/gem5"), Path("configs/dynamoRIO")):
            self.assertTrue(directory.is_dir())
            self.assertTrue(list(directory.iterdir()))
            self.assertTrue(
                all(path.is_file() and path.suffix == ".cfg" for path in directory.iterdir())
            )
        self.assertEqual(
            {
                "business_excitation.json",
                "uarch_excitation.json",
                "uarch_first.json",
            },
            {path.name for path in Path("configs/workloads").glob("*.json")},
        )

    def test_replay_config_fields_are_not_replay_defaults(self) -> None:
        matrix = _load_matrix(Path("configs/workloads/uarch_first.json"))
        self.assertEqual(
            matrix["gem5_fastsim_config"],
            "configs/gem5/v28_1-time-epoch.cfg",
        )
        self.assertEqual(
            matrix["dr_fastsim_config"],
            "configs/dynamoRIO/physical-v28_1-c04.cfg",
        )
        gem5_config, dr_config = _resolved_replay_configs(
            matrix,
            ReplayValidationOptions(
                fst_root=Path("tmp/dr-fst"),
                output_dir=Path("tmp/replay"),
            ),
        )
        self.assertTrue(str(gem5_config).endswith("configs/gem5/v28_1-c04.cfg"))
        self.assertTrue(str(dr_config).endswith("configs/dynamoRIO/physical-v28_1-c04.cfg"))

    def test_default_artifact_roots_are_partitioned_by_matrix(self) -> None:
        matrix = Path("configs/workloads/uarch_first.json")
        self.assertEqual(
            _matrix_root(Path("tmp/dr-traces"), matrix),
            Path("tmp/dr-traces/uarch_first"),
        )
        self.assertEqual(
            _matrix_root(Path("tmp/dr-fst"), matrix),
            Path("tmp/dr-fst/uarch_first"),
        )
        custom = Path("tmp/custom-fst")
        self.assertEqual(_matrix_root(custom, matrix), custom)

    def test_matrix_requires_explicit_positive_scale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            matrix = root / "matrix.json"
            matrix.write_text(
                '{"schema":"test","workloads":['
                '{"name":"v28_int_alu_dense","group":"train"}]}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "missing scale"):
                _load_matrix(matrix)
            matrix.write_text(
                '{"schema":"test","workloads":['
                '{"name":"v28_int_alu_dense","group":"train","scale":0}]}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "scale must be positive"):
                _load_matrix(matrix)

    def test_cli_scale_override_is_reported_per_workload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            matrix = root / "matrix.json"
            matrix.write_text(
                '{"schema":"test","default_cores":4,"default_seed":0,'
                '"workloads":['
                '{"name":"v28_int_alu_dense","group":"train","scale":5}]}',
                encoding="utf-8",
            )
            options = ValidationOptions(
                output_dir=root / "validation",
                matrix_path=matrix,
                fst_root=root / "fst",
                scale=7,
            )
            report = validate_dr_matrix(options=options)
            self.assertEqual(
                report["parameters"]["workload_scales"],
                {"v28_int_alu_dense": 7},
            )

    def test_unsupported_conversion_is_reported_separately(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            matrix = root / "matrix.json"
            matrix.write_text(
                '{"schema":"test","default_cores":4,'
                '"default_seed":0,"workloads":['
                '{"name":"v28_int_alu_dense","group":"train","scale":5}]}',
                encoding="utf-8",
            )
            trace_root = root / "traces"
            fst_root = root / "fst"
            options = MatrixActionOptions(
                matrix_path=matrix,
                trace_root=trace_root,
                fst_root=fst_root,
                force=True,
            )
            original_single = validation._single_dr_trace_dir
            original_convert = validation.convert_dr_trace

            def fake_single(path: Path) -> Path:
                return path / "drmemtrace.fake"

            def fake_convert(**_: object) -> list[Path]:
                raise UnsupportedConversion(
                    reason_code="dynamic_internal_microcode_control",
                    pc=0x402123,
                    reason=(
                        "dynamic internal microcode control flow cannot be "
                        "reconstructed from an architectural instruction trace"
                    ),
                )

            validation._single_dr_trace_dir = fake_single
            validation.convert_dr_trace = fake_convert
            try:
                report = convert_dr_fsts(options=options)
            finally:
                validation._single_dr_trace_dir = original_single
                validation.convert_dr_trace = original_convert

            self.assertEqual(report["status"], "needs_work")
            self.assertEqual(report["status_counts"], {"unsupported": 1})
            self.assertEqual(
                report["unsupported_cases"][0]["reason_code"],
                "dynamic_internal_microcode_control",
            )
            self.assertEqual(report["unsupported_cases"][0]["pc"], "0x402123")

    def test_gem5_raw_trace_is_removed_after_successful_fst_conversion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            matrix = root / "matrix.json"
            matrix.write_text(
                '{"schema":"test","default_cores":1,'
                '"default_seed":0,"workloads":['
                '{"name":"v28_int_alu_dense","group":"train","scale":1}]}',
                encoding="utf-8",
            )
            raw_dir = (
                root / "traces" / "c01" / "v28_int_alu_dense"
                / "gem5" / "tao_trace"
            )
            raw_dir.mkdir(parents=True)
            (raw_dir / "core.records.micro.jsonl").write_text("raw\n", encoding="utf-8")
            options = MatrixActionOptions(
                matrix_path=matrix,
                trace_root=root / "traces",
                fst_root=root / "fst",
                force=True,
                fastsim_binary=Path("fake-fastsim"),
            )
            original_fastsim = validation._fastsim_binary
            original_convert = validation._convert_gem5_records

            def fake_fastsim(_: Path | None = None) -> Path:
                return Path("fake-fastsim")

            def fake_convert(raw: Path, output: Path, cores: int, _: Path) -> list[Path]:
                self.assertEqual(raw, raw_dir)
                output.mkdir(parents=True)
                fst = output / "core0.fst"
                write_fst(fst, 0, [memory_record(address=0x123000, page_token=1)])
                (output / "manifest.txt").write_text(
                    "0 fastsim-binary core0.fst\n", encoding="utf-8"
                )
                return [fst]

            validation._fastsim_binary = fake_fastsim
            validation._convert_gem5_records = fake_convert
            try:
                report = convert_gem5_fsts(options=options)
            finally:
                validation._fastsim_binary = original_fastsim
                validation._convert_gem5_records = original_convert

            self.assertEqual(report["status"], "pass")
            self.assertFalse(raw_dir.exists())

    def test_gem5_raw_trace_is_kept_when_fst_conversion_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            matrix = root / "matrix.json"
            matrix.write_text(
                '{"schema":"test","default_cores":1,'
                '"default_seed":0,"workloads":['
                '{"name":"v28_int_alu_dense","group":"train","scale":1}]}',
                encoding="utf-8",
            )
            raw_dir = (
                root / "traces" / "c01" / "v28_int_alu_dense"
                / "gem5" / "tao_trace"
            )
            raw_dir.mkdir(parents=True)
            (raw_dir / "core.records.micro.jsonl").write_text("raw\n", encoding="utf-8")
            options = MatrixActionOptions(
                matrix_path=matrix,
                trace_root=root / "traces",
                fst_root=root / "fst",
                force=True,
                fastsim_binary=Path("fake-fastsim"),
            )
            original_fastsim = validation._fastsim_binary
            original_convert = validation._convert_gem5_records

            def fake_fastsim(_: Path | None = None) -> Path:
                return Path("fake-fastsim")

            def fake_convert(*_: object) -> list[Path]:
                raise RuntimeError("conversion failed")

            validation._fastsim_binary = fake_fastsim
            validation._convert_gem5_records = fake_convert
            try:
                report = convert_gem5_fsts(options=options)
            finally:
                validation._fastsim_binary = original_fastsim
                validation._convert_gem5_records = original_convert

            self.assertEqual(report["status"], "error")
            self.assertTrue(raw_dir.exists())


class DrTraceReplayValidationTest(unittest.TestCase):
    def test_replay_compare_requires_functional_counters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gem5 = root / "gem5.json"
            dr = root / "dr.json"
            gem5.write_text(
                '{"totals":{"records":10,"retired_uops":8,'
                '"retired_instructions":6,"memory_accesses":2,'
                '"mmio_escape_accesses":0,"unknown_addresses":0,'
                '"branches_without_outcome":0,"serializing_uops":1,'
                '"syscall_uops":1,"branches":3,"conditional_branches":2,'
                '"simulated_makespan_cycles":100}}',
                encoding="utf-8",
            )
            dr.write_text(
                '{"totals":{"records":10,"retired_uops":9,'
                '"retired_instructions":6,"memory_accesses":2,'
                '"mmio_escape_accesses":0,"unknown_addresses":0,'
                '"branches_without_outcome":0,"serializing_uops":1,'
                '"syscall_uops":1,"branches":3,"conditional_branches":2,'
                '"simulated_makespan_cycles":120}}',
                encoding="utf-8",
            )

            result = _compare_replay_outputs(gem5, dr)

            self.assertEqual(result["status"], "fail")
            self.assertEqual(
                result["exact_mismatches"][0]["field"],
                "totals.retired_uops",
            )
            self.assertEqual(
                result["topology_derived"]["simulated_makespan_cycles"]["delta"],
                20,
            )
            self.assertEqual(
                result["contract"]["diagnostic"],
                "address_topology_derived_totals",
            )

    def test_validate_replay_uses_selected_core_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            matrix = root / "matrix.json"
            matrix.write_text(
                '{"schema":"test","default_cores":4,'
                '"workloads":['
                '{"name":"v28_int_alu_dense","group":"train","scale":5}]}',
                encoding="utf-8",
            )
            source = root / "fst" / "c08" / "v28_int_alu_dense"
            dr = source / "dr"
            replay = source / "replay"
            dr.mkdir(parents=True)
            replay.mkdir()
            (dr / "trace.json").write_text(
                '{"strict_physical_address":true,'
                '"address_provenance":{"path":"address-provenance.json"}}',
                encoding="utf-8",
            )
            totals = (
                '{"totals":{"records":10,"retired_uops":8,'
                '"retired_instructions":6,"memory_accesses":2,'
                '"mmio_escape_accesses":0,"unknown_addresses":0,'
                '"branches_without_outcome":0,"serializing_uops":1,'
                '"syscall_uops":1,"branches":3,"conditional_branches":2}}'
            )
            (replay / "gem5.json").write_text(totals, encoding="utf-8")
            (replay / "dr.json").write_text(totals, encoding="utf-8")

            report = validate_replay_matrix(
                ReplayValidationOptions(
                    fst_root=root / "fst",
                    output_dir=root / "out",
                    matrix_path=matrix,
                    cores=8,
                )
            )

            self.assertEqual(report["cores"], 8)
            self.assertEqual(report["status"], "diagnostic_only")
            self.assertTrue(report["gem5_config"].endswith("configs/gem5/v28_1-c04.cfg"))
            self.assertTrue(report["dr_config"].endswith("configs/dynamoRIO/physical-v28_1-c04.cfg"))
            self.assertIn("/c08/v28_int_alu_dense", report["cases"][0]["source"])
            self.assertTrue(
                (
                    root / "out" / "c08" / "v28_int_alu_dense"
                    / "replay-comparison.json"
                ).is_file()
            )

    def test_replay_config_cli_override_wins_over_tool_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            matrix_path = root / "matrix.json"
            matrix_path.write_text(
                '{"schema":"test","default_cores":4,'
                '"workloads":['
                '{"name":"v28_int_alu_dense","group":"train","scale":5}]}',
                encoding="utf-8",
            )
            matrix = _load_matrix(matrix_path)

            gem5_config, dr_config = _resolved_replay_configs(
                matrix,
                ReplayValidationOptions(
                    fst_root=root / "fst",
                    output_dir=root / "out",
                    matrix_path=matrix_path,
                    config_path=Path("configs/gem5/v28_1-c04.cfg"),
                    dr_config_path=Path("configs/gem5/v28_1-c04.cfg"),
                ),
            )

            self.assertTrue(str(gem5_config).endswith("configs/gem5/v28_1-c04.cfg"))
            self.assertTrue(str(dr_config).endswith("configs/gem5/v28_1-c04.cfg"))

class DrTraceAddressProvenanceTest(unittest.TestCase):
    def test_dr_trace_metadata_requires_strict_physical_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "trace.json").write_text(
                '{"strict_physical_address":true,'
                '"address_provenance":{"path":"address-provenance.json"}}',
                encoding="utf-8",
            )

            _validate_dr_trace_metadata(root)

    def test_dr_trace_metadata_rejects_non_strict_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "trace.json").write_text(
                '{"strict_physical_address":false,'
                '"address_provenance":{"path":"address-provenance.json"}}',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "strict physical"):
                _validate_dr_trace_metadata(root)

    def test_dr_trace_metadata_requires_provenance_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "trace.json").write_text(
                '{"strict_physical_address":true}',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "address provenance"):
                _validate_dr_trace_metadata(root)

    def test_address_provenance_requires_one_pid_per_core(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "address-provenance.json"
            path.write_text(
                '{"schema":"fastsim-dr-address-provenance-v1","cores":['
                '{"core":0,"pid":123,"mappings":['
                '{"virtual_page":4,"physical_page":8,"token":1}]}'
                ']}',
                encoding="utf-8",
            )
            result = _validate_dr_address_provenance(root, 1)
            self.assertEqual(result["address_spaces"], 1)
            self.assertEqual(result["mappings"], 1)
            self.assertEqual(result["raw_pa_cross_run_comparison"], "not_comparable")

    def test_address_provenance_rejects_duplicate_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "address-provenance.json").write_text(
                '{"schema":"fastsim-dr-address-provenance-v1","cores":['
                '{"core":0,"pid":123,"mappings":['
                '{"virtual_page":4,"physical_page":8,"token":1},'
                '{"virtual_page":5,"physical_page":9,"token":1}]}'
                ']}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "non-bijective"):
                _validate_dr_address_provenance(root, 1)


if __name__ == "__main__":
    unittest.main()
