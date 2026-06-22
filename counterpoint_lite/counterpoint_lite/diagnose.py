from collections import defaultdict

from .dsl import counter_component_hints, dump_json


PREFIX_HINTS = [
    ("branch.", "frontend_branch"),
    ("frontend.", "frontend"),
    ("core.", "backend_core"),
    ("cache.l1", "l1_l2_cache"),
    ("cache.l2", "l1_l2_cache"),
    ("cache.llc", "llc_cha"),
    ("uncore_cha", "llc_cha"),
    ("uncore_imc", "memory"),
    ("memory.", "memory"),
    ("tlb.", "mmu_tlb"),
    ("dtlb", "mmu_tlb"),
]


def diagnose(report, model=None, signatures=None):
    hints = counter_component_hints(model or {})
    sig_counter_components = _signature_counter_components(signatures) if signatures else {}
    scores = defaultdict(float)
    evidence = defaultdict(list)

    for row in report.get("counters", []):
        norm = abs(float(row.get("normalized_violation", 0.0)))
        if row.get("status") == "inside" or norm <= 1e-9:
            continue
        counter = row["name"]
        component = hints.get(counter) or sig_counter_components.get(counter) or infer_component(counter)
        scores[component] += norm
        evidence[component].append(f"{counter} {row['status']} normalized={row.get('normalized_violation')}")
        row["component_hint"] = component

    ranked = []
    total = sum(scores.values()) or 1.0
    for component, score in sorted(scores.items(), key=lambda kv: kv[1], reverse=True):
        ranked.append({
            "component": component,
            "confidence": min(0.99, score / total if total else 0.0),
            "score": score,
            "reason": _reason(component),
            "evidence": evidence[component],
            "suggested_model_updates": _suggestions(component),
        })
    return {
        "schema_version": "0.1",
        "verdict": report.get("verdict", "unknown"),
        "ranked_components": ranked,
    }


def save_diagnosis(obj, path):
    dump_json(obj, path)


def infer_component(counter):
    for prefix, component in PREFIX_HINTS:
        if counter.startswith(prefix):
            return component
    return "unknown"


def _signature_counter_components(signatures):
    by_counter = {}
    for sig in signatures.get("signatures", []):
        comp = sig.get("component", "unknown")
        for counter, value in sig.get("vector", {}).items():
            if abs(float(value)) > 0:
                by_counter.setdefault(counter, comp)
    return by_counter


def _reason(component):
    return {
        "frontend_branch": "分支/前端相关 counter 无法被当前模型锥解释，可能是分支预测、取指或译码假设不完整。",
        "backend_core": "核心 retire/cycle/uop 约束不一致，可能是执行宽度、OOO/停顿或归一化假设有问题。",
        "l1_l2_cache": "私有 cache 层级 counter 不一致，可能是 L1/L2 命中、写回或预取路径缺失。",
        "llc_cha": "LLC/CHA/directory/snoop counter 不一致，可能是 LLC/目录/一致性流量建模缺失或 socket 聚合错误。",
        "memory": "内存控制器或 DRAM 流量 counter 不一致，可能是内存访问签名、带宽/延迟或 uncore 归一化问题。",
        "mmu_tlb": "TLB/page-walk counter 不一致，可能是 TLB 层级、page walk、prefetch/merge/replay 行为缺失。",
    }.get(component, "该 counter 组无法被当前签名组合解释，需要检查 counter 语义映射和模型规则。")


def _suggestions(component):
    return {
        "frontend_branch": ["增加 branch_miss / fetch_bubble / decode_limit 签名。", "检查仿真器分支统计是否与 PMU branch event 语义一致。"],
        "backend_core": ["增加 backend_stall / memory_stall / issue_width_bound 签名。", "检查 instructions、uops、cycles 的归一化窗口。"],
        "l1_l2_cache": ["拆分 load/store、hit/miss/writeback/prefetch 路径。", "核对 gem5/Sniper cache stats 到 PMU event 的映射。"],
        "llc_cha": ["增加 remote_snoop / directory_lookup / LLC victim / coherence retry 签名。", "检查 socket/CHA 聚合以及 uncore counter 是否按实例求和。"],
        "memory": ["增加 DRAM read/write、LLC miss service、writeback 路径。", "检查 IMC/CHA 事件是否和 core 观测来自同一时间窗口。"],
        "mmu_tlb": ["增加 early PSC lookup、page-walk merging、abort/replay、TLB prefetch 签名。", "按页面大小拆分 DTLB/STLB/walk counter。"],
    }.get(component, ["增加能解释该 counter 的 μpath signature。", "检查 observation adapter 的 alias/mapping。"])
