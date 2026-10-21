# 远程画面与键鼠方案

> 状态：草案 v1 · 2026-10-21。6.2 键鼠租约已落地。6.3 控制台已能按库存连本机 VNC；未配置端口时仍走 JPEG。
> 范围：访客机画面采集、键鼠注入，以及和外发 RPA 同时运行时的输入冲突。
> 关联：[desktop-agent-api.md](../../mibuddy_console/docs/desktop-agent-api.md)、[vm-cluster-comm-plan.md](../../mibuddy_console/docs/vm-cluster-comm-plan.md) 第 5–6 节、[vm-connect-plan.md](../../mibuddy_console/docs/vm-connect-plan.md) 第 8 节。
> 本文选定路线。实施前先做第 6 节的一天验证；验证通过后再改协议文档。

## 1. 结论

预览和接管改走 **VMware Workstation 自带的 VNC**。服务端是宿主机上的 `vmware-vmx`，不在访客机里安装远控软件，也不把 Guacamole / RustDesk 的源码引进来。控制台在「正在看这台」时，用自己的 VNC 客户端连 `127.0.0.1:<该虚拟机端口>`，把帧画进现有画面面板。微信消息、外发、心跳仍走 `console.agent.v1`。

这条路同时满足三件现在做不到的事：

- 系统确认页（UAC 安全桌面）、登录界面、锁屏，画面能看见，键鼠能点到。采集的是虚拟显卡帧缓冲，注入的是虚拟键鼠，不经过访客机用户态。
- 编码在宿主机。低配访客机不再为 Pillow 截屏和 JPEG 让出 CPU。
- 没有人看的机器不建立 VNC 连接。这和集群方案「预算按并发路数，不按登记台数」一致。

键鼠错位要单独做，而且要先做。VNC 解决不了它。外发 RPA 和远控注入进的是同一套 Windows 键盘状态。外发过程中按住的 Alt / Ctrl，会修饰操作员此时的点击。

不选的两条：

| 候选 | 决定 |
|------|------|
| guacd + pyguacamole | 同一条 VNC 的可选转接，不作主路径。禁止把它的后端指到访客机 RDP。 |
| RustDesk | 不进产品通道。和 [vm-connect-plan.md](../../mibuddy_console/docs/vm-connect-plan.md) 里已有的判断一致，低配和 UAC 两点更不合适。 |
| `vmrun` / VIX 客人机操作 | 不用于交互画面。截图是一张一张拉，按键要登录客人机，帧率不够，也到不了安全桌面。 |

## 2. 现在这两类故障分别是什么

### 2.1 系统确认页采集不到、也点不到

`remote/capture.py` 用 `ImageGrab.grab()`，`remote/input.py` 用 `SendInput`。两者都跑在当前登录用户的桌面上。

Windows 的 UAC 同意框、凭据框、登录界面画在 **安全桌面**（`Winlogon`）上。普通用户进程不允许 `BitBlt` 这块桌面，也不允许往它注入输入。所以确认框弹出时，推上去的仍是原来的用户桌面（或卡住的最后一帧），点击落在用户桌面，不落在确认框上。提权后的窗口还有完整性级别限制，中完整性进程的 `SendInput` 同样点不进去。

这不是画质、帧率或坐标缩放能补上的。客人机里的远控（含 RustDesk 未提权时、客人机 VNC、RDP）面对的是同一道墙。RDP 还有额外问题：会话一断，微信 UIA 按现有 README 的前提就会失效。

Workstation 窗口里能看见并点击这个确认框，是因为宿主机在读虚拟显卡、在写虚拟键鼠。内置 VNC 走的是同一条控制台通道（MKS），不是客人机里的 GDI。

客人机截屏还有两处次要偏差，换通道后一并消失：进程没有声明 DPI 感知，`ImageGrab` 默认主显示器，`SendInput` 的 0–65535 绝对坐标按虚拟屏幕换算。高分屏或多显示器上，普通窗口也会点偏。

### 2.2 双击程序变成「查看属性」

Windows 资源管理器里，**按住 Alt 双击图标会打开属性**。`Alt+Enter` 也是属性。这和「疑似按住了 Ctrl」是同一类故障：修饰键还按着，但和这次症状对得上的键是 **Alt**。

外发代码里有两处会按下修饰键，`vm_agent/rpa/wechat_controller.py` 与 `work_place/desktop` 里的副本相同：

- 抢前台时用 `keybd_event(VK_MENU)` 点一下 Alt（`_force_activate_wechat_hwnd`）。按下和抬起是紧挨着的两条调用，中间若插入了远控的鼠标事件，这一下双击就变成 Alt+双击。
- 粘贴、搜索、清空输入框走 `SendKeys("{CTRL}v")`、`"{CTRL}f"`、`"{CTRL}a"`。一次外发会持续许多秒（搜索、校验最长约 40 秒），中间多次按住 Ctrl。

远控这边 `cmd.remote_input` 入队时不看 `agent_busy`。`agent_busy` 也只覆盖 **vm_agent 自己** 的 `cmd.send_text`。desktop 是另一个进程，vm_agent 进程内的 `_rpa_lock` 锁不到它。两条 `SendInput` / `keybd_event` 流进同一个系统输入队列。

还有一个会把键「粘住」的窗口：desktop 外发用 `BlockInput(True)` 挡住物理输入。挡住期间，**只有调用 `BlockInput` 的那个线程** 注入的 `SendInput` 能送达。vm_agent 此时送出的 `key_up` 会被丢掉，Ctrl / Alt / Shift 就停在按下。后面的点击全部带修饰键，直到某次完整的抬起。

所以即使用虚拟键鼠替换 `SendInput`，只要外发仍在客人机里按 Ctrl/Alt，操作员的点击仍会被修饰。第 4 节的互斥要保留。

## 3. 路线比较

约束：访客机配置低；画面要留在现有 PyQt 面板里；微信通道不能被远控产品替换；远控组件以外部进程或现成库调用，不把项目源码并进本仓。

| | 客人机 JPEG + SendInput（现状） | Workstation 内置 VNC（选定） | guacd，后端用这条 VNC | 访客机 RDP，再经 guacd | RustDesk 装进访客机 | vmrun / VIX |
|--|--|--|--|--|--|--|
| UAC / 登录 / 锁屏 | 看不见，点不到 | 看得见，点得到 | 同左，多一跳 | 默认看不见；关掉安全桌面才看得见，微信会话还容易断 | 未提权看不见；提权服务可以改善，捕获切换本身很重 | 客人机按键到不了安全桌面 |
| CPU 落在哪 | 访客机 Pillow + JPEG | 宿主机 `vmware-vmx`，有客户端才编码 | 宿主机上再解码、再编码一遍 | 访客机 RDP 编码 + 宿主机 guacd | 访客机编码。社区反馈空闲连接也能到约 20% CPU，隐私模式更高 | 宿主机，但是逐次拉图 |
| 和现有面板 | 已接通 | 控制台加一层 VNC 客户端，面板不动 | 还要接 Guacamole 协议，面板照样要写 | 同左，且伤害 UIA | 自带窗口。嵌进面板等于把客户端做进去 | 做不成连续画面 |
| 剪贴板 | 客人机轮询，已有 | VMware 写明这条 VNC **不能**在宿主机和客人机之间复制粘贴。轮询保留 | 变不出来 VMware 没有的剪贴板 | RDP 有剪贴板，但会话问题更大 | 自带一套，和现有事件重复 | 无交互剪贴板 |
| 许可与部署 | 无新依赖 | Workstation 的 VNC Connections（Player 没有）。密码最长 8 位 | 宿主机多一个 guacd 进程。不要部署带 Tomcat 的 Guacamole Web | 同左 | AGPL。和微信、席位无关，不能替换协议 | 已有 `vmrun`，只适合开关机和电源状态 |

VMware 官方限制，实施时照做：

- 开启 3D 加速时，这条远程显示效果差。客服机本来不用 3D，虚拟机设置里关掉。
- 每个虚拟机一个端口，文档建议 `5901`–`6001`。默认 `5900` 多台会撞。
- 口令在 RFB 里不加密。端口只给本机用，宿主机防火墙拒绝非 `127.0.0.1`。
- 官方建议客户端优先 Hextile、全彩色。第 6 节要用实际客户端确认 Tight / ZRLE 是否可用；不可用就固定 Hextile。
- 经 VNC 不能开关虚拟机电源、不能改虚拟机设置、不能做快照。这些继续走已有的 `vmrun`。

## 4. 键鼠租约（先做，与画面通道无关）

目标：同一时刻只有一个写入者。外发写键鼠时，远控点击丢掉；远控接管时，外发等待或直接失败，不两边一起点微信。

```
操作员接管  ──持有──▶  Local\vm_input_owner
desktop 外发 ──持有──▶  同一把命名互斥量
vm_agent 自己的 cmd.send_text ──持有──▶  同一把
```

规则：

1. 互斥量跨进程。`vm_agent` 里现有的 `_rpa_lock` 留着保护微信 UIA，不够用来挡 desktop。
2. `cmd.remote_input` 在拿不到锁时丢弃。`mouse_move` 本来就可以丢；`mouse_down` / `key_*` 也丢，并在控制台把这一小段标成「外发占用」。不要把事件排队到外发结束再回放，回放会点错窗口。
3. 外发拿不到锁（操作员正在接管）时失败并回报，不在锁外面抢前台。这和集群方案里 `busy=rpa` 与接管互斥的方向一致；补上的是跨进程，以及 vm_agent 目前没有在入队时检查 `agent_busy`。
4. 释放锁之前，谁用过修饰键谁负责抬起：左/右 Ctrl、Alt、Shift、Win 各发一次 `key_up`。抢前台那段 Alt 放进 `try/finally`，避免异常路径少一次抬起。
5. `BlockInput(True)` 只包住外发自己的短动作，并且这段时间内远控锁必须在外发手里。不允许「挡住输入的同时，另一个进程还在 `SendInput`」。
6. 接管开始、WebSocket 重连、租约到期，都先做一次修饰键全部抬起。现在 `key_up` 丢一帧，键也会粘住，这和 RPA 无关。

画面改走 VNC 之后，第 2 条的丢弃改成：外发持锁期间，VNC 客户端停发指针和键盘。抬起修饰键仍在客人机里做，因为粘住的是 Windows 的键状态，不是 VNC 客户端里的状态。可以用一次很短的客人机注入只发 `key_up`，或者在外发进程的 `finally` 里发。不要为了抬键把整路 `SendInput` 远控留着。

## 5. 目标结构

```
操作员控制台（PyQt，现有画面面板）
    │  会话面不变：消息、外发、心跳、cmd.send_*
    │  画面面：仅 preview / control 时
    ▼
127.0.0.1:590x   Workstation 内置 VNC（vmware-vmx）
    │  虚拟帧缓冲 + 虚拟 HID
    ▼
访客机 Windows
    vm_agent     微信监听、外发、剪贴板轮询、修饰键抬起
    desktop      外发；与 vm_agent 共一把输入锁
```

职责：

| 模块 | 保留 | 停用（VNC 验证通过之后） |
|------|------|--------------------------|
| `vm_agent` 微信与 `cmd.send_text` | 是 | |
| `event.remote_clipboard` | 是。VNC 没有这条剪贴板 | |
| `remote/capture.py` 的预览/接管推流 | 缩略图若要做，仍可低频 JPEG | 有人打开预览或接管时不再编码 |
| `remote/input.py` 的移动和点击 | | 接管改由 VNC 注入。只留「全部修饰键抬起」 |
| 控制台 `RB01` 解码 | 非 VMware 机器、以及验证完成前的退路 | VMware 客人机的 preview/control |
| `vmrun` | 电源、快照、库存 | 不拿它做画面 |

端口和口令记在宿主机库存里（已有 `vmrun` 与 vmx 路径的那一侧），不让访客机上报。客人机不知道自己的 `.vmx`，也不该知道 VNC 口令。

`hello.agent.capabilities` 增加一个事实标记即可，例如 `remote.view=vnc`，表示这台机器的画面不要再向它要 JPEG。标记与口令无关。帧格式文档要改的是：VMware 客人机的 preview/control 不再走 `RB01`。缩略图策略仍按集群方案，第一期大盘可以不订阅画面。

连接只在订阅存在时建立。订阅变为 0 立刻断开 VNC，避免宿主机空转编码。一台虚拟机同时一个控制者，沿用现有租约。第二个查看者若只预览，Workstation 允许多个 VNC 客户端，但两个客户端一起注入会抢指针；预览连接要关输入。

## 6. 实施顺序

### 6.1 第一天：用现成 VNC 客户端验证，不写业务代码

挑一台已经在用的低配 Windows 客人机：

1. 关掉该虚拟机的 3D 加速。
2. 虚拟机设置 → Options → VNC Connections，端口用 `5901` 这一段里的空闲端口，口令不超过 8 位。
3. 宿主机上用现成查看器（TightVNC / RealVNC，编码先选 Hextile）连 `127.0.0.1:5901`。
4. 在客人机里打开一个会弹出 UAC 的动作。确认框应出现在查看器里，并且能点「是 / 否」。
5. 同时让 desktop 跑一次微信外发，在查看器里双击桌面图标。修第 4 节之前，这里应能复现属性框或带 Ctrl 的选择；修完之后，外发期间查看器的点击应被停发，外发结束后双击恢复为打开程序。
6. 看宿主机 `vmware-vmx` 与访客机 CPU。访客机在只有 VNC、没有 JPEG 循环时应不再出现截屏那截 CPU。

这一步失败（确认框仍是黑的、或 3D 关掉后仍花屏）就停，不改控制台。不要改去部署 RustDesk 补这个洞。

### 6.2 键鼠租约

desktop 与 vm_agent 同时改。行为以第 4 节为准。完成后用 6.1 的第 5 步复测。此步不依赖 VNC，现状的 `SendInput` 路径上就能减轻错位。

### 6.3 控制台接入

- 库存：`vm_id` → 本机 VNC 端口、口令。口令不要写进访客机 `config.yaml`，也不要进日志。
- 画面面板的像素来源从「最新 JPEG」换成「VNC 帧缓冲」。归一化坐标按帧缓冲宽高换算，继续只在 `control` 租约下发送。
- 客户端优先用一个小的 VNC 库连在控制台进程里。这是协议客户端，不是把 VNC 服务器写进 `vm_agent`。
- 若这层库不合适，再在宿主机旁路一个 **guacd**，后端只填 `vnc://127.0.0.1:590x`。控制台改用 pyguacamole 收图。这仍然是调用进程，不要部署 Guacamole 的 Web 容器，不要把后端改成客人机 `3389`。
- 剪贴板继续 `event.remote_clipboard`。
- 非 VMware、或这台没开 VNC 的机器，退回现有 JPEG。退路保留到库存里每一台都标了端口再考虑删除。

### 6.4 文档

验证通过后，修订 `desktop-agent-api.md` 的 `cmd.remote_start` / `RB01` 适用范围，以及 `vm-cluster-comm-plan.md` 第 5 节：VMware 上的 preview/control 不再由访客机编码 JPEG。缩略图、订阅路数、`busy` 互斥不推翻。

## 7. 明确不做

- 不在访客机安装 RustDesk、TightVNC、UltraVNC 或 RDP 包装来代替这条通道。
- 不把 `vmrun typeKeystrokesInGuest` 或 `VixVM_CaptureScreenImage` 当作接管。
- 不把画面流放进 `work_place` FastAPI，也不为远控改微信协议。
- 不为了迁就客人机截屏，去关 UAC 或关「切换到安全桌面时提示」。那会削弱确认框，也解决不了登录和锁屏。
- 不在第一期上 H.264 / WebRTC。集群方案已经把编码升级放到带宽不够之后；这次换的是采集位置，不是编码格式。
- 不把 VNC 端口暴露到宿主机网卡之外。操作员不在这台宿主机上时，沿用现有「控制台连宿主机网关」的方向，由网关代连 `127.0.0.1:590x`，而不是把 `5900` 段映射到公网。
