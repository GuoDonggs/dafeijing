# 打包成 exe（Windows 免安装绿色版）

一句话：跑一个脚本，得到 **dist/VoiceAgent/ 一个文件夹**。把这个文件夹拷到别的 Windows
机器上就能跑，那台机器**不需要装 Python、不需要 pip install**。

- 双击 VoiceAgent.exe → 桌面界面（无控制台窗口）
- 命令行用 VoiceAgentCLI.exe 子命令 → 和 python -m voice_agent 子命令 完全一样

> 只做了 Windows 打包。脚本本身是跨平台的，但在 Linux/macOS 上打出来的是对应平台的可执行文件，
> 而 tools.py 里的系统调用（电源、剪贴板、模拟键盘）只实现了 Windows。

---

## 1. 怎么构建

在项目根目录（Windows，Python 3.10+，依赖已装好的环境里）：

    pip install pyinstaller
    python scripts/build_exe.py

第一次 3~10 分钟（要扫 numpy / scipy / cv2 / onnxruntime 几万条依赖），之后有缓存会快一些。

常用开关：

| 命令 | 作用 |
| --- | --- |
| python scripts/build_exe.py | 默认：出桌面版 + 命令行版两个 exe |
| python scripts/build_exe.py --console | 只出 VoiceAgentCLI.exe，构建快一些 |
| python scripts/build_exe.py --slim | 去掉 onnxruntime 的 CUDA / TensorRT provider DLL，产物小约 170 MB |
| python scripts/build_exe.py --clean | 连 PyInstaller 缓存一起清掉（改了 spec 却像没生效时用） |
| python scripts/build_exe.py --with-config | 把项目里的 config.yaml 也复制进产物（**里面有 API Key**） |
| python scripts/build_exe.py --dry-run | 只打印要执行的命令 |

也可以直接 pyinstaller packaging/voice-agent.spec，但那样要自己维护
--distpath/--workpath 和两个环境变量，不如走脚本。

脚本默认把中间产物放在 build/pyinstaller/，**不会动 build/ 里程序运行期的数据**
（memory.json、keywords.generated.txt、截图）。

## 2. 产物长什么样

    dist/VoiceAgent/
    ├─ VoiceAgent.exe          双击运行（GUI，无控制台）
    ├─ VoiceAgentCLI.exe       CLI：tools / doctor / selftest / run / ui --web …
    ├─ config.yaml             构建时从 config.example.yaml 生成（api_key 为空，可改）
    ├─ config.example.yaml     带注释的完整配置模板
    ├─ models/                 ← 模型放这里（见第 3 节，构建时不会创建）
    ├─ skills/                 示例技能（greet / daily_brief / network_info）
    ├─ voice_agent/
    │  └─ web/                 网页控制台的 index.html / style.css / app.js
    ├─ python312.dll、*.pyd、*.dll …   解释器与依赖（sherpa_onnx/、onnxruntime/、cv2/、numpy/ …）
    └─ base_library.zip、PYZ 归档

两个 exe 的区别只有 PE 头里的「控制台」标志和一个默认行为：

- VoiceAgent.exe **不带参数**时默认打开桌面界面（双击场景）；带了参数就按参数执行，
  但它是窗口程序，**stdout/stderr 是空的，看不到任何输出** —— 要用命令行就换下面那个。
- VoiceAgentCLI.exe 不带参数时打印帮助，和 python -m voice_agent 一模一样。

依赖全部**摊平在根目录**，没有 _internal/。这不是偷懒：程序里
PROJECT_ROOT = 「voice_agent 包目录的上一级」（voice_agent/config.py），
config.yaml、models/、skills/、build/ 都按它来找；依赖摊平后
PROJECT_ROOT 正好等于 exe 所在目录，所以「放在 exe 旁边」就是唯一需要知道的规则。
（实现方式是 spec 里的 contents_directory='.'，理由写在 spec 文件头。）

用的是 **onedir 而不是 onefile**：onefile 每次启动都要把几百 MB 的
onnxruntime / sherpa-onnx / scipy DLL 解压到 %TEMP% 下的 _MEIxxxxxx，冷启动十几秒，
退出还要删一遍；onedir 的启动速度和源码模式一样。

## 3. 要跟着 exe 一起发的东西

**模型不进包**（约 1.2 GB，而且换模型不该重新打包）。程序按这个顺序找模型目录：

1. exe 旁边的 models/（dist/VoiceAgent/models/）
2. 上一级目录的 voice-assistant/models/（dist/voice-assistant/models/）
3. config.yaml 里写了 models_dir: 就用它

所以最简单的做法：把已有的 models/ 整个拷到 exe 旁边。本机测试时不想复制 1.2 GB，
可以做一个目录联接（**不需要管理员权限**，删掉联接不会删原文件）：

    mklink /J "<产物目录>\models" "<原来的 models 目录>"

config.yaml 就放在 exe 旁边，用 VoiceAgentCLI.exe ui --web 的「配置」页也能改。
skills/ 同理：往 dist/VoiceAgent/skills/ 里丢 .yaml / .py 就多一个技能，不用重新打包。

**能发的 / 不能发的**：

- 能发：整个 dist/VoiceAgent/ 文件夹（压成一个 zip 更省事）。
- 别发：带 API Key 的 config.yaml（用 --with-config 生成的、或者你自己填过 key 的那份）。
  更安全的做法是保持 api_key: ""，让用户在目标机器上设环境变量
  DEEPSEEK_API_KEY（程序会自己读）。

## 4. 体积

实测（Python 3.12.2 + PyInstaller 6.22.2 + onnxruntime-gpu 1.29 + sherpa_onnx 1.13.7）：

| 构建 | 产物 | 体积 |
| --- | --- | --- |
| 默认（桌面版 + 命令行版） | dist/VoiceAgent/ | **499 MB**（1226 个文件），其中两个 exe 各 12.1 MB |
| python scripts/build_exe.py --console --slim | dist-slim/VoiceAgent/ | **322 MB**（1 个 exe） |

占大头的几项（默认构建里量出来的）：

| 组件 | 大约 | 说明 |
| --- | --- | --- |
| onnxruntime/（capi + providers） | 200 MB | 其中 onnxruntime_providers_cuda.dll 一个就 168 MB，--slim 会去掉它 |
| cv2/ | 111 MB | screen.py 的找图（模板匹配）用得上 |
| scipy/ + scipy.libs/ | 67 MB | audio.py 只用 scipy.signal.resample_poly 一个函数，但整个包都会进来 |
| sherpa_onnx/ | 27 MB | KWS / VAD / ASR / TTS 的 C 运行时（含它自带的一份 onnxruntime.dll） |
| numpy/ + numpy.libs/ | 26 MB | |
| PIL/ | 11 MB | 截图 / 缩图 |
| python312.dll + tcl/tk + 标准库 + 两个 exe | 约 40 MB | |
| pypinyin / requests / psutil / tkinter 数据 | 约 12 MB | |

两个 exe 各嵌了一份 PYZ 归档（约 12 MB），所以窗口版 + 命令行版会比只出命令行版多占一份。
介意体积就用 --console；再要小就加 --slim。

特意排除掉的（本机装了也不进包）：torch / torchvision（4 GB）、pandas（60 MB）、
matplotlib（30 MB）、PyQt5/PySide、IPython / jupyter、tensorflow、sklearn、sympy 等。
排除清单在 packaging/voice-agent.spec 的 EXCLUDES。

## 5. 构建完怎么验

    dist\VoiceAgent\VoiceAgentCLI.exe doctor     # 依赖 / 界面 / 模型目录 / 配置 / 音频设备
    dist\VoiceAgent\VoiceAgentCLI.exe tools      # 工具清单（不需要模型，秒出）
    dist\VoiceAgent\VoiceAgentCLI.exe selftest   # 端到端自检：模型加载 → 合成 → 识别回环 → 工具

selftest 全绿就说明 exe 里的 sherpa-onnx / onnxruntime / sounddevice DLL 都找得到。

## 6. 已知坑

- **杀毒软件误报（本机实测过，不是纸上谈兵）**。PyInstaller 的引导程序和
  「自解压 + 起子进程」的行为很像木马，360 / 火绒 / Defender 经常直接删掉 exe。
  本次构建就撞上了：这台机器上跑着 360 安全卫士（ZhuDongFangYu 主动防御），
  dist\VoiceAgent\VoiceAgentCLI.exe 在被执行过几次之后会被**无声删除**
  —— 同一份文件改个名放进同一个目录、或者放到 %TEMP% 里，也会在一两分钟后消失；
  窗口版的 VoiceAgent.exe 目前还在。Windows Defender 的历史记录里也能查到
  2026/8/17 的一条 Trojan:Win32/Wacatac.H!ml（PyInstaller 的经典误报）。
  处理办法二选一：
  1) 把 dist\VoiceAgent 加进杀软的信任区/白名单，然后重新构建；
  2) 直接从中间产物拷回来（COLLECT 之前的那份，字节完全一样）：
         copy build\pyinstaller\voice-agent\VoiceAgentCLI.exe dist\VoiceAgent\
  对外发布的话，买个代码签名证书给两个 exe 签一下（signtool sign /fd sha256 …），
  误报会少很多。spec 里已经关掉 UPX（upx=False），压缩过的 exe 误报率更高。
- **首次启动慢**。exe 本身启动很快（onedir），慢的是模型：第一次要加载约 1 GB 权重，
  老机器上 10~30 秒，之后稳定在几秒。VoiceAgent.exe 又是窗口程序，
  双击后到界面出现之前没有任何提示 —— 别以为它没启动。
- **缺 VC++ 运行库**。onnxruntime / sherpa-onnx / opencv 都是 MSVC 编译的，
  目标机器需要 **Microsoft Visual C++ 2015-2022 Redistributable (x64)**。
  症状是双击没反应、事件查看器里报 0xc000007b，或者提示找不到 VCRUNTIME140.dll /
  MSVCP140.dll。Win10 1909+ 一般自带，干净的 Server / 精简系统上要手动装（装完重启）。
- **别只拷 exe**。onedir 的 exe 离开同目录的 DLL 起不来，必须整个文件夹一起。
- **窗口版看不到输出**。VoiceAgent.exe tools 什么都不会打印（窗口程序没有 stdout）。
  命令行一律用 VoiceAgentCLI.exe。
- **自己的技能只能 import 已经打进包里的库**。技能是运行时动态加载的，
  PyInstaller 扫不到它们的 import。示例技能只用标准库 + requests（已打包）所以没问题；
  如果你的技能要 import 别的库，得把它加进 spec 的 hiddenimports 再重新打包。
- **路径**。放在中文路径或有空格的目录下没问题（程序内部全程 pathlib + str），
  但别放在需要管理员权限才能写的目录（例如 Program Files），
  程序要在旁边读写 build/（记忆、日志、截图）和 config.yaml。
- **改了 spec 像没生效**？加 --clean 再构建一次（PyInstaller 有缓存）。
- **构建报缺模块**：看 build/pyinstaller/voice-agent/warn-voice-agent.txt，
  里面列的是「扫到但没找到」的模块。pywin32、pytest、matplotlib.* 这类噪音可以忽略
  （已经被 EXCLUDES 挡在外面），只有真正运行时报 ModuleNotFoundError 的才要处理。

## 7. 目录里都有什么

    packaging/
    ├─ voice-agent.spec     PyInstaller 配置（两个 exe、数据文件、排除清单，取舍写在文件头）
    ├─ entrypoint.py        入口脚本：包外导入 + 双击默认开界面 + freeze_support
    └─ README.md            本文
    scripts/build_exe.py    构建脚本（检查 PyInstaller、清理、构建、报体积）

应用代码一行都没改（voice_agent/ 保持原样），打包相关的改动全在上面这三个文件里。


