# -*- coding: utf-8 -*-
"""权限闸门：这个助手到底能动本机的哪些东西。

为什么语音助手特别需要它：**模型的输出是不可信输入**。

- 接了第三方"中转站"（各种 API 代理、便宜的中转 key）时，对面能看到你的全部
  请求，也能改写模型的回答。它只要在回答里塞一句工具调用，就能在你机器上
  执行命令、写文件、点鼠标 —— 而你听到的只是"好的，我来处理"；
- 就算中转站是干净的，网页内容、文件内容、屏幕上的字也可能是注入进去的指令。

所以照 DSH（deepseek-harness）那套 sandbox / escalation 的做法，把"能做什么"
从模型手里拿走，交给一个模型说不上话的地方判定：

1. **会话级的权限模式**，不是每个工具自己说了算：
   - `read-only`（只读）—— 只能查，写/点/执行一律**直接拒绝**，连确认都不给；
   - `workspace-write`（标准，默认）—— 只读和常规操作直接做，敏感操作逐个确认；
   - `danger-full-access`（放开）—— 敏感操作也直接做，**但下面那份
     "无论如何都要确认"的名单照旧**（关机、执行任意命令、退出程序…）。
2. **只能往更宽的方向升级，而且要用户本人点头**（WIDER_MODES + 本地确认）。
   模型不能自己给自己提权，中转站也不能 —— 这是这套设计的核心。
3. **确认通道拿不到就是拒绝**（fail closed），跟 DSH 的 `unavailable` 一样。
4. **一次确认只够一次调用**：确认和调用是同一个函数栈里的两步，没有"记住你
   同意过"这种状态可以重放。
5. **给模型的拒绝标记**是固定措辞（`[permission: …]`），它认得出来就不会
   反复撞墙，也不会把"被拒绝"说成"已经做了"。

另外两条针对中转站的硬约束：

- **明文 HTTP 非本机地址 → 自动降到只读**。http 是明文，路径上任何人都能改
  模型的回答（也就能塞工具调用），这种链路不该拿到本机的写权限；
- **确认次数限流**：一分钟内最多问 N 次（默认 6）。中转站可以反复构造危险调用，
  逼着用户一路点"确认"点到麻木 —— 限流让"疲劳战术"失效。

所有敏感决定都写进 build/audit.jsonl（谁、什么时候、要做什么、批没批）。
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

__all__ = [
    "MODES", "MODE_LABELS", "WIDER_MODES", "TIERS", "Decision", "DELIMITER",
    "ESCALATION_HINT",
    "configure", "mode", "mode_label", "set_mode", "can_escalate",
    "tier_of", "check", "note_prompt", "reset_limits", "taint", "tainted", "reset_turn",
    "transport_trusted", "snapshot", "audit", "audit_tail",
]

#: 权限模式，从紧到松。名字沿用 DSH 的 sandbox 词表，含义对齐：
#: 只读 / 可写 / 完全放开。
MODES = ("read-only", "workspace-write", "danger-full-access")
MODE_LABELS = {"read-only": "只读", "workspace-write": "标准", "danger-full-access": "放开"}
#: 严格更宽的升级表（和 DSH 的 WIDER_MODES 一致）：只能往上走，不能横跳。
WIDER_MODES = {
    "read-only": ("workspace-write", "danger-full-access"),
    "workspace-write": ("danger-full-access",),
    "danger-full-access": (),
}

#: 工具的三个风险档：
#:   read —— 只查不改（时间、系统信息、读文件、找图、看屏幕…）
#:   act  —— 动本机但影响有限（打开应用/网址、点鼠标、打字、调音量…）
#:   exec —— 会改系统或执行任意东西（执行命令、写删文件、关机、杀进程…）
TIERS = ("read", "act", "exec")

#: 只读工具名单：这些工具无论什么模式都放行。
READ_TOOLS = frozenset({
    "get_time", "system_info", "mouse_position", "list_files", "search_files",
    "find_files", "read_file", "grep_files", "recall", "screenshot",
    "find_on_screen", "resize_image", "look_at_screen", "app_map",
    "list_windows", "list_processes", "subagent_status", "list_watches",
    "cancel_subagent", "stop_watch", "keep_listening", "new_session",
    "permission_mode", "skill_status",
})

#: 执行档：会改系统、执行任意代码、或者干脆把电脑关掉。
EXEC_TOOLS = frozenset({
    "run_command", "write_file", "edit_file", "power", "kill_process",
    "lock_screen", "open_path", "restart_self", "quit_self",
})

#: **无论如何都要用户确认**的名单：即使把模式开到最宽也不免除。
#: 这是防中转站的最后一道闸 —— 模型不能靠"先提权再动手"绕过它。
ALWAYS_CONFIRM = frozenset({
    "run_command", "power", "kill_process", "restart_self", "quit_self",
})

#: 这些工具的结果算"外部内容"：网页、文件、屏幕上的字都可能藏着注入的指令。
UNTRUSTED_SOURCES = frozenset({
    "web_search", "open_url", "read_file", "grep_files", "look_at_screen",
    "recall", "clipboard", "app_map", "list_files", "search_files", "find_files",
    "list_windows", "list_processes", "system_info", "screenshot",
})

#: 给模型看的拒绝标记 / 提示，措辞固定，它认得出这是一条"权限"而不是"出错"。
DELIMITER = "[permission: {}]"
ESCALATION_HINT = ("[permission: 如果确实需要，请让用户本人说「进入标准模式」或"
                   "「放开权限」—— 权限只能由用户放开，你不能自己提权]")

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"}


@dataclass(frozen=True)
class Decision:
    """一次权限判定的结果。"""

    code: str = "ok"            # ok / denied / needs_confirm / rate_limited
    text: str = ""              # 给模型/用户看的一句话（被拒时用固定标记打头）
    needs_confirm: bool = False
    tier: str = "read"
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.code == "ok"


_state = {
    "mode": "workspace-write",
    "allow_insecure": False,
    "max_prompts": 6,
    "max_same": 3,
    "audit": True,
    "transport_ok": True,
    "transport_note": "",
    "tainted": False,
    "taint_reason": "",
}
_lock = threading.Lock()
_prompts: deque = deque(maxlen=64)
_same: deque = deque(maxlen=16)


def _build_dir() -> Path:
    base = os.environ.get("VOICE_AGENT_BUILD_DIR")
    root = Path(base) if base else Path(__file__).resolve().parent.parent / "build"
    return root


def transport_trusted(base_url: str) -> tuple[bool, str]:
    """这个模型地址可不可信。

    https 一律可信；本机地址（ollama / lmstudio 这类）也放行；
    **其余 http 一律算不可信** —— 明文链路上任何人都能改写模型的回答，
    也就能往里塞工具调用。
    """
    raw = str(base_url or "").strip()
    if not raw:
        return True, ""
    parsed = urlparse(raw if "://" in raw else "http://" + raw)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if scheme == "https":
        return True, ""
    if host in _LOCAL_HOSTS:
        return True, ""
    return False, "明文 HTTP 的模型地址（" + host + "）：链路上任何人都能改写模型的回答"


def configure(cfg: Any) -> None:
    """从配置装载权限设置。启动时和改完配置都会调。"""
    security = getattr(cfg, "security", None)
    base_url = str(getattr(getattr(cfg, "llm", None), "base_url", "") or "")
    with _lock:
        _state["allow_insecure"] = bool(getattr(security, "allow_insecure", False))
        _state["max_prompts"] = max(0, int(getattr(security, "max_prompts_per_minute", 6)))
        _state["max_same"] = max(0, int(getattr(security, "max_same_action", 3)))
        _state["audit"] = bool(getattr(security, "audit", True))
        wanted = str(getattr(security, "mode", "") or "").strip().lower()
        if wanted in MODES:
            _state["mode"] = wanted
        ok, note = transport_trusted(base_url)
        _state["transport_ok"], _state["transport_note"] = ok, note
        # 不可信链路 + 用户没有明确接受 → 强制只读。
        # 只降不升：配置里写 danger-full-access 也没用，安全优先。
        downgraded = not ok and not _state["allow_insecure"] and _state["mode"] != "read-only"
        if downgraded:
            _state["mode"] = "read-only"
    if downgraded:
        audit({"event": "transport_downgrade", "to": "read-only", "note": note})


def mode() -> str:
    with _lock:
        return str(_state["mode"])


def mode_label(value: str | None = None) -> str:
    return MODE_LABELS.get(value or mode(), value or mode())


def can_escalate(target: str) -> bool:
    """只能往更宽的方向升级（严格更宽），和 DSH 的 WIDER_MODES 一致。"""
    return str(target or "") in WIDER_MODES.get(mode(), ())


def set_mode(target: str, reason: str = "") -> tuple[bool, str]:
    """换权限模式。返回（是否成功，一句人话）。

    调用方负责先确认 —— 这里只做合法性判断（是不是更宽、名字对不对）。
    """
    wanted = str(target or "").strip().lower()
    if wanted not in MODES:
        return False, "没有这个权限模式：" + str(target) + "（只读 / 标准 / 放开）"
    current = mode()
    if wanted == current:
        return True, "现在就是「" + mode_label(wanted) + "」模式。"
    if MODES.index(wanted) > MODES.index(current) and not can_escalate(wanted):
        return False, "不能从「" + mode_label(current) + "」升到「" + mode_label(wanted) + "」。"
    with _lock:
        _state["mode"] = wanted
    audit({"event": "mode", "from": current, "to": wanted, "reason": str(reason or "")[:120]})
    return True, "好，现在是「" + mode_label(wanted) + "」模式。"


def tier_of(tool: Any) -> str:
    """这个工具属于哪一档。显式名单优先，其余按"要不要确认"推。"""
    name = str(getattr(tool, "name", tool) or "")
    if name in READ_TOOLS:
        return "read"
    if name in EXEC_TOOLS:
        return "exec"
    if bool(getattr(tool, "confirm", False)):
        # 自定义技能里 shell / 敏感动作都会被打上 confirm，落到执行档
        return "exec"
    return "act"


def taint(reason: str = "") -> None:
    """标记"这一轮碰过外部内容"。"""
    with _lock:
        _state["tainted"] = True
        if reason and not _state["taint_reason"]:
            _state["taint_reason"] = str(reason)[:60]


def tainted() -> bool:
    with _lock:
        return bool(_state["tainted"])


def reset_turn() -> None:
    """新一轮对话开始：外部内容的标记清掉（权限模式不清）。"""
    with _lock:
        _state["tainted"] = False
        _state["taint_reason"] = ""


def reset_limits() -> None:
    """清空限流计数（测试和人工复位用；正常不该需要）。"""
    with _lock:
        _prompts.clear()
        _same.clear()


def note_prompt(tool_name: str = "") -> tuple[bool, str]:
    """记一次"要弹确认"。返回（是否允许继续问，说明）。

    两道限流，都是冲着"逼用户点确认点到麻木"去的：

    - 一分钟内最多 max_prompts 次；
    - 同一个操作连着要 max_same 次以上，直接拦下（中转站最爱干的就是
      把一个危险调用反复塞过来，直到用户手滑同意）。
    """
    now = time.monotonic()
    with _lock:
        limit = int(_state["max_prompts"])
        same_limit = int(_state["max_same"])
    if limit > 0:
        while _prompts and now - _prompts[0] > 60.0:
            _prompts.popleft()
        if len(_prompts) >= limit:
            audit({"event": "rate_limited", "tool": tool_name, "window_s": 60,
                   "count": len(_prompts)})
            return False, ("一分钟里已经问过 " + str(limit) + " 次确认了，先停一下。"
                           "如果是被反复要求做同一件危险操作，更要小心。")
    if same_limit > 0 and tool_name:
        same = sum(1 for name, _ts in _same if name == tool_name and now - _ts < 60.0)
        if same >= same_limit:
            audit({"event": "repeat_blocked", "tool": tool_name, "count": same})
            return False, ("同一个操作（" + tool_name + "）已经连着问了 " + str(same)
                           + " 次，我先停下 —— 这可能是有人在反复诱导你同意。")
    _prompts.append(now)
    if tool_name:
        _same.append((tool_name, now))
    return True, ""


def check(tool: Any, args: Any = None) -> Decision:
    """这次调用允不允许、要不要确认。**模型说什么都不影响这个判断。**"""
    name = str(getattr(tool, "name", tool) or "")
    tier = tier_of(tool)
    current = mode()
    if tier == "read":
        return Decision(tier=tier)
    if current == "read-only":
        audit({"event": "denied", "tool": name, "tier": tier, "mode": current,
               "args": _short(args)})
        return Decision(
            code="denied", tier=tier, reason="read-only",
            text=DELIMITER.format("只读模式下不能" + _verb(tier) + "，这条被拒绝了")
                 + " " + ESCALATION_HINT,
        )
    force = name in ALWAYS_CONFIRM
    if current == "danger-full-access" and not force:
        return Decision(tier=tier)
    if force or tier == "exec" or bool(getattr(tool, "confirm", False)):
        # 注意：check() 本身**不**消耗限流额度 —— 要等到真的去问用户那一刻
        # （call_result 里调 note_prompt）才记一笔。否则"决定要问、但根本没有
        # 确认通道"也会把额度吃掉，几次之后正常操作反而被限流挡下。
        return Decision(code="needs_confirm", needs_confirm=True, tier=tier)
    return Decision(tier=tier)


def _verb(tier: str) -> str:
    return {"act": "操作你的电脑", "exec": "执行命令或改动系统"}.get(tier, "做这件事")


def _short(args: Any, limit: int = 160) -> str:
    try:
        text = json.dumps(args, ensure_ascii=False) if not isinstance(args, str) else args
    except (TypeError, ValueError):
        text = str(args)
    return text[:limit]


def audit(event: dict) -> None:
    """写一条审计记录（谁在什么时候要做什么、批没批）。"""
    with _lock:
        enabled = bool(_state["audit"])
    if not enabled:
        return
    record = {"ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    record.update(event)
    try:
        path = _build_dir() / "audit.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and path.stat().st_size > 2 * 1024 * 1024:
            # 简单轮转：留一份上一代就够查了
            path.replace(path.with_name("audit.1.jsonl"))
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass   # 审计写不下去不该影响主流程


def audit_tail(limit: int = 20) -> list[dict]:
    path = _build_dir() / "audit.jsonl"
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in lines[-limit:]:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def snapshot() -> dict:
    """给界面看的一份状态。"""
    with _lock:
        return {
            "mode": _state["mode"], "label": MODE_LABELS.get(_state["mode"], _state["mode"]),
            "transport_ok": bool(_state["transport_ok"]),
            "transport_note": str(_state["transport_note"]),
            "allow_insecure": bool(_state["allow_insecure"]),
            "tainted": bool(_state["tainted"]),
            "prompts_last_minute": sum(1 for _ts in _prompts if time.monotonic() - _ts <= 60.0),
        }
