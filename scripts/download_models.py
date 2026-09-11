# -*- coding: utf-8 -*-
"""下载模型权重（约 420 MB）。

国内直连 GitHub Releases 实测只有几十 KB/s，而公共镜像能跑到几十 MB/s，
所以默认走 gh-proxy 镜像，失败再自动换下一个源。

用法::

    python scripts/download_models.py            # 下载缺失的模型
    python scripts/download_models.py --check    # 只看现在有什么，不联网
    python scripts/download_models.py --only kws tts
    python scripts/download_models.py --mirror https://gh-proxy.com/   # 指定镜像
    python scripts/download_models.py --mirror ""                      # 直连 GitHub
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tarfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT / "models"

MIRRORS = ("https://gh-proxy.com/", "https://ghproxy.net/", "")

# key -> (显示名, 下载地址, 解压后应存在的文件, 是否可选)
SPECS: dict[str, tuple[str, str, str, bool]] = {
    "vad": (
        "端点检测 silero-VAD",
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx",
        "silero_vad.onnx",
        False,
    ),
    "kws": (
        "唤醒词 KWS (19MB)",
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01.tar.bz2",
        "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/tokens.txt",
        False,
    ),
    "asr": (
        "语音识别 Paraformer-zh (243MB)",
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-paraformer-zh-2023-09-14.tar.bz2",
        "sherpa-onnx-paraformer-zh-2023-09-14/model.int8.onnx",
        False,
    ),
    "tts": (
        "语音合成 VITS-zh (121MB)",
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/sherpa-onnx-vits-zh-ll.tar.bz2",
        "sherpa-onnx-vits-zh-ll/model.onnx",
        False,
    ),
    "speaker": (
        "声纹识别 3D-Speaker ERes2Net (38MB)",
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
        "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx",
        "speaker/3dspeaker_eres2net_base_zh_16k.onnx",
        True,
    ),
    "punct": (
        "标点恢复 CT-Transformer (75MB)",
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/punctuation-models/sherpa-onnx-punct-ct-transformer-zh-en-vocab272727-2024-04-12-int8.tar.bz2",
        "sherpa-onnx-punct-ct-transformer-zh-en-vocab272727-2024-04-12-int8/model.int8.onnx",
        True,
    ),
}


def human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return "{:.1f}{}".format(size, unit)
        size /= 1024
    return "{:.1f}GB".format(size)


def fetch(url: str, dest: Path, mirror: str) -> bool:
    """下载一个文件；先试镜像，失败再直连。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    candidates = [mirror + url for mirror in ([mirror] if mirror else [])] + [m + url for m in MIRRORS]
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        host = candidate.split("/")[2] if "//" in candidate else candidate
        print("  下载 " + dest.name + "  ← " + host, flush=True)
        try:
            with urllib.request.urlopen(candidate, timeout=30) as response:  # noqa: S310
                total = int(response.headers.get("Content-Length") or 0)
                done = 0
                with dest.open("wb") as handle:
                    while True:
                        chunk = response.read(1024 * 256)
                        if not chunk:
                            break
                        handle.write(chunk)
                        done += len(chunk)
                        if total:
                            percent = done * 100 // total
                            print("\r    {:>3}%  {} / {}".format(percent, human(done), human(total)), end="", flush=True)
                print("", flush=True)
            if dest.stat().st_size > 0:
                return True
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print("  失败：" + str(exc)[:90], flush=True)
    return False


def extract(archive: Path, target: Path) -> bool:
    print("  解压 " + archive.name, flush=True)
    try:
        with tarfile.open(archive, "r:bz2") as tar:
            tar.extractall(target)  # noqa: S202 - 官方模型包，来源可信
        return True
    except (tarfile.TarError, OSError) as exc:
        print("  解压失败：" + str(exc)[:90], flush=True)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="下载语音 Agent 需要的模型")
    parser.add_argument("--only", nargs="*", choices=sorted(SPECS), help="只下载指定模型")
    parser.add_argument("--check", action="store_true", help="只检查本地状态，不联网")
    parser.add_argument("--mirror", default=MIRRORS[0], help="GitHub 镜像前缀，传空字符串表示直连")
    parser.add_argument("--keep-archives", action="store_true", help="保留下载的压缩包")
    parser.add_argument("--skip-optional", action="store_true", help="跳过可选模型")
    args = parser.parse_args()

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    cache = MODELS_DIR / "_cache"
    keys = args.only or [k for k in SPECS if not (args.skip_optional and SPECS[k][3])]

    todo: list[str] = []
    for key in keys:
        name, url, marker, optional = SPECS[key]
        if (MODELS_DIR / marker).exists():
            print("[已就绪] " + name)
        else:
            print("[待下载] " + name + ("（可选）" if optional else ""))
            todo.append(key)

    if args.check:
        print("\n共 {} 个模型，{} 个待下载。".format(len(keys), len(todo)))
        return 1 if todo else 0
    if not todo:
        print("\n全部就绪，不用下载。")
        return 0

    failed: list[str] = []
    for key in todo:
        name, url, marker, optional = SPECS[key]
        print("\n== " + name, flush=True)
        archive = cache / url.rsplit("/", 1)[-1]
        if not archive.is_file():
            if not fetch(url, archive, args.mirror):
                failed.append(key)
                continue
        if archive.suffix == ".onnx":
            shutil.copy2(archive, MODELS_DIR / archive.name)
        elif not extract(archive, MODELS_DIR):
            failed.append(key)
            continue
        if not (MODELS_DIR / marker).exists():
            print("  完成后仍然找不到 " + marker, flush=True)
            failed.append(key)
        if not args.keep_archives and archive.is_file():
            archive.unlink()

    print()
    if failed:
        print("以下模型没装好：" + "、".join(failed))
        return 1
    print("模型目录：" + str(MODELS_DIR))
    print("下一步：python -m voice_agent selftest")
    return 0


if __name__ == "__main__":
    sys.exit(main())
