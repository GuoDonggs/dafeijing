# -*- coding: utf-8 -*-
"""多音字读音修正：把"词 → 正确读音"合并进模型的发音词典。

问题出在哪：vits-zh 的中文前端是"先用 jieba 切词，再拿**发音词典**查读音"。
词典里有「银行 yín háng」「长大 zhǎng dà」这类词条，所以这些词读得对；
但词典只有 2 万条左右，**大量常用词它没有**（重庆、重阳、行走、会议、归还、
朝阳、调研、成都、都行、便宜、露面、露水、给予…）。词条查不到就退回"一个字
一个字念"，而单个汉字只存一个最常用的读音 —— 于是「重庆」被念成 zhòng qìng、
「成都」被念成 chéng dōu。

怎么修：不动模型，**在它自己的词典上补一层**——
把缺的词条按同样的格式（注音符号 + 声调）追加进去，生成一份合并词典，
再让 sherpa-onnx 用这份合并词典。读音从 pypinyin 的**词组读音**取
（它自带词组表，「重庆」直接给出 chóng qìng），拼音到注音符号的换算在本模块里，
并且有一条与模型词典对表的自检（见 tests/test_voices.py）。

用户自己的词（人名、公司名、术语）写到 <数据目录>/tts/polyphone.yaml：
    行: xíng
也可以命令行加：python -m voice_agent voices --fix-word 行=xing2

任何一步出问题都**退回模型的原始词典**，绝不让 TTS 起不来。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

__all__ = [
    "DEFAULT_WORDS", "syllable_tokens", "word_tokens", "merge_lexicon",
    "lexicon_for", "user_table_path", "load_user_words", "save_user_words",
    "meaningful_entries", "read_lexicon",
]

# ── 拼音 → 注音符号（模型 lexicon.txt 用的就是这套）────────────────────────
#: 模型 tokens.txt 里的声母
_INITIALS = {
    "b": "ㄅ", "p": "ㄆ", "m": "ㄇ", "f": "ㄈ", "d": "ㄉ", "t": "ㄊ", "n": "ㄋ", "l": "ㄌ",
    "g": "ㄍ", "k": "ㄎ", "h": "ㄏ", "j": "ㄐ", "q": "ㄑ", "x": "ㄒ",
    "zh": "ㄓ", "ch": "ㄔ", "sh": "ㄕ", "r": "ㄖ", "z": "ㄗ", "c": "ㄘ", "s": "ㄙ",
}
#: 韵母（含 i/u/ü 开头的复韵母）；键统一写成小写、ü 写成 v
_FINALS = {
    "a": "ㄚ", "o": "ㄛ", "e": "ㄜ", "ê": "ㄝ", "ai": "ㄞ", "ei": "ㄟ", "ao": "ㄠ",
    "ou": "ㄡ", "an": "ㄢ", "en": "ㄣ", "ang": "ㄤ", "eng": "ㄥ", "er": "ㄦ",
    "ong": "ㄨㄥ",
    "i": "ㄧ", "ia": "ㄧㄚ", "ie": "ㄧㄝ", "iao": "ㄧㄠ", "iou": "ㄧㄡ", "iu": "ㄧㄡ",
    "ian": "ㄧㄢ", "in": "ㄧㄣ", "iang": "ㄧㄤ", "ing": "ㄧㄥ", "iong": "ㄩㄥ",
    "u": "ㄨ", "ua": "ㄨㄚ", "uo": "ㄨㄛ", "uai": "ㄨㄞ", "uei": "ㄨㄟ", "ui": "ㄨㄟ",
    "uan": "ㄨㄢ", "uen": "ㄨㄣ", "un": "ㄨㄣ", "uang": "ㄨㄤ", "ueng": "ㄨㄥ",
    "v": "ㄩ", "ve": "ㄩㄝ", "üe": "ㄩㄝ", "van": "ㄩㄢ", "üan": "ㄩㄢ",
    "vn": "ㄩㄣ", "ün": "ㄩㄣ",
}
#: 声调 → 声调符号（第 5 声/轻声用 ˙）
_TONES = {"1": "ˉ", "2": "ˊ", "3": "ˇ", "4": "ˋ", "5": "˙", "0": "˙"}
#: 整音节（不加介音）：zhi chi shi ri zi ci si 里的 i 不发音
_SYLLABIC = ("zh", "ch", "sh", "r", "z", "c", "s")
#: y/w 开头按"介音 + 韵母"还原
_Y_FORMS = {
    "yi": "i", "ya": "ia", "ye": "ie", "yao": "iao", "you": "iou", "yan": "ian",
    "yin": "in", "yang": "iang", "ying": "ing", "yong": "iong",
    "yu": "v", "yue": "ve", "yuan": "van", "yun": "vn",
}
_W_FORMS = {
    "wu": "u", "wa": "ua", "wo": "uo", "wai": "uai", "wei": "uei", "wan": "uan",
    "wen": "uen", "wang": "uang", "weng": "ueng",
}
#: 少数"整音节"写法（模型词典里直接对应这几个符号）
_WHOLE = {"yo": "ㄧㄛ", "n": "ㄣ", "m": "ㄇ", "hm": "ㄏㄇ", "ng": "ㄣ", "ê": "ㄝ", "e": "ㄜ"}
#: 带声调的元音 → (基本元音, 声调)。用户手写"chóng qìng"这种太常见了，必须认。
_TONE_CHARS = {
    "ā": ("a", "1"), "á": ("a", "2"), "ǎ": ("a", "3"), "à": ("a", "4"),
    "ē": ("e", "1"), "é": ("e", "2"), "ě": ("e", "3"), "è": ("e", "4"),
    "ê": ("e", "5"),
    "ī": ("i", "1"), "í": ("i", "2"), "ǐ": ("i", "3"), "ì": ("i", "4"),
    "ō": ("o", "1"), "ó": ("o", "2"), "ǒ": ("o", "3"), "ò": ("o", "4"),
    "ū": ("u", "1"), "ú": ("u", "2"), "ǔ": ("u", "3"), "ù": ("u", "4"),
    "ǖ": ("v", "1"), "ǘ": ("v", "2"), "ǚ": ("v", "3"), "ǜ": ("v", "4"),
    "ü": ("v", "5"),
    "ń": ("n", "2"), "ň": ("n", "3"), "ǹ": ("n", "4"), "ḿ": ("m", "2"),
}
_PINYIN_RE = re.compile(r"^([a-züêv]+?)([1-5])?$")


def _split_tone(raw: str) -> tuple[str, str]:
    """把音节拆成 (不带调的拼音, 声调)。

    两种写法都收：数字调「chong2」和声调符号「chóng」；都没有就是轻声。
    """
    text = str(raw or "").strip().lower().replace("u:", "v").replace("ü", "v")
    tone = ""
    plain: list[str] = []
    for ch in text:
        if ch in _TONE_CHARS:
            base, mark = _TONE_CHARS[ch]
            plain.append(base)
            tone = tone or mark
        else:
            plain.append(ch)
    body = "".join(plain)
    match = _PINYIN_RE.match(body)
    if match and match.group(2):
        tone = match.group(2)          # 数字调优先
        body = match.group(1)
    return body, tone or "5"


def syllable_tokens(raw: str) -> list[str]:
    """一个拼音音节 → 注音符号 + 声调（和模型 lexicon.txt 的写法一致）。

    「chong2」→ ["ㄔ", "ㄨ", "ㄥ", "ˊ"]
    """
    body, tone = _split_tone(raw)
    if not body:
        return []
    initial = ""
    for cand in ("zh", "ch", "sh", "b", "p", "m", "f", "d", "t", "n", "l",
                 "g", "k", "h", "j", "q", "x", "r", "z", "c", "s"):
        if body.startswith(cand):
            initial = cand
            break
    rest = body[len(initial):]
    if not initial and body in _WHOLE:      # yo（唷）、n（嗯）这类整音节
        return list(_WHOLE[body]) + [_TONES.get(tone, "˙")]
    if not initial:
        # y/w 开头（以及零声母的 a/o/e…）
        if body in _Y_FORMS:
            rest, initial = _Y_FORMS[body], ""
        elif body in _W_FORMS:
            rest, initial = _W_FORMS[body], ""
        elif body.startswith("y"):
            rest = "i" + body[1:]
        elif body.startswith("w"):
            rest = "u" + body[1:]
    if initial in _SYLLABIC and rest == "i":
        rest = ""            # zhi/chi/shi/ri/zi/ci/si：那个 i 不发音
    if initial in ("j", "q", "x") and rest.startswith("u"):
        rest = "v" + rest[1:]      # ju/qu/xu 里的 u 其实是 ü
    if initial and rest == "o" and initial in ("b", "p", "m", "f"):
        rest = "o"                 # bo/po/mo/fo 直接是 ㄛ
    symbols: list[str] = []
    if initial:
        symbols.append(_INITIALS[initial])
    if rest:
        mapped = _FINALS.get(rest)
        if mapped is None:
            return []              # 认不出来的音节：宁可不写，也不要写错
        symbols.extend(list(mapped))
    elif not initial:
        return []
    symbols.append(_TONES.get(tone, "˙"))
    return symbols


def word_tokens(readings: str | list[str]) -> list[str]:
    """一个词的读音 → 完整符号序列。

    readings 可以是"chóng qìng"这样的整串，也可以是 pypinyin 那种**按字逐个**的
    列表（["chóng", "qìng"]）；用户词表里写的是整串，两种都要认 ——
    所以先把每个元素再按空格摊平一次。
    """
    raw = readings.split() if isinstance(readings, str) else [
        piece for item in readings for piece in str(item).split()]
    parts = [p for p in raw if p]
    tokens: list[str] = []
    for part in parts:
        piece = syllable_tokens(part)
        if not piece:
            return []
        tokens.extend(piece)
    return tokens


# ── 内置词表 ───────────────────────────────────────────────────────────────
#: 常见多音字词。**只列"模型词典里没有、或者读音不对"的**（构建时会自动过滤）。
DEFAULT_WORDS: tuple[str, ...] = (
    # 重：chóng / zhòng
    "重庆", "重阳", "重来", "重名", "重叠", "重回", "重逢", "重蹈覆辙", "重演",
    # 行：xíng / háng
    "行走", "行程", "行驶", "一行", "各行各业", "行家里手", "银行", "行长",
    # 长：zhǎng / cháng
    "长安", "长城", "长春", "长沙", "长江", "长跑", "增长", "长辈", "长见识",
    # 乐：yuè / lè
    "乐队", "乐器", "乐谱", "乐章", "乐山", "奏乐",
    # 会：huì / kuài
    "会议", "开会", "一会儿", "会儿",
    # 还：huán / hái
    "归还", "还给", "还债", "偿还", "返还原物",
    # 朝：zhāo / cháo
    "朝阳", "朝气蓬勃", "朝霞", "朝三暮四", "朝拜",
    # 调：diào / tiáo
    "调研", "调动", "调换", "声调", "情调", "调皮", "调节", "调整",
    # 都：dōu / dū
    "都行", "都是", "都要", "首都", "都市", "成都", "京都",
    # 便：pián / biàn
    "便宜", "方便", "便利", "便签", "大便",
    # 露：lù / lòu
    "露面", "露脸", "露马脚", "露水", "露天", "暴露", "披露",
    # 给：jǐ / gěi
    "给予", "供给", "补给", "给养", "交给",
    # 血：xuè / xiě
    "血液", "血压", "血管", "血型", "献血", "出血",
    # 角：jiǎo / jué
    "角色", "主角", "配角", "角逐", "号角", "角度", "墙角",
    # 强：qiáng / qiǎng
    "勉强", "强迫", "强求", "牵强", "强大", "倔强",
    # 弹：dàn / tán
    "子弹", "弹药", "弹弓", "弹琴", "弹奏", "反弹",
    # 假：jiǎ / jià
    "假期", "放假", "休假", "真假", "假装", "请假",
    # 教：jiāo / jiào
    "教书", "教课", "教师", "教室", "教育",
    # 落：luò / là / lào
    "落枕", "丢三落四", "落地", "落选",
    # 提：tí / dī
    "提防", "提高", "提醒", "提问",
    # 模：mó / mú
    "模样", "模子", "模型", "模仿",
    # 系：xì / jì
    "关系", "联系", "系鞋带", "系统",
    # 数：shù / shǔ
    "数量", "数数", "数学", "数落",
    # 挨：āi / ái
    "挨打", "挨骂", "挨着", "挨个",
    # 脉：mài / mò
    "脉搏", "动脉", "含情脉脉", "山脉",
    # 着：zhe / zháo / zhuó
    "着凉", "着急", "着火", "着手", "着陆", "看着",
    # 咽：yān / yàn / yè
    "咽喉", "咽下", "呜咽", "狼吞虎咽",
    # 折：zhé / shé / zhē
    "打折", "折本", "折腾", "骨折", "转折",
    # 参：cān / shēn / cēn
    "人参", "参差", "参加", "参考", "海参",
    # 差：chà / chā / chāi / cī
    "差劲", "差不多", "差错", "出差", "参差不齐",
    # 大：dà / dài
    "大夫", "大王", "大小",
    # 号：hào / háo
    "号码", "号叫", "信号", "口号",
    # 咖：kā / gā
    "咖啡", "咖喱",
    # 似：sì / shì
    "似的", "相似", "类似",
    # 粘：zhān / nián
    "粘贴", "粘住",
    # 扁：biǎn / piān
    "扁担", "扁舟",
    # 泊：bó / pō
    "停泊", "湖泊", "泊车", "血泊",
    # 嚼：jiáo / jué
    "嚼舌", "咀嚼", "细嚼慢咽",
    # 悄：qiāo / qiǎo
    "悄悄", "悄然",
    # 翘：qiào / qiáo
    "翘起", "翘首",
    # 畜：chù / xù
    "牲畜", "畜牧", "家畜",
    # 埋：mái / mán
    "埋怨", "埋没", "埋伏",
    # 率：shuài / lǜ
    "率领", "效率", "概率",
    # 抹：mǒ / mā / mò
    "抹布", "抹去", "抹墙",
    # 校：xiào / jiào
    "学校", "校长", "校场", "校对",
    # 秘：mì / bì
    "秘密", "秘鲁", "秘书",
    # 绿：lǜ / lù
    "绿色", "绿林", "绿豆",
    # 柏：bǎi / bó
    "柏树", "柏林",
    # 六：liù / lù
    "六安", "六月",
    # 番：fān / pān
    "番茄", "番禺", "三番五次",
    # 厦：shà / xià
    "大厦", "厦门",
    # 佛：fó / fú
    "佛教", "仿佛",
    # 应：yīng / yìng
    "应该", "答应", "应用", "应付", "应聘",
    # 与：yǔ / yù
    "参与", "与会", "与其",
    # 钻：zuān / zuàn
    "钻研", "钻石", "钻进",
    # 壳：ké / qiào
    "蛋壳", "地壳", "贝壳",
    # 拓：tuò / tà
    "开拓", "拓片",
    # 解：jiě / jiè / xiè
    "解放", "押解", "姓解",
    # 单：dān / shàn / chán
    "单独", "单于", "姓单",
    # 监：jiān / jiàn
    "监督", "监生",
    # 什：shén / shí
    "什么", "什锦",
    # 裳：shang / cháng
    "衣裳", "霓裳",
    # 地：dì / de
    "地方", "地球", "土地",
    # 了：le / liǎo
    "了解", "了不起", "受不了",
    # 得：dé / děi / de
    "得到", "得亏", "不得不",
    # 少：shǎo / shào
    "多少", "少年", "少爷", "减少",
    # 传：chuán / zhuàn
    "传说", "传记", "自传", "水浒传",
    # 卷：juàn / juǎn
    "试卷", "卷子", "卷起",
    # 卡：kǎ / qiǎ
    "卡片", "关卡", "卡住", "发卡",
    # 夹：jiā / jiá
    "夹住", "夹克", "夹生",
    # 尽：jìn / jǐn
    "尽力", "尽管", "尽快", "尽头",
    # 冲：chōng / chòng
    "冲动", "冲着", "冲床",
    # 处：chǔ / chù
    "处理", "相处", "处境", "到处", "办事处",
    # 铺：pū / pù
    "铺床", "店铺", "卧铺",
    # 种：zhǒng / zhòng
    "种子", "种类", "种地", "播种",
    # 缝：féng / fèng
    "缝补", "缝隙", "门缝",
    # 磨：mó / mò
    "磨刀", "磨面", "磨蹭",
    # 迎：yíng,
    # 冠：guān / guàn
    "冠军", "皇冠", "鸡冠",
    # 载：zài / zǎi
    "载重", "记载", "转载", "下载",
    # 供：gōng / gòng
    "供给", "提供", "供品", "口供",
    # 量：liàng / liáng
    "数量", "重量", "量力而行", "测量",
    # 曲：qū / qǔ
    "弯曲", "歌曲", "曲子", "曲折",
    # 济：jì / jǐ
    "经济", "济南", "救济",
    # 识：shí / zhì
    "认识", "标识",
    # 症：zhèng / zhēng
    "症状", "症结",
    # 颤：chàn / zhàn
    "颤抖", "打颤",
    # 佣：yōng / yòng
    "佣人", "佣金",
    # 巷：xiàng / hàng
    "巷子", "巷道",
    # 华：huá / huà
    "华丽", "华山", "中华",
    # 纶：lún / guān
    "涤纶", "纶巾",
)


def read_lexicon(path: Path) -> dict[str, str]:
    """读模型词典 → {词: "符号 序列"}（同一个词以最后一条为准）。"""
    table: dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return table
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) > 1:
            table[parts[0]] = " ".join(parts[1:])
    return table


def user_table_path() -> Path:
    from . import paths  # noqa: PLC0415

    return paths.sub("tts", create=True) / "polyphone.yaml"


def load_user_words() -> dict[str, str]:
    """用户自己加的词（<数据目录>/tts/polyphone.yaml）。读不出来就当空的。"""
    path = user_table_path()
    if not path.is_file():
        return {}
    try:
        import yaml  # noqa: PLC0415

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 - 用户手写的文件坏了也不能影响合成
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k).strip(): str(v).strip() for k, v in data.items() if str(k).strip() and str(v).strip()}


def save_user_words(words: dict[str, str]) -> None:
    import yaml  # noqa: PLC0415

    path = user_table_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    header = ("# 多音字读音修正：词 → 拼音（空格分开，数字表示声调，不写数字是轻声）\n"
              "# 例：\n#   重庆: chóng qìng\n#   行: xing2\n")
    body = yaml.safe_dump(words, allow_unicode=True, sort_keys=True)
    path.write_text(header + body, encoding="utf-8")


def _readings_of(word: str) -> list[str]:
    """用 pypinyin 的词组读音（含声调数字）拿一个词的读音。"""
    try:
        from pypinyin import Style, lazy_pinyin  # noqa: PLC0415

        return list(lazy_pinyin(word, style=Style.TONE3, errors="default"))
    except Exception:  # noqa: BLE001 - 没装 pypinyin 就修不了多音字，退回原样
        return []


def meaningful_entries(model_lexicon: Path | str | None = None,
                       extra: dict[str, str] | None = None) -> dict[str, list[str]]:
    """挑出真正需要修正的词 → 符号序列。

    - 词表 = 内置的常用多音字词 + 用户自己加的；
    - 读音用 pypinyin 的词组读音换算成注音符号；
    - **只在"模型词典里没有这个词、或者读音和我们要的不一样"时才收**，
      已经是正确读音的不重复写（免得把词典撑大、也免得盖掉模型自己的好词条）。
    """
    table = read_lexicon(Path(model_lexicon)) if model_lexicon else {}
    result: dict[str, list[str]] = {}
    words = list(DEFAULT_WORDS) + list((extra or {}).keys())
    user = {k: v for k, v in (extra or {}).items()}
    for word in words:
        if not word or not word.strip():
            continue
        word = word.strip()
        if any(ch in word for ch in "，。！？、；：（）「」《》——…"):
            continue          # 带标点的不是词
        readings = [user[word]] if word in user else _readings_of(word)
        tokens = word_tokens(readings)
        if not tokens:
            continue
        current = table.get(word, "")
        if current and current.split() == tokens:
            continue          # 模型本来就是对的
        result[word] = tokens
    return result


def merge_lexicon(model_lexicon: Path, out_path: Path,
                  extra: dict[str, str] | None = None,
                  log=None) -> Path | None:
    """生成合并词典（模型词典 + 我们的修正），返回写入的路径。

    写不成就返回 None（调用方退回模型原词典）。
    """
    source = Path(model_lexicon)
    if not source.is_file():
        return None
    entries = meaningful_entries(source, extra)
    if not entries:
        return None
    try:
        text = source.read_text(encoding="utf-8")
    except OSError:
        return None
    lines = [ln for ln in text.splitlines() if ln.strip()]
    # 去掉模型里同名的旧词条，再把我们这份写在最后（同名以我们为准）
    kept = [ln for ln in lines if ln.split()[0] not in entries]
    for word, tokens in sorted(entries.items()):
        kept.append(word + " " + " ".join(tokens))
    out_path = Path(out_path)
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        temp = out_path.with_suffix(out_path.suffix + ".tmp")
        temp.write_text("\n\n".join(kept) + "\n", encoding="utf-8")
        temp.replace(out_path)
        meta = {"source": str(source), "size": source.stat().st_size,
                "words": len(entries), "version": 2,
                # 用户词表的指纹：改了词表就要重新合并，否则新加的词不会生效
                "user": json.dumps((extra or {}), ensure_ascii=False, sort_keys=True)}
        out_path.with_suffix(out_path.suffix + ".meta.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        if log is not None:
            log("[tts] 合并发音词典失败（" + str(exc)[:60] + "），用模型自带的")
        return None
    if log is not None:
        log("[tts] 多音字词典已合并：" + str(len(entries)) + " 个词 → " + str(out_path))
    return out_path


def lexicon_for(model_lexicon: Path, extra: dict[str, str] | None = None,
                log=None) -> Path:
    """TTS 该用哪份词典：有修正就用合并的，否则用模型自带的。"""
    from . import paths  # noqa: PLC0415

    model_lexicon = Path(model_lexicon)
    try:
        user = load_user_words()
        if extra:
            user.update(extra)
        target = paths.sub("tts", create=True) / "lexicon.merged.txt"
        meta_path = target.with_suffix(target.suffix + ".meta.json")
        need = True
        if target.is_file() and meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                need = (meta.get("source") != str(model_lexicon)
                        or int(meta.get("size", -1)) != model_lexicon.stat().st_size
                        or int(meta.get("version", 0)) != 2
                        or meta.get("user") != json.dumps(user, ensure_ascii=False,
                                                          sort_keys=True))
            except Exception:  # noqa: BLE001
                need = True
        if not need and target.is_file():
            return target
        merged = merge_lexicon(model_lexicon, target, user, log=log)
        return merged or model_lexicon
    except Exception as exc:  # noqa: BLE001 - 词典修正失败不该影响合成
        if log is not None:
            log("[tts] 多音字词典没做成（" + str(exc)[:60] + "），用模型自带的")
        return model_lexicon
