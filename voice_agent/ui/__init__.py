# -*- coding: utf-8 -*-
"""界面层：PyQt6 桌面窗口 + 网页版。

桌面版的结构：

    main_window.py  侧边栏 + 页面容器 + 状态轮询
    pages.py        七个页面（主页/对话/工具/技能/设置/设备/关于）
    components.py   可复用控件（卡片、开关、分段控件、导航项、状态圆球）
    theme.py        设计令牌：色板、圆角、字体、QSS、SVG 图标
    webui.py        网页版后端（在上一层）

界面只跟 voice_agent.console.Console 打交道，业务逻辑一概不在这里。
"""

__all__ = ["theme", "components", "pages", "main_window"]
