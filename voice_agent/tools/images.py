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


def find_in_image_tool(image: str = "", template: str = "",
                       confidence: float = 0.8, scales: str = "") -> str:
    """在一张图片里找另一张图。"""
    if not str(image).strip():
        return "没说要在大图是哪张（给我图片路径）"
    if not str(template).strip():
        names = _screen().list_reference_images()
        if names:
            return "没说要找哪张图。参考图片目录里现有的：" + "、".join(names)
        return "没说要找哪张图"
    try:
        factors = tuple(float(part) for part in str(scales).replace("，", ",").split(",")
                        if part.strip()) if str(scales).strip() else (1.0, 0.9, 1.1)
        hits = _screen().find_in_image(image, template, confidence=float(confidence),
                                       scales=factors or (1.0,))
    except FileNotFoundError as exc:
        return str(exc)
    except Exception as exc:  # noqa: BLE001
        return "比对失败：" + str(exc)[:80]
    if not hits:
        return ("这张图里没有找到它（阈值 " + str(round(float(confidence), 2)) + "）")
    best = hits[0]
    extra = ("，另外还有 " + str(len(hits) - 1) + " 处相似位置") if len(hits) > 1 else ""
    return ("找到了，在这张图的 " + str(best["x"]) + "," + str(best["y"])
            + " 位置，相似度 " + str(round(best["score"] * 100)) + "%" + extra)


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
    except OSError:
        pass
    return "参考图片放在这里：" + str(folder)
