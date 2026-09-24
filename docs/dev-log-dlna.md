# Dromify DLNA 研发日志

工作分支：`feature/dlna-output`（fork: `gmaxxxie/dromify-omarchy`）
仓库：`/home/max/project/dromify-omarchy`
安装位置：`~/.config/omarchy/plugins/tallahootie.dromify`

---

## 一、当前待办与状态

| # | 问题 | 状态 | 现象 | 定位 |
|---|---|---|---|---|
| 1 | DLNA 后端 `load-queue` 崩溃 | 已修；CLI 实机播放已验证 | `payload` 未定义的问题已改为使用 `args.start` | `bin/dromify-dlna` |
| 2 | 面板 queue 显示为空、与实际不符 | Service IPC 已验证 | 播放请求立即设置索引，状态轮询再校准 | 见下方「根因 A」 |
| 3 | 切歌按钮状态 | 后端 `next` 已在 ZR7 实测；UI Service 路径仍待验证 | `next` / `previous` 由后端驱动队列 | 见「根因 B」 |
| 4 | `loading` 超时 | 已定位并修复，实机播放已验证 | 路由器等待 Quickshell stdin EOF，进程不退出 | 见第十一节 |
| 5 | 待播放列表（queue）UI | 暂缓 | 当前优先保持上游原版面板，只保留独立 Output 弹窗 | 见「待做需求」及第十节 |
| 6 | 重启 shell 后渲染器名丢失 | 已修 | `output=dlna` 但名字为空 | `_adoptRenderer()`，见「已完成」 |
| 7 | 安装目录与仓库不同步 | 已同步 | 修复文件已安装，shell 已重启 | 见第十一节 |
| 8 | 列表点选其它歌曲无反应 | 已修，实机连续选曲已验证 | 忙时第二次 `playFrom` 丢弃，却使第一次结果过期 | 见第十二节 |

---

## 二、明天第一件事

```
# 1. 把仓库改动同步到已安装目录
rsync -a --exclude .git /home/max/project/dromify-omarchy/ \
      ~/.config/omarchy/plugins/tallahootie.dromify/

# 2. 重启 shell
omarchy-restart-shell

# 3. 用面板自己的诊断入口验证（不需要鼠标）
P=$(pgrep -f "quickshell -n -p /usr/share/omarchy" | head -1)
qs ipc --pid $P call dromifyService state    # 面板认为的状态
qs ipc --pid $P call dromifyService events   # 操作日志 + 失败原因
qs ipc --pid $P call dromifyService playAlbum <albumId>
```

诊断入口是这次新加的，**这是明天最有用的工具**（见「已完成」第 8 条）。

---

## 三、已定位的根因（按重要性）

### 根因 A：`clearPlaybackState()` 把更新的播放状态清掉了

`selectDlnaOutput` / `selectLocalOutput` 的完成回调里会清空 `queue`，但这个回调**可能比 `playFrom` 晚很多**——因为 `outputProcess` 是共享的，前面可能排着一次设备扫描（6–8 秒）。

实测时序（事件日志）：

```
+  6130ms  playFrom       5 tracks from 0, output dlna   ← queue 设成 5 首
+ 13554ms  selectDlnaOutput done                          ← 7.4 秒后清空了它
```

结果：硬件在播，面板 `queueIndex=-1 / queueLength=0`。

**已做的修**：用 `_playGen` 代际门控——只有当这次切换期间**没有**新的 `playFrom` 时才清空。`Service.qml:1342` 和 `:1367`。

**已做**：`playFrom` 现在立即同步 `queueIndex`，状态轮询随后按后端实际位置校准。仍需在真实面板上验证显示。

### 根因 B：`next` 委托给设备是错的

设备会**谎报成功**：它 `GetCurrentTransportActions` 里有 `Next`，调用返回 `OK`，但**实际不动**（实测：`state.index` 不变、`TrackURI` 不变、位置继续走）。

**已做的修**：`cmd_next` 改为与 `cmd_previous` 一样由后端驱动队列（`bin/dromify-dlna`），并支持 `repeat=all` 回绕。

**还需做**：验证「暂停 → SetAVTransportURI 新歌 → Play」这条更快的路径（用户的建议）。目前 `start_track` 是直接 `SetAVTransportURI + Play`，没有先 Pause。ZR7 在 PLAYING 状态下 `SetAVTransportURI` 会**间歇性**返回 501/716（已有一次重试）。若明天验证发现失败率高，就在 `start_track` 里加「先 Stop、再 Set、再 Play」的退避路径。

### 根因 C：`loading` 标志仍会卡住

`loading=true` 会同时导致两件坏事：
- `enabled: !nav.loading` → 切歌按钮永久灰色
- `statusPollProcess` 的 `onExited` 里 `if (root.loading) return` → 轮询结果被丢弃 → 面板永远不更新

我已经做了两处缓解：
- 加了 12 秒看门狗 `loadingWatchdog`（事件日志里的 `loading timeout` 就是它在工作）
- **删掉了** `if (root.loading) return`（宁可偶尔显示前一拍的位置，也不要面板完全停止更新）
- 去掉了按钮的 `!nav.loading` 门控

**已做**：`playFrom` 和 `_reorderTail` 检查 `_run` / `_runWithSecret` 的返回值；共享进程忙时记录错误并清除 `loading`。若真实运行中仍出现超时，再用 IPC 事件日志定位具体未退出的子进程。

### 根因 D：`OutputRow` 曾经是 0 像素宽（已修）

`CursorSurface` 是 `Rectangle`，不会从子元素取 `implicitWidth`。在 `ColumnLayout` 里量出来宽 0 → `MouseArea` 面积 0 → **点哪儿都没反应**。

实测：

```
修复前  This Computer    w=0    MouseArea w=0
修复后  This Computer    w=400  MouseArea w=400
```

修法：`OutputRow` 给 `implicitWidth`，两个使用处加 `Layout.fillWidth: true`。

### 根因 E：`IpcHandler` 重复注册

`Panel.qml` 里有个 fallback 的 `Service { id: _localNav }`，它也注册 `dromifyService` → 我的诊断命令可能打到**面板没在用的那个实例**。修法：`Service.exposeIpc` 属性，fallback 实例设为 `false`。

---

## 四、已完成（这轮，均已实测）

1. **SSH 隧道**：`~/.config/systemd/user/dromify-tunnel.service`，`127.0.0.1:4533 → max-192:4533`，enabled + active。这样服务器变成 loopback，**上游的 https 强制得以保留**（我原来加的那个 `DROMIFY_ALLOW_INSECURE_LAN` 开关已整个撤掉，`bin/dromify-api` 现在与上游逐字节一致）。

2. **`Panel.qml` 还原成上游 + 只加一个按钮**：`git diff origin/main --numstat` = **210 新增 / 0 删除**。即：面板本身一行都没改，只在头部加了一个「输出」按钮，它打开**独立的输出弹窗**（不是嵌在歌单面板里）。你之前说的「不如不碰原版的，只加个 output 的渠道，要改渠道的再弹窗去选择」——就是这样。

3. **stdin 不再依赖 EOF**：此处记录的是上一轮尝试；第十一节的实测证明路由器仍在等待 Quickshell stdin EOF，最终改为按曲目数量读取。

4. **队列格式统一**：此处记录的是上一轮的 url/title 行对；第十一节改为 JSON Lines，以便传递音频格式。

5. **发现渲染器不再卡 45 秒**：
   - 所有网卡同时发 M-SEARCH，用一个 `select` 一起收，轮次并入同一窗口（原来 = 网卡数 × 轮次 × 窗口）
   - 描述文档并发抓取
   - 有硬预算，超时放弃而不是干等
   - **关键**：缓存里的设备靠**直接问它自己的控制地址**（毫秒）确认存活，而不是指望它应答 SSDP。实测你这台 ZR7 **只对约 1/4 的 M-SEARCH 应答，且总在窗口末尾才回**。
   - 面板用 `--cached` 秒开，扫描在后台跑

6. **HTTPS / loopback 都自动走 bridge**：渲染器取不到 `https://`（无 TLS）和取不到 `127.0.0.1`（那是它自己）是同一个问题，`dromify-bridge` 都能解决。

7. **面板布局溢出已修**：整个面板主体放进 `ScrollView`；列表高度上限在 Output 展开时从 460 收到 300，避免出现两层滚动。实测卡片下边界 y=1187（屏幕 1200），内容不再越过边框。

8. **新增诊断 IPC**（明天的主要工具）：

```
P=$(pgrep -f "quickshell -n -p /usr/share/omarchy" | head -1)
qs ipc --pid $P call dromifyService state        # 面板认为的状态
qs ipc --pid $P call dromifyService events       # 环形缓冲：做过什么、结果如何
qs ipc --pid $P call dromifyService devices      # 扫一轮
qs ipc --pid $P call dromifyService select <udn> # 等于点那一行
qs ipc --pid $P call dromifyService useLocal
qs ipc --pid $P call dromifyService playAlbum <albumId>   # 等于点一首歌
qs ipc --pid $P call dromifyService clearErrors
```

**为什么需要它**：面板是个弹窗，而这台机器的 Hyprland 是 lua 配置、**没有暴露合成鼠标点击的 API**（试过 `hl.dsp.cursor.click`、`ydotool`、`dotool` 都没有）。所以「点了没反应」和「点了没传到」从外面无法区分——之前好几个 bug 都是这么误判的。

---

## 五、待做需求

### 5.1 待播放列表（queue）UI（暂缓）

当前界面优先保持上游原版布局，不在主面板增加队列列表。后续如重新排期，再评估独立弹窗方案。

- 数据已在 `Service.qml`：`queue` 数组 + `queueIndex`
- 后端 `dromify-dlna status` 已返回 `playlistPos` / `playlistCount`
- 队列状态修复（根因 A）与 UI 是否展示是两项工作；修复不依赖新增队列界面

### 5.2 通知栏控制

Omarchy 的 `omarchy.media` 控件（`/usr/share/omarchy/shell/plugins/media`）已能控制 mpv。**DLNA 模式下它控制不到音箱**，因为没有对应的 MPRIS 服务。

- 方案：起一个最小的 MPRIS 服务代理到 `dromify-output`
- 依赖已具备：`python-dbus` 是系统已有包
- 成本：一个独立进程 + 生命周期管理，可能几十到几百行
- 决定：等 5.1 完成、播放稳定后再做

---

## 十、续记（2026-09-24）

本轮按上述遗留项处理了仓库代码：

- `playFrom` 立即同步 `queueIndex` 的逻辑已在当前分支中；状态轮询继续用后端位置校准它。
- `_run` / `_runWithSecret` 忙时会记录明确错误并清除 `loading`，避免请求被静默丢弃后只能等看门狗。
- 保持上游主面板布局；分支已有的独立 Output 弹窗继续提供 *This Computer* 和 DLNA 渲染器选项。本轮没有把输出选择嵌进歌曲浏览面板。
- 队列列表 UI 暂缓，避免扩大对原版界面的修改。

本轮已同步到 `~/.config/omarchy/plugins/tallahootie.dromify` 并重启 shell。真实运行发现 Output 弹窗原先用错了 `KeyboardPanel` 的 `opened` 属性和信号，已改为 `open` / `onOpenChanged`；最新 shell 日志不再出现 Dromify widget load failure。面板视觉点击未能自动化验证。

ZR7 的缓存设备发现可列出 SRS-ZR7 和 Epson；通过安装版 `dromify-output` 实测了播放、Next、暂停、恢复和停止，状态转换正确。通过 `dromifyService.playAlbum` 的 Service 路径则仍有间歇性 `SetAVTransportURI 501` 和 `loading timeout`，因此端到端 UI 播放尚未通过。测试结束时设备为 `NO_MEDIA_PRESENT`，没有遗留播放。

---

## 十一、播放修复与实机复测（2026-09-24）

- 现场有一个 `dromify-output load-queue 0 1` 运行近 20 分钟，卡在 `anon_pipe_read`，临时文件已写入一条队列记录。原因是路由器继续等待 Quickshell 的 stdin EOF。路由器现按命令给出的曲目数量读取 JSON 记录；`Service.qml` 保持 stdin 可供下次调用复用。
- 修复阻塞后，单曲请求能退出，但 ZR7 曾短暂进入 `PLAYING` 后变为 `STOPPED`，位置为 0。桥接 URL 的 Range 请求返回 HTTP 206、`audio/mp4`，而 DLNA 队列因缺少曲目 `contentType` 将它声明成 `audio/mpeg`。面板现传递 JSON Lines，包含 `contentType`、后缀和曲目元数据；路由器仍为本地 mpv 转成 URL/title 行对。
- 假后端回归检查：保持 stdin 打开时，旧路由器超时；新路由器对一首歌正常退出，对不足数量的队列报错。两首歌的 JSON 队列在 DLNA 路径保留 `contentType`，在本地路径转为四行 URL/title。
- 安装版同步并重启 shell 后，通过 `dromifyService.playAlbum` 两次发起同一首歌。第一次 ZR7 的播放位置增长到 10 秒，第二次重新开始并增长到 3 秒；两次面板均显示 `loading=false`、`playing=true`、`lastError=""`，后端 MIME 为 `audio/mp4`。最终安装版复测再次增长到 3 秒，之后停止了测试播放，后端队列已清空。

实测范围是面板 Service IPC 到音箱状态和播放进度；没有自动化鼠标点击面板。待播放列表 UI 继续暂缓。

---

## 十二、列表点击其它歌曲无反应（2026-09-24）

用户再次点选不同歌曲时，面板事件日志已经收到正确的 `playFrom` 索引，但同一点击后约 150 ms 又出现第二次 `playFrom`。第二次调用先递增 `_playGen`，随后因 `loadProcess` 正忙而被 `_run` 拒绝；第一次的 URL 回调发现代际过期也直接返回，结果两次都不切歌。

`Service.qml` 现在只保留最新的待播放请求，在 `loadProcess` 或 `playQueueProcess` 退出后自动继续；播放加载期间的状态轮询不再用旧曲目覆盖刚点选的索引。新增 `dromifyService.playQueueIndex` 作为诊断入口，可走与歌曲行相同的 `playFrom` 路径。

安装版重启后，在 12 首歌的队列里快速请求索引 6、再请求索引 3。ZR7 最终报告 `PLAYING`、`playlistPos=3`、进度 8 秒；面板报告 `queueIndex=3`、`loading=false`、`lastError=""`，事件日志没有 `playFrom dropped`。本次通过 IPC 模拟连续点选，实际鼠标事件此前已由日志证实到达 `playFrom`。

---

## 六、已知设备行为（实测记录，别再重复踩）

Sony SRS-ZR7（`192.168.1.27:54380`）：

| 行为 | 实测结果 |
|---|---|
| 对 M-SEARCH 的应答率 | 约 **1/4**，且常延迟到窗口末尾；只在 TCP 上收（从 UDP 1900 端口发反而 0/40） |
| SSDP 里的 UDN vs 描述文档里的 UDN | **不一致**（两个不同 UUID）→ 身份必须用 AVTransport control URL 判定 |
| 描述文档 | 有两份（`MediaRenderer_SRS-ZR7.xml` 和 `MediaRenderer.xml`） |
| `GetCurrentTransportActions` | **每次调用都不一样**（有时 `Stop,Next,Previous`，有时含 `Pause,Seek`）→ 必须每次现读，不能缓存 |
| `Previous` | 返回成功但**什么都不做** → 必须后端驱动队列 |
| `Next` | 同上，返回成功但不动 |
| 播放到流的末尾 | 落在 **`PAUSED_PLAYBACK`** 且位置=时长（不是 `STOPPED`）→ 结束时判定要看「位置≈时长」而不是只看状态字 |
| `SetAVTransportURI` | **间歇性** 501/716，隔 1 秒重试通常成功 |
| 转码流（无 Content-Length）的 `TrackDuration` | 返回**垃圾值**（4 分钟的歌报 `596:31:23`）→ 用队列里 Subsonic 给的时长 |
| `https://` 流 URL | **一律拒绝**（fault 501）→ 必须走 bridge 或明文 http |
| `audio/dsd` | sink list 里**列了**，但喂 DSF 就 501 → 不能信 sink list，DSD/APE 一律转码 |
| 支持的格式（实测可播） | MP3 / FLAC / WAV / ALAC(M4A) 直连；DSF / APE 需转码 |

---

## 七、环境事实（别丢）

- Navidrome 0.64.1 在 `192.168.1.69:4533`（NAS 上，监听 `0.0.0.0`），**ZR7 能直连它并播放**（实测 FLAC 直连 PLAYING）
- 隧道：`127.0.0.1:4533`（本地播放走这个，保留 https 强制）
- bridge：`192.168.1.21:53317`（投送时用这个，因为 ZR7 取不到 127.0.0.1）
- 本机两个网卡：`enp1s0f0 192.168.1.21`（有线）、`wlp2s0 192.168.1.20`（无线）
- 免密 SSH：`ssh max-192`（`~/.ssh/config` 已配，用户 `max`）
- **Hyprland 是 lua 配置**，没有合成鼠标点击的 API → 只能用无头 Quickshell 或诊断 IPC 验证
- 复现用的无头探针在 `/tmp/qc/`（`cp` 仓库文件进去 + 写个 `shell.qml` 就能跑真实 `Service`）

---

## 八、上一轮状态（2026-09-24）

```
分支        feature/dlna-output
本轮修改    Panel.qml / Service.qml / docs/dev-log-dlna.md
检查        bash -n、py_compile、git diff --check 通过；Service.qml 的 qmllint 通过
面板检查    Quickshell 已加载插件；修复前的 widget load failure 已消失
后端实测    ZR7 播放 / Next / Pause / Resume / Stop 通过；Service 播放仍有 501 / loading timeout
安装目录    已 rsync；Panel.qml、Service.qml 与仓库一致
```

这一轮的 Service 播放超时见第十一节；队列 UI 按「尽量保留原版界面」的要求暂缓。**先验证再提交**，不要凭「代码看起来对」就 commit。

---

## 九、教训（写给明天的自己）

1. **改共享 Process 的副作用**：`outputProcess` 被「切换输出」「读状态」「刷设备」三处共用，一次只能跑一个，其余静默丢弃。这一条的连锁反应制造了本轮一半的 bug。
2. **`loading` 这种全局标志是陷阱**：它同时门控 UI 和丢弃轮询，一旦卡住就是「面板又没反应又不对」。任何这类标志都应该带超时。
3. **不要用命令行验证代替真实 UI 验证**。这轮所有「我这边通过」的判断，在真实面板上都翻过车（0 宽度、重复 IpcHandler、代际竞态）。
4. **先问用户，再动手改上游行为**。我加的那个明文 http 开关属于改上游安全策略，本来应该先问。
5. **`Panel.qml` 现在相对上游是 210 新增 / 0 删除**——这个约束要守住。以后改 UI 时每次都核对 `git diff origin/main --numstat`。
