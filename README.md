# VM Agent（访客机无界面进程）

对接宿主机 **Console Pro**（`D:/D/console`）的 `console.agent.v1`：微信消息监听/文本外发、桌面画面预览与键鼠接管。

> **AI / 协作者先读：** [AGENTS.md](AGENTS.md) · 跨仓联调：[D:/D/console/docs/vm-agent-bridge.md](../console/docs/vm-agent-bridge.md) · 协议：[D:/D/console/docs/desktop-agent-api.md](../console/docs/desktop-agent-api.md)

## 运行前提

1. **Windows 访客机**且保持**交互桌面会话**（不要锁屏；RDP 断开常导致 UIA/截屏失效）
2. 能访问宿主机 `ws://<host>:18765/v1/agent`（Host-Only / NAT）
3. `config.yaml` 中 `token` 与 console `console.agent_token` 一致
4. 启用微信时：微信 PC **4.1.9** 已登录，主窗口类名 `mmui::MainWindow`

## 安装与启动

```powershell
cd D:\D\vm_agent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Agent **独立于 Console**：Console 关闭后会自动重连，不会退出。

```powershell
# 先起 Console：cd D:\D\console && python main.py
python main.py
python main.py --no-wechat   # 无微信空跑
```

### 登录自启（测试常用）

写入当前用户 `HKCU\...\Run`（无需管理员；需已登录交互桌面）：

```powershell
# 按 config.yaml 里 autostart.extra_args 注册（默认含 --no-wechat）
vm_agent.exe --autostart install
# 或显式指定空跑
vm_agent.exe --autostart install --no-wechat
vm_agent.exe --autostart status
vm_agent.exe --autostart uninstall
```

源码同理：`python main.py --autostart install --no-wechat`。改启动参数后需重新 `install`。

`config.yaml` 的 `host` 必须是**宿主机在虚拟网卡上的 IP**（访客机内不要用 `127.0.0.1`）：

| 网卡模式 | 常见 host |
|----------|-----------|
| NAT (VMnet8) | `192.168.159.1` |
| Host-Only (VMnet1) | `192.168.150.1` |
| 本机双进程 | `127.0.0.1` |

测通：`Test-NetConnection <host> -Port 18765`

## 打包进 VMware

```powershell
cd D:\D\vm_agent
.\.venv\Scripts\Activate.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\build_exe.ps1
```

拷贝整个 `dist\vm_agent\`（含 `_internal`），编辑**与 exe 同目录**的 `config.yaml`，再运行 `vm_agent.exe`。

若 venv 基于 Miniconda，打包脚本会把 `Library\bin` 里的 `ffi.dll` / OpenSSL 等打进 `_internal`；缺这些时访客机常见报错：`DLL load failed while importing _ctypes`。更稳妥可用 [python.org](https://www.python.org/downloads/) 官方 CPython 建 venv 再打包。

## 目录

```
main.py              # 入口（打包后从 exe 目录读配置）
config.yaml
AGENTS.md            # AI 协作约定
agent/               # WS、映射、服务编排
rpa/                 # wxrpa 副本（listener / controller）
remote/              # 截屏 JPEG + SendInput
scripts/build_exe.ps1
```

## 协议要点

- JSON：握手 / 心跳 / 会话 / 消息 / `cmd.send_text` / `cmd.remote_*`
- 二进制画面：`RB01` + uint32 BE length + JPEG（同 WS 连接）
- `chat_id` MVP：`title:<会话名>`

汇合 `work_place/desktop` 时：迁移 `agent/` + `remote/`，RPA 换 desktop 适配层；Console 协议尽量不动。
