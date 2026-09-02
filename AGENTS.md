# AGENTS.md — vm_agent

访客机无界面 Agent：连宿主机 [Console Pro](D:/D/console)，上报微信消息、执行文本外发、推送桌面画面并接收键鼠。

## 与 Console 的关系（必读）

| 路径 | 角色 |
|------|------|
| **`D:/D/console`** | 宿主机控制面（WS **服务端** + UI） |
| **本仓 `D:/D/vm_agent`** | 访客机 Agent（WS **客户端**） |
| `D:/D/wxrpa` | UIA 参照源；本仓 `rpa/` 为**复制副本**，勿 `import` 活体 wxrpa |
| `work_place/desktop` | 远期汇合目标 |

- 协议：`console.agent.v1`，权威文档在 **`D:/D/console/docs/desktop-agent-api.md`**
- 联调总览：**`D:/D/console/docs/vm-agent-bridge.md`**
- 金样：`D:/D/console/contracts/examples/`
- **禁止** import console 源码；字段变更先改 Console 仓协议文档再改本仓

## 快速上手

```powershell
cd D:\D\vm_agent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
# 编辑 config.yaml：host=宿主机 IP，token 与 console 一致
python main.py
python main.py --no-wechat    # 无微信空跑握手/心跳/画面
```

打包：`scripts/build_exe.ps1` → 拷贝整个 `dist/vm_agent/`；exe **旁**的 `config.yaml` 才会被读取（勿只用 `_internal` 里的路径）。

## 目录职责

```
main.py              # 入口；frozen 时 ROOT=exe 目录
config.yaml          # host/port/token/vm_id/监听黑名单
agent/
  service.py         # 连接、重连、心跳、指令、远程循环
  protocol.py        # 帧辅助、RB01、title: chat_id、msg_id 哈希
  mapper.py          # WxMessage / Session → event.*
rpa/
  wx_listener.py     # 入站（blacklist 默认）
  wechat_controller.py
  lock.py            # 监听与外发互斥
remote/
  capture.py         # ImageGrab → JPEG（LANCZOS；max_width<=0 不缩放）
  input.py           # SendInput（归一化坐标）
scripts/build_exe.ps1
```

## 硬性约定

- **进程独立**：Console 断开只重连，不退出；仅 Ctrl+C / `stop()` 结束。
- **网络**：访客机 `host` 禁止默认死守 `127.0.0.1`（那是访客机自己）；NAT 常用 `192.168.159.1`，Host-Only 常用 `192.168.150.1`。
- **微信**：需交互桌面 + 4.1.9 + `mmui::MainWindow`；发送前可停 listener，发完再启。
- **远程**：`cmd.remote_start` 的 fps/quality/max_width 在循环内即时生效；高画质接管由 Console 下发，勿被本机写死成预览档。
- **chat_id**：`title:<规范化会话名>`，与 Console hub 约定一致。
- **风格**：中文注释；`from __future__ import annotations`；最小改动；不引入未使用依赖。

## 改协议时

1. 改 `D:/D/console/docs/desktop-agent-api.md` + `contracts/examples/`
2. 同步本仓 `agent/protocol.py` / `mapper.py` / `service.py` 指令分支
3. Console 侧改 `agent_gateway` / `message_hub` / `remote`（若涉及）

## 参考

- 本仓用户说明：[README.md](README.md)
- Console AI 指引：`D:/D/console/AGENTS.md`
