# 新 FLAC C4：STLB 功能 MMU/PCID 输入现状

源：`tmp/spr-functional-10m-c4-20261105/source/c04-817.flac_s`，其既有
`spr-capture-preparation.json` 已验证控制文件结构和功能翻译 transport；这不是
OoO 时间或 cache 命中输入，也不是新增采集。

逐核用现有 `mmuctl_v2::decode_header/decode_baseline` 和
`archb::decode_header/decode_epoch` 解码并比对 policy、context、generation、known、
proof 及完整状态：四核均 `known=15`、`proof_flags=15`、`generation=2`、
`cut_kind=1`；在**采集起点**的 CR4，core0 为 1776、core1–3 为 1760；均有
bit17 `CR4.PCIDE=0`。结果 `tmp/spr-stlb-mmu-epoch.jsonl`。初始 CR3（十进制）
分别 34854453248/34810421248/34810421248/34810421248。这里是 warmup
开始的 armed epoch，不是已经证明的全测量窗口连续状态。

以现有 v2 wire decoder 顺序扫描 4 个 `.mmuctl.gz` 所有 owner 的写/命令帧：
共有3条控制寄存器写，均 core0 `reg=Cr3`、source ordinal 为
130176/140045/202248（该核 warmup UOP cutoff=841026），未观察到已记录
CR4 写；未绑定命令 core0–3 分别 66/15/24/16，共121，另有 incomplete/
unsupported 证据，既有控制结果 `runtime_qualified=false`、`control_ready_files=0`。
因此不能仅从缺少已记录的 CR4 写断言全 ROI PCIDE 始终为0，也不能安全重放
全部 MMU 失效。

以 `tools/fst_dynamic_translations.py::read_sidecar` 读取四核 `.xlat.gz` v3
定义：已知分页 leaf 定义数分别312/125/177/120；这些定义的 PCID **全部为0**，
ASID root 数分别2/1/1/1。该检查针对完整文件内定义，不是按退休 load 条数
统计；本次调用未再次逐条绑定 hot FST，身份和完整性采用既有 capture
preparation 的 transport 验证。数据侧 byte mapping 完整但 949 条只有 leaf，
指令侧有25个 decode/retirement ASID 跨上下文 span；不得推广为所有 I/D
访问和整个 MMU 生命周期均有完整、可回放的上下文证据。

可复制命令（临时 helper 的源码在同名 `.cpp`；仅解码记录，不修改源）：

```bash
/opt/gcc-11/bin/g++ -std=c++17 -O2 -static-libstdc++ -static-libgcc -I include tmp/spr-mmu-epoch-read.cpp -lz -o tmp/spr-mmu-epoch-read
/opt/gcc-11/bin/g++ -std=c++17 -O2 -static-libstdc++ -static-libgcc -I include tmp/spr-mmuctl-writes.cpp -lz -o tmp/spr-mmuctl-writes
for core in 0 1 2 3; do
  prefix="tmp/spr-functional-10m-c4-20261105/source/c04-817.flac_s/tao_trace/board.processor.switch${core}.core.tao_trace.tao_trace.records.micro.fst"
  tmp/spr-mmu-epoch-read "${prefix}.mmuctl.gz" "${prefix}.archb.gz"
  tmp/spr-mmuctl-writes "$core" "${prefix}.mmuctl.gz"
done
PYTHONPATH=tools python3 - <<'PY'
from collections import Counter
from pathlib import Path
from fst_dynamic_translations import read_sidecar
root=Path('tmp/spr-functional-10m-c4-20261105/source/c04-817.flac_s/tao_trace')
for core in range(4):
    f=root / f'board.processor.switch{core}.core.tao_trace.tao_trace.records.micro.fst.xlat.gz'
    d=read_sidecar(f)
    known=[m for m in d['definitions'] if m['flags'] & 1 and not m['kind']]
    print(core, len(known), dict(Counter(m['pcid'] for m in known)),
          len({m['address_space_id'] for m in known}), d['footer'])
PY
```

结论：有扎实的采集起点 PCIDE=0 和 data leaf 已观察 PCID=0 的局部证据；
**全窗口 PCIDE/失效完整性仍为 unknown**。不能将 STLB 的 ASID+PCID 近似
直接推广成全量已对齐路径；可以继续实现显式 opt-in 局部模型并计数缺口，
但需保留 121 个未绑定命令的限制，不从 OoO 时间/命中倒推输入。
