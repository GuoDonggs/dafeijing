# -*- mode: python ; coding: utf-8 -*-
"""voice-agent 的 PyInstaller 打包配置（Windows 绿色版）。

别直接敲 pyinstaller，走脚本：python scripts/build_exe.py
（脚本负责清理旧产物、设置下面的环境变量、最后打印体积。）

── 三个关键取舍，动之前先看完 ──────────────────────────────────────────────

1) onedir（COLLECT）而不是 onefile。
   onefile 每次启动都要把几百 MB 的 onnxruntime / sherpa-onnx / scipy DLL
   解压到 %TEMP% 下的 _MEIxxxxxx 目录，冷启动十几秒，退出时还要再删一遍；
   onedir 就是一个文件夹，依赖原样躺在磁盘上，启动速度和源码模式一样快。
   代价是分发的是「目录」而不是单文件 —— 这正好也方便把 models/ 放在旁边。

2) 一次构建出两个 exe：VoiceAgent.exe（窗口）+ VoiceAgentCLI.exe（控制台）。
   Windows 上「有没有控制台」是写进 PE 头里的属性，运行时改不了：
   - 只出 console=True：双击先弹一个黑框，语音助手这种常驻窗口程序很出戏；
   - 只出 console=False：VoiceAgent.exe tools 的输出没人看得到，命令行没法用。
   所以两个都出。它们共用同一份 Analysis / PYZ / COLLECT（依赖只存一份），
   差别只有 PE 头和一个默认行为（见 packaging/entrypoint.py）。
   --console 只出命令行版，构建时间能省掉一次窗口版链接的时间。

3) contents_directory='.'：把依赖摊平到 exe 同级，不塞进 _internal/。
   PyInstaller 6 默认把依赖放进 dist/VoiceAgent/_internal/，而程序里
   PROJECT_ROOT = voice_agent 包目录的上一级（voice_agent/config.py 第 21 行），
   config.yaml、models/、skills/、build/ 全按它来找。
   摊平之后 PROJECT_ROOT 正好等于 exe 所在目录 —— 用户把 models/ 和 config.yaml
   放在 exe 旁边就能直接跑，应用代码一行都不用改（约束：不改 voice_agent/）。

环境变量开关（scripts/build_exe.py 会设置）：
   VOICE_AGENT_CONSOLE_ONLY=1   只出命令行版
   VOICE_AGENT_SLIM=1           去掉 onnxruntime 的 CUDA / TensorRT DLL（省约 170 MB）
   VOICE_AGENT_WITH_CHATTS=1    把 ChatTTS 那套（torch 3.9 GB）也打进去

4) **默认不带 ChatTTS**（2026-09-20 定的）。
   实测：整个产物 4.76 GB 里 torch 一个包就占 3.9 GB（82%），而它只服务一个可选
   音色引擎 ChatTTS；用户实际用的是默认的 sherpa-onnx VITS 音色。去掉之后安装包
   从 3.7 GB 降到 GB 级以下，启动也更快。源码运行/开发完全不受影响 —— 想连
   ChatTTS 一起打包，就设 VOICE_AGENT_WITH_CHATTS=1（speech.py 在没有它的时候
   会给出明确提示，而不是抛一个看不懂的 ImportError）。
"""

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

# SPECPATH 由 PyInstaller 注入，指向本 .spec 所在的 packaging/ 目录
PACKAGING_DIR = Path(SPECPATH).resolve()
PROJECT_ROOT = PACKAGING_DIR.parent
WEB_DIR = PROJECT_ROOT / "voice_agent" / "web"
SKILLS_DIR = PROJECT_ROOT / "skills"


def _switch(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


CONSOLE_ONLY = _switch("VOICE_AGENT_CONSOLE_ONLY")
SLIM = _switch("VOICE_AGENT_SLIM")
#: 默认**不带** ChatTTS/torch（见文件头第 4 条）；要带就设这个开关
WITH_CHATTS = _switch("VOICE_AGENT_WITH_CHATTS")
# exe 的版本资源（属性 → 详细信息里那份版本号）。由 build_exe.py 生成；
# 直接敲 pyinstaller 时用仓库里现成的那份，没有就不带版本号。
VERSION_FILE = os.environ.get("VOICE_AGENT_VERSION_FILE") or str(PACKAGING_DIR / "version_info.txt")
if not Path(VERSION_FILE).is_file():
    VERSION_FILE = None

# exe 的图标：用仓库根目录的 icon.webp 生成的那份 .ico
# （scripts/make_icon.py，build_exe.py 打包前会自动跑一次）。
ICON_FILE = os.environ.get("VOICE_AGENT_ICON") or str(PACKAGING_DIR / "voice-agent.ico")
if not Path(ICON_FILE).is_file():
    print("[spec] 没找到图标 " + str(ICON_FILE) + "，用默认图标")
    ICON_FILE = None

print("[spec] 构建模式：" + ("只出命令行版 VoiceAgentCLI.exe" if CONSOLE_ONLY
                             else "窗口版 VoiceAgent.exe + 命令行版 VoiceAgentCLI.exe"))
print("[spec] 精简 CUDA/TensorRT：" + ("开" if SLIM else "关"))
print("[spec] ChatTTS/torch：" + ("打进包里" if WITH_CHATTS
                                 else "不带（默认；要带请设 VOICE_AGENT_WITH_CHATTS=1）"))

# ── 数据文件 ───────────────────────────────────────────────────────────────
# web/ 是网页控制台的三个静态文件，程序用 Path(__file__).parent / "web" 找它们，
# 不打进去 --web 一打开就是 500。
datas = [(str(p), "voice_agent/web") for p in sorted(WEB_DIR.glob("*")) if p.is_file()]

# 带注释的示例配置放到 exe 旁边，用户要改配置时有得抄
_example_config = PROJECT_ROOT / "config.example.yaml"
if _example_config.is_file():
    datas.append((str(_example_config), "."))

# 示例技能：技能目录就是 PROJECT_ROOT/skills，放进去开箱就有 3 个演示技能可调用
if SKILLS_DIR.is_dir():
    datas += [(str(p), "skills") for p in sorted(SKILLS_DIR.iterdir()) if p.is_file()]

# 自定义工具示例：和 skills 同一套格式，放在 tools/ 里
TOOLS_DIR = PROJECT_ROOT / "tools"
if TOOLS_DIR.is_dir():
    datas += [(str(p), "tools") for p in sorted(TOOLS_DIR.iterdir()) if p.is_file()]

# ChatTTS 的 res/ 是数据文件（同音字表、分词表），只收 Python 模块是不够的：
# 少了它运行时报 "No such file: ChatTTS/res/homophones_map.json"。
def _add_package_data(package, subdir=""):
    try:
        import importlib.util
        spec = importlib.util.find_spec(package)
    except Exception:
        spec = None
    if spec is None or not spec.submodule_search_locations:
        print("[spec] 没装 " + package + "，跳过它的数据文件")
        return
    root = Path(list(spec.submodule_search_locations)[0])
    src = root / subdir if subdir else root
    if src.is_dir():
        datas.append((str(src), package + ("/" + subdir if subdir else "")))
        print("[spec] 收 " + package + "/" + subdir + " 的数据文件")


if WITH_CHATTS:
    _add_package_data("ChatTTS", "res")
    # transformers / tokenizers 的钩子（PyInstaller 自带）会处理自己的数据，
    # 这里只补 ChatTTS 自己的。
else:
    print("[spec] 不带 ChatTTS，跳过它的 res/ 数据文件")

# ── 二进制 ─────────────────────────────────────────────────────────────────
# sherpa_onnx 把 C 运行时放在 sherpa_onnx/lib/ 下（含它自己那份 onnxruntime.dll）。
# Python 扩展模块 _sherpa_onnx.*.pyd 会被自动收集，但它依赖的 DLL 不是 Python 模块，
# 必须显式收。collect_dynamic_libs 保留 lib/ 这个层级很关键：
# CPython 载入 .pyd 走的是 LOAD_WITH_ALTERED_SEARCH_PATH，会在 .pyd 所在目录找依赖，
# 层级一改（比如全丢到根目录）就可能和 onnxruntime 包的 capi/onnxruntime.dll 撞车。
binaries = collect_dynamic_libs("sherpa_onnx")
# onnxruntime 的 provider 插件（capi/*.dll）：官方 hook 也会收一遍，这里显式写出来
# 是为了 SLIM 模式下能过滤掉 CUDA/TensorRT 那两个大家伙。重复项 PyInstaller 会去重。
binaries += collect_dynamic_libs("onnxruntime")

# ── 隐藏导入 ───────────────────────────────────────────────────────────────
# 包内模块：gui / webui / screen / cv2 / PIL 这些都是函数体内才 import 的，
# 用 collect_submodules 一次收全，省得以后加模块忘了改这里。
hiddenimports = collect_submodules("voice_agent")
hiddenimports += [
    "cv2",              # screen.py：截图找图（模板匹配）
    "PIL.Image",        # screen.py：缩图
    "PIL.ImageGrab",    # screen.py：抓屏
    "yaml",
    "requests",
    "psutil",
    "_sounddevice_data",  # PortAudio 的 DLL 装在这个包里，sounddevice 是条件 import
]
hiddenimports += collect_submodules("pypinyin")   # 拼音词库分散在多个子模块

# ── 排除 ───────────────────────────────────────────────────────────────────
# 本机装了 torch(4 GB) / pandas / matplotlib 这些大家伙：不排掉的话 PyInstaller
# 会顺着 import 链条把它们全拖进产物。排掉它们只是不让它们进包，
# 程序本身一行都没用到（doctor 会如实报告依赖）。
EXCLUDES = [
    "matplotlib", "mpl_toolkits", "pandas", "IPython", "IPython.core",
    "jupyter", "jupyter_client", "jupyter_core", "notebook", "nbconvert", "nbformat",
    # 注意：PyQt6 不能排 —— 桌面界面就是用它写的，排掉之后 exe 起不来。
    # 只排我们不用的那几个 GUI 框架（Tkinter 也已经不用了）。
    "PyQt5", "PySide2", "PySide6", "wx", "tkinter",
    "torchvision",
    "tensorflow", "keras", "sklearn",
    "cupy", "pyarrow", "dask",
    "pytest", "_pytest", "sphinx", "docutils",
]

if not WITH_CHATTS:
    # ChatTTS 那一套（torch 一个包就 3.9 GB）默认不进包，理由见文件头第 4 条。
    # torchgen 跟着 torch 一起排 —— torch 自己 import 它，所以"带 torch"和
    # "排 torchgen" 不能并存；反过来不带 torch 时排掉它不会误伤任何东西。
    EXCLUDES += [
        "torch", "torchgen", "torchaudio",
        "ChatTTS", "einx", "sympy", "vector_quantize_pytorch",
        "numba", "llvmlite",
        "transformers", "tokenizers", "safetensors", "huggingface_hub", "hf_xet",
    ]

a = Analysis(
    [str(PACKAGING_DIR / "entrypoint.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)

if not WITH_CHATTS:
    # excludes 管的是"模块图"，可 torch 的 DLL 是**二进制**那一侧被 hook 拖进来的
    # （实测残留 torch/lib/cudnn64_9.dll）。这里再按路径滤一道，产物里不留 torch 目录。
    _before = len(a.binaries)

    def _in_torch(entry):
        # 源路径（site-packages\torch\lib\…）和产物里的目标目录（torch\lib）都看一眼
        return any(part.replace("/", "\\").lower().split("\\").count("torch") > 0
                   for part in (str(entry[0]), str(entry[1])))

    a.binaries = [entry for entry in a.binaries if not _in_torch(entry)]
    if _before != len(a.binaries):
        print("[spec] 丢掉 " + str(_before - len(a.binaries)) + " 个 torch 残留二进制")

if SLIM:
    # onnxruntime-gpu 里 CUDA provider 一个 DLL 就 168 MB，TensorRT 那个也要 0.9 MB。
    # 排掉之后 ort.get_available_providers() 里就不会再有 CUDAExecutionProvider，
    # 程序会如实报告「CPU（onnxruntime 里没有 CUDA）」并照常跑 CPU。
    _DROP = {
        "onnxruntime_providers_cuda.dll",
        "onnxruntime_providers_tensorrt.dll",
        "onnxruntime_providers_tensorrt_plugin.dll",
    }
    _before = len(a.binaries)
    a.binaries = [entry for entry in a.binaries if Path(entry[0]).name.lower() not in _DROP]
    print("[spec] SLIM：丢掉 " + str(_before - len(a.binaries)) + " 个 GPU provider DLL")

pyz = PYZ(a.pure)

# 两个 exe 的公共参数：
#   exclude_binaries=True  → 依赖不进 exe，交给下面唯一的 COLLECT 收（共享一份）
#   contents_directory="." → 依赖摊平到 dist/VoiceAgent/ 根目录（见文件头第 3 条）
#   upx=False              → 不用 UPX 压缩：省不了多少，还容易引发杀软误报
_COMMON = dict(
    exclude_binaries=True,
    contents_directory=".",
    upx=False,
    strip=False,
    debug=False,
    bootloader_ignore_signals=False,
    version=VERSION_FILE,        # exe 属性里的版本号（取自 voice_agent.__version__）
    icon=ICON_FILE,              # exe 自己的图标（任务栏、资源管理器里看到的）
)

targets = []
if not CONSOLE_ONLY:
    targets.append(EXE(
        pyz, a.scripts, [],
        name="VoiceAgent",
        console=False,                     # 双击不弹黑框
        disable_windowed_traceback=False,  # 崩了弹窗显示栈，别让用户对着空气发呆
        **_COMMON,
    ))
targets.append(EXE(
    pyz, a.scripts, [],
    name="VoiceAgentCLI",
    console=True,                          # 命令行必须能看见输出
    disable_windowed_traceback=False,
    **_COMMON,
))

coll = COLLECT(
    *targets,
    a.binaries,
    a.datas,
    name="VoiceAgent",
    upx=False,
    upx_exclude=[],
    strip=False,
)
