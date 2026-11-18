#!/usr/bin/env python3
"""Merge /dev/kvm into TRAE configuration on the client computer."""
import argparse
import datetime
import json
import os
from pathlib import Path
import stat
import tempfile


def configure(path):
    path = path.expanduser().resolve()
    original = path.read_bytes() if path.exists() else None
    data = json.loads(original.decode("utf-8-sig")) if original is not None else {}
    if not isinstance(data, dict):
        raise ValueError("sandbox.json 顶层必须是对象")
    filesystem = data.setdefault("filesystem", {})
    if not isinstance(filesystem, dict):
        raise ValueError("filesystem 必须是对象")
    for name in ("readWrite", "readOnly"):
        value = filesystem.setdefault(name, [])
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError(f"filesystem.{name} 必须是字符串数组")
    before = {name: list(filesystem[name]) for name in ("readWrite", "readOnly")}
    if "/dev/kvm" not in filesystem["readWrite"]:
        filesystem["readWrite"].append("/dev/kvm")
    filesystem["readOnly"] = [item for item in filesystem["readOnly"] if item != "/dev/kvm"]
    if original is not None and all(before[name] == filesystem[name] for name in before):
        print(f"配置已有 /dev/kvm 读写权限：{path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if original is not None else 0o600
    if original is not None:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup = path.with_name(f"{path.name}.bak-{stamp}")
        with backup.open("xb") as stream:
            stream.write(original)
        os.chmod(backup, mode)
        print(f"原配置已备份：{backup}")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.chmod(temporary, mode)
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    print(f"已加入 /dev/kvm 读写权限：{path}")


def main():
    parser = argparse.ArgumentParser(
        description="在 TRAE 客户端所在电脑运行；备份并合并 /dev/kvm 放行配置")
    parser.add_argument("--config", type=Path,
                        default=Path.home() / ".trae-cn/sandbox.json",
                        help="可指定 TRAE 设置→对话流→打开配置实际打开的文件")
    args = parser.parse_args()
    try:
        configure(args.config)
    except (OSError, ValueError) as error:
        parser.exit(1, f"配置失败，未强行覆盖原配置：{error}\n")
    print("请在 TRAE 的“设置 → 对话流 → 沙箱自定义配置 → 打开配置”中核对该文件。")
    print("如仍未生效，可重载窗口或重连 Remote SSH，再用真实 FS 命令验证。")
    print("此脚本只更新配置；不修改设备系统权限，也不证明当前会话已加载配置。")


if __name__ == "__main__":
    main()
