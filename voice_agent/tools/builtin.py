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
from ._shared import keep_listening
from .apps import open_app, open_url, web_search
from .files import (
    edit_file,
    find_files,
    grep_files,
    list_files,
    open_path,
    read_file,
    recall,
    remember,
    search_files,
    write_file,
)
from .images import find_in_image_tool, list_reference_tool, reference_dir_tool
from .marks import (
    clear_marks_tool,
    list_marks_tool,
    mark_point_tool,
    mark_region_tool,
    remove_mark_tool,
)
from .selfctl import (
    new_session_tool,
    permission_mode_tool,
    quit_self_tool,
    restart_self_tool,
)
from .subagents import cancel_subagent_tool, spawn_subagent_tool, subagent_status_tool
from .watches import list_watches_tool, start_watch_tool, stop_watch_tool
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
    focus_window,
    kill_process,
    list_processes,
    list_windows,
    lock_screen,
    media_control,
    power,
    press_keys,
    run_command,
    screenshot,
    type_text,
    volume,
    wait,
    window,
)

# ─────────────────────────── 交互类 ───────────────────────────

_register(Tool(
    name="start_watch",
    description=(
        "让助手每隔几秒盯着一件事，条件成立就主动汇报。用户说「每 3 秒看一下…」"
        "「盯着…出现了告诉我」时用它。三种方式："
        "kind=图片（target 是图片路径，本地找图，最省）；"
        "kind=屏幕（condition 是要等的画面情况，会调用视觉模型，最贵）；"
        "kind=命令（target 是要跑的命令，condition 是判断条件）。"
        "一次就能问完的事不要用它。"
    ),
    parameters=_params(
        kind={"type": "string", "description": "图片 / 屏幕 / 命令", "_required": True},
        target={"type": "string",
                "description": "kind=图片 时是要找的图（路径，或参考图片目录里的名字）；"
                               "kind=命令 时是要跑的命令"},
        condition={"type": "string", "description": "要等的条件，例如「出现了下载完成」"},
        region={"type": "string", "description": "只在框选过的这块里找，例如 范围1"},
        expect={"type": "string", "description": "出现（默认）或 消失"},
        interval_s={"type": "number", "description": "每隔多少秒查一次，默认 5"},
        once={"type": "boolean", "description": "命中一次就停，默认 true"},
    ),
    handler=start_watch_tool,
    confirm=True,
))

_register(Tool(
    name="list_watches",
    description="查看正在盯的事情的进度。",
    parameters=_params(watch_id={"type": "string", "description": "留空表示全部"}),
    handler=list_watches_tool,
))

_register(Tool(
    name="stop_watch",
    description="停掉在盯的事情（用户说「别盯了」时用）。留空表示全部停掉。",
    parameters=_params(watch_id={"type": "string", "description": "留空表示全部"}),
    handler=stop_watch_tool,
))

_register(Tool(
    name="new_session",
    description=(
        "开一个新会话：之前聊过的内容不再带进后面的对话。用户说「我们换个话题」"
        "「忘掉刚才的」「重新开始」时用它；只是插一句无关的话不要用。"
    ),
    parameters=_params(reason={"type": "string", "description": "可选：为什么换话题"}),
    handler=new_session_tool,
))

_register(Tool(
    name="find_in_image",
    description=(
        "在一张图片里找另一张图（本地比对，不碰屏幕、不要钱）。"
        "要找的图可以直接给路径，也可以只给**名字** —— 会在参考图片目录里按文件名找，"
        "所以说「看看这张截图里有没有下载按钮」就行。"
    ),
    parameters=_params(
        image={"type": "string", "description": "大图（通常是截图）的路径", "_required": True},
        template={"type": "string", "description": "要找的图：路径或参考图片目录里的名字", "_required": True},
        confidence={"type": "number", "description": "相似度阈值，默认 0.8"},
        scales={"type": "string", "description": "多尺度，例如 1.0,0.9,1.1（默认这三个）"},
    ),
    handler=find_in_image_tool,
))

_register(Tool(
    name="list_reference",
    description="列出参考图片目录里有哪些图（这些图可以直接用名字去找）。",
    parameters=_params(),
    handler=list_reference_tool,
))

_register(Tool(
    name="reference_dir",
    description="告诉用户参考图片该放到哪个目录（会顺手把目录建出来）。",
    parameters=_params(),
    handler=reference_dir_tool,
))

_register(Tool(
    name="mark_region",
    description=(
        "在屏幕上框一块区域，记成「范围1」「范围2」…。用户说「框一下这块」"
        "「记住这个区域」时用它；没有坐标就留空，界面会让用户自己拖一个框。"
        "框过之后说「看看范围1」「截一下范围2」就能直接引用。"
    ),
    parameters=_params(
        x1=_I, y1=_I, x2=_I, y2=_I,
        name={"type": "string", "description": "给它起个名字，留空自动叫范围N"},
        note={"type": "string", "description": "这块是什么，例如「下载按钮」"},
    ),
    handler=mark_region_tool,
))

_register(Tool(
    name="mark_point",
    description=(
        "在屏幕上标一个点，记成「点1」「点2」…（画一个半透明圆点）。"
        "用户说「记住这个位置」「标一下这儿」时用它；没有坐标就留空，"
        "界面会让用户自己点一下。"
    ),
    parameters=_params(
        x=_I, y=_I,
        name={"type": "string", "description": "给它起个名字，留空自动叫点N"},
        note={"type": "string", "description": "这个点是什么，例如「登录按钮」"},
    ),
    handler=mark_point_tool,
))

_register(Tool(
    name="list_marks",
    description="列出屏幕上现有的框选范围和标记点。",
    parameters=_params(),
    handler=list_marks_tool,
))

_register(Tool(
    name="remove_mark",
    description="擦掉一个标记（留空 = 最近画的那个）。",
    parameters=_params(name={"type": "string", "description": "标记名，例如 范围1、点2"}),
    handler=remove_mark_tool,
))

_register(Tool(
    name="clear_marks",
    description="清掉屏幕上的标记。kind 填「区域」只清框，填「点」只清点，留空全清。",
    parameters=_params(kind={"type": "string", "description": "区域 / 点，留空 = 全部"}),
    handler=clear_marks_tool,
))

_register(Tool(
    name="permission_mode",
    description=(
        "查看或切换权限模式。用户说「进入只读模式」「恢复正常」「放开权限」时用它。"
        "read-only = 只能查；workspace-write = 敏感操作先确认；"
        "danger-full-access = 敏感操作直接做（关机和执行命令仍然要确认）。"
        "**放宽只能由用户本人要求**，不能因为任务做不下去就自己提权。"
    ),
    parameters=_params(
        mode={"type": "string",
              "description": "read-only / workspace-write / danger-full-access，留空表示只查询"},
        reason={"type": "string", "description": "为什么要改（会被念给用户听）"},
    ),
    handler=permission_mode_tool,
    confirm=True,
))

_register(Tool(
    name="restart_self",
    description="重启助手程序本身（用户说「重启你自己」时用）。属于敏感操作，调用前先确认。",
    parameters=_params(),
    handler=restart_self_tool,
    confirm=True,
))

_register(Tool(
    name="quit_self",
    description="退出（关闭）助手程序本身（用户说「关掉你自己」「退出程序」时用）。"
                "注意区别于 power 工具：那个关的是电脑。属于敏感操作，调用前先确认。",
    parameters=_params(),
    handler=quit_self_tool,
    confirm=True,
))

_register(Tool(
    name="keep_listening",
    title="继续听你说",
    description="你希望用户接着说下去时才调用：你刚反问了一句、需要用户补充信息，"
                "或者任务要分几步、下一步还等着用户开口。"
                "只是汇报结果、不需要用户回话时**不要**调用，否则助手会一直等着，像没听懂一样。",
    parameters=_params(
        reason={"type": "string", "description": "一句话说明为什么还要等用户说话"},
    ),
    handler=keep_listening,
))


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

# ─────────────────────── 窗口与进程 ───────────────────────

_register(Tool(
    name="list_windows",
    title="看看开了哪些窗口",
    description="列出当前打开的窗口。用户问「我开了哪些窗口」「浏览器开了吗」时调用。",
    parameters=_params(
        filter={"type": "string", "description": "只看标题里含这个词的窗口，可留空"},
    ),
    handler=list_windows,
))

_register(Tool(
    name="focus_window",
    title="切换窗口",
    description="把标题匹配的窗口切到最前面。用户说「切到浏览器」「把记事本调出来」时调用。",
    parameters=_params(
        title={"type": "string", "description": "窗口标题的一部分，例如「浏览器」「记事本」", "_required": True},
    ),
    handler=focus_window,
))

_register(Tool(
    name="list_processes",
    title="看看谁在占资源",
    description="按内存占用列出正在运行的进程。用户问「什么东西这么卡」「看看进程」时调用。",
    parameters=_params(
        filter={"type": "string", "description": "只看名字里含这个词的进程，可留空"},
        top={"type": "integer", "description": "列几个，默认 5"},
    ),
    handler=list_processes,
))

_register(Tool(
    name="kill_process",
    title="结束进程",
    description="按名字结束一个进程（例如卡死的程序）。用户说「把那个程序关掉」「结束 xxx 进程」时调用。"
                "属于敏感操作，调用前应先跟用户确认。",
    parameters=_params(
        name={"type": "string", "description": "进程名或窗口标题的一部分，例如 notepad", "_required": True},
        force={"type": "boolean", "description": "是否强制结束，默认是"},
    ),
    handler=kill_process,
    confirm=True,
))

_register(Tool(
    name="wait",
    title="等一会儿",
    description="等待若干秒再继续。用户说「等 5 秒再做」或者某个操作需要缓冲时调用。",
    parameters=_params(
        seconds={"type": "number", "description": "等多少秒，最多 30"},
    ),
    handler=wait,
))


# ─────────────────────── 文件读写 ───────────────────────

_register(Tool(
    name="write_file",
    title="写文件",
    description="把一段文字写进一个文本文件（可以覆盖或追加）。用户说「把这句话记到 xxx.txt」时调用。",
    parameters=_params(
        path={"type": "string", "description": "文件路径，支持「桌面」「文档」这类说法", "_required": True},
        content={"type": "string", "description": "要写入的内容", "_required": True},
        mode={"type": "string", "description": "overwrite（覆盖，默认）或 append（追加）"},
    ),
    handler=write_file,
))

_register(Tool(
    name="edit_file",
    title="改文件里的一段",
    description="把文件里已有的某段文字替换成新的。用户说「把文件里的 xxx 改成 yyy」时调用。",
    parameters=_params(
        path={"type": "string", "description": "文件路径", "_required": True},
        old={"type": "string", "description": "要被替换掉的原文", "_required": True},
        new={"type": "string", "description": "替换成什么"},
    ),
    handler=edit_file,
))

_register(Tool(
    name="find_files",
    title="按通配符找文件",
    description="用 *.pdf、报表*.xlsx 这类通配符找文件，比按名字搜更精确。",
    parameters=_params(
        pattern={"type": "string", "description": "通配符，例如 *.pdf", "_required": True},
        root={"type": "string", "description": "从哪个目录开始找，默认用户目录"},
        limit={"type": "integer", "description": "最多返回几个，默认 20"},
    ),
    handler=find_files,
))

_register(Tool(
    name="grep_files",
    title="在文件内容里搜",
    description="在文件内容里搜一段文字，返回命中的文件名和行号。用户问「哪个文件里写过 xxx」时调用。",
    parameters=_params(
        pattern={"type": "string", "description": "要找的文字", "_required": True},
        root={"type": "string", "description": "从哪个目录开始找，默认用户目录"},
        include={"type": "string", "description": "只看这类文件，默认 *.txt"},
    ),
    handler=grep_files,
))

_register(Tool(
    name="open_path",
    title="打开文件或文件夹",
    description="用系统默认程序打开一个具体的文件或文件夹（不是应用名）。用户说「打开这个文档」时调用。",
    parameters=_params(
        path={"type": "string", "description": "文件或文件夹路径", "_required": True},
    ),
    handler=open_path,
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
    description=(
        "截屏并保存到图片文件夹。多显示器时用 monitor 指定哪一块屏幕"
        "（1 = 主屏，2、3… 从左到右，0 = 全部）；也可以只截框选过的"
        "「范围1」或「左,上,右,下」。只截需要的那一块更快、存下来的图也更有用。"
    ),
    parameters=_params(
        monitor={"type": "integer", "description": "第几块屏幕，1 = 主屏，0 = 全部"},
        region={"type": "string", "description": "范围1 / 点1 / 左,上,右,下"},
        name={"type": "string", "description": "给这张图起个短名字，方便回头找"},
    ),
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
    parameters=_params(
        action={"type": "string", "description": "shutdown / restart / sleep / logoff"},
        delay={"type": "integer", "description": "延迟多少秒再执行，0 = 立刻"},
    ),
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
    description=(
        "把鼠标移到指定坐标（不会点击）。也可以直接说某个标记：mark=点1 或 范围1"
        "（范围会移到它的中心）—— 用户框过/标过的地方就别再让他报坐标了。"
    ),
    parameters=_params(x=_I, y=_I, duration_ms=_I,
                       mark={"type": "string", "description": "标记名，例如 点1、范围1"}),
    handler=mouse_move_tool,
))

_register(Tool(
    name="mouse_click",
    description=(
        "点击鼠标：可以给坐标，也可以直接说某个标记（mark=点1 / 范围1），"
        "还能指定左右键和连点次数。"
    ),
    parameters=_params(x=_I, y=_I, button=_S, count=_I,
                       mark={"type": "string", "description": "标记名，例如 点1、范围1"}),
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
    description=(
        "在屏幕上找一张图并返回坐标（配合 mouse_click 就能点它）。"
        "图可以给路径，也可以只给名字 —— 会去参考图片目录里按文件名找，"
        "所以说「桌面上的下载按钮在哪」就行；region 限定只在框过的范围里找。"
    ),
    parameters=_params(image=_S_REQ,
                       confidence={"type": "number", "description": "相似度阈值 0~1，默认 0.8"},
                       region={"type": "string", "description": "只在框选过的这块里找，例如 范围1"},
                       monitor={"type": "integer", "description": "第几块屏幕，1 = 主屏"}),
    handler=find_on_screen_tool,
))

_register(Tool(
    name="click_image",
    description=(
        "在屏幕上找到指定图片并点击它（连点器）。图可以给路径或参考图片目录里的名字；"
        "region 限定只在框过的范围里找 —— 「点范围1 里的那个按钮」就是这么用的。"
    ),
    parameters=_params(image=_S_REQ, times=_I,
                       interval_ms={"type": "integer", "description": "连点间隔毫秒，最小 60"},
                       confidence={"type": "number", "description": "相似度阈值，默认 0.8"},
                       button=_S,
                       region={"type": "string", "description": "只在框选过的这块里找，例如 范围1"},
                       monitor={"type": "integer", "description": "第几块屏幕，1 = 主屏"}),
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
    description=(
        "看一眼屏幕并回答关于它的问题（需要配置支持图片输入的视觉模型）。"
        "可以只用主屏（monitor=1）或只看框选过的「范围1」—— 图小看得更准，也更省。"
    ),
    parameters=_params(
        question={"type": "string", "description": "想问屏幕上的什么"},
        region={"type": "string", "description": "范围1 / 点1 / 左,上,右,下，留空 = 整个桌面"},
        monitor={"type": "integer", "description": "第几块屏幕，1 = 主屏，0 = 全部"},
    ),
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
# ─────────────────────────── 子代理 ───────────────────────────
# 参考 DSH / CodeWhale 的做法：把「要跑好几步、中间结果又长又吵」的事丢到后台，
# 主对话立刻回一句「我让人去查了」，用户的耳朵不用干等，主对话的上下文也不会
# 被一堆中间工具结果撑爆。子代理做完会自己回来汇报。

_register(Tool(
    name="spawn_subagent",
    description=(
        "把一件需要好几步、比较费时的事交给后台子代理去做，立刻返回，不耽误继续对话。"
        "适合「查三样东西再汇总」「把一堆文件里的某个信息找出来」这类任务；"
        "一句就能答完的小事不要用它。派完之后如果用户问进度，用 subagent_status 查。"
    ),
    parameters=_params(
        task=_S_REQ,
        name={"type": "string", "description": "给这件事起个短名字，例如「查磁盘」，留空自动编号"},
    ),
    handler=spawn_subagent_tool,
))

_register(Tool(
    name="subagent_status",
    description="查看后台子代理的进度和结果。留空返回所有子代理的一句话状态。",
    parameters=_params(name={"type": "string", "description": "子代理的名字或编号，留空表示全部"}),
    handler=subagent_status_tool,
))

_register(Tool(
    name="cancel_subagent",
    description="叫停一个还在跑的后台子代理，例如用户说「别查了」。",
    parameters=_params(name={"type": "string", "description": "子代理的名字或编号，留空表示最近派出的那个"}),
    handler=cancel_subagent_tool,
))

