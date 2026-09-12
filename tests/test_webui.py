# -*- coding: utf-8 -*-
"""网页控制台测试：不需要浏览器，也不需要人工点。

三件事：
1. 静态一致性：app.js 里引用的每个元素 id 都存在于 index.html，
   调用的每个 /api/... 都在 webui.py 里有对应路由（防手滑改错名字）；
2. 真起一个服务，用 HTTP 打一遍所有接口，断言返回结构；
3. 安全检查：无 token / 跨站 Origin 的写操作必须被拒，静态文件不许路径穿越。

运行：python tests/test_webui.py
"""

from __future__ import annotations

import os
import json
import re
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 运行时文件挪到临时目录：测试**绝不能**碰用户真实的对话记录 / 记忆 / 标记 ——
# 否则「上次聊过什么」会渗进断言（真出现过：webui 那句回复变成「跟刚才一样」）。
_BUILD = tempfile.TemporaryDirectory()
os.environ["VOICE_AGENT_DATA_DIR"] = _BUILD.name


from voice_agent import rules, tools, webui  # noqa: E402
from voice_agent.webui import Console, _make_handler, _Server  # noqa: E402

WEB = Path(webui.__file__).resolve().parent / "web"
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  [通过] " if ok else "  [失败] ") + name + ("  " + detail if detail else ""))
    if not ok:
        failures.append(name)


def http(url: str, method: str = "GET", payload: dict | None = None, headers: dict | None = None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read().decode("utf-8", "replace")
            return response.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def static_consistency() -> None:
    print("静态一致性")
    html = (WEB / "index.html").read_text(encoding="utf-8")
    js = (WEB / "app.js").read_text(encoding="utf-8")
    backend = Path(webui.__file__).read_text(encoding="utf-8")

    html_ids = set(re.findall(r'id="([^"]+)"', html))
    js_ids = set(re.findall(r"\$\('([^']+)'\)", js))
    # 拼出来的 id（'tab-' + name）单独处理
    for name in ("tools", "skills", "config", "devices"):
        js_ids.add("tab-" + name)
    missing = sorted(js_ids - html_ids)
    check("app.js 引用的 id 都存在于 index.html", not missing, "缺少：" + "、".join(missing))

    # 去掉查询串再比对：EventSource 的 token 是拼在 URL 上的
    js_apis = set(re.findall(r"api\('(/api/[^'?]+)", js))
    js_apis |= set(re.findall(r"new EventSource\('(/api/[^'?]+)", js))
    backend_apis = set(re.findall(r'path == "(/api/[^"]+)"', backend))
    unknown = sorted(js_apis - backend_apis)
    check("app.js 调用的接口都存在", not unknown, "未实现：" + "、".join(unknown))
    check("接口数量对得上", len(js_apis) >= 8, "前端用了 " + str(len(js_apis)) + " 个接口")

    check("CSS 文件存在且非空", (WEB / "style.css").stat().st_size > 2000,
          str((WEB / "style.css").stat().st_size) + " 字节")


def live_server() -> None:
    print("\nHTTP 接口")
    console = Console(None, host="127.0.0.1", port=0)
    # 强制离线规则模式：否则断言会依赖用户 config.yaml 里的真实 API Key
    console.cfg.llm.enabled = False
    # 技能写到临时目录：测试绝不能弄脏开发者真实的 ./skills
    skill_tmp = Path(tempfile.mkdtemp(prefix="voice-ui-skills-"))
    console.skill_dir = skill_tmp
    console.reload_skills(announce=False)
    server = _Server(("127.0.0.1", 0), _make_handler(console))
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:" + str(port)

    try:
        status, html = http(base + "/")
        check("首页可访问", status == 200 and "大肥鲸" in html, "HTTP " + str(status))
        token = (re.search(r'window\.VOICE_TOKEN = "([^"]+)"', html) or [None, ""])[1]
        check("首页注入了访问令牌", len(token) >= 16, "长度 " + str(len(token)))
        check("首页没有残留占位符", "__VOICE_TOKEN__" not in html)

        for name, needle in (("app.js", "renderState"), ("style.css", "--accent")):
            status, body = http(base + "/static/" + name)
            check("静态资源 " + name, status == 200 and needle in body, "HTTP " + str(status))

        status, body = http(base + "/static/..%2F..%2Fwebui.py")
        check("静态文件拒绝路径穿越", status == 404 and "def _make_handler" not in body, "HTTP " + str(status))

        expectations = {
            "/api/state": lambda d: d["ok"] and "status" in d and d["tools"] >= 20,
            "/api/tools": lambda d: d["ok"] and len(d["items"]) >= 20 and "title" in d["items"][0],
            "/api/skills": lambda d: d["ok"] and isinstance(d["items"], list) and "template" in d,
            "/api/settings": lambda d: d["ok"] and "wake.keywords" in d["settings"],
            "/api/devices": lambda d: d["ok"] and "input" in d and "output" in d,
            "/api/voices": lambda d: d["engine"] and len(d["rows"]) >= 5 and d["current"] is not None,
        }
        for path, predicate in expectations.items():
            status, body = http(base + path)
            try:
                data = json.loads(body)
                ok = status == 200 and predicate(data)
            except ValueError:
                ok = False
            check("GET " + path, ok, "HTTP " + str(status))

        # 配置原文里有 llm.api_key，读取也必须鉴权
        status, _ = http(base + "/api/config")
        check("未授权读取配置被拒绝", status == 403, "HTTP " + str(status))
        status, body = http(base + "/api/config", headers={"X-Voice-Token": token})
        check("带令牌可读配置", status == 200 and "text" in json.loads(body), "HTTP " + str(status))

        # 防 DNS rebinding：Host 不是本机时一律拒绝
        status, _ = http(base + "/api/state", headers={"Host": "evil.example"})
        check("伪造 Host 被拒绝", status == 403, "HTTP " + str(status))

        # 写操作鉴权
        status, _ = http(base + "/api/engine", "POST", {"action": "stop"})
        check("无令牌的写操作被拒绝", status == 403, "HTTP " + str(status))
        status, _ = http(base + "/api/engine", "POST", {"action": "stop"}, {"X-Voice-Token": token})
        check("带令牌的写操作放行", status == 200, "HTTP " + str(status))
        status, _ = http(base + "/api/engine", "POST", {"action": "stop"}, {"X-Voice-Token": "wrong-token-value"})
        check("错误令牌被拒绝", status == 403, "HTTP " + str(status))
        status, _ = http(base + "/api/engine", "POST", {"action": "stop"},
                         {"X-Voice-Token": token, "Origin": "http://evil.example"})
        check("跨站 Origin 被拒绝", status == 403, "HTTP " + str(status))

        # 音量：接口收的是百分比，落到引擎上是增益（别 100 倍地接错）
        from voice_agent import audio as audio_io

        status, body = http(base + "/api/volume", "POST",
                            {"percent": 40, "persist": False, "token": token})
        ok = status == 200 and json.loads(body).get("ok")
        check("网页调音量成功", ok, "HTTP " + str(status))
        check("40% 对应增益 0.40", abs(audio_io.get_output_gain() - 0.40) < 0.01,
              str(round(audio_io.get_output_gain(), 3)))
        status, body = http(base + "/api/volume", "POST", {"percent": 100, "token": token})
        check("调回 100% 写入配置", status == 200 and json.loads(body).get("ok"),
              "HTTP " + str(status))
        audio_io.set_output_gain(1.0)

        status, body = http(base + "/api/command", "POST", {"text": "现在几点了", "token": token})
        payload = json.loads(body)
        check("文字指令返回回复", status == 200 and payload.get("reply", "").find("现在") >= 0,
              repr(payload.get("reply", "")[:30]))

        # 敏感指令：「没被执行」才是真正要保证的事，文案随大脑模式而变，不能拿去断言
        from dataclasses import replace as dc_replace

        executed: list[dict] = []
        original_power = tools.REGISTRY["power"]
        tools.REGISTRY["power"] = dc_replace(
            original_power, handler=lambda **kw: (executed.append(kw), "已执行")[1]
        )
        try:
            status, body = http(base + "/api/command", "POST", {"text": "关闭电脑", "token": token})
            reply = json.loads(body).get("reply", "")
        finally:
            tools.REGISTRY["power"] = original_power
        check("引擎未启动时敏感指令没有被执行", executed == [], "实际执行：" + str(executed))
        check("并且给了用户一句话", bool(reply), repr(reply[:46]))

        status, body = http(base + "/api/skills/save", "POST",
                            {"filename": "bad.yaml", "content": "name: [1,2", "token": token})
        check("坏 YAML 被拒绝", status == 200 and not json.loads(body)["ok"])

        status, body = http(base + "/api/skills/delete", "POST",
                            {"path": "C:/Windows/system32/drivers/etc/hosts", "token": token})
        check("技能删除限制在技能目录内", status == 200 and not json.loads(body)["ok"])

        status, body = http(base + "/api/nope")
        check("未知接口返回 404", status == 404, "HTTP " + str(status))

        # 技能热加载：界面「新建技能」走的就是这个接口
        skill_yaml = (
            "name: ui_probe_skill\n"
            "title: 界面测试技能\n"
            "description: 由测试脚本创建的临时技能。\n"
            "parameters: {}\n"
            "triggers: [界面测试口令]\n"
            "action:\n"
            "  type: say\n"
            "  text: 界面测试技能已生效\n"
        )
        saved_path = ""
        status, body = http(base + "/api/skills/save", "POST",
                            {"filename": "_ui_probe.yaml", "content": skill_yaml, "token": token})
        saved = json.loads(body)
        check("界面保存技能成功", status == 200 and saved.get("ok"), body[:90])
        saved_path = str(saved.get("path") or "")
        check("技能写到了注入的目录里", saved_path.startswith(str(skill_tmp)), saved_path)

        status, body = http(base + "/api/tools")
        names = [item["name"] for item in json.loads(body)["items"]]
        check("新技能立刻出现在工具表里", "ui_probe_skill" in names)

        check("新技能的触发词可路由", rules.route("来一个界面测试口令") == ("ui_probe_skill", {}))
        check("新技能可被调用", tools.call("ui_probe_skill") == "界面测试技能已生效")

        status, body = http(base + "/api/skills/read?token=" + token + "&path=" + urllib.parse.quote(saved_path))
        check("可以读回技能原文", status == 200 and "ui_probe_skill" in json.loads(body).get("content", ""))
        status, body = http(base + "/api/skills/read?token=" + token + "&path=" + urllib.parse.quote(str(Path.home())))
        check("技能读取限制在技能目录内", status == 200 and not json.loads(body)["ok"])

        status, body = http(base + "/api/tools/call", "POST",
                            {"name": "ui_probe_skill", "args": {}, "token": token})
        check("界面可以试运行技能", status == 200 and json.loads(body).get("ok"))
        status, body = http(base + "/api/tools/call", "POST",
                            {"name": "power", "args": {"action": "shutdown"}, "token": token})
        check("试运行拒绝敏感工具", status == 200 and not json.loads(body)["ok"], body[:70])

        status, body = http(base + "/api/skills/delete", "POST",
                            {"path": saved_path, "token": token})
        check("界面删除技能成功", json.loads(body).get("ok"))
        check("删除后工具表里不再有它", "ui_probe_skill" not in tools.REGISTRY)
        check("技能文件已从磁盘删除", not Path(saved_path).is_file())
        shutil.rmtree(skill_tmp, ignore_errors=True)

        # SSE：连上后应立刻补发历史日志。
        # 注意用 readline()：SSE 是长连接，read(n) 会一直等到攒够 n 字节或超时。
        http(base + "/api/engine", "POST", {"action": "stop", "token": token})
        request = urllib.request.Request(base + "/api/events?token=" + token)
        with urllib.request.urlopen(request, timeout=10) as response:
            first = response.readline().decode("utf-8", "replace").strip()
        check("SSE 事件流可订阅", first.startswith("data: "), first[:60])

        request = urllib.request.Request(base + "/api/events")
        try:
            urllib.request.urlopen(request, timeout=5)
            blocked = False
        except urllib.error.HTTPError as exc:
            blocked = exc.code == 403
        check("SSE 无令牌被拒绝", blocked)
    finally:
        server.shutdown()
        server.server_close()


def first_run_without_config() -> None:
    """没有 config.yaml 时应该退回示例配置，而不是直接崩掉。"""
    print("\n首次运行")
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "config.yaml"
        console = Console(target, host="127.0.0.1", port=0)
        check("缺少配置时回退到示例", console.cfg is not None and "llm:" in console.config_text())
        wrote = console.save_config_text("wake:\n  keywords: [测试词]\n")
        check("保存后生成配置文件", wrote.get("ok") and target.is_file(), str(wrote)[:80])
        check("保存后配置生效", console.cfg.wake.keywords == ["测试词"], str(console.cfg.wake.keywords))

        # 关键：验证失败时绝不能落盘，否则一次手滑就把配置写死
        before = target.read_text(encoding="utf-8")
        bad = console.save_config_text("audio:\n  sample_rate: 不是數字\n")
        check("类型不合法的配置被拒绝", not bad.get("ok"), str(bad)[:70])
        check("被拒绝的配置没有写进文件", target.read_text(encoding="utf-8") == before)
        broken = console.save_config_text("wake: [1,2\n")
        check("语法错误的配置被拒绝", not broken.get("ok"), str(broken)[:70])
        check("语法错误也没有写进文件", target.read_text(encoding="utf-8") == before)

        # YAML 1.1 会把 yes/no 这样的**键**解析成布尔值。写出去的却是 true/false，
        # 用户自定义的确认词表就会被静默忽略、只剩默认值。这条断言守住归一化。
        console.save_config_text(
            "agent:\n  confirm:\n    yes: [打住, 行了]\n    no: [别动, 慢着]\n"
        )
        check("自定义确认词表能读回来",
              console.cfg.agent.confirm.yes == ["打住", "行了"]
              and console.cfg.agent.confirm.no == ["别动", "慢着"],
              str(console.cfg.agent.confirm.yes) + " / " + str(console.cfg.agent.confirm.no))
        raw_text = target.read_text(encoding="utf-8")
        check("保存后键名仍是可读的 yes/no",
              "yes:" in raw_text and "no:" in raw_text and "false:" not in raw_text,
              raw_text.replace("\n", " ")[:80])


def main() -> int:
    print("=== 网页控制台测试 ===")
    static_consistency()
    first_run_without_config()
    live_server()
    print()
    if failures:
        print("失败 " + str(len(failures)) + " 项：" + "、".join(failures))
        return 1
    print("网页控制台测试全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
