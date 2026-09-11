# -*- coding: utf-8 -*-
"""定时轮询：隔一会儿检查一件事，条件成立了就主动告诉你。

典型用法：

    「每 3 秒看一眼屏幕上有没有出现『下载完成』，出现了告诉我」
    「盯着这个按钮的图，一出现就点它」（配合 click_image 就是连点器）
    「每 10 秒看一下 D 盘剩余空间，低于 10G 就提醒我」

三种检查方式，成本差很多，所以分得很清楚：

| kind      | 每次干什么                | 成本            |
| --------- | -------------------- | ------------- |
| image     | 本地找图（模板匹配）           | 几乎为零          |
| command   | 跑一条命令 + 让模型判断输出      | 一次判定调用        |
| screen    | 截图 + 视觉模型判断          | 最贵（一次识图）      |

screen 那一种额外做了一件事：**画面没变就不问模型**。盯着屏幕等一个东西出现，
绝大多数时刻画面是完全静止的，不比对一下就每 3 秒送一张图给视觉模型，
纯属烧钱。

另外所有轮询都有上限（默认最长 30 分钟、最多若干次），到点自己停 ——
"每 3 秒看一眼"这种东西要是没人管，会一直跑到天荒地老。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .config import Config

__all__ = ["Watch", "Watcher"]

#: 每隔多久检查一次的下限。比这更密没有意义：截图本身就要几十毫秒，
#: 视觉模型一次要好几秒，排得再密也只是排队。
MIN_INTERVAL_S = 1.0
#: 轮询最长跑多久（分钟）。到点自动停，避免"忘了关"。
DEFAULT_MAX_MINUTES = 30
#: 画面静止的判定：两次截图缩到 32×18 灰度后，平均像素差小于它就算"没变"。
STILL_THRESHOLD = 2.0

_KIND_WORDS = {
    "image": "找图", "图片": "找图", "找图": "找图",
    "command": "命令", "命令": "命令",
    "screen": "屏幕", "屏幕": "屏幕", "识图": "屏幕",
}


@dataclass
class Watch:
    id: str
    kind: str
    target: str
    condition: str = ""
    interval_s: float = 5.0
    once: bool = True
    state: str = "running"        # running / hit / stopped / error
    hits: int = 0
    checks: int = 0
    started: float = field(default_factory=time.time)
    finished: float = 0.0
    last: str = ""
    error: str = ""

    @property
    def seconds(self) -> float:
        end = self.finished or time.time()
        return round(end - self.started, 1)

    def as_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "target": self.target,
                "condition": self.condition, "interval_s": self.interval_s,
                "state": self.state, "hits": self.hits, "checks": self.checks,
                "seconds": self.seconds, "last": self.last, "error": self.error}

    def summary(self) -> str:
        head = "每 " + str(self.interval_s) + " 秒"
        what = {"image": "看一眼屏幕上的图", "command": "跑一次检查",
                "screen": "看一眼屏幕"}.get(self.kind, "检查一次")
        if self.state == "running":
            return head + what + "（已经查了 " + str(self.checks) + " 次）"
        if self.state == "hit":
            return "查到啦：" + (self.last or "条件成立")
        if self.state == "stopped":
            return "已经停了（查了 " + str(self.checks) + " 次）"
        return "出错了：" + (self.error or "未知原因")


class Watcher:
    """管理所有轮询任务。每个任务一个后台线程，互不影响。"""

    def __init__(self, cfg: Config, log: Callable[[str], None] = print,
                 judge: Callable[[str, str], bool | None] | None = None,
                 on_hit: Callable[[Watch], None] | None = None) -> None:
        self.cfg = cfg
        self.log = log
        self.judge = judge
        self.on_hit = on_hit
        self._items: dict[str, Watch] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._counter = 0
        self._still: dict[str, object] = {}

    # ── 对外 ──
    def start(self, kind: str, target: str, condition: str = "",
              interval_s: float = 5.0, once: bool = True,
              max_minutes: float = DEFAULT_MAX_MINUTES) -> Watch:
        what = _KIND_WORDS.get(str(kind or "").strip().lower())
        if what is None:
            raise ValueError("不认识的检查方式：" + str(kind)
                             + "（可以用 图片 / 屏幕 / 命令）")
        target = str(target or "").strip()
        if what == "找图" and not target:
            raise ValueError("要找哪张图？给我一个图片路径")
        if what == "命令" and not target:
            raise ValueError("要跑什么命令？")
        if what == "屏幕" and not str(condition or "").strip():
            raise ValueError("盯着屏幕看什么？说清楚要等的条件")
        try:
            interval = max(MIN_INTERVAL_S, float(interval_s or 5.0))
        except (TypeError, ValueError):
            interval = 5.0
        with self._lock:
            running = sum(1 for item in self._items.values() if item.state == "running")
            if running >= 5:
                raise RuntimeError("同时最多盯着 5 件事，先停掉一个")
            self._counter += 1
            item = Watch(id="watch" + str(self._counter), kind=what,
                         target=target, condition=str(condition or "").strip(),
                         interval_s=interval, once=bool(once))
            self._items[item.id] = item
            self._order.append(item.id)
        self.log("[watch] 开始轮询 " + item.id + "：" + item.summary())
        threading.Thread(target=self._run, args=(item, max(1.0, float(max_minutes))),
                         name="watch-" + item.id, daemon=True).start()
        return item

    def get(self, key: str) -> Watch | None:
        with self._lock:
            item = self._items.get(str(key or "").strip())
            if item is not None:
                return item
            for candidate in self._items.values():
                if candidate.id == key:
                    return candidate
        return None

    def all(self) -> list[Watch]:
        with self._lock:
            return [self._items[key] for key in self._order]

    def running(self) -> list[Watch]:
        return [item for item in self.all() if item.state == "running"]

    def stop(self, key: str) -> bool:
        item = self.get(key)
        if item is None or item.state != "running":
            return False
        item.state = "stopped"
        item.finished = time.time()
        self.log("[watch] 停了 " + item.id)
        return True

    def stop_all(self) -> int:
        count = 0
        for item in self.running():
            if self.stop(item.id):
                count += 1
        return count

    def clear_finished(self) -> int:
        with self._lock:
            done = [key for key, item in self._items.items() if item.state != "running"]
            for key in done:
                self._items.pop(key, None)
                if key in self._order:
                    self._order.remove(key)
        return len(done)

    def snapshot(self) -> dict:
        items = self.all()
        running = [item for item in items if item.state == "running"]
        return {"total": len(items), "running": len(running),
                "text": running[-1].summary() if running else ""}

    def describe(self) -> str:
        items = self.all()
        if not items:
            return "现在没有在盯的事情"
        return "；".join(item.id + "：" + item.summary() for item in items[-5:])

    # ── 内部 ──
    def _run(self, watch: Watch, max_minutes: float) -> None:
        deadline = time.time() + max_minutes * 60.0
        first = True
        while watch.state == "running":
            if not first:
                # 睡一会儿再查；拆成小段是为了停得及时
                slept = 0.0
                while slept < watch.interval_s and watch.state == "running":
                    time.sleep(min(0.2, watch.interval_s - slept))
                    slept += 0.2
                if watch.state != "running":
                    break
            first = False
            if time.time() > deadline:
                watch.state = "stopped"
                watch.last = "盯了 " + str(round(max_minutes)) + " 分钟，先停下了"
                break
            watch.checks += 1
            try:
                hit, note = self._check(watch)
            except Exception as exc:  # noqa: BLE001 - 轮询出错不能拖垮主程序
                watch.state, watch.error = "error", str(exc)[:120]
                self.log("[watch] " + watch.id + " 出错：" + watch.error)
                break
            watch.last = note
            if hit:
                watch.hits += 1
                watch.state = "hit"
                watch.finished = time.time()
                self.log("[watch] " + watch.id + " 命中：" + note)
                if self.on_hit is not None:
                    try:
                        self.on_hit(watch)
                    except Exception as exc:  # noqa: BLE001
                        self.log("[watch] 通知失败：" + str(exc)[:80])
                break
        if watch.state == "running":
            watch.state = "stopped"
        if not watch.finished:
            watch.finished = time.time()
        self.log("[watch] " + watch.id + " 结束（" + watch.state + "，查了 "
                 + str(watch.checks) + " 次）")

    def _check(self, watch: Watch) -> tuple[bool, str]:
        """查一次，返回（是否命中，一句说明）。"""
        if watch.kind == "找图":
            return self._check_image(watch)
        if watch.kind == "命令":
            return self._check_command(watch)
        return self._check_screen(watch)

    def _check_image(self, watch: Watch) -> tuple[bool, str]:
        from . import screen as screen_mod

        hits = screen_mod.find_template(watch.target, confidence=0.8, limit=1)
        if hits:
            where = str(hits[0]["x"]) + "," + str(hits[0]["y"])
            return True, "屏幕上找到了那张图，在 " + where
        return False, "屏幕上还没有"

    def _check_command(self, watch: Watch) -> tuple[bool, str]:
        import subprocess

        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", watch.target],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            return False, "命令跑超时了"
        output = ((proc.stdout or "") + (proc.stderr or "")).strip()[:600]
        if not watch.condition:
            return bool(output), output[:80] or "命令没有输出"
        verdict = self._verdict(watch.condition, output)
        return verdict, (watch.condition + "：" + ("成立" if verdict else "还没到"))

    def _check_screen(self, watch: Watch) -> tuple[bool, str]:
        from . import screen as screen_mod
        from . import tools

        shot = screen_mod.grab_screen()
        if self._unchanged(watch.id, shot):
            return False, "画面没变化"
        answer = str(tools.call("look_at_screen", {"question": watch.condition}))
        if "没有配置" in answer or "失败" in answer:
            raise RuntimeError(answer[:80])
        verdict = self._verdict(watch.condition, answer)
        return verdict, (answer[:60] if verdict else "还没出现")

    def _verdict(self, condition: str, evidence: str) -> bool:
        """让模型判断条件成不成立；没有模型就退化成"包含关键词"。"""
        if not str(evidence or "").strip():
            return False
        if self.judge is not None:
            try:
                result = self.judge("根据下面的内容判断：" + condition, evidence)
            except Exception:  # noqa: BLE001 - 判定失败就当没成立
                result = None
            if result is not None:
                return bool(result)
        words = [w for w in str(condition).replace("，", " ").split() if len(w) > 1]
        return any(word in evidence for word in words) if words else bool(evidence)

    def _unchanged(self, key: str, shot) -> bool:  # noqa: ANN001
        """画面和上一次比几乎没变？（缩到 32×18 灰度再比，够用且极快）"""
        import numpy as np

        try:
            small = np.asarray(shot, dtype=np.float32)
            step_y = max(1, small.shape[0] // 18)
            step_x = max(1, small.shape[1] // 32)
            small = small[::step_y, ::step_x].mean(axis=2)
        except Exception:  # noqa: BLE001 - 比不了就当它变了
            return False
        previous = self._still.get(key)
        self._still[key] = small
        if previous is None or getattr(previous, "shape", None) != small.shape:
            return False
        return float(np.abs(small - previous).mean()) < STILL_THRESHOLD
