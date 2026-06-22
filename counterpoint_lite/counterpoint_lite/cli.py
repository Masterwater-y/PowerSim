import argparse
import json
import os
from pathlib import Path

from .diagnose import diagnose, save_diagnosis
from .dsl import dump_json, load_json, load_model
from .feasibility import check_feasible
from .machine import validate_machine
from .observations import load_observation, save_observation, write_violations_csv
from .signatures import enumerate_signatures, load_signatures, save_signatures_csv, save_signatures_json


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PMU_JSON = "/data00/yinhaolang/simulators/PMU/SPR/events/sapphirerapids_core.json"
DEFAULT_UNCORE_ALIAS = "/data00/yinhaolang/simulators/uncore_msr/docs/PMU/uncore_aliases/Intel/SPR/aliases.tsv"
DEFAULT_SPR_DISCOVERY = "/data00/yinhaolang/simulators/uncore_msr/scripts/spr_discovery.py"


def main(argv=None):
    parser = argparse.ArgumentParser(prog="counterpoint-lite", description="CounterPoint-style PMU cone validation for simulators")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("validate-machine", help="validate this host microarchitecture and PMU files")
    p.add_argument("--target", default="spr")
    p.add_argument("--pmu-json", default=DEFAULT_PMU_JSON)
    p.add_argument("--uncore-alias", default=DEFAULT_UNCORE_ALIAS)
    p.add_argument("--spr-discovery", default=DEFAULT_SPR_DISCOVERY)
    p.add_argument("--output")
    p.set_defaults(func=cmd_validate_machine)

    p = sub.add_parser("enumerate", help="enumerate model counter signatures")
    p.add_argument("--model", required=True)
    p.add_argument("--output-json", required=True)
    p.add_argument("--output-csv")
    p.set_defaults(func=cmd_enumerate)

    p = sub.add_parser("gen-minesim-model", help="generate a CounterPoint model from a MineSim config file")
    p.add_argument("--config", required=True)
    p.add_argument("--name", default="minesim_config_cone")
    p.add_argument("--output", required=True)
    p.set_defaults(func=cmd_gen_minesim_model)

    p = sub.add_parser("import-observation", help="convert PMU/simulator output into observation JSON")
    p.add_argument("--kind", required=True, choices=["pmu-csv", "perf-stat", "gem5-stats", "sniper-sqlite", "minesim-stats"])
    p.add_argument("--input", required=True)
    p.add_argument("--events", help="comma-separated event/stat columns for pmu-csv")
    p.add_argument("--mapping", help="JSON mapping file; may contain top-level kind keys")
    p.add_argument("--aggregation", default="sum", choices=["sum", "mean"])
    p.add_argument("--ci-scale", type=float, default=3.0)
    p.add_argument("--min-relative-ci", type=float, default=0.01)
    p.add_argument("--min-absolute-ci", type=float, default=1.0)
    p.add_argument("--output", required=True)
    p.set_defaults(func=cmd_import_observation)

    p = sub.add_parser("check", help="run model-cone feasibility check")
    p.add_argument("--signatures", required=True)
    p.add_argument("--observation", required=True)
    p.add_argument("--algorithm", default="auto", choices=["auto", "scipy-linprog", "projected-hinge-nnls"])
    p.add_argument("--tolerance", type=float, default=1e-6)
    p.add_argument("--output", required=True)
    p.add_argument("--violations-csv")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("diagnose", help="map violations to simulator/microarchitecture components")
    p.add_argument("--report", required=True)
    p.add_argument("--model")
    p.add_argument("--signatures")
    p.add_argument("--output", required=True)
    p.set_defaults(func=cmd_diagnose)

    p = sub.add_parser("run", help="end-to-end enumerate/check/diagnose")
    p.add_argument("--model", required=True)
    p.add_argument("--observation", required=True)
    p.add_argument("--output-dir", required=True)
    p.add_argument("--algorithm", default="auto", choices=["auto", "scipy-linprog", "projected-hinge-nnls"])
    p.add_argument("--tolerance", type=float, default=1e-6)
    p.set_defaults(func=cmd_run)

    args = parser.parse_args(argv)
    return args.func(args)


def cmd_validate_machine(args):
    obj = validate_machine(args.target, args.pmu_json, args.uncore_alias, args.spr_discovery)
    _write_or_print(obj, args.output)
    return 0 if obj.get("ok") else 2


def cmd_enumerate(args):
    model = load_model(args.model)
    sigs = enumerate_signatures(model)
    save_signatures_json(sigs, args.output_json)
    if args.output_csv:
        save_signatures_csv(sigs, args.output_csv)
    print(f"enumerated {len(sigs['signatures'])} signatures for {len(sigs['counters'])} counters")
    return 0


def cmd_gen_minesim_model(args):
    from .minesim_model import generate_model_from_minesim_cfg, save_generated_model
    model = generate_model_from_minesim_cfg(args.config, name=args.name)
    save_generated_model(model, args.output)
    print(f"generated MineSim config model with {len(model['rules'])} rules into {args.output}")
    return 0


def cmd_import_observation(args):
    mapping = _load_mapping(args.mapping, args.kind)
    ci_kwargs = {"ci_scale": args.ci_scale, "min_relative_ci": args.min_relative_ci, "min_absolute_ci": args.min_absolute_ci}
    if args.kind == "pmu-csv":
        from .adapters.pmu_csv import import_pmu_csv
        obs = import_pmu_csv(args.input, events=args.events, aliases=mapping, aggregation=args.aggregation, **ci_kwargs)
    elif args.kind == "perf-stat":
        from .adapters.perf_stat import import_perf_stat
        obs = import_perf_stat(args.input, aliases=mapping, **ci_kwargs)
    elif args.kind == "gem5-stats":
        from .adapters.gem5_stats import import_gem5_stats
        obs = import_gem5_stats(args.input, mapping=mapping, **ci_kwargs)
    elif args.kind == "sniper-sqlite":
        from .adapters.sniper_stats import import_sniper_sqlite
        obs = import_sniper_sqlite(args.input, mapping=mapping, **ci_kwargs)
    elif args.kind == "minesim-stats":
        from .adapters.minesim_stats import import_key_value_text
        obs = import_key_value_text(args.input, mapping=mapping, **ci_kwargs)
    else:
        raise AssertionError(args.kind)
    save_observation(obs, args.output)
    print(f"imported {len(obs['counters'])} counters into {args.output}")
    return 0


def cmd_check(args):
    sigs = load_signatures(args.signatures)
    obs = load_observation(args.observation)
    report = check_feasible(sigs, obs, tolerance=args.tolerance, algorithm=args.algorithm)
    dump_json(report, args.output)
    if args.violations_csv:
        write_violations_csv(report, args.violations_csv)
    print(f"verdict={report['verdict']} max_norm={report['objective']['max_normalized_violation']:.6g}")
    return 0 if report["verdict"] == "feasible" else 1


def cmd_diagnose(args):
    report = load_json(args.report)
    model = load_model(args.model) if args.model else None
    sigs = load_signatures(args.signatures) if args.signatures else None
    obj = diagnose(report, model=model, signatures=sigs)
    save_diagnosis(obj, args.output)
    print(f"diagnosed {len(obj['ranked_components'])} suspect components")
    return 0


def cmd_run(args):
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    model = load_model(args.model)
    sigs = enumerate_signatures(model)
    sig_json = out / "signatures.json"
    sig_csv = out / "signatures.csv"
    report_json = out / "report.json"
    violations_csv = out / "violations.csv"
    diagnosis_json = out / "diagnosis.json"
    save_signatures_json(sigs, sig_json)
    save_signatures_csv(sigs, sig_csv)
    obs = load_observation(args.observation)
    report = check_feasible(sigs, obs, tolerance=args.tolerance, algorithm=args.algorithm)
    dump_json(report, report_json)
    write_violations_csv(report, violations_csv)
    diag = diagnose(report, model=model, signatures=sigs)
    save_diagnosis(diag, diagnosis_json)
    print(json.dumps({"verdict": report["verdict"], "output_dir": str(out), "suspects": diag["ranked_components"][:3]}, ensure_ascii=False, indent=2))
    return 0 if report["verdict"] == "feasible" else 1


def _load_mapping(path, kind):
    if not path:
        return {}
    obj = load_json(path)
    return obj.get(kind, obj)


def _write_or_print(obj, output):
    if output:
        dump_json(obj, output)
    else:
        print(json.dumps(obj, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    raise SystemExit(main())
