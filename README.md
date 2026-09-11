# 语音控制电脑的 Agent（小爱同学式）

对着电脑喊一声唤醒词，说一句人话，电脑就照做，再用语音把结果报给你：

    你： 「大肥鲸」
    它： 「我在」
    你： 「帮我看看 D 盘还剩多少空间」
    它： 「D 盘还剩 144 G，用了百分之八十四。」

全程本地推理：唤醒词、语音识别、语音合成都在你自己的电脑上跑，麦克风音频不出本机。
大模型只负责「听懂人话」这一步，而且**可以不用** —— 不配 API Key 也能干活。

桌面界面用 **PyQt6** 写的：启动后是一块**竖着的、没有系统标题栏的圆角面板**，
中间只有一个会呼吸的圆球和状态文字 —— 打开就知道「它在不在听」。
对话、工具、技能、设置、设备、日志、关于全部收进左上角的 ☰ 菜单，
点开是独立窗口。图标是内联 SVG，跟随主题色实时上色，不带任何图片资源。
另外还有网页版，两者共用同一套后端，也都能打包成免安装的 exe。

---

## 目录

- [一、它能做什么](#一它能做什么)
- [二、快速开始](#二快速开始)
- [三、界面](#三界面)
- [四、语音链路：算力与音质怎么选](#四语音链路算力与音质怎么选)
- [五、对话：上下文与连续对话](#五对话上下文与连续对话)
- [六、自己加工具](#六自己加工具)
- [七、多模型：不同用途挂不同的模型](#七多模型不同用途挂不同的模型)
- [八、工具清单](#八工具清单)
- [九、自定义技能](#九自定义技能)
- [十、声纹：只认你的声音](#十声纹只认你的声音)
- [十一、实战：找图 + 连点器](#十一实战找图--连点器)
- [十二、命令行](#十二命令行)
- [十三、配置文件详解](#十三配置文件详解)
- [十四、关键设计取舍](#十四关键设计取舍)
- [十五、实测数据](#十五实测数据)
- [十六、打包成 exe](#十六打包成-exe)
- [十七、常见问题](#十七常见问题)
- [十八、目录结构](#十八目录结构)
- [十九、已知限制](#十九已知限制)

## 一、它能做什么

| 能力 | 说明 |
| --- | --- |
| 🖥 桌面面板 | PyQt6 无边框竖版面板（384 宽）：只有圆球和状态，其余全在 ☰ 菜单；22 个内联 SVG 图标随主题色上色 |
| 🎤 声纹 | 开启后只有你的声音能唤醒；用 3D-Speaker 声纹模型（512 维），实测同人 1.0 / 异人 0.28 |
| 🌐 网页版 | 同一套后端的浏览器界面，深浅双主题，适合在另一台机器上查看 |
| 🧩 自定义技能 | 往 skills/ 丢一个 YAML 或 Python 文件，助手立刻多一项能力；界面里可新建、编辑、试运行 |
| 🎙 唤醒词监听 | 常驻低开销监听，唤醒词随便改，中文自动转拼音音素 |
| ⏱ 说完就回待命 | 一次任务做完自动回到「等待唤醒」；唤醒后一直没提问也会自动回落，不会一直占着麦克风 |
| ✋ 随时打断 | 播报或执行中再喊一次唤醒词，立即停播、取消任务、重新听你说 |
| 🗣 本地语音识别 | Paraformer 中文模型，RTF 0.03（1 秒音频约 30 毫秒出字） |
| 🔊 本地语音合成 | VITS（快，CPU）或 ChatTTS（更自然，显卡）二选一，长回复按句流式播放 |
| ⚙️ 算力可选 | CPU / GPU 自动或手动，三档资源占用预设，界面里直接切 |
| 🧠 多模型 | 主对话、同意判定、看图可以分别挂不同的模型和思考深度 |
| 🪟 电脑控制 | 41 个内置工具：应用、网址、搜索、音量、媒体、截图、剪贴板、文件读写、通配符查找、内容搜索、打字、快捷键、窗口与进程、锁屏、电源、命令、记忆 |
| 🖱 鼠标模拟 | 移动、单击、连点、右键、拖拽、滚轮 —— 真正能替你操作界面 |
| 🔍 屏幕找图 | 给它一张小图，它在屏幕上找到并返回坐标，可多尺度匹配、抗缩放 |
| 🖼 图像缩放 | 缩放图片到指定尺寸；先把截图压小再送视觉模型，能省下一大半 token |
| 👁 看图问答 | look_at_screen：截屏交给视觉模型，回答「屏幕上是什么情况」 |
| 📋 应用映射表 | 用 apps.yaml 把「我的项目」映射到任意程序或路径，说「打开我的项目」就行 |
| 🔒 敏感操作确认 | 关机、执行命令、拖拽这类先问一句；**没有确认通道时一律拒绝**，不会因为漏传参数就放行 |
| 📦 打包 exe | onedir 绿色版，拷到没装 Python 的机器就能用；模型放在 exe 旁边 |
| 🧪 离线可验证 | 6 个测试脚本、161 项断言，不接麦克风也能跑完 |

---

## 二、快速开始

### 1. 装依赖

```powershell
python -m pip install -r requirements.txt
```

### 2. 准备模型（约 560 MB）

```powershell
python scripts/download_models.py
```

> 国内直连 GitHub Releases 实测只有几十 KB/s。脚本默认走 gh-proxy.com 镜像，
> 失败自动换源；--mirror "" 可切回直连。

模型落在**项目自己的 ./models/** 下，拷走整个目录就能跑，不依赖外部路径。

**已经装过别的 sherpa-onnx 项目？** 不用重复下载：程序会按顺序自动探测
./models 和 ../voice-assistant/models，也可以在 config.yaml 里写死 models_dir。
（探测到哪儿了、有没有缺文件，跑 `python -m voice_agent doctor` 一眼就能看到。）

### 3. 配置

```powershell
Copy-Item config.example.yaml config.yaml
```

至少看一眼这几项：

```yaml
wake:
  keywords: ["小爱同学"]      # 想叫它什么就写什么
speech:
  profile: balanced          # fast(低占用) / balanced / quality(更好听但慢)
  device: auto               # auto / cpu / cuda
llm:
  api_key: ""                # 留空则读环境变量 DEEPSEEK_API_KEY；不填也能用
```

### 4. 自检，然后开跑

```powershell
python -m voice_agent selftest     # 不用麦克风，验证模型与工具
python -m voice_agent ui           # 打开桌面窗口
```

---

## 三、界面

### 桌面窗口（默认，PyQt6）

```powershell
python -m voice_agent ui                  # 启动后自动开始监听
python -m voice_agent ui --no-autostart   # 只开面板，不自动开麦克风
```

**主界面是一块竖长方形面板，没有系统标题栏**（384 宽，高度按屏幕自适应）：

```text
╭──────────────────────────────╮
│  ☰                       — ✕  │   ← 平时隐形，鼠标移上来才浮现
│                              │
│             ◉                │
│           正在听              │
│    说指令就好，8 秒内没有提问    │
│    我会回到待命                │
│                              │
│    [启动监听]  [停止]  [打断]   │
│                              │
│   唤醒 大肥鲸      算力 CPU     │
│   合成 vits       声纹 关闭     │
╰──────────────────────────────╯
```

- **无边框**：圆角、描边、最小化/关闭按钮都是自己画的，看起来像浮在桌面上的卡片；
- **拖动**：按住面板任意空白处就能拖走，位置会记住；
- **标题栏只在鼠标移上来时浮现**，平时整个面板只有圆球和状态；
- 顶部那排按钮平时透明度 0，进入窗口后 220ms 淡入 —— 这也是"过渡动效"的一部分。

圆球本身就是状态显示器：

| 状态 | 圆球的表现 |
| --- | --- |
| 未启动 | 灰色、静止 |
| 待命 | 蓝色，极轻微的明暗起伏（"我在，但没在干活"） |
| 正在听 | 青色，向外扩散的光环有节奏地脉动；有声音时跟着鼓一下 |
| 思考中 | 琥珀色，脉动更快 |
| 播报中 | 紫色，脉动平缓 |

**点一下圆球 = 打断当前任务**（和喊唤醒词等效）。

其它功能全在左上角 **☰ 菜单**里，点开是独立窗口，关掉就回到面板：

| 菜单项 | 内容 |
| --- | --- |
| 对话记录 | 对话历史 + 文字指令输入 |
| 工具 | 30 个内置 + 技能工具，可搜索、可试运行 |
| 技能 | 新建 / 编辑 / 删除 / 重新加载 |
| 设置 | 分组卡片，开关和分段控件**改完立即生效** |
| 设备 | 麦克风与扬声器、录音测试 |
| 运行日志 / 关于 | 排查问题、版本与环境 |

这些窗口同样是无边框圆角、可以拖动，打开时有 170ms 淡入。
页面是**按需创建**的：没点开过的页面不占内存，启动也更快。

### 输出音量

设置页「语音与算力 → 输出音量」是一条滑杆（0~150%），拖动**即时生效**：

- 拖动过程中只改内存里的总增益，声音立刻跟着变；
- **松手才写配置文件** —— 否则每动一格就要重写一遍 config.yaml；
- 旁边的按钮一键静音 / 取消静音；
- 网页控制台里也有，走 `POST /api/volume`。

实现上是一个模块级的总增益（audio.py 的 set_output_gain），播放时统一乘上去。
放在配置对象里不行：播报和提示音走的是两条路径、各读各的 cfg，收敛到一处才能
"拖一下立刻听到区别"，也不用重启引擎。

配置里对应 `audio.output_gain`（0.0~2.0，1.0 = 原始音量）。

### 主题色

设置页「外观 → 主题色」是一排色点，点一下整个界面跟着换：

| 预设 | 颜色 |
| --- | --- |
| blue | #0A84FF（默认，iOS 系统蓝） |
| teal / green / purple / pink / orange / indigo / graphite | 见 config.example.yaml |

也可以点最后一个色点开取色盘，填任意颜色。配置里写 `ui.accent`（预设名或
`#RRGGBB`）。**不用重启**：换完立刻 setStyleSheet 重刷，主面板的圆球、
按钮图标、音量条、提示条一起变色。

> 图标是"按颜色渲染成位图再缓存"的（这样高分屏不糊），所以光换样式表不够 ——
> 换色时会把用主色画过的控件重做一遍（main_window.set_accent）。

### 改了要重启的设置？主界面会告诉你

有些设置只能在**造模型/开音频流时读一次**：资源档位、推理算力、线程数、
合成引擎、唤醒词、麦克风与扬声器、采样率……

在界面里改这些时，主面板顶部会滑出一条提示条：

```
⟳  有 2 项设置要重启引擎才生效
   合成引擎、唤醒词                      [ 立即重启 ]
```

点「立即重启」就地把引擎停掉重建（不关程序、不丢对话）。提示条是淡入 + 高度
一起动的，下面内容不会"啪"地跳一下。

**其余设置都是即时生效的**，不会打扰你：语速、音色、音量、响度、LLM 各项、
追问窗口、确认词、声纹开关与阈值、主题色…… 这里能成立是因为保存设置时是
**就地更新**配置对象（Config.update_from）—— agent 和它内部的 wake / asr /
tts 都握着一开始那份 cfg 的引用，换成新对象它们就永远读不到新值了。

### 过渡动效一览

| 动效 | 在哪 | 时长 |
| --- | --- | --- |
| 圆球脉动 | 主面板 | 随状态 0.45~2.4 倍速 |
| 状态文字淡入淡出 | 主面板 | 200ms |
| 标题栏 / 窗口按钮浮现 | 主面板 | 220ms |
| 重启提示条 | 主面板 | 透明度 + 高度，200/220ms |
| 窗口淡入 | 每个独立窗口 | 170~200ms |
| 开关滑块 | 设置页 | 160ms |
| 分段控件选中药丸 | 设置页 | 170ms |
| 音量条把手 | 设置页 | 原生 + hover 变色 |

快捷键：Ctrl+1 启动 / Ctrl+2 停止 / Esc 打断 / Ctrl+K 对话 /
Ctrl+T 工具 / Ctrl+, 设置 / Ctrl+L 日志 / Ctrl+Q 退出。

### 网页版（可选）

```powershell
python -m voice_agent ui --web              # http://127.0.0.1:8760/
python -m voice_agent ui --web --port 8761  # 换端口
```

两个界面**共用 voice_agent/console.py**，功能一致。网页版多一个深浅主题切换。

**安全**：只绑 127.0.0.1；读配置、读技能原文和所有写操作都要求 X-Voice-Token
（页面启动时随机生成、只下发给本页），并校验 Origin / Sec-Fetch-Site / Host
（后者防 DNS rebinding）；静态文件做了路径穿越检查，技能删除被限制在技能目录内。
没有语音通道时（引擎未启动），需要确认的敏感指令一律拒绝。

---

## 四、语音链路：算力与音质怎么选

一个开关就够：

```yaml
speech:
  profile: balanced   # fast | balanced | quality
  device: auto        # auto | cpu | cuda
  threads: 0          # 0 = 按档位自动
```

| 档位 | 线程 | 合成引擎 | 适合 |
| --- | --- | --- | --- |
| fast | 2 | VITS | 后台常驻，尽量不抢 CPU |
| balanced（默认） | 4 | VITS | 日常使用 |
| quality | 6 | ChatTTS | 想要更自然的音色，机器有独显 |

单独写 speech.threads 或 tts.engine 就按你写的来，档位只提供默认值。

### 关于 GPU：先说清楚一件事

**语音识别那一侧**（唤醒词 / VAD / ASR）走 sherpa-onnx，而
**sherpa-onnx 官方 pip 包是 CPU-only 构建**：即使你装了 onnxruntime-gpu、
机器上有显卡，它也会打印一句 "Please compile with -DSHERPA_ONNX_ENABLE_GPU=ON"
然后悄悄退回 CPU。所以 provider 检测会同时看两件事：onnxruntime 有没有 CUDA、
sherpa-onnx 这个安装包本身有没有把 GPU 编进去。只要有一项不满足，doctor 和界面就写
「CPU（sherpa-onnx 是 CPU 版构建，需换 GPU 版才能用显卡）」，而不是假装在用显卡。

**语音合成那一侧**不一样：ChatTTS 是纯 torch，能用上显卡，界面会如实显示
「GPU NVIDIA GeForce RTX 4060 Ti」这样的字样。

### 合成引擎对比（本机实测：i5 + RTX 4060 Ti）

| 引擎 | 跑在哪 | 加载 | RTF | 音色数 | 离线 |
| --- | --- | --- | --- | --- | --- |
| VITS（默认） | CPU | 0.8s | **0.27** | 5 个角色音 | 是 |
| ChatTTS | GPU | 约 14s | 0.9~1.9 | 种子无限（预设 8 个） | 是（首次要下 1 GB 权重） |

VITS 快得多，日常用足够了；ChatTTS 明显更自然，但每次合成本身有 1.5~2 秒的固定
开销，第一次出声要等 2 秒左右。所以默认给 VITS，想要更好听就切 quality 档。

> 上一版还有个 Kokoro 引擎，已经**移除**：它的 0~2 号是英文音色，配置里写
> speaker_id: 0 就会拿英文风格向量去念中文，出来发闷发粗还带电流声；中文自然度
> 也一直不理想。留着的 models/kokoro-* 目录可以直接删掉（省 205 MB）。
> 老配置里如果还写着 engine: kokoro，会被当成 vits 处理，不会报错。

### 怎么选音色

| 引擎 | "音色"是什么 | 怎么写 |
| --- | --- | --- |
| vits | 5 个固定的角色音 | 名字或编号：suyingxue(0) / gunian(1) / fushiyu(2) / bingjiao(3) / bazong(4) |
| chattts | 一个**随机种子** | seed42 或直接写 42；同一个种子永远是同一个人 |

```powershell
python -m voice_agent voices                        # 列清单，标出正在用的
python -m voice_agent voices suyingxue              # 试听一个
python -m voice_agent voices --audition --count 8   # 连着听 8 个
python -m voice_agent voices --set bazong           # 写进配置（不动其它内容）
```

桌面界面「设置 → 语音与算力 → 音色」里也有下拉框和「试听」按钮，改完立刻生效。
**引擎没启动时点试听也不会没反应**：它会按需把合成模型加载起来，并提示"首次要等几秒"。

### 切到 ChatTTS 之前要知道的四件事

界面里选 chattts 时会在最上面弹一张警告卡，内容和这里一样：

1. **要独显**：约 2 GB 显存；没有 N 卡会自动退回 CPU，那就非常慢了；
2. **首次加载十几秒**：要联网下约 1 GB 权重，之后走本地缓存；
3. **慢**：RTF 0.9~1.9，一句话要等 1~2 秒才开口；
4. **源码运行要装包**：python -m pip install ChatTTS（约 2 GB，含 torch 依赖）。
   没装却在配置里选它，启动会直接报错并告诉你怎么办 —— 不会静悄悄地哑掉。
   **打包版已经把 ChatTTS 打进去了**，开箱即用；代价是产物从 563 MB 涨到 4.6 GB
   （torch 一个包就 4 GB）。只想要小体积见 packaging/README.md 第 4 节。

改完要**重启引擎**（主界面会弹提示条，点「立即重启」即可）。

### 声音不好听，往往是这三件事
1. **音色选错**（上面那条）—— 影响最大，且最容易被忽略；
2. **按峰值归一化**：原始输出峰值只有 0.24 左右，拉到 0.9 要乘 3.7 倍，
   底噪跟着抬 9.5 dB，安静段落就是一片嘶声。现在按 **RMS** 定响度，
   峰值交给软限幅（tanh）收，不做硬削波；
3. **系统重采样**：Windows 默认走 MME，设备通常是 44100/48000，
   喂它 16k/24k 的语音会过一次系统自己的重采样。现在先用多相滤波器
   转成设备原生采样率再送进去（audio.py 的 _native_rate）。

### 慢引擎（ChatTTS）怎么调才不拖沓

ChatTTS 每次调用有 1.5~2 秒的固定开销，调 length_scale 之类的没用。
真正管用的是**只把第一块切短**：

1. 第一块切到 14 字左右 —— 耳朵只需要等这一块；
2. **后面的块按整句走**（60 字、只认句末标点）。切得越碎，每一块都要重新起调，
   语气就越假，接缝还越多，而且固定开销会乘上块数；
3. **预取线程**：播第一块的时候，后面的块已经在合成了（speak 里的 tts-prefetch），
   所以块放大不会让等待变长。

> 预取这条路上踩过一个很隐蔽的坑，记在这里免得再犯：消费端原本用
> `item[0] == "error"` 判断出错标记，而正常消息的第一项是 **numpy 数组** ——
> 数组比较得到逐元素的布尔数组，`if` 它直接抛
> "The truth value of an array with more than one element is ambiguous"。
> 它只在"回复够长、切成多块"时才会走到，短回复怎么测都正常，
> 所以躲过了全部测试，表现就是**长回复整句都不出声**。
> 现在改用对象哨兵，并且加了一条专门的回归测试（场景 9）。

---

## 五、对话：上下文与连续对话

语音助手最容易做成"每次都从零开始"：问一句、答一句、回待命，下一句又是新的一天。
这一版专门治这个。

### 它记得住

| 记得什么 | 存在哪 | 有效多久 |
| --- | --- | --- |
| 最近 12 轮问答 | build/conversation.json | 2 小时（隔夜就忘，免得吓人） |
| 最近做过什么（打开了哪个程序、跑了什么命令） | 同上 | 一直留着，跨天也认 |
| 长期记忆（用户明确让记的事） | build/memory.json | 永久 |

"最近的动作"是接指代用的。用户先说"打开微信"，过一会儿说"再打开一次"或者
"把它关掉" —— 模型得知道"它"是什么。它作为一条 system 消息跟在用户这句话后面，
前面整段（系统提示 + 工具声明 + 历史）保持字节稳定，服务端的前缀缓存照样命中。

### 要不要接着听，由 LLM 决定

老版本只有一个 `follow_up_ms`：设了就每次都留窗口，助手于是"赖着不走"，
用户不接着问也得干等它超时；不设就永远只答一句。

现在默认 `follow_up_mode: auto`，由模型自己判断：

- 它反问了一句（"要打开哪一个？"）→ 留窗口，用户直接说就行；
- 它主动调了 **keep_listening** 工具（任务要分几步、还等用户补充）→ 留窗口；
- 只是汇报结果 → 回待命，不拖泥带水。

```yaml
agent:
  follow_up_mode: auto     # auto（LLM 决定）/ always（每次都留）/ off（从不）
  follow_up_ms: 6000       # 窗口长度
```

> 为什么"要不要接着听"做成一个工具，而不是让模型输出一个字段：语音链路上只有
> 一条纯文本通道，让模型"顺便吐个 JSON"很不可靠。做成工具就是一次明确的动作，
> 要么调了、要么没调，没有中间状态。

---

## 六、自己加工具

**内置的 40 多个工具不是全部** —— 用户可以自己加，写法和技能完全一样，
放在 `tools/` 目录里就行（技能放 `skills/`，本质是同一个加载器）：

```yaml
# tools/open_downloads.yaml（仓库里就带了这个例子）
name: open_downloads
title: 打开下载文件夹
description: 打开当前用户的「下载」文件夹。用户说「打开下载文件夹」时调用。
action:
  type: app
  target: "%USERPROFILE%\\Downloads"
triggers:
  - 打开下载文件夹
  - 我的下载
```

三个目录都会被扫描，后加载的可以覆盖前面的：

| 目录 | 用途 |
| --- | --- |
| ./tools | 项目自带 / 你写在项目里的工具 |
| ~/.voice-agent/tools | 你自己的工具（跟着用户走，不跟着项目） |
| ./skills、~/.voice-agent/skills | 技能，同一套格式 |

支持的 action：say / shell / open / url / app / sequence。界面上
「菜单 → 技能与工具 → 新建工具」会给你一份带注释的模板，保存即生效，不用重启。
工具页里它们和内置工具并排显示，带一个「自定义」徽章。

> 工具出错只会让这一个工具不可用，不会影响助手启动。

### 应用映射表：把"怎么启动"变成一个名字

`apps.yaml` 只干一件事：让「打开 XXX」这句话能对上号。两种写法都认：

```yaml
# 简写：名字 → 程序名 / 完整路径 / 网址
微信: C:\Program Files\Tencent\WeChat\WeChat.exe

# 完整写法：一个目标可以有多个叫法，还能带参数
我的备份:
  target: D:\scripts\backup.bat
  aliases: [备份脚本, backup]
  type: command          # exe / path / url / command，不写会按 target 自动猜
  args: ["--fast"]
```

于是"微信"、"威信"、"打开我的备份"、"备份脚本"都能命中。也可以直接说：
「添加应用 我的项目 指向 D:\code」「给微信加个别名 威信」。

---

## 七、多模型：不同用途挂不同的模型

一个模型干所有事既慢又贵。llm.profiles 定义档案，llm.routes 决定谁干什么：

```yaml
llm:
  # 顶层这一组是默认值，档案里没写的字段都回落到这里
  base_url: "https://api.deepseek.com/v1"
  model: "deepseek-chat"
  api_key: ""                    # 所有档案共用，也可以各自覆盖
  reasoning_effort: low          # 思考程度：off / low / medium / high / max

  routes:
    chat:   default              # 主对话：要会调用工具
    judge:  fast                 # 判断「确认 / 取消」：要快，别插一段沉默
    vision: vlm                  # 看图：需要支持图片输入的模型

  profiles:
    default:
      model: "deepseek-chat"
      reasoning_effort: low
    fast:
      model: "deepseek-chat"
      reasoning_effort: off      # 一个字的是非题不需要思考
      temperature: 0
    vlm:
      base_url: "https://api.siliconflow.cn/v1"
      model: "Qwen/Qwen2.5-VL-32B-Instruct"
      api_key: "sk-..."          # 档案可以自带密钥
      vision: true
```

三个用途都是真实用到的：

- **chat** —— 主对话，决定调哪个工具、怎么总结；
- **judge** —— 用户回答「行 / 别动」之后，判断到底同不同意。这一步卡在确认对话中间，
  用主模型会插入一段尴尬的沉默；
- **vision** —— look_at_screen 用的看图模型。

### 思考程度

reasoning_effort 取 off / low / medium / high / max（max 会按 high 发送）。
只发送各家公认的取值，off 时干脆不发这个字段 —— 严格的网关收到不认识的字段会直接报错，
那比「思考关不掉」更糟。

> 语音场景默认 low：思考是**看不见的沉默**，用户只听到助手不说话。

需要设置厂商专有字段（比如 DeepSeek 的 thinking、通义的 enable_thinking）时，
用 llm.extra_body 原样并进请求体，不必改代码：

```yaml
llm:
  extra_body:
    thinking: {type: disabled}
```

---

## 八、工具清单

```powershell
python -m voice_agent tools
```

共 **41 个**内置工具（外加你自己写的工具与技能）：

| 分类 | 工具 |
| --- | --- |
| 信息 | get_time、system_info、mouse_position |
| 应用与网页 | open_app、open_url、web_search、app_map |
| 系统 | volume、media_control、window、lock_screen、power（需确认）、run_command（需确认） |
| 屏幕 | screenshot、find_on_screen、click_image、look_at_screen |
| 鼠标 | mouse_move、mouse_click、mouse_drag（需确认）、mouse_scroll |
| 键鼠 | type_text、press_keys |
| 文件与图像 | list_files、search_files、read_file、resize_image |
| 剪贴板与记忆 | clipboard、remember、recall |

三条贯穿所有工具的约定：

1. **返回值就是朗读内容** —— 一句给人听的中文，不返回路径、ID、JSON；
2. **永不抛异常** —— 失败也返回一句中文，但会带上「失败」标记，大脑据此先垫一句提醒；
3. **确认失败即拒绝** —— 敏感工具拿不到确认通道就是拒绝，不会因为调用方漏传参数而放行。

---

## 九、自定义技能

往 skills/（或 ~/.voice-agent/skills/）丢一个文件即可，**不用改框架代码**。

### YAML 声明式

```yaml
name: greet
title: 打招呼
description: 用一句自定义的话跟用户打招呼。
parameters:
  who: {type: string, description: 称呼, required: true}
triggers:                    # 没配 API Key 时靠这些说法命中
  - phrase: 打个招呼
    args: {who: "你"}
action:
  type: say
  text: "你好呀，{who}！"
```

动作类型：say / shell（默认需确认）/ open / url / app / sequence。

### Python 编程式

```python
def handler(city: str = "") -> str:
    return city + " 今天晴，25 度"

TOOLS = [{
    "name": "weather", "title": "查天气",
    "description": "查询指定城市的天气。",
    "parameters": {"city": {"type": "string", "description": "城市名"}},
    "handler": handler,
    "triggers": ["天气"],
}]
```

要点：

- 技能报错只让这一个技能不可用，不会拖垮助手；
- 组合技能会自动继承敏感性：sequence 里只要有一环是敏感工具，整个技能也要先确认；
  引用了不存在的工具同样按敏感处理 —— 宁可多问一句，也不留后门；
- 命令行：skills new demo 生成模板，skills check 查看加载状态。

---

## 十、声纹：只认你的声音

开启之后，**别人的声音喊唤醒词不会被理会** —— 不答应、不开麦、连对话记录都不留。

```yaml
speaker:
  enabled: false           # 默认关闭
  threshold: 0.55          # 余弦相似度阈值：0.45 宽松 / 0.55 标准 / 0.65 严格
  model: ""                # 留空 = 自动用 models/speaker/*.onnx
  profile: ""              # 留空 = build/voiceprint.json
```

在「设置 → 声纹」里操作：打开开关 → 点「录制声纹」（录 3 次更稳）→ 点「试一次」验证。
模型用 scripts/download_models.py --only speaker 下载（约 38 MB）。

### 为什么默认是关的

声纹是一件**配错了就把自己锁在门外**的功能。所以：

- 默认关闭，必须你显式打开；
- 打开了但**还没录声纹**时，程序会放行并在界面上反复提醒，而不是把人挡在外面 ——
  那样只能改配置文件才能恢复；
- 音频太短（算不出向量）时也放行：宁可漏放，也不要因为一声咳嗽把主人关在门外。

### 实测

用同一个合成引擎的两个不同音色当「两个人」测出来的分布：

| | 相似度 |
| --- | --- |
| 同一个人再说一句 | **1.00** |
| 另一个人说话 | **0.28** |

默认阈值 0.55 正好落在两者中间，两边都有很大余量。验证逻辑在
tests/test_speaker.py 里，可以自己跑一遍。

---
## 十一、实战：找图 + 连点器

三个工具串起来就是自动化：

    你： 「在屏幕上找一下『确定』按钮的图，找到了点它三次」
    它： （find_on_screen 返回坐标 → click_image 连点）
         「已经在 820,410 点击 3 次，间隔 200 毫秒」

- **多尺度匹配**：会自动尝试 0.8~1.25 倍缩放，所以 125% DPI、浏览器缩放都不会失手；
- **相似度去重**：同一目标在多个尺度上命中时只保留分数最高的那个；
- **只定位一次**：连点是「找一次、原地连点」，因为找图比点击慢得多；
- 阈值默认 0.8，找不到时如实说「屏幕上没找到这张图（阈值 0.8）」，不会硬点一个位置。

配合「应用映射表」可以先打开程序再操作：

    你： 「把『我的项目』设成 D:\code\my-project」
    你： 「打开我的项目」

映射表存在 apps.yaml，也可以直接编辑；open_app 会**先查它**，再走内置规则。

---

## 十二、命令行

| 命令 | 作用 |
| --- | --- |
| python -m voice_agent ui | 打开桌面控制台（原生窗口） |
| python -m voice_agent ui --web | 改用浏览器版控制台 |
| python -m voice_agent run | 终端里常驻运行：唤醒词 + 语音对话 |
| python -m voice_agent ask "看看C盘还剩多少空间" | 用文字下指令（不占麦克风），加 --speak 会读出来 |
| python -m voice_agent say "你好" --out build/a.wav | 只做语音合成 |
| python -m voice_agent listen | 录一句并识别，验证麦克风 |
| python -m voice_agent wake | 只跑唤醒词检测，验证喊得醒 |
| python -m voice_agent voices | 列出音色，标出正在用的那个 |
| python -m voice_agent voices zf_092 | 试听某个音色 |
| python -m voice_agent voices --audition --female --count 8 | 连着听 8 个女声 |
| python -m voice_agent voices --set zf_092 | 把音色写进配置文件 |
| python -m voice_agent skills check / skills new demo | 技能管理 |
| python -m voice_agent tools | 列出全部工具 |
| python -m voice_agent devices | 列出音频设备 |
| python -m voice_agent doctor | 环境体检：依赖、模型、桌面界面、LLM、算力、设备 |
| python -m voice_agent selftest | 端到端自检 |

测试（都不需要麦克风、不需要联网、不需要 API Key）：

```powershell
python scripts/run_tests.py       # 一把跑完下面七个，最后给个总账
python scripts/run_tests.py gui   # 也可以只跑名字里带 gui 的
```

单跑某个脚本也行，每个都自带 sys.path 引导：

```powershell
python tests/test_pipeline.py     # 状态机：唤醒→识别→执行→播报→打断→确认→超时回落
python tests/test_llm_loop.py     # 假模型服务：工具调用循环、降级、断线不重跑
python tests/test_skills.py       # 技能加载/调用/报错/触发词/覆盖/确认契约
python tests/test_webui.py        # 网页版接口、鉴权、路径穿越、前后端一致性
python tests/test_gui.py          # 桌面界面：七个页面、导航、状态刷新、SVG 图标
python tests/test_voices.py       # 音色表：英文音色会被换掉、按名字选、只给中文音色
python tests/test_speaker.py      # 声纹：同人放行、异人拦住、关掉就放行
```

---

## 十三、配置文件详解

config.yaml 里每一项都有默认值，只写想改的就行。常用的几组：

```yaml
wake:
  keywords: ["小爱同学", "你好小爱"]   # 唤醒词；建议 3~5 个音节、别用叠词
  threshold: 0.25                      # 越低越灵敏，吵的环境调高
  replies: ["我在", "请说"]            # 唤醒后的应答语

audio:
  output_gain: 1.0                     # 输出音量，0.0~2.0；界面上那个滑杆改的就是它
  input_device: null                   # null = 系统默认；也可写设备序号或名字片段

ui:
  accent: ""                           # 主题色：blue/teal/purple/… 或 #RRGGBB
  show_stats: true                     # 主界面底部的四格速览

speech:                                # ↓ 这一节的改动要重启引擎
  profile: balanced                    # 资源档位
  device: auto                         # 算力
  threads: 0                           # 线程数，0 = 按档位

tts:
  engine: vits                         # vits 快（CPU）/ chattts 更自然（显卡）；换引擎要重启
  voice: suyingxue                     # 音色写名字；见第四节「怎么选音色」
  speed: 1.0
  target_rms: 0.10                     # 响度（按 RMS，不是按峰值）
  noise_gate: -55.0                    # 底噪门；-100 = 关掉

llm:
  reasoning_effort: low                # 思考程度
  routes: {chat: default, judge: fast, vision: vlm}
  profiles: { ... }                    # 见第五节

agent:
  listen_timeout_ms: 8000              # 唤醒后等待说话；超时回待命
  follow_up_ms: 0                      # 0 = 任务做完就回待命；想连续追问设 3000~8000
  cues: true                           # 收音/确认/结束的提示音
  barge_in_wake: true                  # 播报时允许唤醒词打断
  confirm:
    enabled: true                      # 敏感操作语音确认
    yes: [确认, 确定, 可以, 同意, 好, 行, 是, 对, ok]
    no:  [取消, 不要, 不用, 别, 停, 否, 算了, 先不, 不好, 不行, no]
```

> 界面上「配置」页保存会自动备份成 config.yaml.bak，之后注释会丢。
> 想保留注释就直接编辑文件，别用表单保存。

---

## 十四、关键设计取舍

几个踩过坑才定下来的地方，改代码前值得先看：

**1. 为什么唤醒词、识别、合成必须本地？**
唤醒词要 7×24 常驻在线。云端方案要么延迟高、要么持续计费、要么把麦克风一直往上传。
本地 KWS 模型只有几 MB，CPU 占用可以忽略。

**2. KWS 需要「尾巴上还有声音」才肯报命中。**
把「小爱同学」四个字单独喂进去，检测器不吭声；后面补 0.5 秒静音才报出来 ——
它要 num_trailing_blanks 个空白帧才确认关键词结束。所以 WakeWord.flush() 存在。

**3. 收音保护期按「样本数」而不是墙上时间。**
唤醒词和提示音的尾音还没散干净时不能收音，否则会出现「喊完唤醒词，助手把唤醒词本身
当成一条指令执行了」。用样本数是因为麦克风本来就是实时的，两者等价，但离线回放和
测试里样本数是确定的。

**4. 中文 TTS 念不出拉丁字母。**
vits 的 lexicon.txt 里一个英文字母都没有，C盘 的 C 会被判为 OOV 直接丢掉。
所以朗读前先把字母换成读音最接近的汉字：C盘 → 西盘、156G → 156记。

**5. 确认回答先判「否」再判「是」。**
「不行」里含有「行」，顺序反了就会把拒绝当成同意。词表只兜底，语义判断交给模型，
模型不可用且答非所问时**保守拒绝**。

**6. 被打断的任务必须失效。**
工作线程可能正卡在「等用户确认」里，join 超时后它还会醒来。用 epoch 标记任务代次，
过期的任务不许再写状态。

**7. 敏感操作的确认闸门放在调度器里，而且失败即拒绝。**
以前写「有确认通道才问」，于是任何忘了传参数的调用方都能把关机直接跑掉。
现在反过来：拿不到确认通道就是拒绝。

**8. system 提示里不放时间。**
以前每轮都把 datetime.now() 拼进 system，于是系统提示 + 三十个工具声明的前缀缓存
每分钟失效一次，而一轮对话最多调 6 次模型。现在时间跟在用户消息后面单独发，
而且只有跨分钟才重新发。

**9. Windows PowerShell 输出要显式切 UTF-8。**
5.1 默认按 OEM 代码页（中文机上是 GBK）输出，用 UTF-8 去解会得到乱码 ——
表现是「桌面」这种中文路径解析不到。

---

## 十五、实测数据

本机 CPU（无 GPU 版 sherpa-onnx），16k 单声道：

| 环节 | 结果 |
| --- | --- |
| 模型加载 | 唤醒词 0.9s，VAD 0.0s，ASR 1.6s，TTS 0.8s |
| 语音合成（VITS） | RTF 0.27（比实时快约 3.7 倍） |
| 语音合成（ChatTTS） | RTF 0.9~1.9（音质更好，但要显卡、首次加载十几秒） |
| 语音识别 | RTF 0.03（1 秒音频约 30 毫秒） |
| 端到端响应 | 说完话到开口：规则模式约 0.1s；LLM 模式取决于模型首字延迟 |
| 打包体积 | dist/VoiceAgent 约 4.6 GB / 5457 个文件（含 torch，模型仍不在包内） |

八个测试脚本共 **249 项断言全部通过**：selftest 10、test_pipeline 34、
test_llm_loop 12、test_skills 41、test_webui 55、test_gui 52、test_voices 33、
test_speaker 12。

> test_pipeline 用的是合成语音，而 VITS 合成带随机噪声，同一句「小爱同学」
> 约 1/3 的概率打不动 KWS。脚本会先挑一段「本机确实能唤醒 / 能识别」的音频再喂给
> 状态机，所以连跑多次结果一致。

---

## 十六、打包成 exe

```powershell
python -m pip install pyinstaller
python scripts/build_exe.py            # 约 40 秒（有缓存）
python scripts/build_exe.py --slim     # 精简版：去掉 CUDA provider，省 177 MB
```

产物是 dist/VoiceAgent/ 这**一个文件夹**，拷到没装 Python 的机器上就能用：

| 可执行文件 | 用途 |
| --- | --- |
| VoiceAgent.exe | 双击打开桌面窗口（无控制台，不会闪黑框） |
| VoiceAgentCLI.exe | 命令行：VoiceAgentCLI.exe tools / selftest / run … |

Windows 把「有没有控制台」编在 PE 头里，所以一个 exe 没法同时是两者，
这里用一次构建产出两个入口，依赖只存一份。

几点要知道的：

- **模型不进包**：四百多 MB 的权重放在 exe 旁边（`dist/VoiceAgent/models`），
  程序按 config.yaml 自动去找；不想复制可以用目录联接：
  `mklink /J "dist\VoiceAgent\models" "D:\path\to\models"`；
- **用 onedir 不用 onefile**：onefile 每次启动都要把几百 MB 解压一遍，慢得没法用；
- dist 里的 config.yaml 是**从示例生成的，api_key 为空**，不会把你的密钥打进去；
- 常见坑（杀软误报、首次启动稍慢、缺 VC++ 运行库）都写在
  [packaging/README.md](packaging/README.md)。

---

## 十七、常见问题

**喊不醒？**
先 python -m voice_agent wake 单独测。仍不灵就把 wake.threshold 从 0.25 调到 0.15，
或者换个唤醒词 —— **别用叠词**（「小爱小爱」在这类音素级 KWS 上命中率明显低）。

**它自己把自己打断了？**
外放时麦克风会听到扬声器。默认只允许「喊唤醒词」打断，不会因为听到声音就抢话；
如果仍然自激，把 agent.barge_in_wake 关掉，或者戴耳机 / 开回声消除。

**唤醒之后我什么都没说，它会一直等着吗？**
不会。默认 8 秒（agent.listen_timeout_ms）没有听到提问就自动回到待命，并给一声提示音。
如果只是听到点动静（咳嗽、电视声）却没形成完整句子，从你开口那刻起再给一句话的时间，
到点同样收工。

**回答完之后它还在听吗？**
默认不会：一次任务做完就回到「等待唤醒」。想让助手支持连续追问，把
agent.follow_up_ms 设成 3000~8000。

**确认提示还没念完我就回答了，它却说没听到？**
确认提示播放期间麦克风是关着的（否则助手会把自己的提问听成回答）。等提示念完再回答。

**声音发闷、发粗，还有明显的电流声？**
先看启动日志里那行 `[tts] 引擎：… 音色：…`。九成是音色选错了 ——
先看是不是引擎/音色没选对（第四节有对比表），再看响度和重采样那两条。
代码会自动把它换成中文音色，想自己挑：

```powershell
python -m voice_agent voices --audition --female --count 8
python -m voice_agent voices --set zf_092
```

确定不是音色的话，再看第二节那三条（峰值归一化、系统重采样）。
另外 `tts.noise_gate` 调到 -45 会让静音处更干净，但调太高会咬字头。

**改了配置怎么没生效？**
分两种情况：

1. **改错文件了**。桌面版和 exe 版读的是各自的 config.yaml：源码跑用项目根目录
   那份，双击 exe 用 exe 旁边那份（dist/VoiceAgent/config.yaml）。
2. **这项本来就要重启**。资源档位、算力、线程数、合成引擎、唤醒词、声卡、
   采样率这些只在造模型时读一次。在界面里改的话主面板会滑出一条提示条，
   点「立即重启」即可；手动改文件的话，回界面点一下停止再启动也一样。

除此之外的设置（语速、音色、音量、LLM、追问窗口、声纹阈值、主题色……）
**改完立刻生效**，不会有任何提示打扰你。

**音量条拖了没反应？**
拖动时只改内存里的总增益，松手才写配置 —— 所以是"立刻能听到、配置文件稍后更新"。
如果彻底没声音，看看是不是点了静音（滑杆左边的按钮），或者系统音量本身是 0。

**主题色换了但有个窗口还是旧颜色？**
主面板和已经打开过的窗口会立刻刷新；极少数用主色画过一次的图标需要重开那个窗口。
设置页里换色时不会再关掉设置窗口（早期版本会，很突兀）。

**配了 GPU 为什么还是 CPU？**
见第四节：sherpa-onnx 官方 pip 包是 CPU-only 构建。doctor 会如实告诉你最终跑在哪里。

**麦克风被占用？**
devices 看设备号，在界面「设备」页里选。同一个麦克风不要被两个程序同时独占。

**杀毒软件报毒 / 删了 exe？**
PyInstaller 打的包经常被杀软误报（典型是 Wacatac）。把 dist/VoiceAgent 加进白名单，
或者从中间产物复制回来：`copy build\pyinstaller\voice-agent\VoiceAgentCLI.exe dist\VoiceAgent\`。

**网页打不开？**
换端口 --port 8761。服务只绑本机，别的电脑访问不到。

---

## 十八、目录结构

```text
voice-agent/
├─ voice_agent/
│  ├─ __main__.py        CLI：ui / run / ask / say / listen / wake / skills / doctor …
│  ├─ console.py         控制台后端（状态/控制/配置/技能/设备），两个界面共用
│  ├─ ui/                PyQt6 桌面界面
│  │   ├─ main_window.py   无边框竖版面板 + ☰ 菜单 + 重启提示条 + 换主题
│  │   ├─ pages.py         七个页面（主页/对话/工具/技能/设置/设备/关于）
│  │   ├─ components.py    卡片、开关、分段、圆球、音量条、主题色、提示条
│  │   └─ theme.py         色板 / 可换主色 / QSS / 26 个内联 SVG 图标
│  ├─ tools/             工具层（清单与实现分开）
│  │   ├─ __init__.py      注册表、调用闸门、JSON Schema、确认逻辑
│  │   ├─ builtin.py       30 个内置工具的声明（给模型看的措辞）
│  │   ├─ windows.py       系统：窗口、键鼠、音量、媒体、电源、命令
│  │   ├─ apps.py          打开程序 / 网址 / 搜索
│  │   ├─ files.py         文件与长期记忆
│  │   ├─ vision.py        鼠标、屏幕找图、图像缩放、应用映射
│  │   ├─ system_info.py   时间与本机状态
│  │   └─ _shared.py       子模块共用的常量
│  ├─ speaker.py         声纹：注册、校验、持久化
│  ├─ screen.py          鼠标模拟、截屏找图、图像缩放（Pillow / OpenCV）
│  ├─ webui.py           网页版后端（标准库 HTTP + SSE）
│  ├─ web/               网页版前端（无依赖的三件套）
│  ├─ config.py          YAML → dataclass，模型/算力/界面，支持就地热更新
│  ├─ voices.py          音色表：sid ↔ 名字，拦住"拿英文音色念中文"
│  ├─ audio.py           麦克风与扬声器，总音量、电平表与提示音
│  ├─ speech.py          唤醒词 KWS、VAD、ASR、TTS（VITS / ChatTTS）
│  ├─ textutil.py        拼音关键字生成、朗读前文本清洗、分句
│  ├─ skills.py          技能加载器（YAML / Python）
│  ├─ rules.py           离线意图路由（含技能触发词）
│  ├─ llm.py             OpenAI 兼容客户端（思考程度、多模型、用量统计）
│  ├─ brain.py           大脑：多模型路由、工具循环、失败提醒
│  ├─ agent.py           状态机与主循环（打断、确认、epoch 失效）
│  └─ selftest.py        端到端自检
├─ skills/               自定义技能（附 3 个示例）
├─ packaging/            PyInstaller 配置与打包说明
├─ scripts/              模型下载、打包、测试总入口
├─ tests/                七个测试脚本（run_tests.py 一把跑完）
├─ config.example.yaml
└─ requirements.txt
```

---

## 十九、已知限制

- **只在 Windows 上验证过**：鼠标模拟、窗口、剪贴板、电源都调用 Win32 API。
  语音、技能、界面是跨平台的，移植到 macOS / Linux 主要替换 tools.py / screen.py
  里的系统调用。
- 语音识别是 **Paraformer 中文模型**，中英混说时英文部分容易掉字。
- 唤醒词对**叠词**和**单字**不友好，建议 3~5 个音节。
- 离线规则模式只覆盖常用句式 + 技能触发词，长尾表达需要 LLM。
- type_text / press_keys / 鼠标动作会作用到当前焦点窗口，用之前先确认焦点在对的地方。
- 屏幕找图对**半透明、动画、动态渲染**的界面元素不敏感，阈值太低会误命中。
- 打包版**不会自动带上模型**，需要放在 exe 旁边（或做目录联接）。
- **尚未实现**（已在设计中，但不在这版里）：模型流式输出边生成边朗读、
  工具调用的统一超时、长任务转后台并播报完成。
- 控制台没有多用户概念，它是「本机单人使用」的工具。
