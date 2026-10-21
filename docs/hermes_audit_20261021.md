# Hermes 全系统只读巡检报告（2026-10-21）

- 卡：t_ec0ee42f「只读巡检 hermes 全系统状态并产出证据化问题清单」
- 巡检人：megumi（加藤惠，三老婆）
- 巡检窗口：2026-10-21 15:34–16:00 +08:00（主机 JOJO 桌面机，Windows 11）
- 主机口径：`HERMES_HOME = D:\Program Files (x86)\AI\hermes`，代码库 `hermes-agent`（HEAD `49f72a2411`，`v0.22.2-10-g49f72a2411`，分支 `main`）
- 阅读方式：只读。除本报告文件外，未向 hermes home / profile / 生产配置写入任何内容（证明见 §6）
- 证据根目录：`D:\Program Files (x86)\AI\hermes\kanban\workspaces\t_ec0ee42f\evidence\`
  - `state_20261021.json`（本轮新鲜探针：nul / repo / gateway / pywin32 / 日志尾部 / 端口 / 容量）
  - `probe_20261021_*.py`（可复跑，全部只读）、`ro_before.json`、`repo_status_before.txt`
  - 10-16 一轮的历史证据 `00_*.json … 25_*.json`、`fresh_*.json`（作为趋势对照，不与本轮现状混用）

---

## 0. 结论摘要（按修复顺序）

| # | 严重度 | 问题 | 现状（2026-10-21 复核） |
|---|--------|------|--------------------------|
| D1 | P0 | hermes-lcm 在压缩期崩溃：`store.close()` 置空 `_conn` 后其他线程继续用它 | 今天 14:16 真实发生，仍有 16 次同类 |
| D2 | P0 | kanban worker 崩溃后 **run 停在 crashed、任务停在 running**，不回收 | 本卡与 mitsuri 卡共 4 条幽灵 run |
| D3 | P0 | 运行时解释器缺 pywin32 → 日志轮转降级，日志无界增长 | 每天约 1400+ 条告警，仍在发生 |
| D4 | P1 | 队列/账本口径失真：`review` 车道 173 条 run 永不关闭；`budget_stopped` 456 条无原因 | 仍在（数据层） |
| D5 | P1 | home 根目录 `nul` 设备名残留 + 代码 `relpath` 无保护 | 文件仍在，代码仍在，属未触发的崩溃雷 |
| D6 | P1 | `config.yaml` 的 `human_delay.mode: true` 是布尔值，枚举非法 | 仍在，gateway.log 200 条/轮 |
| D7 | P1 | 投递失败被静默丢弃（Discord 网络不可达、async delegation 租约冲突） | gateway.log 16 条 |
| D8 | P2 | 本地修改与升级窗口互相踩踏的历史残留（已修复的 Path/ImportError 崩溃波） | 已修复，但模式会复发 |
| D9 | P2 | 容量：state.db 6.48 GB、lcm.db 2.66 GB、memory.sqlite3 594 MB | 5 天 +365 MB，无维护窗口 |
| D10 | P2 | 空壳 `kanban/kanban.db`（0 B）充当真实库路径，误导巡检 | 仍在 |
| D11 | P2 | 仓库卫生：24 个未跟踪本地补丁 + 12 个 `C␺Users…` 变形文件名 | 仍在 |
| D12 | P2 | 保底 provider 端点 18434 已死，却仍在 fallback 链尾 | 本次连接超时 |
| D13 | P3 | kaguya profile cron 失败率 39%（206/524）与 205 条 incident 噪声 | 历史数据，未复跑 |

---

## 1. 环境基线（本轮实测）

**进程与版本**

- gateway：pid 19744，`gateway_state.json` → `state=running`，`code_sha=49f72a2411…` 与磁盘 HEAD **一致**。
- 运行命令：`"D:\Program Files (x86)\AI\hermes\tools\python-3.14.7+…\python.exe" -m hermes_cli.main gateway run`
- 渠道：qqbot / api_server / telegram / discord 及 kaguya、megumi、mitsuri、tsunade 四人的 telegram+qqbot 全部 `connected`；仅 `weixin` 为 `disconnected`（配置里有凭据但平台未启用，属一致性噪声，见 §4）。
- 端口实测：8450 ✓ / 8940 ✓ / 8645 ✓ / 8788 ✓ / 3080 ✓ / 18188 ✓ / 7792 ✓ / **18434 ✗（connect timeout）**。
- 磁盘热点：`state.db` 6480.9 MB（10-16 为 6116.2 MB）、`lcm.db` 2663.4 MB、`scope-recall/memory.sqlite3` 593.8 MB、`cache/scratch` 106,637 文件 / 871.0 MB（10-16 为 200,128 文件 / 3797.2 MB，清理机制已生效）。
- 日志体量：`gateway-stdio.log` 68.71 MiB（10-20 12:46 后未再增长）、`gui.log` 8.63 MiB（今日仍在写）、`desktop.log` 8.11 MiB，另有 4 个 ~10 MiB 轮转文件。

**仓库状态**

- `git status --porcelain`：48 项 = 24 个 `M`（已改源码/测试）+ 24 个 `??`（12 个真实新文件 + 12 个变形垃圾名）。
- 无 `CHERRY_PICK_HEAD` / `MERGE_HEAD` / rebase 目录，**无未合并路径**（10-16 曾有 UU 冲突与进行中的 cherry-pick，现已收尾）。
- 仓库内**没有 `docs/` 目录**，本报告是首篇，目录由本次建立。

---

## 2. 缺陷详表

### D1（P0）hermes-lcm 压缩期崩溃：use-after-close

- **现象**：自动压缩触发时抛 `AttributeError: 'NoneType' object has no attribute 'execute'`，紧接着 `RuntimeError: Event loop is closed`；本轮 gateway 侧崩溃今天 14:16:31 实际发生。
- **调用链**（`logs/tui_gateway_crash.log`）：
  `agent/compression_facade.py:280` → `agent/conversation_compression.py:4227 → 3916 → 3026`
  → `plugins/hermes-lcm/compaction.py:356 → 565` → `reconcile.py:969` → `store.py:794`
  `store.py:1673 def close()` → `1684 self._conn = None`；而 `store.py:270` 声明该连接跨线程共享（`check_same_thread=False`）。
- **量化**：`tui_gateway_crash.log` 共 501 条未处理异常，其中 `NoneType … execute` 16 条；`agent.log.1` 另有 `lcm_timeout` 50 条、`lcm_locked`（database is locked）29 条。
- **影响**：长会话压缩中途崩，表现为「回复卡住 / 上下文没压成功 / 该轮丢失」。`lcm.db` 2.66 GB 让锁窗口更长，正反馈。
- **建议修法**：`Store.close()` 设 `_closed=True` 并让读取路径抛可识别异常（如 `StoreClosed`），由 `reconcile/compaction` 捕获后降级为「无 store 的压缩」而不是炸整轮；压缩入口对 store 持有引用或 clone；补一条 use-after-close 回归测试。

### D2（P0）kanban：worker 崩溃后任务与 run 状态不复位

- **现象**：`task_runs.status='crashed'` 的记录，其 `tasks.status` 仍是 `running`，共 4 条，全部属于本卡族：

  | run | 任务 | profile | 结束时刻 | 任务状态 |
  |-----|------|---------|----------|----------|
  | 1273 | t_ec0ee42f | megumi | 10-16 21:23 | running |
  | 1277 | t_ec0ee42f | megumi | 10-16 22:25 | running |
  | 1274 | t_66d5273e | mitsuri | 10-16 21:23 | running |
  | 1278 | t_66d5273e | mitsuri | 10-16 22:25 | running |

- **错误文本**：`pid 54636 exited with code 1 Worker's last output: 'Kanban owner refused: Gateway disconnected; turn outcome is unknown…'`（4 条 owner refused、2 条 exit 130）。
- **影响**：看板出现「幽灵 running」，槽位与统计被占用；重试会再造一条 run，旧 run 永不清理；下游卡无法判定上游是否真跑完。
- **建议修法**：worker 非零退出路径里，把 run 终结为 `crashed` 的同时按分类回落任务状态（可重试 → `ready`，需人工 → `blocked`）；「Gateway disconnected」属可重试类，应 requeue 而非 `crashed` 终态；启动时做一次一致性巡检（`crashed` run ↔ 非终态任务）自动收敛。

### D3（P0）运行时解释器缺 pywin32，日志轮转降级

- **现象**：`pywin32_missing`（`pywintypes is required … falling back`）在 `agent.log` 尾部 412 条、`errors.log` 尾部 368 条/今日，`agent.log.1/.2/.3` 各 1100–2600 条，日均 1400+。
- **根因定位**：gateway 由**捆绑解释器** `tools/python-3.14.7+…/python.exe` 运行，实测
  `{'pywintypes': False, 'win32file': False}`；
  而 install venv `installs/…/venv/Scripts/python.exe` 里为 `{'pywintypes': True, …}`。
  → pywin32 装进了 venv，却没装进实际运行的解释器。
- **影响**：`concurrent-log-handler` 不可用，日志退回标准库轮转；`gateway-stdio.log` 已 68.71 MiB，`gui.log`/`desktop.log` 各约 10 MiB 且仍在增长。排查时大量噪声淹没有效行（本轮 4 MiB 尾部采样里 pywin32 告警占比最高）。
- **建议修法**：把 pywin32 装到实际运行的解释器（或把该依赖写进捆绑运行时的安装清单）；启动自检增加「运行解释器 vs venv 依赖一致性」比对，不一致直接打一条明确告警而不是每天 1400 条同类噪声。

### D4（P1）kanban 账本口径失真

- **review 泄漏**：`task_runs.status='review'` 173 条，**全部 `ended_at IS NULL`**，最长 1580 小时（65 天）；其中 146 条的任务已 `archived`、27 条已 `done`。当前看板 review 列任务数为 **0**。
  → 「待复核」既不在列上、又在账本上敞着 173 条，`review_requested` 179 也就虚高了。
- **停机原因不可读**：`budget_stopped` 456 条，`summary` 全为空串；`max_runtime_seconds` 在 **1121/1258** 条 run 里为 NULL（墙钟上限基本没生效）；`gave_up` 41 条为「Iteration budget exhausted 100/100」。
- **可读性对照**：`crashed` 115 / `reclaimed` 37 / `rate_limited` 16 / `timed_out` 12 / `done` 251 / `completed` 268 —— 这些有原因文本，上面两类没有。
- **建议修法**：任务离开 review（archive / complete）时，把对应 review run 一并收尾为 `review_approved` / `changes_requested` / `abandoned`；`budget_stopped` 必写「为何停 + 剩余额度」；dispatcher 给默认 `max_runtime_seconds`；提供一次性数据修复脚本收敛存量。

### D5（P1）home 根目录 `nul` + 无保护的 relpath

- **现状**：`D:\Program Files (x86)\AI\hermes\nul` 仍存在（0 字节，mtime 1970），并出现在 `os.listdir(home)` 里。
- **复现**：`ntpath.relpath(home\nul, home)` 直接抛
  `ValueError: path is on mount '\\\\.\\nul', start on mount 'D:'`（本轮实测，见 `state_20261021.json.home_nul / nul_relpath_repro`）。
- **代码位置**：`tui_gateway/methods_complete.py:219 rel = pth.relpath(full, root)` —— 对目录列举结果逐条做 relpath，无 try、无设备名过滤。
- **历史伤害**：`gateway-stdio.log`（68.71 MiB）尾部 4 MiB 采样里 `ws dispatch crash` 118 条，`method=complete.path` 在整个文件里出现 425 次，最后一次崩溃在 2026-10-16 16:12（`peer=127.0.0.1:14469 method=complete.path`）；`gui.log` 尾部 4 MiB 亦有 994 条同类。`16_residue_schedule.json.root_residue.nul` 记录该文件 size=0、mtime 1970；home 目录下 `git add -A` / `stat` 触到它会直接失败。
- **现状判定**：10-17 之后没有新样本（0.22.2 重启过，路径未被触发），但**文件在、代码在**，属条件触发的崩溃雷。
- **建议修法**：列目录时过滤 Win32 保留名（`nul/con/prn/aux/com1..lpt9`，含扩展名变体）；relpath 前用 `os.path.splitdrive` 判同盘并 try 兜底；把该 `nul` 删除（它本身是历史误创建物）。

### D6（P1）配置类型错误：`human_delay.mode: true`

- **证据**：`config.yaml:949-952`
  ```yaml
  human_delay:
    mode: true
    min_ms: 2500
    max_ms: 8000
  ```
- **表现**：`gateway.log`（今日 15:31）尾部 200 条 `human_delay.mode` 相关告警，`gateway.log.1` 另有 202 条、`gateway-stdio.log` 48 条 —— 基本是每次启动/每次发送都重复，运行期静默回落默认值。
- **建议修法**：改成 `mode: natural`（或 `custom`，看原意）；校验层对枚举字段做类型检查，非法值只报一次而不是刷屏。

### D7（P1）投递失败被静默处理

- **样本**：
  - `Discord send failed: Cannot connect to host discord.com`（网络不可达）
  - `Async delegation delivery persist failed … active turn lease; refusing transcript mutation`（租约冲突直接放弃落盘）
- **量化**：`gateway.log` 尾部 16 条 `delivery_fail`，`agent.log` 2 条，历史 `agent.log.3` 1 条。
- **影响**：对思远表现为「消息发了但没到」「子 agent 结果丢了」，且没有重试记录可查。
- **建议修法**：区分「网络不可达（入 outbox 重试）」与「租约占用（排队延后写）」；两路都落一条 deferred-retry 记录，并在下一次成功时补投。

### D8（P2）本地补丁 × 升级窗口互相踩踏（已自愈，模式仍在）

- 10-15～10-18 期间 `tui_gateway_crash.log` 出现连续 493 条 `NameError: name 'Path' is not defined`（`tui_gateway/methods_onboarding.py:71` 注解在 import 期求值），最后一次在 2026-10-18 13:40:25；同段另有 `ImportError: cannot import name 'HOME_CREDENTIAL_DIRS'`（`agent/context_references.py:18`）。这是两个模块在合并/更新窗口里的**半套代码**状态，导致每次 gateway 启动都炸。
- **本轮复核**：两处已修复 —— `methods_onboarding.py:2` 已 `from pathlib import Path`，`agent/file_safety.py:210` 已定义 `HOME_CREDENTIAL_DIRS` 且 `context_references.py:18` 可正常导入。最后一次 `NameError` 在 2026-10-18 13:40:25，此后无新增。
- **风险**：仓库里仍有 24 个 `M` + 12 个未跟踪的新源码文件（`hermes_cli/goal_admission.py`、`goal_gates.py`、`goal_kanban_loop.py` 等）。这些本地补丁**不在版本控制里**，下一次升级或冲突解决会再次产生半套状态。
- **建议修法**：本地补丁走分支提交 + 打 tag，升级后做一次「启动 + 关键导入」冒烟（`python -c "import tui_gateway.server"` 这类），把 import 期求值的类型注解改成字符串注解或 `from __future__ import annotations`。

### D9（P2）容量与维护窗口

| 对象 | 10-16 | 10-21 | 备注 |
|------|-------|-------|------|
| `state.db` | 6116.2 MB | 6480.9 MB | 5 天 +365 MB，主力会话库 |
| `lcm.db` | 2424.7 MB | 2663.4 MB | 与 D1 的锁竞争直接相关 |
| `scope-recall/memory.sqlite3` | 455.0 MB | 593.8 MB | 5 天 +139 MB |
| `cache/scratch` | 200,128 文件 / 3797.2 MB（`cache` 全量 448,434 文件 / 8969.5 MB） | 106.6k 文件 / 871 MB | 清理机制有效，保持 |

- **建议**：安排离线维护窗口（checkpoint + VACUUM），lcm 加保留策略（按会话年龄段裁剪），维护前先确认 gateway 停止。

### D10（P2）空壳 DB 误导巡检路径

- `<home>\kanban\kanban.db` = **0 字节**，`<home>\kanban\boards\default\kanban.db` = **0 字节**，真实库是 `<home>\kanban.db` = 5,238,784 字节（表 `tasks / task_runs / task_events / task_links / task_comments / task_attachments / kanban_notify_subs`）。
- 本轮第一次查询就踩了：指向 `kanban/kanban.db` 得到 `tables: []`，会读成「看板空」。
- **建议**：删掉空壳，或在两处放 `README` 指针；工具侧统一走 kanban DB 解析函数而不是拼路径。

### D11（P2）仓库卫生

- **变形文件名（12 个）**：仓库根出现 `C␺Users␺JoJo␺AppData…out.bin` 这类文件名（U+F03A 替代冒号），来自测试把带冒号的绝对路径落到了 cwd —— NTFS 不接受 `:`，于是变形。
- **建议**：测试统一用 `tmp_path`（注意 Windows 路径转换规则），仓库根加 `.gitignore` 兜底并在 CI 里断言「仓库根无新增文件」。
- 24 个 `M` 文件的处理见 D8。

### D12（P2）保底 provider 端点已死

- `127.0.0.1:18434`（built-in `llamacpp`，全家五人的最后保底）本轮 connect **超时**，其余端口全通。
- 配置层 `fallback_providers` 已无未定义 provider（该项 10-16 的告警已消失），但端点本身不可达时仍会打到死端点。
- **建议**：启动时探测保底端点，不可达则标 `unavailable` 并在 fallback 链里跳过（并告警一次），避免把「最后保底」变成「最后失败」。

### D13（P3）kaguya profile 的 cron 噪声

- `profiles/kaguya/cron/executions.db`：executions 524（completed 318 / failed 206），`cron_incidents` 205；root `cron/executions.db` 1000 次仅 1 失败。
- **建议**：归因 kaguya 的 206 次失败（本会话范围外，交对应 owner 处理），否则 incident 表会被噪声淹没。

---

## 3. 10-16 清单中「本轮复核已消失」的项

| 项 | 10-16 证据 | 10-21 复核 |
|----|-----------|-----------|
| 运行代码与磁盘 checkout 漂移（model picker 503） | `gui.log` 中 `running code from f7ca805491 but checkout is now 49f72a2411`，46 条 | gateway `code_sha == HEAD`；最后一次 drift 在 10-19 11:43，之后无样本 |
| `fallback_providers` 引用未定义 provider | `10_config_integrity.json`：`opencode-go`、`llamacpp` | `state_20261021.json.config.fallback_undefined = []` |
| 未合并冲突 / 进行中的 cherry-pick | 当时 `UU` + `CHERRY_PICK_HEAD` | 无 unmerged、无 CHERRY_PICK_HEAD |
| `NameError: Path` / `HOME_CREDENTIAL_DIRS` ImportError | `tui_gateway_crash.log` 493 / 多次 | 代码已修（`methods_onboarding.py:2`、`file_safety.py:210`） |
| cron 数据库未归类 | `16_residue_schedule.json` | 已测量 root 1000 / kaguya 524（见 D13） |
| `cache/scratch` 3.80 GB / 200k 文件 | `12_fs_anomalies.json.dir_sizes.scratch` | 871 MB / 106.6k，清理生效 |

**仍然存在（不要当已修）**：`nul` 文件（D5）、`human_delay.mode: true`（D6）、pywin32（D3）、lcm 锁/崩溃（D1）、kanban 状态机（D2/D4）、空壳 DB（D10）、`plugins.enabled ∩ plugins.disabled = {hermes-telemetry}`（见 §4）。

---

## 4. 待观察 / 低优先一致性项

1. **scope-recall「session bound to no memory scope」（capability_gap）**：`gateway-stdio.log` 尾部 19 条，最后样本 10-16；本轮无新样本，判定待观察（若桌面端继续使用仍会丢记忆）。
2. **配置语义矛盾**：`plugins.enabled` 与 `plugins.disabled` 同时包含 `hermes-telemetry`（10-16 为 3 个插件，现只剩 1 个）。行为取决于加载顺序，建议去掉一边。
3. **`.env` 里 `NO_PROXY` 出现两次**（后者覆盖前者，容易误读生效值）。
4. **weixin 凭据齐备但平台 `disconnected`**：`WEIXIN_*` 六个键在 `.env`，配置未启用 → 每次启动的噪声告警；要么启用要么移除。
5. **`kanban` 里历史脏数据**：`t_3bc140b1` assignee=`nobody-xyz`（已 archived）、`probe-nonexistent-lane` / `budget-e2e-worker` 等测试床位；当前 ready 队列无未知 assignee（0）。

---

## 5. 只读性证明

1. **本轮所有对 hermes home 的操作都是读**：`open(..., "r")`、`os.stat`、`sqlite3 … ?mode=ro`、`socket.connect`、`git rev-parse/status/diff`、`grep/tail`。唯一的写是 `$WORKSPACE/evidence/*`（kanban 工作区）与本报告文件。
2. **静态配置哈希对照**：`evidence/ro_before.json` 记录 `config.yaml`、根 `.env`、四份 profile `config.yaml`+`megumi/.env` 的 size/mtime/sha256；巡检结束时 `ro_after.json` 与本文件比对（除本报告外无差异）。
3. **仓库 diff 对照**：`evidence/repo_status_before.txt` 48 行（24 `M` + 24 `??`）；结束时只多一条 `?? docs/hermes_audit_20261021.md`。
4. **运行时 DB 的 mtime 变化不算巡检写入**：`state.db` / `lcm.db` / `memory.sqlite3` 由运行中的 gateway 与本 agent 自身持续写入，报告中对其只做体积与 mtime 采样，不声明「未变」。

---

## 6. 复跑方式（只读）

```bash
cd "D:/Program Files (x86)/AI/hermes/kanban/workspaces/t_ec0ee42f/evidence"
python probe_20261021_state.py        # → state_20261021.json（nul/repo/gateway/pywin32/日志/端口/容量）
python probe_20261021_kanban2.py      # → 看板 run/task 口径（只读 URI）
python probe_20261021_kanban3.py      # → review 泄漏、crashed-run-悬空任务、账本可读性
python probe_20261021_kanban4.py      # → 「Kanban owner refused」样本
python probe_20261021_facts.py        # → 04_log_summary 结构、assignee 分布、.env 键
python snapshot_ro.py > ro_after.json # → 与 ro_before.json 比对
```

修 D1/D2/D3 时建议各配一条回归：

- `store.close()` 后再调 `get_session_messages_after` → 断言抛 `StoreClosed`（不是 `AttributeError`）；
- worker 非零退出 → 断言 run 终态 + 任务状态收敛且不残留 `running`；
- 启动自检 → 断言「运行解释器可 import pywin32」为硬检查。

---

## 附录 A：证据索引

| 结论 | 证据 |
|------|------|
| D1 lcm use-after-close | `logs/tui_gateway_crash.log`（2026-10-21 14:16:31）；`plugins/hermes-lcm/store.py:270,291,1673,1684`；`state_20261021.json.logs["agent.log"].counts_tail4mb` |
| D2 崩溃不复位 | `probe_20261021_kanban3.py` 输出（run 1273/1277/1274/1278）；`probe_20261021_kanban4.py` 错误原文 |
| D3 pywin32 | `state_20261021.json.pywin32`（venv True / tools False）；`logs/agent.log*`、`errors.log*` |
| D4 账本口径 | `probe_20261021_kanban2.py`（review 173、ended_at 全 NULL）、`probe_20261021_kanban3.py`（1580h、max_runtime NULL 1121/1258） |
| D5 nul | `state_20261021.json.home_nul / nul_relpath_repro`；`tui_gateway/methods_complete.py:219`；`logs/gateway-stdio.log`、`gui.log` 采样 |
| D6 human_delay | `config.yaml:949-952`；`logs/gateway.log` 200 条 |
| D7 投递失败 | `logs/gateway.log` 16 条 `delivery_fail` 样本 |
| D8 合并窗口崩溃 | `logs/tui_gateway_crash.log`（`NameError: Path` 493、`HOME_CREDENTIAL_DIRS` ImportError）；现状代码复核 `methods_onboarding.py:2`、`agent/file_safety.py:210` |
| D9 容量 | `state_20261021.json.big / cache_scratch`；对照 `15_memory_chain.json.dbs` |
| D10 空壳 DB | `probe_20261021_kanban2.py` 首行（5238784 vs 0） |
| D11 仓库卫生 | `evidence/repo_status_before.txt`（12 个 U+F03A 变形名） |
| D12 保底端点 | `state_20261021.json.ports["127.0.0.1:18434"]` |
| D13 cron | `16_residue_schedule.json.cron_exec_dbs` |
| 只读证明 | `evidence/ro_before.json`、`evidence/ro_after.json`、`evidence/repo_status_before.txt` |
