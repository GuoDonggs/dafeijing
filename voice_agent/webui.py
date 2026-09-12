# -*- coding: utf-8 -*-
"""本地网页控制台：交互、配置、工具与技能管理。

只依赖标准库（http.server），不引入任何前端构建步骤：
页面是三个静态文件（index.html / style.css / app.js），改完刷新即生效。

安全边界（重要）：
- 只监听 127.0.0.1，外网访问不到；
- 所有写操作（POST）都要带启动时随机生成的 X-Voice-Token，
  页面由本服务自己下发，跨站脚本读不到它，因此别的网页无法借你的浏览器驱动助手；
- 同时校验 Origin / Sec-Fetch-Site，双重保险；
- 静态文件做了路径穿越检查。
"""

from __future__ import annotations

import json
import mimetypes
import queue
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


from . import __version__, tools
from .config import PROJECT_ROOT
from .skills import SKILL_DIRS

WEB_DIR = Path(__file__).resolve().parent / "web"
PROJECT_SKILL_DIR = PROJECT_ROOT / "skills"
MAX_BODY = 512 * 1024
LOG_LIMIT = 600


from .console import Console  # noqa: F401 - 两个前端共用同一个后端


# ───────────────────────── HTTP 层 ─────────────────────────


def _make_handler(console: Console):
    class Handler(BaseHTTPRequestHandler):
        server_version = "voice-agent/" + __version__
        protocol_version = "HTTP/1.1"

        # -- 工具方法 --------------------------------------------------
        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _json(self, payload: Any, code: int = 200) -> None:
            self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _body(self) -> dict:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                return {}          # 畸形的头：当空 body 处理，别让 int() 抛出去
            if length <= 0 or length > MAX_BODY:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8")) or {}
            except (ValueError, UnicodeDecodeError):
                return {}

        def _same_origin(self) -> bool:
            origin = self.headers.get("Origin")
            if origin:
                netloc = urlparse(origin).netloc
                if netloc not in ("127.0.0.1:" + str(console.port), "localhost:" + str(console.port),
                                  "127.0.0.1", "localhost"):
                    return False
            site = (self.headers.get("Sec-Fetch-Site") or "").lower()
            return site in ("", "same-origin", "none")

        def _authorized(self, token: str = "") -> bool:
            """敏感操作必须同时带上正确的 token，且不是跨站发起的。"""
            supplied = token or self.headers.get("X-Voice-Token") or ""
            return secrets.compare_digest(supplied, console.token) and self._same_origin()

        def _host_ok(self) -> bool:
            """防 DNS rebinding：域名被解析到 127.0.0.1 时，浏览器发来的 Host 是攻击者的域名。"""
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            return host in ("127.0.0.1", "localhost", "::1")

        def log_message(self, *args) -> None:  # 默认会把每个请求打到 stderr，太吵
            return

        # -- 路由 ------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802
            if not self._host_ok():
                return self._json({"ok": False, "error": "Host 不合法"}, 403)
            try:
                self._route_get()
            except Exception as exc:  # noqa: BLE001 - 接口异常也要回一个正经响应
                console.log("[ui] GET " + self.path + " 出错：" + str(exc))
                self._json({"ok": False, "error": str(exc)[:200]}, 500)

        def _route_get(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)

            if path in ("/", "/index.html"):
                return self._index()
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path == "/api/state":
                return self._json(console.snapshot())
            if path == "/api/events":
                if not self._authorized((query.get("token") or [""])[0]):
                    return self._json({"ok": False, "error": "token 不对"}, 403)
                return self._events()
            if path == "/api/tools":
                return self._json({"ok": True, "items": [
                    {
                        "name": t.name, "title": t.display, "description": t.description,
                        "confirm": t.confirm, "source": t.source,
                        "tags": list(t.tags),
                        "parameters": list((t.parameters.get("properties") or {}).keys()),
                    }
                    for t in tools.REGISTRY.values()
                ]})
            if path == "/api/skills":
                return self._json(dict(console.skills_payload(), ok=True))
            if path == "/api/config":
                # 原文里可能带着 llm.api_key，读也要和写一样鉴权
                if not self._authorized((query.get("token") or [""])[0]):
                    return self._json({"ok": False, "error": "未授权的请求"}, 403)
                return self._json({"ok": True, "path": str(console.config_path), "text": console.config_text()})
            if path == "/api/settings":
                return self._json({"ok": True, "settings": console.settings()})
            if path == "/api/skills/read":
                if not self._authorized((query.get("token") or [""])[0]):
                    return self._json({"ok": False, "error": "未授权的请求"}, 403)
                return self._json(console.read_skill((query.get("path") or [""])[0]))
            if path == "/api/devices":
                return self._json(console.devices())
            if path == "/api/voices":
                return self._json(console.voices_payload())
            return self._json({"ok": False, "error": "没有这个接口：" + path}, 404)

        def do_POST(self) -> None:  # noqa: N802
            if not self._host_ok():
                return self._json({"ok": False, "error": "Host 不合法"}, 403)
            parsed = urlparse(self.path)
            payload = self._body()
            token = str(payload.get("token") or "")
            if not self._authorized(token):
                return self._json({"ok": False, "error": "未授权的请求"}, 403)

            path = parsed.path
            try:
                if path == "/api/engine":
                    action = str(payload.get("action") or "")
                    if action == "start":
                        return self._json(console.start_engine())
                    if action == "stop":
                        return self._json(console.stop_engine())
                    if action == "cancel":
                        if console.agent is not None:
                            console.agent.cancel()
                        return self._json({"ok": True})
                    return self._json({"ok": False, "error": "不认识的 action"}, 400)

                if path == "/api/command":
                    text = str(payload.get("text") or "").strip()
                    if not text:
                        return self._json({"ok": False, "error": "指令是空的"}, 400)
                    agent = console.ensure_agent()
                    if not agent.running:
                        console.log("[ui] 文字指令（引擎未启动）：" + text)
                        reply = agent.ask(text, speak=False, confirm=console.web_confirm)
                        if reply == tools.CANCEL_REPLY:
                            reply = ("这条指令属于敏感操作，需要语音确认。"
                                     "请先点右上角「启动监听」，再对着麦克风说一次。")
                        return self._json({"ok": True, "reply": reply, "spoke": False})
                    agent.dispatch(text)
                    return self._json({"ok": True, "reply": "", "spoke": True})

                if path == "/api/voices/audition":
                    return self._json(console.audition_voice(payload.get("voice")))

                if path == "/api/volume":
                    return self._json(console.set_volume(
                        payload.get("percent"),
                        persist=bool(payload.get("persist", True)),
                    ))

                if path == "/api/say":
                    text = str(payload.get("text") or "").strip()
                    if not text:
                        return self._json({"ok": False, "error": "内容是空的"}, 400)
                    agent = console.ensure_agent()
                    if agent.tts is None:
                        agent.load()
                    agent.speak(text, kind="reply")
                    return self._json({"ok": True})

                if path == "/api/listen":
                    return self._json(console.record_once(float(payload.get("timeout") or 12)))

                if path == "/api/config":
                    if "text" in payload:
                        return self._json(console.save_config_text(str(payload["text"])))
                    return self._json(console.update_config(payload.get("updates") or {}))

                if path == "/api/tools/call":
                    # 试运行统一走 Console.call_tool：敏感工具在那里被拦下，
                    # 两个前端共用同一条规则，不会各写各的
                    return self._json(console.call_tool(
                        str(payload.get("name") or ""), payload.get("args") or {}
                    ))

                if path == "/api/skills/reload":
                    console.reload_skills()
                    return self._json({"ok": True, "items": [s.as_dict() for s in console.skills]})

                if path == "/api/skills/save":
                    return self._json(console.save_skill(
                        str(payload.get("filename") or "my_skill.yaml"), str(payload.get("content") or "")
                    ))

                if path == "/api/skills/delete":
                    return self._json(console.delete_skill(str(payload.get("path") or "")))
            except Exception as exc:  # noqa: BLE001 - 接口层兜底，别让页面拿到 500 空响应
                console.log("[ui] 接口出错：" + str(exc))
                return self._json({"ok": False, "error": str(exc)[:200]}, 500)

            return self._json({"ok": False, "error": "没有这个接口：" + path}, 404)

        # -- 具体实现 --------------------------------------------------
        def _index(self) -> None:
            page = WEB_DIR / "index.html"
            if not page.is_file():
                return self._send(500, "缺少 web/index.html".encode("utf-8"), "text/plain; charset=utf-8")
            html = page.read_text(encoding="utf-8").replace("__VOICE_TOKEN__", console.token)
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")

        def _static(self, name: str) -> None:
            safe = Path(unquote(name)).name
            target = WEB_DIR / safe
            if not target.is_file():
                return self._json({"ok": False, "error": "文件不存在"}, 404)
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript",):
                ctype += "; charset=utf-8"
            self._send(200, target.read_bytes(), ctype)

        def _events(self) -> None:
            """SSE：日志实时推给页面，省得轮询。"""
            channel = console.subscribe()
            # 对端「悄悄消失」（休眠、断网）时不会发 RST，写操作会一直阻塞，
            # 于是线程和它的队列永远留在订阅表里。给套接字设个超时兜底。
            try:
                self.connection.settimeout(45.0)
            except OSError:
                pass
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                for item in list(console.logs)[-80:]:
                    self.wfile.write(("data: " + json.dumps(item, ensure_ascii=False) + "\n\n").encode("utf-8"))
                self.wfile.flush()
                while True:
                    try:
                        item = channel.get(timeout=15)
                        self.wfile.write(("data: " + json.dumps(item, ensure_ascii=False) + "\n\n").encode("utf-8"))
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
                pass
            finally:
                console.unsubscribe(channel)
                try:
                    self.connection.settimeout(None)
                except OSError:
                    pass

    return Handler


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address) -> None:
        """刷新页面 / 关掉 SSE 连接都会触发连接中止，这不是错误，别刷栈。"""
        import sys as _sys

        error = _sys.exc_info()[1]
        if isinstance(error, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def serve(config_path: Path | None = None, host: str = "127.0.0.1", port: int = 8760,
          open_browser: bool = True) -> int:
    console = Console(config_path, host=host, port=port)
    try:
        server = _Server((host, port), _make_handler(console))
    except OSError as exc:
        print("端口 " + str(port) + " 起不来：" + str(exc))
        print("换一个端口：python -m voice_agent ui --port 8761")
        return 1

    url = "http://" + host + ":" + str(server.server_address[1]) + "/"
    print("大肥鲸 控制台已启动：" + url)
    print("  · 引擎默认不启动，在页面上点「启动监听」即可")
    print("  · 配置：" + str(console.config_path))
    print("  · 技能目录：" + "、".join(str(d) for d in SKILL_DIRS))
    print("  · Ctrl+C 退出")
    if open_browser:
        threading.Thread(target=lambda: (time.sleep(0.4), webbrowser.open(url)), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在退出……")
    finally:
        try:
            console.stop_engine()
        except Exception:
            pass
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="大肥鲸 网页控制台")
    parser.add_argument("--config", help="配置文件路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8760)
    parser.add_argument("--no-browser", action="store_true", help="不要自动打开浏览器")
    args = parser.parse_args(argv)
    return serve(Path(args.config) if args.config else None, args.host, args.port, not args.no_browser)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
