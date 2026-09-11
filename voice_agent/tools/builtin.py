# -*- coding: utf-8 -*-
"""内置工具的**声明**：名字、说明、参数、要不要确认。

这个文件就是那份「助手会做什么」的清单 —— 想加一个工具，在这里加一条；
真正的实现按主题放在同目录的其它模块里。分成两处是故意的：
声明是给模型看的（措辞直接影响它会不会调、怎么调），实现是给人看的。

参数写法：_params(x=_I, y=_I) 里，_S 是字符串、_I 是整数、_S_REQ 是必填字符串。
带 "_required": True 的参数会进 JSON Schema 的 required 列表。
"""

from __future__ import annotations

from . import Tool, _I, _S, _S_REQ, _params, _register
from .apps import open_app, open_url, web_search
from .files import list_files, read_file, recall, remember, search_files
from .system_info import get_time, system_info
from .vision import (
    app_map_tool,
    click_image_tool,
    find_on_screen_tool,
    look_at_screen_tool,
    mouse_click_tool,
    mouse_drag_tool,
    mouse_move_tool,
    mouse_position_tool,
    mouse_scroll_tool,
    resize_image_tool,
)
from .windows import (
    clipboard,
    lock_screen,
    media_control,
    power,
    press_keys,
    run_command,
    screenshot,
    type_text,
    volume,
    window,
)

# ─────────────────────────── 信息类 ───────────────────────────

_register(Tool(
    name="get_time",
    description="查询当前的日期、时间和星期。用户问「几点了」「今天几号」「星期几」时调用。",
    parameters=_params(),
    handler=get_time,
))

_register(Tool(
    name="system_info",
    description="查询本机状态：CPU 占用、内存、磁盘剩余空间、电池电量、开机时长。",
    parameters=_params(
        kind={"type": "string", "description": "想查什么：cpu / 内存 / 磁盘 / 电池 / 全部；查某个盘可以写「D盘」"},
    ),
    handler=system_info,
))

# ─────────────────────── 应用与网页 ───────────────────────

_register(Tool(
    name="open_app",
    description="打开电脑上的应用程序、文件夹或系统设置，例如 记事本、计算器、微信、浏览器、设置、回收站。"
                "会先查用户自己的应用映射表（apps.yaml）。",
    parameters=_params(name=_S_REQ),
    handler=open_app,
))

_register(Tool(
    name="open_url",
    description="用默认浏览器打开一个网址。",
    parameters=_params(url=_S_REQ),
    handler=open_url,
))

_register(Tool(
    name="web_search",
    description="在浏览器里搜索一个关键词（用户说「帮我搜一下 X」「百度一下 X」时用）。",
    parameters=_params(query=_S_REQ),
    handler=web_search,
))

_register(Tool(
    name="app_map",
    description="管理本地应用映射表：查看 / 添加 / 删除「说法 → 程序或路径」。加过之后说「打开 XXX」就能直接启动。",
    parameters=_params(action={"type": "string", "description": "list / add / remove"},
                       name={"type": "string", "description": "说法，例如「我的项目」"},
                       target={"type": "string", "description": "程序名、完整路径或网址"}),
    handler=app_map_tool,
))

# ─────────────────────────── 系统类 ───────────────────────────

_register(Tool(
    name="volume",
    description="调节系统音量：调大、调小、静音、最大。",
    parameters=_params(
        action={"type": "string", "description": "up / down / mute / max / min"},
        steps=_I,
    ),
    handler=volume,
))

_register(Tool(
    name="media_control",
    description="控制正在播放的音乐或视频：播放暂停、下一首、上一首、停止。",
    parameters=_params(action={"type": "string", "description": "play_pause / next / prev / stop"}),
    handler=media_control,
))

_register(Tool(
    name="screenshot",
    description="截取整个屏幕并保存到图片文件夹。",
    parameters=_params(),
    handler=screenshot,
))

_register(Tool(
    name="clipboard",
    description="读取或写入系统剪贴板。",
    parameters=_params(
        action={"type": "string", "description": "get 读取，set 写入"},
        text={"type": "string", "description": "action=set 时要写入的文字"},
    ),
    handler=clipboard,
))

_register(Tool(
    name="type_text",
    description="把一段文字输入到当前光标位置（相当于替你打字），最多 2000 字。",
    parameters=_params(text=_S_REQ),
    handler=type_text,
))

_register(Tool(
    name="press_keys",
    description="按下快捷键组合，例如 win+d 显示桌面、ctrl+c 复制、alt+f4 关闭窗口。",
    parameters=_params(keys=_S_REQ),
    handler=press_keys,
))

_register(Tool(
    name="window",
    description="窗口操作：显示桌面、关闭当前窗口、切换窗口。",
    parameters=_params(action={"type": "string", "description": "desktop / close / switch"}),
    handler=window,
))

_register(Tool(
    name="lock_screen",
    description="锁定屏幕。",
    parameters=_params(),
    handler=lock_screen,
))

_register(Tool(
    name="power",
    description="关机、重启、睡眠或注销电脑。属于敏感操作，调用前应先跟用户确认。",
    parameters=_params(action={"type": "string", "description": "shutdown / restart / sleep / logoff"}),
    handler=power,
    confirm=True,
))

_register(Tool(
    name="run_command",
    description="执行一条系统命令（cmd/PowerShell）并返回输出（输出最多回传约 400 字）。"
                "属于敏感操作，调用前应先跟用户确认。",
    parameters=_params(command=_S_REQ, timeout=_I),
    handler=run_command,
    confirm=True,
))

# ─────────────────────────── 鼠标类 ───────────────────────────

_register(Tool(
    name="mouse_position",
    description="报告鼠标当前在屏幕上的坐标。",
    parameters=_params(),
    handler=mouse_position_tool,
))

_register(Tool(
    name="mouse_move",
    description="把鼠标移动到屏幕上的指定坐标（不会点击）。",
    parameters=_params(x={**_I, "_required": True}, y={**_I, "_required": True}, duration_ms=_I),
    handler=mouse_move_tool,
))

_register(Tool(
    name="mouse_click",
    description="点击鼠标：可以指定坐标和左键/右键/中键，也可以连点。",
    parameters=_params(x=_I, y=_I, button=_S, count=_I),
    handler=mouse_click_tool,
))

_register(Tool(
    name="mouse_drag",
    description="按住鼠标从一点拖到另一点（拖窗口、拖文件、选中文字），会改变界面内容。",
    parameters=_params(x1={**_I, "_required": True}, y1={**_I, "_required": True},
                       x2={**_I, "_required": True}, y2={**_I, "_required": True}, button=_S),
    handler=mouse_drag_tool,
    confirm=True,
))

_register(Tool(
    name="mouse_scroll",
    description="滚动鼠标滚轮：正数向上、负数向下。",
    parameters=_params(amount=_I,
                       horizontal={"type": "boolean", "description": "true 表示横向滚动"}),
    handler=mouse_scroll_tool,
))

# ─────────────────────── 屏幕与图像 ───────────────────────

_register(Tool(
    name="find_on_screen",
    description="在屏幕上找一张图片并返回它的坐标，配合 mouse_click 就能点它。",
    parameters=_params(image=_S_REQ,
                       confidence={"type": "number", "description": "相似度阈值 0~1，默认 0.8"}),
    handler=find_on_screen_tool,
))

_register(Tool(
    name="click_image",
    description="在屏幕上找到指定图片并点击它，可以连点多次（连点器）。",
    parameters=_params(image=_S_REQ, times=_I,
                       interval_ms={"type": "integer", "description": "连点间隔毫秒，最小 60"},
                       confidence={"type": "number", "description": "相似度阈值，默认 0.8"},
                       button=_S),
    handler=click_image_tool,
))

_register(Tool(
    name="resize_image",
    description="把图片缩放到指定尺寸；也可以先压小截图再送给视觉模型，省 token。",
    parameters=_params(image=_S_REQ, width=_I, height=_I,
                       out={"type": "string", "description": "另存路径，留空自动命名"},
                       quality=_I),
    handler=resize_image_tool,
))

_register(Tool(
    name="look_at_screen",
    description="看一眼当前屏幕并回答关于它的问题（需要配置支持图片输入的视觉模型）。",
    parameters=_params(question={"type": "string", "description": "想问屏幕上的什么"}),
    handler=look_at_screen_tool,
))

# ─────────────────────── 文件与记忆 ───────────────────────

_register(Tool(
    name="list_files",
    description="列出某个文件夹里的内容。路径支持「桌面」「下载」「D盘」这类口语说法。",
    parameters=_params(path={"type": "string", "description": "文件夹路径，留空表示桌面"}),
    handler=list_files,
))

_register(Tool(
    name="search_files",
    description="按文件名在电脑里找文件。",
    parameters=_params(
        name=_S_REQ,
        root={"type": "string", "description": "从哪个目录开始找，留空表示用户主目录"},
        limit=_I,
    ),
    handler=search_files,
))

_register(Tool(
    name="read_file",
    description="读取一个文本文件的内容并朗读其中的摘要（最多返回约 800 字，超出会截断并说明）。",
    parameters=_params(path=_S_REQ, max_chars=_I),
    handler=read_file,
))

_register(Tool(
    name="remember",
    description="把一件值得记住的事存进长期记忆，例如「我的车牌是京A12345」。",
    parameters=_params(text=_S_REQ, key={"type": "string", "description": "可选的分类标签"}),
    handler=remember,
))

_register(Tool(
    name="recall",
    description="回忆之前用 remember 记下的事情。",
    parameters=_params(query={"type": "string", "description": "关键词，留空则返回最近几条"}),
    handler=recall,
))
