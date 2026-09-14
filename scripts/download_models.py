#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""下载模型权重（约 420 MB）。

实现搬进了 voice_agent/models_setup.py：打包成 exe 之后 scripts/ 不在包里，
而"缺模型"是第一次运行最容易撞上的事 —— 那时候界面要能当场下载。
这个脚本保留原来的命令行用法，只是转调包里的那份实现。

用法::

    python scripts/download_models.py                  # 下载缺失的模型
    python scripts/download_models.py --check          # 只看现在有什么，不联网
    python scripts/download_models.py --only kws tts
    python scripts/download_models.py --mirror https://gh-proxy.com/
    python scripts/download_models.py --mirror ""      # 直连 GitHub
    python scripts/download_models.py --dir D:\\models # 指定目录（默认 <数据目录>/models）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from voice_agent import models_setup, paths  # noqa: E402


def _default_target():
    """不指定 --dir 时用"当前生效的模型目录"。

    这样老用户（模型放在程序目录旁边）看到的是"已就绪"，而不是被误导成
    "6 个全都要下" —— 只有真的一个都没有时，才落到 <数据目录>/models
    （自动下载的新家，和日志/缓存在同一个父目录）。
    """
    try:
        from voice_agent.config import Config

        return Config.load().models_dir
    except Exception:  # noqa: BLE001 - 配置坏了也要能下载
        return paths.data_dir() / "models"


def main() -> int:
    parser = argparse.ArgumentParser(description="下载语音 Agent 需要的模型")
    parser.add_argument("--check", action="store_true", help="只检查本地状态，不联网")
    parser.add_argument("--only", nargs="*", choices=sorted(models_setup.MODEL_SPECS),
                        help="只下载指定模型")
    parser.add_argument("--with-optional", action="store_true", help="连可选模型一起下")
    parser.add_argument("--dir", help="装到哪个目录（默认 <数据目录>/models）")
    parser.add_argument("--mirror", default=models_setup.MIRRORS[0],
                        help="GitHub 镜像前缀，传空字符串表示直连")
    parser.add_argument("--keep-archives", action="store_true",
                        help="保留压缩包（默认也留在 downloads/ 缓存里）")
    parser.add_argument("--skip-optional", action="store_true", help="跳过可选模型")
    args = parser.parse_args()

    target = Path(args.dir).expanduser() if args.dir else _default_target()
    info = models_setup.status(target)
    print("模型目录：" + info["dir"])
    for key, spec in models_setup.MODEL_SPECS.items():
        if args.skip_optional and spec.optional:
            continue
        mark = "已就绪" if key in info["ready"] else ("待下载（可选）" if spec.optional else "待下载")
        print("  [" + mark + "] " + spec.label + "（约 " + str(spec.size_mb) + " MB）")
    if args.check:
        todo = [k for k in models_setup.missing_keys(target, not args.skip_optional)]
        print("\n共 {} 个待下载。".format(len(todo)))
        return 1 if todo else 0

    result = models_setup.download(
        target, keys=args.only or None, mirror=args.mirror,
        include_optional=not args.skip_optional,
        keep_archives=True,
        log=lambda line: print(line, flush=True))
    print()
    if result["failed"]:
        print("以下模型没装好：" + "、".join(result["failed"]))
        return 1
    print("模型目录：" + result["dir"])
    print("下载缓存：" + str((Path(result["dir"]).parent / "downloads")))
    print("下一步：python -m voice_agent selftest")
    return 0


if __name__ == "__main__":
    sys.exit(main())
