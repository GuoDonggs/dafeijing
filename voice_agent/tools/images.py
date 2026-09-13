# -*- coding: utf-8 -*-
"""在图片里找图：本地比对，不碰屏幕、不要钱。

两个典型用法：

- 「帮我看看这张截图里有没有那个按钮」—— 先用它比一遍，比中了就不用花
  视觉模型的钱；
- 「参考图 A 里有没有 B」—— 两张图之间的比对。

要找的那张图可以直接给路径，也可以只给**名字**：会在参考图片目录
（~/Pictures/voice-agent/reference）里按文件名找。语音场景里没人念得出一长串
路径，说「下载按钮」才是自然的。
"""

from __future__ import annotations

__all__ = ["find_in_image_tool", "list_reference_tool", "reference_dir_tool"]


def _screen():
    from .. import screen as screen_mod  # noqa: PLC0415

    return screen_mod


#: 文档提示（"这不是图片，要内容请用 read_file"）在 files 里，和 read_file 共用一份判断
from . import files as files_mod  # noqa: E402


def _how(hit: dict) -> str:
    """这一处是模板匹配找到的，还是 SIFT 兜底找到的？

    SIFT 找到的通常是"参考图被缩放过/转过一点角度"，说清楚用户才知道
    为什么相似度不是 99%。
    """
    return "（SIFT 特征匹配）" if str(hit.get("method")) == "sift" else ""


def find_in_image_tool(image: str = "", template: str = "",
                       confidence: float = 0.8, scales: str = "",
                       method: str = "auto") -> str:
    """在一张图片里找另一张图。"""
    if not str(image).strip():
        return "没说要在大图是哪张（给我图片路径）"
    if not str(template).strip():
        names = _screen().list_reference_images()
        if names:
            return "没说要找哪张图。参考图片目录里现有的：" + "、".join(names)
        return "没说要找哪张图"
    for candidate in (image, template):
        hint = files_mod.document_hint(candidate)
        if hint:
            return hint
    try:
        factors = tuple(float(part) for part in str(scales).replace("，", ",").split(",")
                        if part.strip()) if str(scales).strip() else (1.0, 0.9, 1.1)
        hits = _screen().find_in_image(image, template, confidence=float(confidence),
                                       scales=factors or (1.0,), method=method)
    except FileNotFoundError as exc:
        return str(exc)
    except Exception as exc:  # noqa: BLE001
        return "比对失败：" + str(exc)[:80]
    if not hits:
        if _screen().template_is_flat(template):
            return ("要找的那张图（" + str(template) + "）基本是纯色的，没有花纹/文字/边框"
                    "可做定位依据 —— 换一张带周围内容的小图当模板更靠谱")
        return ("这张图里没有找到它（阈值 " + str(round(float(confidence), 2))
                + "；模板匹配和 SIFT 都试过了）")
    best = hits[0]
    extra = ("，另外还有 " + str(len(hits) - 1) + " 处相似位置") if len(hits) > 1 else ""
    return ("找到了，在这张图的 " + str(best["x"]) + "," + str(best["y"])
            + " 位置，相似度 " + str(round(best["score"] * 100)) + "%"
            + _how(best) + extra +
            "。要接着在屏幕上找就把它交给 find_on_screen（本地找，别去看图）；"
            "屏幕上找到之后用 mark_point / mark_region 把位置固定下来。")


def list_reference_tool() -> str:
    """看看参考图片目录里有哪些图（能直接用名字去找它们）。"""
    module = _screen()
    folder = module.reference_dir()
    names = module.list_reference_images()
    if not names:
        return ("参考图片目录还是空的：" + str(folder)
                + "。把要找的小图（比如「下载按钮.png」）放进去，"
                "之后说「找一下下载按钮」就行。")
    return "参考图片目录（" + str(folder) + "）里有 " + str(len(names)) + " 张：" + "、".join(names)


def reference_dir_tool() -> str:
    """参考图片目录在哪（方便用户往里放图）。"""
    folder = _screen().reference_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # 建不出来就别说"放在这里" —— 用户去资源管理器里会发现根本没有这个目录，
        # 而工具刚才还一本正经地报了路径。
        return ("参考图片目录建不出来（" + str(exc)[:60] + "）：" + str(folder)
                + "。手动建一个，或者到设置里换数据目录。")
    return "参考图片放在这里：" + str(folder)
