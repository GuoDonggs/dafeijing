# -*- coding: utf-8 -*-
"""模型的检查 / 下载 / 安装。

为什么放在**包里**而不是 scripts/ 下：打包成 exe 之后 scripts/ 根本不在，
而"缺模型"恰恰是用户第一次运行最可能撞上的事 —— 那时候必须能当场下载，
不能再让用户去敲一个 exe 旁边并不存在的 python 脚本。

目录约定（"自动下载目录与缓存等数据放在同一个父目录下"）：

    <数据目录>/                        ← 用户可以在设置里改
    ├─ models/                         ← 自动下载到这里（KWS/ASR/TTS/VAD/声纹…）
    ├─ downloads/                      ← 下载缓存（压缩包，装完可删）
    ├─ logs/  screenshots/  vision/    ← 其它运行期数据
    └─ memory.json  conversation.json …

如果 <数据目录>/../models 已经存在（老用户的布局：模型放在程序目录旁边），
config._detect_models_dir() 也能找到它，不会让人重下一遍。
"""

from __future__ import annotations

import shutil
import tarfile
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ModelSpec", "MODEL_SPECS", "REQUIRED_KEYS", "MIRRORS", "status", "missing_keys",
    "download", "human", "looks_like_models_dir", "resolve_models_root",
]

#: GitHub 直连在国内实测只有几十 KB/s，公共镜像能跑到几十 MB/s；
#: 所以默认先走镜像，失败再换下一个，最后才直连。
MIRRORS = ("https://gh-proxy.com/", "https://ghproxy.net/", "")


@dataclass(frozen=True)
class ModelSpec:
    """一个模型包：显示名、下载地址、装好后应该存在的文件、估算大小。"""

    key: str
    label: str
    url: str
    marker: str          # 相对模型目录的路径，用来判断"装好了没有"
    size_mb: int         # 估算大小（只用来告诉用户要下多少）
    optional: bool = False


MODEL_SPECS: dict[str, ModelSpec] = {
    "vad": ModelSpec(
        "vad", "端点检测 silero-VAD",
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx",
        "silero_vad.onnx", 2),
    "kws": ModelSpec(
        "kws", "唤醒词 KWS",
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/"
        "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01.tar.bz2",
        "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/tokens.txt", 19),
    "asr": ModelSpec(
        "asr", "语音识别 Paraformer-zh",
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
        "sherpa-onnx-paraformer-zh-2023-09-14.tar.bz2",
        "sherpa-onnx-paraformer-zh-2023-09-14/model.int8.onnx", 243),
    "tts": ModelSpec(
        "tts", "语音合成 VITS-zh",
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/"
        "sherpa-onnx-vits-zh-ll.tar.bz2",
        "sherpa-onnx-vits-zh-ll/model.onnx", 121),
    "punct": ModelSpec(
        "punct", "标点恢复 CT-Transformer",
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/punctuation-models/"
        "sherpa-onnx-punct-ct-transformer-zh-en-vocab272727-2024-04-12-int8.tar.bz2",
        "sherpa-onnx-punct-ct-transformer-zh-en-vocab272727-2024-04-12-int8/model.int8.onnx",
        75, optional=True),
    "speaker": ModelSpec(
        "speaker", "声纹识别 3D-Speaker ERes2Net",
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
        "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx",
        "speaker/3dspeaker_eres2net_base_zh_16k.onnx", 38, optional=True),
}

#: 能让程序跑起来的最小集合（可选模型不算）
REQUIRED_KEYS = tuple(k for k, spec in MODEL_SPECS.items() if not spec.optional)


def human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return "{:.1f}{}".format(size, unit)
        size /= 1024
    return "{:.1f}GB".format(size)


def ready(spec: ModelSpec, root: Path) -> bool:
    return (Path(root) / spec.marker).exists()


def status(root: Path) -> dict:
    """这个目录里哪些模型齐了、缺哪些。"""
    root = Path(root)
    ready_keys = [k for k, spec in MODEL_SPECS.items() if ready(spec, root)]
    missing = [k for k, spec in MODEL_SPECS.items()
               if not spec.optional and k not in ready_keys]
    optional_missing = [k for k, spec in MODEL_SPECS.items()
                        if spec.optional and k not in ready_keys]
    return {"dir": str(root), "exists": root.is_dir(), "ready": ready_keys,
            "missing": missing, "optional_missing": optional_missing,
            "missing_bytes": sum(MODEL_SPECS[k].size_mb for k in missing) * 1024 * 1024}


def missing_keys(root: Path, include_optional: bool = False) -> list[str]:
    info = status(root)
    return list(info["missing"]) + (list(info["optional_missing"]) if include_optional else [])


def resolve_models_root(path: Path) -> Path | None:
    """把一个用户选的目录规整成"真正的模型目录"。认不出来返回 None。"""
    given = Path(path).expanduser()
    if not given.is_dir():
        return None
    for candidate in (given, given / "models"):
        if (candidate / "silero_vad.onnx").is_file() or any(
                (candidate / spec.marker).exists() for spec in MODEL_SPECS.values()):
            return candidate
    return None


def looks_like_models_dir(path: Path) -> bool:
    """用户选的那个目录像不像"模型目录"。

    两种都认：直接是模型目录（里面就是 silero_vad.onnx 等），
    或者是它的上一层（里面有个 models/）。
    """
    candidate = resolve_models_root(Path(path))
    if candidate is None:
        return False
    return bool(status(candidate)["ready"]) or (candidate / "silero_vad.onnx").is_file()


def download(root: Path, keys: Iterable[str] | None = None, mirror: str | None = None,
             cache: Path | None = None, log: Callable[[str], None] = print,
             on_progress: Callable[[str, int, int], None] | None = None,
             cancelled: Callable[[], bool] | None = None,
             keep_archives: bool = True,
             include_optional: bool = False) -> dict:
    """把缺的模型下到 root，返回 {ok, installed, failed, dir}。

    - cache 默认是 <数据目录>/downloads —— 压缩包留在那儿，重跑不用重下；
    - on_progress(key, done, total)：给界面画进度条用（total 为 0 表示未知）；
    - cancelled()：返回 True 就地停下（已下好的部分保留）。
    """
    root = Path(root)
    cache = Path(cache) if cache is not None else root.parent / "downloads"
    root.mkdir(parents=True, exist_ok=True)
    if keys is None:
        keys = [k for k, spec in MODEL_SPECS.items()
                if not spec.optional or include_optional]
    wanted = [k for k in keys if k in MODEL_SPECS]
    todo = [k for k in wanted if not ready(MODEL_SPECS[k], root)]
    for key in wanted:
        if key not in todo:
            log("[已就绪] " + MODEL_SPECS[key].label)

    installed: list[str] = []
    failed: list[str] = []
    for key in todo:
        spec = MODEL_SPECS[key]
        if cancelled is not None and cancelled():
            log("已取消")
            break
        log("")
        log("== " + spec.label + "（约 " + str(spec.size_mb) + " MB）")
        archive = cache / spec.url.rsplit("/", 1)[-1]
        if not archive.is_file():
            if not _fetch(spec, archive, mirror, log, on_progress, cancelled):
                if cancelled is not None and cancelled():
                    break
                failed.append(key)
                continue
        if archive.suffix == ".onnx":
            shutil.copy2(archive, root / archive.name)
        elif not _extract(archive, root, log):
            failed.append(key)
            continue
        if not ready(spec, root):
            log("  完成后仍然找不到 " + spec.marker)
            failed.append(key)
        else:
            installed.append(key)
        if not keep_archives and archive.is_file():
            archive.unlink()
    return {"ok": not failed, "installed": installed, "failed": failed, "dir": str(root)}


def _fetch(spec: ModelSpec, dest: Path, mirror: str | None, log: Callable[[str], None],
           on_progress: Callable[[str, int, int], None] | None,
           cancelled: Callable[[], bool] | None) -> bool:
    """下载一个文件；先试指定镜像，再按 MIRRORS 顺序挨个试。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    prefixes = ([mirror] if mirror else []) + list(MIRRORS)
    seen: set[str] = set()
    for prefix in prefixes:
        url = prefix + spec.url
        if url in seen:
            continue
        seen.add(url)
        host = url.split("/")[2] if "//" in url else url
        log("  下载 " + dest.name + "  ← " + host)
        try:
            with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310
                total = int(response.headers.get("Content-Length") or 0)
                done = 0
                with dest.open("wb") as handle:
                    while True:
                        if cancelled is not None and cancelled():
                            return False
                        chunk = response.read(1024 * 256)
                        if not chunk:
                            break
                        handle.write(chunk)
                        done += len(chunk)
                        if on_progress is not None:
                            on_progress(spec.key, done, total)
            if dest.stat().st_size > 0:
                return True
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            log("  失败：" + str(exc)[:90])
    return False


def _extract(archive: Path, target: Path, log: Callable[[str], None]) -> bool:
    log("  解压 " + archive.name)
    try:
        with tarfile.open(archive, "r:bz2") as tar:
            tar.extractall(target)  # noqa: S202 - 官方模型包，来源可信
        return True
    except (tarfile.TarError, OSError) as exc:
        log("  解压失败：" + str(exc)[:90])
        return False
