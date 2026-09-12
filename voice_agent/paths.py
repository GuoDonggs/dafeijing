# -*- coding: utf-8 -*-
"""程序自己产生的文件放哪 —— 全部收在一个目录下（用户可以改）。

以前这些东西散在四处：项目根的 build/、图片文件夹里的 voice-agent/、
还有直接写在项目根的 apps.yaml。想备份、想清理、想搬到 D 盘，得挨个找。
现在统一由这里解析，默认是「程序目录/build」，可以在设置窗口里改成任意目录：

    ├─ memory.json          长期记忆
    ├─ conversation.json    跨轮上下文
    ├─ voiceprint.json      声纹（生物特征）
    ├─ marks.json           屏幕标记
    ├─ translit.json        英文音译学习缓存
    ├─ audit.jsonl          权限审计
    ├─ keywords.generated.txt  唤醒词音素表
    ├─ selftest.wav         自检音频
    ├─ apps.yaml            应用/目录映射表
    ├─ screenshots/         截图（含 reference/ 参考图片）
    └─ vision/              送给视觉模型的压缩图缓存

解析顺序（前面的优先）：
1. set_data_dir()  —— 运行中改配置用这个，立刻生效；
2. 环境变量 VOICE_AGENT_DATA_DIR（旧的 VOICE_AGENT_BUILD_DIR 仍然认，测试用）；
3. 默认：程序目录下的 build/。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

__all__ = [
    "ENV_DATA_DIR", "ENV_LEGACY", "set_data_dir", "data_dir", "base_dir",
    "default_data_dir", "legacy_data_dir", "sub", "describe", "migrate_legacy",
]

ENV_DATA_DIR = "VOICE_AGENT_DATA_DIR"
#: 早期版本用的名字，测试和脚本里还在用，继续认
ENV_LEGACY = "VOICE_AGENT_BUILD_DIR"

_override: list = [None]

#: 各个子路径（都在数据目录下）
_LAYOUT = {
    "memory": "memory.json",
    "conversation": "conversation.json",
    "voiceprint": "voiceprint.json",
    "marks": "marks.json",
    "translit": "translit.json",
    "audit": "audit.jsonl",
    "keywords": "keywords.generated.txt",
    "selftest_audio": "selftest.wav",
    "apps": "apps.yaml",
    "screenshots": "screenshots",
    "reference": "screenshots/reference",
    "vision": "vision",
    "logs": "logs",
}


def base_dir() -> Path:
    """程序自己在哪：打包版是 exe 所在目录，源码运行是项目根。"""
    import sys

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def default_data_dir() -> Path:
    return base_dir() / "build"


def legacy_data_dir() -> Path:
    """上一版默认的数据目录（程序目录/build）。

    用户把数据目录改到别处之后，老文件还留在原处；迁移时得按"默认位置"
    去找，而不是按当前 data_dir()（那已经是新目录了，两边相等时要靠
    migrate_legacy 的 target.exists() 兜住，不会自己搬给自己）。
    """
    return default_data_dir()


def data_dir() -> Path:
    """当前的数据目录（不存在也会返回路径，用的时候自己 mkdir）。"""
    if _override[0] is not None:
        return Path(_override[0])
    for name in (ENV_DATA_DIR, ENV_LEGACY):
        value = os.environ.get(name)
        if value:
            return Path(value).expanduser()
    return default_data_dir()


def set_data_dir(value: str | Path | None) -> Path:
    """改数据目录（配置里改了会调到这里）。空值 = 回到默认。"""
    raw = str(value or "").strip()
    if not raw:
        _override[0] = None
    else:
        _override[0] = Path(os.path.expandvars(os.path.expanduser(raw)))
    target = data_dir()
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return target


def sub(name: str, create: bool = False) -> Path:
    """数据目录下的某个文件 / 子目录。

    create=True 时顺手把父目录建出来（写文件前用）。
    """
    relative = _LAYOUT.get(name, name)
    path = data_dir() / relative
    if create:
        target = path if path.suffix == "" else path.parent
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    return path


def describe() -> dict:
    """给设置页 / doctor 看的一份清单。"""
    root = data_dir()
    rows = []
    for name in ("memory", "conversation", "voiceprint", "marks", "translit",
                 "audit", "apps", "screenshots", "vision"):
        path = sub(name)
        rows.append({"name": name, "path": str(path),
                     "exists": path.exists(),
                     "size": _size_of(path)})
    return {"dir": str(root), "exists": root.is_dir(),
            "default": str(default_data_dir()), "items": rows}


def _size_of(path: Path) -> int:
    try:
        if path.is_file():
            return path.stat().st_size
        if path.is_dir():
            return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
    except OSError:
        return 0
    return 0


def migrate_legacy(pairs: list[tuple[Path, Path]], log=None) -> list[str]:
    """把老位置的文件搬到新位置（只搬"新的还没有、老的还在"的那些）。

    升级不该让用户丢东西：以前截图放在图片文件夹、apps.yaml 放在项目根，
    换了目录之后得把它们带过来 —— 但也只在第一次做，而且不覆盖新目录里已有的。
    """
    moved: list[str] = []
    for legacy, target in pairs:
        try:
            if not legacy.exists() or target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            # shutil.move 对文件和目录是同一套逻辑：目标已存在时会被上面的
            # 「target.exists() 就跳过」挡掉，所以这里不需要分情况
            shutil.move(str(legacy), str(target))
            moved.append(legacy.name + " → " + str(target))
        except (OSError, shutil.Error):
            continue
    if moved and log is not None:
        for line in moved:
            log("[paths] 已迁移：" + line)
    return moved
