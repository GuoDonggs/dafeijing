# -*- coding: utf-8 -*-
"""英文音译：中文 TTS 念不出拉丁词，得先把它们换成汉字。

用户报的问题：回复里的英文单词/字母念不出来（听到的是那句话少了一截）。
根因是 vits-zh 的 lexicon 里没有任何拉丁词，整词被当 OOV 丢掉。

这里验证三层兜底：内置表 → 学习缓存 → 问模型（用假客户端）。

运行：python tests/test_translit.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_BUILD = tempfile.TemporaryDirectory()
os.environ["VOICE_AGENT_DATA_DIR"] = _BUILD.name
# 旧名字也指到同一个沙箱：两个都设，谁优先都落在同一个临时目录
os.environ["VOICE_AGENT_BUILD_DIR"] = _BUILD.name

from voice_agent import translit  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  [通过] " if ok else "  [失败] ") + name + ("  " + detail if detail else ""))
    if not ok:
        failures.append(name)


class FakeClient:
    """假装是 LLM：把每个词按首字母拼一个"汉字读音"回来。"""

    def __init__(self, reply: str | None = None) -> None:
        self.calls: list[str] = []
        self.reply = reply

    def chat(self, messages, tools=None):  # noqa: ANN001, ANN201
        prompt = messages[-1]["content"]
        self.calls.append(prompt)
        if self.reply is not None:
            return {"content": self.reply}
        words = [line.strip() for line in prompt.splitlines() if line.strip().isascii()
                 and line.strip().isalpha()]
        return {"content": json.dumps({w: "音" + w[0].upper() for w in words}, ensure_ascii=False)}


def main() -> int:
    print("=== 英文音译 ===")

    print("离线：内置表 + 字母兜底")
    check("常见词走内置表", translit.apply("打开 API 文档") == "打开 诶皮爱 文档",
          translit.apply("打开 API 文档"))
    check("长单词也有内置读音",
          translit.apply("deepseek 发布了新模型") == "迪普西克 发布了新模型",
          translit.apply("deepseek 发布了新模型"))
    check("中文原样不动", translit.apply("现在几点了") == "现在几点了")
    check("没有字母就不做任何事", translit.apply("D盘") == "滴盘", translit.apply("D盘"))
    check("查得到就是查得到", translit.lookup("python") == "派森")
    check("查不到返回空", translit.lookup("zzzznotaword") == "")
    spelled = translit.apply("HTTPSX 是什么")
    check("没学过的缩写逐字母念（总比整词丢掉强）",
          spelled != "HTTPSX 是什么" and "是什么" in spelled, spelled)

    print("\n问模型：学会之后写进缓存")
    client = FakeClient()
    out = translit.apply("zzzunknown 出现了", client=client)
    check("新词问了一次模型", len(client.calls) == 1, str(len(client.calls)))
    check("学到了读音并替换", out != "zzzunknown 出现了" and "出现了" in out, out)
    check("缓存文件写下来了", translit.cache_path().is_file(), str(translit.cache_path()))
    learned = json.loads(translit.cache_path().read_text(encoding="utf-8"))
    check("缓存里有那个词", "zzzunknown" in learned, str(list(learned))[:60])

    before = len(client.calls)
    again = translit.apply("zzzunknown 又来了", client=client)
    check("第二次不再问模型（走缓存）", len(client.calls) == before, str(len(client.calls)))
    check("缓存也真的生效", again == out.replace("出现了", "又来了"), again)

    plain = translit.apply("zzzunknown 没有客户端也念得出来")
    check("没有客户端时缓存照样管用", "zzzunknown" not in plain, plain)

    print("\n模型回复的各种写法都要能解析")
    check("标准 JSON", translit._parse('{"foo": "夫"}') == {"foo": "夫"})
    check("带解释文字的 JSON",
          translit._parse('好的：\n{"bar": "巴"}\n以上') == {"bar": "巴"})
    check("逐行写法", translit._parse("baz: 巴兹") == {"baz": "巴兹"})
    check("纯英文回复不算数", translit._parse("sorry I cannot") == {})
    check("空回复不算数", translit._parse("") == {})

    print("\n模型挂了也不能影响说话")
    class Dead:
        def chat(self, messages, tools=None):  # noqa: ANN001, ANN201
            raise RuntimeError("网络不通")

    out = translit.apply("zzzbroken 还在", client=Dead())
    check("问不到就用字母读法兜底", "zzzbroken" not in out and "还在" in out, out)

    check("统计信息可读", "内置" in translit.describe(), translit.describe())

    print()
    if failures:
        print("失败 " + str(len(failures)) + " 项：" + "、".join(failures))
        return 1
    print("英文音译全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
