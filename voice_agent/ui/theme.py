# -*- coding: utf-8 -*-
"""设计系统：色板、圆角、字号、样式表、SVG 图标。

参考的是当代移动端/桌面端应用的通行做法（iOS 的深色模式、系统蓝、分组卡片、
柔和阴影）：**层次靠留白和圆角，而不是靠边框和分割线**。

想换配色只改这里；所有界面代码都从这取颜色和尺寸，不写死数值。
"""

from __future__ import annotations

from PyQt6.QtCore import QByteArray, QSize, Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPixmap
from PyQt6.QtSvg import QSvgRenderer

# ─────────────────────────── 色板（iOS 深色）───────────────────────────

BG = "#0A0A0F"            # 窗口底：接近纯黑，让卡片浮起来
SIDEBAR = "#111118"
CARD = "#16161D"
CARD_HOVER = "#1D1D26"
CARD_ACTIVE = "#24242F"
FIELD = "#0E0E14"

TEXT = "#F5F5F7"
MUTED = "#9A9AA8"
DIM = "#5E5E6E"
SEPARATOR = "#26262F"

# 主色：可以在设置里换（ui.accent）。所有"强调色"的地方都走 ACCENT，
# 换色时 set_accent() 会把下面这一组派生值一起更新，界面重建后整体变色。
BLUE = "#0A84FF"          # 默认主色（iOS 系统蓝）
GREEN = "#30D158"
ORANGE = "#FF9F0A"
PURPLE = "#BF5AF2"
TEAL = "#64D2FF"
PINK = "#FF375F"
RED = "#FF453A"
YELLOW = "#FFD60A"

ACCENT = BLUE
ACCENT_HOVER = "#3D9BFF"
ACCENT_PRESSED = "#0A6FD8"
ACCENT_DISABLED_BG = "#1B3A5C"
ACCENT_DISABLED_FG = "#6E88A6"
ACCENT_GLOW = "rgba(10, 132, 255, 0.28)"
# 这几个也由 set_accent() 更新，见下。
# 上一版把它们调得太接近灰色（面板描边只掺了 38% 主色），换主题时肉眼几乎看不出
# 区别 —— 用户的原话是"只有按钮变了颜色"。现在主色掺得足够多，整块窗口都跟着变。
PANEL_BORDER = "#1B1B24"
CARD_BORDER = "#22222C"
ROW_HOVER = "rgba(10, 132, 255, 0.14)"
# 面板从上到下的渐变：顶部掺一点主色，整块窗口的色调就跟着主色走
SURFACE_TOP = "#0C0C14"
# 列表行左边的图标底、胶囊标签的底色
BADGE_BG = "rgba(10, 132, 255, 0.16)"
PILL_BG = "rgba(10, 132, 255, 0.12)"


def rgba(color: str, alpha: float) -> str:
    """把 #RRGGBB 转成 rgba(...)，QSS 里用得上。"""
    c = QColor(color)
    if not c.isValid():
        c = QColor(BLUE)
    return "rgba({}, {}, {}, {:.3f})".format(c.red(), c.green(), c.blue(), max(0.0, min(alpha, 1.0)))


def qcolor(color: str, alpha: float = 1.0) -> QColor:
    """把色板里的颜色（可带透明度）变成 QColor。

    **不要写成 QColor(rgba(...))**：rgba(...) 是 QSS 的语法，QColor 不认，
    构造出来是个无效颜色，拿去当画刷会画成不透明的纯色 ——
    框选时那块"实心色"就是这么来的。
    """
    base = QColor(color)
    if not base.isValid():
        base = QColor(BLUE)
    base.setAlphaF(max(0.0, min(1.0, float(alpha))))
    return base


def mix(color_a: str, color_b: str, ratio: float) -> str:
    """按比例混合两个颜色，返回 #RRGGBB。"""
    return blend(color_a, color_b, ratio).name()


def set_accent(color: str) -> str:
    """换主色，并更新所有派生色。返回实际生效的 #RRGGBB。

    界面上的用法：先 set_accent()，再 setStyleSheet(qss()) 并重建图标 ——
    图标是按颜色渲染并缓存的，光换样式表不会让已有图标变色。

    预设名（blue / teal / …）走 config.resolve_accent 解析：直接交给 QColor 的话，
    QColor("teal") 会拿到 CSS 的 #008080，不是我们预设的青绿。

    **改色范围覆盖到"大块底色"**：早先只有按钮、描边、胶囊这些小块掺主色，
    卡片和窗口底色一直是中性灰，换主题时看到的效果就是"只有按钮变了颜色"。
    现在卡片、输入框、分隔线也各掺 6%~12% 主色 —— 掺得少是有意的：
    要的是"整块界面偏过去"，而不是变成另一个 App。
    文字颜色（TEXT / MUTED / DIM）**不掺**，保证对比度不随主题波动。
    """
    global ACCENT, BLUE, ACCENT_HOVER, ACCENT_PRESSED
    global ACCENT_DISABLED_BG, ACCENT_DISABLED_FG, ACCENT_GLOW
    from ..config import resolve_accent  # noqa: PLC0415 - 避免 ui 反向依赖配置

    c = QColor(resolve_accent(color))
    if not c.isValid():
        c = QColor("#0A84FF")
    ACCENT = c.name()
    BLUE = ACCENT                      # 兼容旧引用
    ACCENT_HOVER = c.lighter(122).name()
    ACCENT_PRESSED = c.darker(118).name()
    # 待命态用的就是主色。这个字典是模块级的，不跟着改的话圆球还是旧颜色。
    STATE_COLORS["idle"] = ACCENT

    # ── 大块底色（必须排在依赖它们的派生色前面）──
    # 每次都从写死的基色重新混，不做增量累加：连续换十次主题也不会越混越偏。
    global BG, SIDEBAR, CARD, CARD_HOVER, CARD_ACTIVE, FIELD, SEPARATOR
    BG = mix("#0A0A0F", ACCENT, 0.06)
    SIDEBAR = mix("#111118", ACCENT, 0.06)
    CARD = mix("#16161D", ACCENT, 0.08)
    CARD_HOVER = mix("#1D1D26", ACCENT, 0.10)
    CARD_ACTIVE = mix("#24242F", ACCENT, 0.12)
    FIELD = mix("#0E0E14", ACCENT, 0.07)
    SEPARATOR = mix("#26262F", ACCENT, 0.12)

    ACCENT_DISABLED_BG = mix(ACCENT, BG, 0.72)
    ACCENT_DISABLED_FG = mix(ACCENT, MUTED, 0.55)
    ACCENT_GLOW = rgba(ACCENT, 0.28)

    # ── 描边、悬停、胶囊 ──
    global PANEL_BORDER, CARD_BORDER, ROW_HOVER, SURFACE_TOP, BADGE_BG, PILL_BG
    PANEL_BORDER = mix(ACCENT, SEPARATOR, 0.30)
    CARD_BORDER = mix(ACCENT, SEPARATOR, 0.55)
    ROW_HOVER = rgba(ACCENT, 0.14)
    # 面板从上到下的渐变：顶部掺一点主色，整块窗口的色调就跟着主色走
    SURFACE_TOP = mix(BG, ACCENT, 0.16)
    BADGE_BG = rgba(ACCENT, 0.16)
    PILL_BG = rgba(ACCENT, 0.12)
    return ACCENT


def accent() -> str:
    return ACCENT

# 状态 → 颜色
STATE_COLORS = {
    "idle": BLUE,
    "listen": TEAL,
    "think": ORANGE,
    "speak": PURPLE,
    "off": DIM,
}
STATE_LABELS = {
    "idle": "待命",
    "listen": "正在听",
    "think": "思考中",
    "speak": "播报中",
    "off": "未启动",
}
STATE_HINTS = {
    "idle": "喊一声唤醒词就能使唤我",
    "listen": "我在听，说吧",
    "think": "正在处理…",
    "speak": "正在播报",
    "off": "点下面的按钮开始",
}

# ─────────────────────────── 尺寸与字体 ───────────────────────────

RADIUS_CARD = 16
RADIUS_CONTROL = 10
RADIUS_PILL = 999
PAD = 20
GAP = 12

FONT_UI = '"Segoe UI Variable Display", "Microsoft YaHei UI", "PingFang SC", "Segoe UI", sans-serif'
FONT_MONO = '"Cascadia Mono", Consolas, "SF Mono", monospace'

SIDEBAR_WIDTH = 208


def qss() -> str:
    """全局样式表。控件各自的特殊样式在自己的类里设，这里只管通用部分。

    强调色一律走 ACCENT 及其派生值，所以 set_accent() 之后重新 setStyleSheet
    就能整体换色。
    """
    ghost_hover = rgba(ACCENT, 0.14)
    accent_line = rgba(ACCENT, 0.42)
    banner_bg = mix(ACCENT, CARD, 0.86)
    return f"""
    QWidget {{
        background: transparent;
        color: {TEXT};
        font-family: {FONT_UI};
        font-size: 13px;
    }}
    QMainWindow, #Root {{ background: {BG}; }}

    /* 侧边栏 */
    #Sidebar {{ background: {SIDEBAR}; border-right: 1px solid {SEPARATOR}; }}
    #SidebarTitle {{ color: {TEXT}; font-size: 15px; font-weight: 600; }}
    #SidebarSubtitle {{ color: {DIM}; font-size: 11px; }}

    /* 卡片 */
    #Card {{
        background: {CARD};
        border: 1px solid {CARD_BORDER};
        border-radius: {RADIUS_CARD}px;
    }}
    #ListRow {{ border-radius: {RADIUS_CONTROL}px; }}
    #ListRow:hover {{ background: {ROW_HOVER}; }}
    #SectionBar {{ background: {ACCENT}; border-radius: 2px; }}
    #TurnCard {{ background: {CARD}; border: 1px solid {CARD_BORDER}; border-radius: 12px; }}
    #TurnHeard {{ color: {MUTED}; font-size: 12px; }}
    #TurnReply {{ color: {TEXT}; font-size: 13px; }}
    #Badge {{ background: {BADGE_BG}; border-radius: 8px; }}
    #Pill {{ background: {PILL_BG}; border-radius: 9px; padding: 2px 10px; font-size: 11px; }}
    #CardTitle {{ color: {TEXT}; font-size: 14px; font-weight: 600; }}
    #CardSubtitle {{ color: {MUTED}; font-size: 12px; }}
    #RowTitle {{ color: {TEXT}; font-size: 13px; }}
    #RowSubtitle {{ color: {MUTED}; font-size: 11px; }}
    #SectionTitle {{ color: {MUTED}; font-size: 11px; font-weight: 600; }}
    #PageTitle {{ color: {TEXT}; font-size: 22px; font-weight: 700; }}
    #PageSubtitle {{ color: {MUTED}; font-size: 12px; }}
    #Hint {{ color: {MUTED}; font-size: 12px; }}
    #Mono {{ font-family: {FONT_MONO}; color: {MUTED}; font-size: 11px; }}
    #Value {{ color: {ACCENT}; font-size: 12px; font-weight: 600; }}

    /* 输入 */
    QLineEdit, QPlainTextEdit, QTextEdit {{
        background: {FIELD};
        border: 1px solid {SEPARATOR};
        border-radius: {RADIUS_CONTROL}px;
        padding: 8px 12px;
        color: {TEXT};
        selection-background-color: {ACCENT};
        selection-color: #FFFFFF;
    }}
    QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus {{ border-color: {ACCENT}; }}
    QComboBox {{
        background: {FIELD}; border: 1px solid {SEPARATOR};
        border-radius: {RADIUS_CONTROL}px; padding: 7px 12px; color: {TEXT};
    }}
    QComboBox:focus {{ border-color: {ACCENT}; }}
    QComboBox::drop-down {{ border: none; width: 24px; }}
    QComboBox QAbstractItemView {{
        background: {CARD}; border: 1px solid {SEPARATOR};
        border-radius: 10px; padding: 4px; outline: none;
        selection-background-color: {CARD_ACTIVE};
    }}

    /* 普通按钮：胶囊形 */
    QPushButton {{
        background: {CARD_ACTIVE};
        border: none; border-radius: {RADIUS_CONTROL}px;
        padding: 8px 18px; color: {TEXT};
        font-size: 13px;
    }}
    QPushButton:hover:enabled {{ background: #2E2E3B; }}
    QPushButton:pressed {{ background: #383846; }}
    QPushButton:disabled {{ color: {DIM}; background: {CARD}; }}
    QPushButton#Primary {{
        background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                    stop:0 {ACCENT_HOVER}, stop:1 {ACCENT});
        color: #FFFFFF; font-weight: 600;
    }}
    QPushButton#Primary:hover:enabled {{ background: {ACCENT_HOVER}; }}
    QPushButton#Primary:pressed {{ background: {ACCENT_PRESSED}; }}
    QPushButton#Primary:disabled {{ background: {ACCENT_DISABLED_BG}; color: {ACCENT_DISABLED_FG}; }}
    QPushButton#Danger {{ background: rgba(255, 69, 58, 0.16); color: {RED}; }}
    QPushButton#Danger:hover:enabled {{ background: rgba(255, 69, 58, 0.26); }}
    QPushButton#Ghost {{ background: transparent; color: {ACCENT}; }}
    QPushButton#Ghost:hover:enabled {{ background: {ghost_hover}; }}
    QPushButton#Banner {{ background: {ACCENT}; color: #FFFFFF; font-weight: 600;
                          padding: 6px 14px; font-size: 12px; }}
    QPushButton#Banner:hover:enabled {{ background: {ACCENT_HOVER}; }}

    /* 音量条 / 滑杆 */
    QSlider::groove:horizontal {{ height: 6px; border-radius: 3px; background: {CARD_ACTIVE}; }}
    QSlider::sub-page:horizontal {{ height: 6px; border-radius: 3px; background: {ACCENT}; }}
    QSlider::handle:horizontal {{
        width: 16px; height: 16px; margin: -6px 0; border-radius: 8px;
        background: #FFFFFF; border: 3px solid {ACCENT};
    }}
    QSlider::handle:horizontal:hover {{ border-color: {ACCENT_HOVER}; }}
    QSlider::handle:horizontal:disabled {{ border-color: {DIM}; background: {MUTED}; }}
    QSlider::sub-page:horizontal:disabled {{ background: {DIM}; }}

    /* 需要重启的提示条 */
    #Banner {{
        background: {banner_bg};
        border: 1px solid {accent_line};
        border-radius: {RADIUS_CONTROL}px;
    }}
    #BannerText {{ color: {TEXT}; font-size: 12px; }}
    #BannerHint {{ color: {MUTED}; font-size: 11px; }}

    /* 对话气泡（照着 iMessage 的样子来的） */
    #BubbleMine {{ background: {ACCENT}; border-radius: 16px; }}
    #BubbleTheirs {{ background: {CARD_ACTIVE}; border-radius: 16px; }}
    #BubbleSystem {{ background: transparent; border-radius: 12px; }}
    #BubbleWho {{ color: {MUTED}; font-size: 11px; }}
    #BubbleText {{ color: {TEXT}; font-size: 13px; }}
    #BubbleMine #BubbleWho {{ color: {rgba("#FFFFFF", 0.72)}; }}
    #BubbleMine #BubbleText {{ color: #FFFFFF; }}
    #BubbleSystem #BubbleWho {{ color: {DIM}; }}
    #BubbleSystem #BubbleText {{ color: {MUTED}; }}

    /* 滚动条：细、无箭头 */
    QScrollArea {{ border: none; background: transparent; }}
    QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: #33333F; border-radius: 4px; min-height: 32px; }}
    QScrollBar::handle:vertical:hover {{ background: {ACCENT}; }}
    QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{
        background: none; height: 0; width: 0;
    }}
    QScrollBar:horizontal {{ background: transparent; height: 8px; margin: 2px; }}
    QScrollBar::handle:horizontal {{ background: #33333F; border-radius: 4px; min-width: 32px; }}
    QScrollBar::handle:horizontal:hover {{ background: {ACCENT}; }}

    QToolTip {{
        background: {CARD_ACTIVE}; color: {TEXT};
        border: 1px solid {SEPARATOR}; border-radius: 8px; padding: 6px 10px;
    }}
    QCheckBox {{ spacing: 8px; color: {TEXT}; }}
    QCheckBox::indicator {{
        width: 18px; height: 18px; border-radius: 6px;
        border: 1.5px solid #4A4A5A; background: transparent;
    }}
    QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
    QSplitter::handle {{ background: {SEPARATOR}; }}
    """


# ─────────────────────────── SVG 图标 ───────────────────────────
# 直接内联 SVG，省得带一堆图片文件，也方便按主题色实时上色。
# 全部是 24x24、描边式的线性图标（和主流的图标库同一套画法）。

_S = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
      'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
      '{body}</svg>')

_BODIES = {
    "home": '<path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V21h14V9.5"/><path d="M9.5 21v-6h5v6"/>',
    "chat": '<path d="M21 11.5a8 8 0 0 1-8 8H8l-5 3 1.4-4.2A8 8 0 1 1 21 11.5Z"/>',
    "tools": '<path d="M14.7 6.3a4 4 0 0 0 5.3 5.3l-8.1 8.1a2.1 2.1 0 0 1-3-3l8.1-8.1"/>'
             '<path d="M6.5 3.5 3.5 6.5l3 3 3-3-3-3Z"/>',
    "skills": '<path d="M12 3v4"/><path d="M12 17v4"/><path d="M3 12h4"/><path d="M17 12h4"/>'
              '<path d="m6.3 6.3 2.8 2.8"/><path d="m14.9 14.9 2.8 2.8"/>'
              '<path d="m17.7 6.3-2.8 2.8"/><path d="m9.1 14.9-2.8 2.8"/>',
    "settings": '<circle cx="12" cy="12" r="3"/>'
                '<path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-2.9 1.2v.2a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-2.9-1.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1A1.7 1.7 0 0 0 3 15H2.8a2 2 0 1 1 0-4H3a1.7 1.7 0 0 0 1.2-2.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1A1.7 1.7 0 0 0 10 4.1V4a2 2 0 1 1 4 0v.2a1.7 1.7 0 0 0 2.9 1.2l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0 1.2 2.9h.2a2 2 0 1 1 0 4h-.2a1.7 1.7 0 0 0-1.5 1Z"/>',
    "mic": '<rect x="9" y="2.5" width="6" height="11" rx="3"/>'
           '<path d="M5.5 11.5a6.5 6.5 0 0 0 13 0"/><path d="M12 18v3.5"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><path d="M12 7.8h.01"/>',
    "play": '<path d="M7 4.5 19 12 7 19.5V4.5Z"/>',
    "stop": '<rect x="6" y="6" width="12" height="12" rx="2.5"/>',
    "hand": '<path d="M18 11V6.5a1.75 1.75 0 0 0-3.5 0V11"/>'
            '<path d="M14.5 11V4.75a1.75 1.75 0 0 0-3.5 0V11"/>'
            '<path d="M11 11V5.75a1.75 1.75 0 0 0-3.5 0V14"/>'
            '<path d="M18 11a1.75 1.75 0 0 1 3.5 0v2.5c0 4.4-3.1 8-7.5 8s-7.5-3.6-7.5-8V14"/>',
    "trash": '<path d="M4 7h16"/><path d="M9.5 7V5.2A1.2 1.2 0 0 1 10.7 4h2.6a1.2 1.2 0 0 1 1.2 1.2V7"/>'
             '<path d="M6.5 7l.8 12.2A1.8 1.8 0 0 0 9.1 21h5.8a1.8 1.8 0 0 0 1.8-1.8L17.5 7"/>',
    "plus": '<path d="M12 5v14"/><path d="M5 12h14"/>',
    "refresh": '<path d="M20 11a8 8 0 0 0-13.7-5.3L3.5 8.5"/><path d="M3 4v4.5h4.5"/>'
               '<path d="M4 13a8 8 0 0 0 13.7 5.3l2.8-2.8"/><path d="M21 20v-4.5h-4.5"/>',
    "edit": '<path d="M4 20h4L19 9a2.1 2.1 0 0 0-3-3L5 17v3Z"/><path d="M14.5 6.5 17.5 9.5"/>',
    "search": '<circle cx="11" cy="11" r="6.5"/><path d="m16 16 4.5 4.5"/>',
    "check": '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
    "usercheck": '<circle cx="10" cy="8" r="3.5"/><path d="M4 20.5a6 6 0 0 1 12 0"/>'
                 '<path d="m16.5 13.5 2 2 3.5-3.5"/>',
    "alert": '<circle cx="12" cy="12" r="9"/><path d="M12 7.5V13"/><path d="M12 16.3h.01"/>',
    "chevrondown": '<path d="m6 9.5 6 6 6-6"/>',
    "sparkle": '<path d="M12 3.5 13.6 9 19 10.5 13.6 12 12 17.5 10.4 12 5 10.5 10.4 9 12 3.5Z"/>'
               '<path d="M18.5 16.5 19.2 18.8 21.5 19.5 19.2 20.2 18.5 22.5 17.8 20.2 15.5 19.5 17.8 18.8Z"/>',
    "activity": '<path d="M3 12h3.5l2.5-6 4 13 2.5-7H21"/>',
    "power": '<path d="M12 3.5v8"/><path d="M6.8 6.8a8 8 0 1 0 10.4 0"/>',
    "volume": '<path d="M11 5 6.5 9H3v6h3.5L11 19V5Z"/>'
              '<path d="M15.5 9.2a4 4 0 0 1 0 5.6"/>',
    "volumehigh": '<path d="M11 5 6.5 9H3v6h3.5L11 19V5Z"/>'
                  '<path d="M15.5 9.2a4 4 0 0 1 0 5.6"/><path d="M18.4 6.4a7.5 7.5 0 0 1 0 11.2"/>',
    "mute": '<path d="M11 5 6.5 9H3v6h3.5L11 19V5Z"/><path d="m16 10 4 4"/>'
            '<path d="m20 10-4 4"/>',
    "palette": '<path d="M12 3a9 9 0 1 0 0 18c1.4 0 2-.9 2-1.8 0-1.4-1.3-1.7-1.3-2.9'
               ' 0-.8.6-1.3 1.5-1.3H16a5 5 0 0 0 5-5c0-3.9-4-7-9-7Z"/>'
               '<circle cx="8" cy="10" r="1.2"/><circle cx="12" cy="7.5" r="1.2"/>'
               '<circle cx="16" cy="10" r="1.2"/>',
    "restart": '<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4.5V11h-6.5"/>',
    # 「看得见 / 看不见」：屏幕标记的显示开关（不是删除，只是画不画）
    "eye": '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z"/>'
           '<circle cx="12" cy="12" r="3.2"/>',
    "eyeoff": '<path d="M4 4.5 20 20.5"/>'
              '<path d="M9.9 5.9A9.6 9.6 0 0 1 12 5.5c6 0 9.5 6.5 9.5 6.5a17 17 0 0 1-3.2 4.1"/>'
              '<path d="M6.3 8.1A16.7 16.7 0 0 0 2.5 12S6 18.5 12 18.5c1.2 0 2.2-.2 3.2-.5"/>'
              '<path d="M10.1 10.3a3.2 3.2 0 0 0 4.3 4.4"/>',
}

_CACHE: dict[tuple[str, str, int], QIcon] = {}


def svg_bytes(name: str, color: str) -> bytes:
    body = _BODIES.get(name, _BODIES["info"])
    return _S.format(body=body).replace("currentColor", color).encode("utf-8")


def icon(name: str, color: str = TEXT, size: int = 20) -> QIcon:
    """把内联 SVG 渲染成 QIcon（带 2 倍图，高分屏不糊）。"""
    key = (name, color, size)
    if key in _CACHE:
        return _CACHE[key]
    scale = 2
    renderer = QSvgRenderer(QByteArray(svg_bytes(name, color)))
    pixmap = QPixmap(QSize(size * scale, size * scale))
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    renderer.render(painter)
    painter.end()
    pixmap.setDevicePixelRatio(scale)
    result = QIcon(pixmap)
    _CACHE[key] = result
    return result


def pixmap(name: str, color: str = TEXT, size: int = 20) -> QPixmap:
    return icon(name, color, size).pixmap(QSize(size, size))


def blend(color_a: str, color_b: str, ratio: float) -> QColor:
    """两个颜色按比例混合（画光晕用）。"""
    ratio = max(0.0, min(1.0, ratio))
    a, b = QColor(color_a), QColor(color_b)
    return QColor(
        int(a.red() + (b.red() - a.red()) * ratio),
        int(a.green() + (b.green() - a.green()) * ratio),
        int(a.blue() + (b.blue() - a.blue()) * ratio),
    )
