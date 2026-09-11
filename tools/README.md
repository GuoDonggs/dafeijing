# 大肥鲸 自定义工具

把 .yaml 或 .py 丢进这个目录，就能给助手加一个**属于你自己的工具**。
它和内置工具是平等的：模型能自动发现、能调，离线规则模式也能用触发词喊出来。

## YAML 写法（推荐）

```yaml
name: backup_docs          # 工具名：小写字母开头，英文/数字/下划线
title: 备份文档             # 给人看的名字，语音确认时会念出来
description: >-             # 这句是写给模型看的，说清楚"什么时候该用它"
  把「文档」目录打包备份到 D 盘。用户说「备份一下文档」时调用。
parameters:                # 参数表；{} 里写 required: true 表示必填
  target:
    type: string
    description: 备份到哪里
    required: false
action:
  type: shell              # say / shell / open / url / app / sequence
  command: robocopy "%USERPROFILE%\Documents" "{target}" /MIR
  confirm: true            # 有副作用的默认就要确认；确认过再执行更安全
triggers:                  # 离线模式（没配 API Key）靠它喊得动
  - 备份文档
  - 备份一下文档
```

## 支持的 action

| type | 必填字段 | 说明 |
| --- | --- | --- |
| say | text | 直接返回这句话（可含 {参数}） |
| shell | command | 执行系统命令并返回输出；**默认需要语音确认** |
| open / url | target | 用浏览器打开网址 |
| app | target | 打开本机应用 |
| sequence | steps | 依次调用已有工具，如 steps: [{tool: get_time, args: {}}] |

## Python 写法（需要任意逻辑时）

```python
def handler(city: str = "") -> str:
    return city + " 今天晴，25 度"

TOOLS = [{
    "name": "weather",
    "title": "天气",
    "description": "查询某个城市的天气。",
    "parameters": {"city": {"type": "string", "description": "城市名"}},
    "handler": handler,
}]
```

> 工具出错只会让这一个工具不可用，不会影响助手启动。
> 改完在界面上点「重新加载」，不用重启。
