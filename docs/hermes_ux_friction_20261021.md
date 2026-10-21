# Hermes 操作链路走查：不顺畅卡点与改进清单

- 日期：2026-10-21
- 走查视角：使用者 / 值班操作者（不是源码审计），按“创建 → 拆解分派 → 执行与查看状态 → 通知 → 失败重试/恢复 → 对账 → 复核”逐段实跑
- 约束：只读排查，不改生产、不做实现。本文所有结论都有命令输出或日志行支撑，证据文件见 §7
- 环境：Hermes home `D:\Program Files (x86)\AI\hermes`，板 `default`，profile 集合 {default, qianhua, kaguya, megumi, mitsuri, tsunade}
- 缺陷承接：P0/P1 缺陷项由 `t_071e68d6`（实施）与 `t_4e357498`（独立复核）承接；本文只出清单与验收口径

---

## 1. 结论先行

主链路能跑通，**但有 3 个 P0 会在“没人看着的时候”静默吃掉一整张卡**：

1. **工作进程里任何 `hermes kanban list` 都直接失败**（rc=1）。不是权限设计，是读命令里夹了一次写事务：`_cmd_list` → `recompute_ready()` → `write_txn()` → delegated-child 守卫。工作进程因此看不到板面，连“我这张卡是不是重复了”都查不了。
2. **崩溃后的恢复指令不可执行**：错误文本让你“resume the printed session ID”，但 `kanban runs` 把那个 ID 截断成 39/70 字符，而且 CLI 根本没有 `resume` 动词。想恢复只能去 SQL 里翻 payload。
3. **崩溃 / 放弃(gave_up) 不发通知**：本卡 `t_66d5273e` 两次 crashed + 一次 gave_up，订阅表里 0 条订阅；`auto_subscribe_on_create: true` 只在“创建源本身是活着的 gateway 会话”时生效，dashboard / auto-decomposer 建的卡一个订阅都没有（近 7 天 16 张）。卡停在 blocked，没人知道。

另有一类**语义风险**值得单列：分解树的根卡（`t_49f4ed0c`，也是本次分解的源卡）在分解后仍是 `implementation` 角色、**body 为空**，成了 6 个子卡的汇合点——所有子卡 done 之后，它会被当作实现卡派发，一个没有 body 的 worker 只能自己重新理解“检测、核验整个 hermes 系统状态”这件大活。

顺畅的地方也要记账：无 gateway 时 `create` 仍成功并给出下一步命令；拼错子命令/非法参数 exit 2 + usage；`unblock / promote / reclaim` 三条恢复入口语义清楚；`stats --json`、`runs --json`、`show --json` 是纯净 JSON，可脚本化。

---

## 2. 卡点总表

| ID | 级别 | 类型 | 一句话 | 改动范围 |
|---|---|---|---|---|
| UX-01 | P0 | 缺陷 | 工作进程内 `list/ls/list --json` 被写守卫整体拒绝 | 后端 + CLI |
| UX-02 | P0 | 缺陷 | 崩溃恢复指令不可执行：session id 被截断、无 resume 动词 | 展示 + CLI |
| UX-03 | P0 | 缺陷 | crashed/gave_up 无订阅即静默，无兜底通知 | 通知 + 配置 |
| UX-04 | P1 | 缺陷(噪音) | heartbeat 事件淹没事件流（322 条里 302 条是心跳） | 后端(存储) |
| UX-05 | P1 | 体验 | stats / boards / list / 原始 SQL 四套计数口径 | 展示 |
| UX-06 | P1 | 缺陷 | 分解树根卡仍是空 body 的 `implementation`，子卡齐了会被派去重做整场审计 | 后端 + 分解器 |
| UX-07 | P1 | 缺陷 | “未知结果”崩溃仍自动重派，与错误文本自相矛盾 | 后端(dispatch) |
| UX-08 | P1 | 缺陷 | 诊断面在真正卡住时返回 “No active diagnostics” | 展示 + 后端 |
| UX-09 | P1 | 体验 | 运行中进度不可见（无 elapsed/attempt/心跳年龄） | 展示 |
| UX-10 | P2 | 体验 | `list --json` 全量带 body，100 张卡 193KB | CLI/展示 |
| UX-11 | P2 | 缺陷(数据) | spawn 事件 `started_at` 写成 `"|179008636285"` | 后端(小) |
| UX-12 | P2 | 体验 | 单次 CLI 调用 3.6–4.1s 冷启动 | 性能/CLI |
| UX-13 | P2 | 体验 | `log` 无 `--run`，2MB×1 轮转吃掉崩溃现场 | 展示/存储 |
| UX-14 | P1 | 缺陷(环境) | SQLite 3.50.4 WAL-reset 漏洞告警，波及 kanban.db | 环境/依赖（交回总控） |
| UX-15 | P2 | 缺陷 | 部分动词的非法参数报出**顶层** `hermes` usage | CLI |

---

## 3. 逐条卡点

### UX-01 [P0·缺陷] 工作进程里只读 `list` 被写守卫拒绝

**操作步骤**（在 dispatcher 派生的 worker 里，即本卡自己的执行环境）
```
hermes kanban list --status ready
hermes kanban list --json
hermes kanban ls
```
**实际表现**：三条全部 rc=1、stdout 0 字节，stderr 只有一句
`kanban: delegate_task child contexts cannot mutate Kanban tasks or boards`
同一环境下 `stats`（rc=0）、`show`（rc=0）正常。也就是说“只读”这件事本身没被禁，被禁的是 `list` 实现里夹带的写。

**期望表现**：只读命令在 worker 上下文里能读到板面；即使不能刷新 ready 状态，也应返回数据 + 一行提示“ready 未刷新（受 fence 限制）”，而不是整条命令失败。

**证据**
- `evidence/probe8_chain.txt` §P0-1：`list/ls/list --json` rc=1，`stats/show` rc=0
- `evidence/probe9_fence.txt` 对照实验（同一 DB、同一解释器）：
  - 无 marker：`list_tasks rows: 100` + `recompute_ready: OK`
  - 带 marker：`list_tasks rows: 100` + `recompute_ready: PermissionError -> delegate_task child contexts cannot mutate Kanban tasks or boards`
- 触发环境变量：`HERMES_DELEGATED_CHILD_CONTEXT=D:\Program Files (x86)\AI\hermes`（每个 dispatcher worker 都有）

**根因定位**
- `hermes_cli/kanban.py:425` — `_cmd_list` 里先跑 `kb.recompute_ready(conn)`（注释写着 “Cheap mini-dispatch so list reflects dependencies cleared since the last tick”）
- `hermes_cli/kanban_db.py:2063-2078` — `recompute_ready()` 第一句就是 `with write_txn(conn):`
- `hermes_cli/kanban_db.py:138-150` — `_assert_not_delegated_child_mutation()` 在 `write_txn` 起点把关；`agent/delegation_context.py:141-162` 决定 fence 命中
- 注意 `hermes_cli/kanban.py:208-215` 的 `_DELEGATED_CHILD_DENIED_ACTIONS` **并不包含** `list`，所以这不是有意的策略，而是写路径漏进读命令的副作用

**改进建议**：读路径不得隐式开启写事务。两选一（后者更保守）：
(a) `_cmd_list` 在 `kanban_path_is_fenced()` 为真时跳过 `recompute_ready`，必要时加一行 `--no-recompute` 开关；
(b) 新增 `recompute_ready_readonly()`：只做 `SELECT` 计算“本可提升”的卡并在输出里提示 `N task(s) pending promotion — run 'hermes kanban promote --ids …'`，不在读命令里改状态。

**验收**：在带 `HERMES_DELEGATED_CHILD_CONTEXT` 的进程里 `hermes kanban list --status ready; echo rc=$?` 输出真实行且 `rc=0`；`list --json` 可被 `json.loads` 解析；带同样环境跑 `pytest -q tests/hermes_cli/test_kanban*.py` 无回归。

---

### UX-02 [P0·缺陷] 崩溃恢复指令不可执行

**操作步骤**：卡片崩溃两次后，看恢复线索
```
hermes kanban runs t_66d5273e
```
**实际表现**
```
  1  crashed  mitsuri  4.2h  2026-10-16 11:10
     ✖ pid 46672 exited with code 1 Worker's last output: 'Session: local-f4ba7f9fe854d1b7371024dafcad9e188
```
错误文本原意是 `Kanban owner refused: Gateway disconnected; turn outcome is unknown. Resume the printed session ID …`，但打印出来的 session id 只有 **39 个字符**（完整 70 字符：`local-f4ba7f9fe854d1b7371024dafcad9e18881ccba99354fcba7a9a16e8763557e4`）。库里 `task_runs.error` 长度分别是 305 / 455 字符 —— 数据是全的，只是展示被截。

同时，`hermes kanban --help` 列出的 47 个动词（含 `ls`/`diag` 别名）里**没有 resume / retry**，唯一沾边的 `reclaim / promote / unblock / repair` 都不做“接着上一轮会话继续说”。

**期望表现**：`runs` 默认打印完整可复制的恢复命令，例如
`Resume: hermes --resume local-f4ba7f9fe854d1b7371024dafcad9e18881ccba99354fcba7a9a16e8763557e4`，并提供 `hermes kanban resume <task_id> [--run N]` 直接做这件事。

**证据**：`evidence/probe8_chain.txt` §P0-2（打印 39 字符 / 完整 70 字符；库里 305 / 455 字符）；同目录 `probe5_json.txt` 记录 `runs --json` 里的完整 error 片段；worker 日志首两行见 `D:\Program Files (x86)\AI\hermes\kanban\logs\t_66d5273e.log` 头部。

**根因定位**：`hermes_cli/kanban_output.py`（`runs` 渲染对 `error` 做单行截断）+ `hermes_cli/kanban.py` 的 `runs` 处理器；恢复动词缺失属 `hermes_cli/kanban_parser.py` 未定义该子命令。

**改进建议**：(1) `runs` 渲染 error 时优先抽出 `Session: local-…` 完整打印，其余部分截断；(2) 加 `--full` 输出原始 error；(3) 新增 `resume` 动词，内部转成 `hermes --resume <session>` 并在本卡事件流记一条 `resumed`。

**验收**：造一张崩溃卡，`runs` 输出的 session id 长度 = 70 且能直接粘贴执行；`hermes kanban resume` 出现在 `--help` 且在已 done 卡上报清晰错误。

---

### UX-03 [P0·缺陷] crashed / gave_up 无订阅即静默

**操作步骤**：本卡 10-16 两次被网关断连杀掉 → `gave_up`（failures=2, effective_limit=2）→ 停在 blocked；不主动看板就永远不知道。

**实际表现**
```
本卡订阅数: 0
platform  chat_id                  n  max_cursor
tui       20260816_202935_97438d   6       15499
tui       20260827_213051_cf7185   2       15507
tui       20260902_124337_004bdd   1       15183
近 7 天新建且无任何订阅的未归档任务数: 16
```
订阅表里 9 条全部指向 8–9 月就已结束的 TUI 会话（cursor 停在 15xxx，当前事件 id 已 17xxx）。`kanban_notify_subs` 的投递目标 = 创建者当时的会话，dashboard / auto-decomposer 创建的卡拿不到这个目标，于是 `auto_subscribe_on_create: true`（`config.yaml:1202`）形同虚设。通知模块本身是好的：`gateway/kanban_watchers_notifier.py:47` 的 `TERMINAL_KINDS` 确实包含 `crashed / gave_up / timed_out / blocked`，只是**没有收件人**。

**期望表现**：终态事件至少有一个兜底收件人（配置里的默认渠道 / 看板 owner 的 TG 会话）；没有收件人时，`gave_up` 必须落到一个显眼位置（看板顶部 banner 或该卡诊断块），而不是只写进事件流。

**证据**：`evidence/probe8_chain.txt` §P0-3；`logs/gateway.log` 只有 `kanban notifier: purged N stale … subscription(s)`，未见任何投递成功日志。

**根因定位**：`gateway/kanban_watchers.py:56-110`（notifier 只按 `kanban_notify_subs` 消费）+ `hermes_cli/kanban_db_notify.py`（订阅创建依赖创建源）；`kanban.auto_subscribe_on_create` 缺 `fallback_target`。

**改进建议**：给 `kanban` 增 `notify_fallback`（platform + chat_id，或复用 gateway 的默认 owner 渠道）；创建时解析不到活会话就写 fallback；`gave_up` 若无任何订阅，补写一条 board 级 `notice` 事件供 dashboard 渲染。

**验收**：在无 TUI 会话的情况下造一张必崩卡（`exit 1`），走完 failure_limit，目标渠道收到一条含卡 id、失败原因、恢复命令的消息。

---

### UX-04 [P1·缺陷·噪音] heartbeat 事件淹没事件流

**操作步骤**：看一张跑了几小时的卡
```
hermes kanban show t_66d5273e     # 6.9KB（10-16 时为 40KB）
hermes kanban tail t_66d5273e
```
**实际表现**：取证时刻本卡 322 条事件里 **302 条是 `heartbeat`**（撰写时已涨到 325 条，心跳还在长——这本身就是要修的问题），`payload` 304/305 为 NULL；全板 16855 条事件里 9887 条是 heartbeat（58%）。`tail/watch` 与任何“看最近发生了什么”的界面都被心跳刷屏；真正有价值的事件（claimed/spawned/crashed/commented）需要翻找。

**期望表现**：心跳只更新 `tasks.last_heartbeat_at`，不落事件；如果必须落，按分钟合并为一条可读进度（`heartbeat x37, last 15:38`），并给 `show/tail` 加 `--no-heartbeat`。

**证据**：`evidence/probe8_chain.txt` §P1-1（heartbeat 302 / 非 heartbeat 20；全板 9887 / 16855）；事件来源 `hermes_cli/kanban_db_dispatch.py:615-646`（`heartbeat_claim` → `_append_event(... "heartbeat" ...)`）。

**改进建议**：`heartbeat_claim` 里把事件写入改成“仅当距上一条 heartbeat > 300s 或 note 非空时落一条”；或落 `heartbeat` 但由 `list_events` 默认折叠。范围：后端（存储/读取），不动状态机语义。

**验收**：连续 10 分钟正常运行后该卡 `SELECT COUNT(*) FROM task_events WHERE kind='heartbeat'` ≤ 2；`last_heartbeat_at` 仍按真实节奏更新（liveness / reclaim 不受影响）。

---

### UX-05 [P1·体验] 四套计数口径

**操作步骤**：同一条板，四种读法
```
sqlite3 -readonly kanban.db "SELECT status,COUNT(*) FROM tasks GROUP BY status;"   # 352（含 archived 259）
hermes kanban stats                                                                # 100
hermes kanban boards list                                                          # archived=259, blocked=2, done=91, running=2, todo=5
hermes kanban list                                                                 # 默认排除 archived → 100
```
**实际表现**：`stats` 无 total 行、无 archived 行；`boards list` 的 COUNTS 里 archived 与其它状态并列，读起来像 359 张卡同时在板；`list` 默认排除 archived 但输出里不说明。对账时三个数都对不上，需要人去猜口径。

**期望表现**：三处使用同一份“活跃 / 归档”二分口径，并在输出里显式标注，例如
`stats: total=100 (active) + archived=259 (excluded; --include-archived)`。

**证据**：`evidence/probe8_chain.txt` §P1-2；早前同一结论见 `evidence/probe4_reconcile.txt`（raw 352 / cli stats 94 / list 94、archived 258）。

**改进建议**：`stats` 增加 `total` 与 `archived` 两行；`boards list` 把 archived 单独括注；三个命令统一支持 `--include-archived`。范围：展示层（`hermes_cli/kanban_output.py`、`kanban_parser.py`）。

**验收**：同一时刻 `stats`、`boards list`、`list --json` 的 active 合计完全相等，且 `stats` 的 total 满足 `active + archived = raw SQL 总数`。

---

### UX-06 [P1·缺陷] 分解树根卡仍是空 body 的 implementation

**操作步骤**：`t_49f4ed0c`（“检测、核验整个hermes系统状态…”，`created_by=dashboard`）被 auto-decomposer 拆出子卡后，看依赖图
```
sqlite3 kanban.db "SELECT parent_id,child_id FROM task_links WHERE parent_id='t_66d5273e' OR child_id='t_66d5273e';"
```
**实际表现**
```
t_66d5273e  t_071e68d6   （实施卡）
t_66d5273e  t_49f4ed0c   （源卡自己，status=todo, task_role=implementation, assignee=default）
```
源卡没有标成“伞卡”，仍是 `implementation` 且 **body 为 NULL**；它同时也是本次分解的根，已经挂着 6 个父（`t_ec0ee42f` done、`t_2d128298` running、本卡、`t_071e68d6`、`t_4e357498`、`t_4d76d5f8`）。等这 6 张全部 done，`t_49f4ed0c` 会被当作**实现卡**派发：一个没有 body、只有一行标题的 worker，被要求“检测、核验整个 hermes 系统状态，修复期间遇到的bug”——它要么重做整条审计，要么瞎猜范围。

**期望表现**：分解树的根/源卡应明确标为汇总或伞卡（`task_role='umbrella'`，dispatcher 跳过），或在 decompose 时自动写入“已知子卡结论 + 收口标准”的 body；只有子卡全部完成且确有人工指令时才执行。

**证据**：`evidence/probe8_chain.txt` §P2-3 + `task_links` 查询（`child_id='t_49f4ed0c'` 有 6 条父边）；`tasks.task_role` 当前取值只有 `implementation`（`t_49f4ed0c / t_66d5273e / t_071e68d6 / t_4e357498 / t_2d128298 / t_4d76d5f8` 全为 implementation）。

**改进建议**：`hermes kanban decompose` 落图时给根卡写 `task_role='umbrella'` 并生成汇合 body（子卡清单 + 收口标准）；`hermes_cli/kanban_db_dispatch.py` 的候选查询排除 umbrella，或要求 umbrella 必须有 body 才可派发。范围：后端（分解器 + dispatcher），需后端配合，不在本卡实施。

**验收**：对一张 todo 卡执行 decompose 后，根卡不出现在 `dispatch --dry-run` 候选里（或 body 非空）；子卡全部完成后根卡保持非执行态直到人工唤醒。

---

### UX-07 [P1·缺陷] “未知结果”崩溃仍自动重派

**操作步骤**：网关异常死亡（10-16 16:21，`gateway.lifecycle_ledger` 记录 `exited UNCLEANLY`），worker 被 `rc=1` / `rc=130` 杀掉。

**实际表现**：事件 payload 自己写着要求人工判断
```
crashed ... "worker_output": "Kanban owner refused: Gateway disconnected; turn outcome is unknown. Resume the printed session ID to check whether it completed or was interrupted; do not resend the work."
crashed ... "retry_status": "ready"
gave_up  ... {"failures": 2, "effective_limit": 2, "limit_source": "dispatcher"}
```
“do not resend the work”和`retry_status: ready`直接矛盾：系统一边说别重发，一边 2 分钟后就把同一个 prompt 再发一遍（本卡确实被重派了两次）。对**已产生副作用**的卡，重派等于双写。

**期望表现**：崩溃分两类——“未开工崩溃”（spawn 失败、进程没接管）可自动重派；“开工后崩溃/未知结果”只能进 `scheduled`/`blocked` 等人工 `resume` 或确认重派。

**证据**：`evidence/probe8_chain.txt` 与本卡事件流（`crashed` payload 见 `kanban show t_66d5273e`）；网关侧证据 `logs/gateway.log:10713`（`Previous gateway life (pid=29580 …) exited UNCLEANLY`）。

**改进建议**：`_record_task_failure` 读取 worker 输出里的 `turn outcome is unknown` 标记，置 `retry_status='todo'`（待人工 promote），并在卡上写一条 `needs_input` 诊断。范围：后端 dispatch 判定（交回总控）。

**验收**：模拟“开工后断连”崩溃，卡进入待人工态而非自动重派；`runs` 能看到 `outcome=crashed(unknown)` 与建议操作。

---

### UX-08 [P1·缺陷] 诊断面在真正卡住时是空的

**操作步骤**
```
hermes kanban diagnostics
```
**实际表现**：`kanban.dispatch_profiles: any` + `No active diagnostics on this board.`，而同一块板上有两张 `blocked` 卡：`t_3201dfa9`（`block_kind=needs_input`，自 **2026-10-01 22:00 起卡了 19.7 天**）和 `t_e92cb069`（十分钟前刚 block）。`t_3201dfa9` 的 block 事件还显示 `requested_kind=dependency` 被改写成 `needs_input`，理由 `rekind_reason=no_open_parent` —— 使用者从输出里读不出“为什么卡住、要我做什么”。

**期望表现**：`needs_input` 且超过 N 小时无新评论的卡必须出现在 `diagnostics` 里，带上 `reason` 摘要与建议动作（`unblock` / `request-changes` / 关闭）。

**证据**：`evidence/probe8_chain.txt` §P3-1；`kanban show t_3201dfa9` 的 Events 段（含 `rekind_reason`）。

**改进建议**：`kanban_db_diagnostics` 增加 `needs_input_stale` 规则；`show` 顶部对 blocked 卡直接打印 `block_kind / reason / 积压时长`。范围：展示 + 后端判定。

**验收**：板上存在 blocked>24h 的卡时，`diagnostics` 必含对应条目且给出可执行动作。

---

### UX-09 [P1·体验] 运行中进度不可见

**操作步骤**：人想知道“跑了 4 小时的卡还活着吗、第几次尝试、卡在哪一步”
```
hermes kanban list
```
**实际表现**：单行只有 `icon id status assignee title`（`hermes_cli/kanban_output.py:72-76`）。`elapsed / attempt n/N / last heartbeat age / 当前工具` 全部要另开 `runs`、`show`、`show --json` 才能拼出来；而 `show` 又被 302 条 heartbeat 灌满（UX-04）。

**期望表现**：`list --wide` 输出 `running 2/2 attempts, elapsed 4h12m, hb 38s ago`；`show` 头部给一段“进度摘要”（当前 run、最近一条非 heartbeat 事件、下一次 reclaim 时间）。

**证据**：`evidence/probe8_chain.txt` §P1-1 与 §P3-2；`kanban_output.py:72-76`（格式串）；`kanban_db.py` 已有 `last_heartbeat_at / consecutive_failures / current_run_id` 字段可直接复用。

**改进建议**：新增 `--wide` 渲染分支（纯展示，不动查询语义）；`show` 头部复用 `runs` 的最后一行。

**验收**：`list --wide` 对 running 卡输出心跳年龄；误报为 0 与 `PRAGMA` 查询一致。

---

### UX-10 [P2·体验] `list --json` 全量带 body

100 张未归档卡共 75,200 字节 body（22 张单卡 body > 1000 字节）；`list --json` 在 94 行快照下实测输出 **193,563 字节**（`evidence/probe7_axes.txt` 的 `fenced=False` 一栏），因为 `_TASK_DICT_FIELDS`（`hermes_cli/kanban_output.py:16-22`）把 `body`/`completion_contract` 一起塞进每一行。Agent 想“看一眼板面”就得吞整段正文。

**建议**：`_task_to_dict` 默认剔除 `body`/`completion_contract`，新增 `--with-body`；或加 `--fields id,status,assignee,title,last_heartbeat_at`。范围：CLI/展示。
**验收**：`list --json` 在 100 卡板上 < 30KB 且仍可 `json.loads`；`--with-body` 能取回全文。

---

### UX-11 [P2·缺陷·数据] spawn 事件时间戳被写成 `"|179008636285"`

```
{"pid": 2516, "started_at": "|179008636285"}
```
管道符前缀混进 epoch 值，任何按该字段做时间差/排序的消费方都会静默得到垃圾（字符串化后不会报错）。

**建议**：修 dispatcher 里 `spawned` 事件的字段构造（`hermes_cli/kanban_db_dispatch.py` 附近），并对历史行做一次清洗/回填。范围：后端（小）。
**验收**：新 spawn 事件的 `started_at` 为纯整数；`json_extract(payload,'$.started_at')` 可直接算术比较。

---

### UX-12 [P2·体验] CLI 冷启动 3.6–4.1s

```
hermes kanban stats   3603 ms
hermes kanban show    4101 ms
```
一次走查要跑十几条命令，光启动就一分钟；agent 场景下这是实打实的 token/时间税。

**建议**：短期在文档里给出“一次调用拿全”的姿势（`hermes kanban show <id> --json` 已含 parents/children/runs/events）；中期考虑常驻 daemon 复用（`hermes kanban daemon` 已废弃但说明曾有这个方向）。范围：性能/CLI/文档。
**验收**：文档中出现推荐姿势；或 `hermes kanban` 支持 `--batch -f cmds.txt` 单进程多命令。

---

### UX-13 [P2·体验] `log` 没有 run 维度

worker 日志按任务落 `<root>/kanban/logs/<task>.log`，轮转 `worker_log_rotate_bytes=2097152` / `backup_count=1`；`hermes kanban log` 只有 `--tail`。多次崩溃的现场会互相覆盖，只剩最后两轮。

**建议**：`log --run N`（或用 `<task>.run<N>.log` 分文件）；`runs` 每行给出对应日志路径。范围：展示/存储。
**验收**：对两次崩溃的卡，`log --run 1` 与 `--run 2` 分别拿到各自内容。

---

### UX-14 [P1·缺陷·环境] SQLite 3.50.4 WAL-reset 漏洞告警

用仓库 venv 解释器打开 `kanban.db` 时 stderr 报：
```
kanban.db … linked SQLite 3.50.4 … is vulnerable to the WAL-reset corruption bug (https://sqlite.org/wal.html#walresetbug)
Upgrade to SQLite 3.51.3+ (or backports 3.50.7 / 3.44.6); … `hermes doctor`
```
看板中央库正在 WAL 模式下跑并发读写，这不是“提醒一下”的级别。属于环境/依赖面，**不在本卡实施范围**，需交回总控（`hermes update` / `hermes doctor` 复核运行时版本）。

**证据**：`evidence/probe9_fence.txt` §“对照 A”第 1 行（原始告警全文）；触发条件为仓库 `.venv` 解释器（Python 3.11.15）链接的 SQLite 3.50.4。

**验收**：`hermes doctor` 不再报该漏洞；`PRAGMA` 查询确认运行时 sqlite ≥ 3.50.7 或 3.44.6+。

---

### UX-15 [P2·缺陷] 部分动词的非法参数报出顶层 `hermes` usage

**操作步骤**：给带 variadic 位置参数的动词多打一个不存在的选项
```
hermes kanban complete t_deadbeef --bogus
hermes kanban runs t_deadbeef --bogus
hermes kanban request-changes t_66d5273e --summary x   # --summary 不是本动词的选项
```
**实际表现**：rc=2 正确，但打印的是**顶层 `hermes` 的 60 行 usage**（`usage: hermes [-h] [--version] [-z PROMPT] …`），而不是
`hermes kanban complete: error: unrecognized arguments: --bogus`。同一次实验里 `list --status bogus`、`show` 缺参却正确打印了子命令 usage —— 使用者会以为是自己敲错了顶层命令。

**期望表现**：一律由叶子子命令报错，一行 `unrecognized arguments: …` + 该动词 usage。

**证据**：`evidence/probe10_review.txt` §“非法参数时 usage 归属”。

**根因定位**：`hermes_cli/main.py:3146` 顶层 `parser.parse_known_args(...)` 之后，未被叶子 parser 认领的选项回流到主 parser 报错；`hermes_cli/kanban_parser.py:295-306` 这类 `task_ids nargs="+"` + 选项混排的动词最容易触发。

**改进建议**：叶子 parser 用 `parse_args` 严格模式（或 `parse_known_args` + 残留非空时自己 `error()`），确保 `unrecognized arguments` 归属被调用的动词。范围：CLI。

**验收**：上表三条命令都打印 `hermes kanban <verb>` 的 usage 并以 rc=2 退出。

---

## 4. 复核段（review）观察

复核链路本身是齐的、且被大量使用，但有两处摩擦：

1. **往返成本高**：全板 `review_requested` 180 次、`changes_requested` 56 次（≈1.3 次返工/次复核），当前 `review` 列 0 张卡（无积压）。流程跑得动，缺的是“复核轮次/耗时”的可见性——`runs` 只有 `review_consecutive_failures` 字段，没有任何“这张卡被退回了几次、卡在谁手里多久”的展示。
   建议：`show`/`list --wide` 输出 `review: 2 次退回, 等待 3h`；`diagnostics` 增加 `review_stale`。范围：展示。
2. **worker 里复核只能走 MCP 工具**：`hermes kanban request-review` 在 worker 上下文里被 fast-fail 拒绝（`kanban: delegate_task child contexts cannot mutate Kanban tasks via the CLI`），但 `--help` 完全没提这件事；照帮助操作必然踩空。（`request-changes` / `reopen-review` 本次实验被非法参数拦住，未取得干净的可用性证据，不写结论。）
   建议：在 worker 上下文里把提示改成“请使用 kanban_request_review 工具”，并在 `--help` 里注明 worker 限制。范围：CLI/文档。

---

## 5. 顺畅的部分（保持，不要在改动里碰坏）

| 场景 | 表现 | 证据 |
|---|---|---|
| 无 gateway 时创建任务 | 建卡成功 + 明确下一步 `hermes gateway start` + 说明 dispatch 周期 | `evidence/lifecycle.txt` §0.1 |
| 拼错子命令 / 非法参数 | rc=2 + usage（`kanban lst`、`--status` 非法值、`show` 不存在的 id → rc=1 + `no such task: t_deadbeef`） | `evidence/consistency-run.txt` A1–A12 |
| 恢复入口语义 | `unblock --reason` / `promote [--dry-run] --ids` / `reclaim [--reason]` / `repair` 各司其职 | `probe8_chain.txt` §P2-1 |
| 机器可读输出 | `stats --json` / `show --json` / `runs --json` / `boards list --json` / `diagnostics --json` 均为纯净 JSON | `evidence/probe5_json.txt` |
| 依赖图完整性 | `task_links` 强连通分量 0（无环，取证时 363 行，现 374 行） | `evidence/probe3_db.txt` |
| 沙箱与生产隔离 | 沙箱 init/create/link/block 全程不污染生产板 | `evidence/consistency-run.txt` E0–E11 |

---

## 6. 需要后端配合 / 越权事项（交回总控）

按本卡约束（只读、不做实现），以下需要后端改动，实施卡 `t_071e68d6` 只能做展示 / 通知 / 对账面：

1. **UX-01** `_cmd_list` 的隐式写事务（`kanban.py:425` + `kanban_db.py:2063`）——要么读路径去写，要么给 fenced 上下文一条只读分支。
2. **UX-02** 新增 `resume` 动词（涉及 worker 会话接管语义，不只是打印）。
3. **UX-06** `task_role='umbrella'` 与 dispatcher 候选过滤。
4. **UX-07** 崩溃分类（未开工 / 未知结果）与 `retry_status` 降级。
5. **UX-11** `spawned` 事件字段构造修复 + 历史数据清洗。
6. **UX-14** 运行时 SQLite 版本升级与复核。

**缺陷转交**：UX-01/02/03/04/07/08/11 的展示 / 通知 / 对账面 → 实施卡 `t_071e68d6`（P0 优先），独立复核 `t_4e357498`；UX-01/02/06/07/11/14 的后端（dispatch 判定、动词新增、role 过滤、依赖升级）需总控确认后另行派单，本卡不做实现。

---

## 7. 证据文件与复现

证据根目录：`D:\Program Files (x86)\AI\hermes\kanban\workspaces\t_66d5273e\evidence\`

| 文件 | 内容 |
|---|---|
| `probe8_chain.sh` / `.txt` | 本轮主链路只读取证（11 段：fence、runs 截断、订阅、heartbeat、计数、恢复动词、spawn 时间戳、分解图、body 体积、诊断、冷启动耗时） |
| `probe9_fence.sh` / `.txt` | UX-01 的 A/B 机制实验（同解释器，仅差 marker） |
| `probe10_review.sh` / `.txt` | 复核链路使用量、worker 内复核动词可用性、非法参数 usage 归属 |
| `cli-help.txt` / `cli-matrix*.{txt,json}` | 动词/参数矩阵（创建、board 前置、子命令拼写、退出码） |
| `lifecycle.{txt,json}`、`lifecycle-run.txt` | 创建 → 认领 → 完成 生命周期观察 |
| `consistency-run.txt` | 退出码/拼写/参数形状一致性（A1–A12、B1–B8、C1–C7、D1–D4、E0–E11） |
| `fenced-reads-run.txt`、`fenced-reads.json` | 四种上下文（in-process / descendant / dispatcher / 无 marker）下只读命令可用性 |
| `probe3_db.txt`、`probe4_reconcile.{txt,json}` | 原始 SQL 与 CLI 的对账（计数口径） |
| `probe5_json.{txt,json}` | 各命令的 JSON 纯净度（含当时出现的 `source update is unfinished` stderr，本轮已消失） |
| `probe6_repeat.txt` | 重复调用稳定性（list/stats/show/create --help 各 3–6 次） |
| `probe7_axes.{txt,json}` | fenced × update-lock 两轴对照，隔离出“fence 才是 list 失败主因” |

一键复现本轮：
```bash
cd "D:/Program Files (x86)/AI/hermes/kanban/workspaces/t_66d5273e/evidence"
bash probe8_chain.sh | tee probe8_chain.txt
bash probe9_fence.sh | tee probe9_fence.txt
bash probe10_review.sh | tee probe10_review.txt
```

单点复现 UX-01（最省事的自证）：
```bash
cd "D:/Program Files (x86)/AI/hermes/hermes-agent"
HERMES_DELEGATED_CHILD_CONTEXT="D:\\Program Files (x86)\\AI\\hermes" \
  ./.venv/Scripts/python.exe -c 'import sys;sys.path.insert(0,".")
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
print(len(kb.list_tasks(kbc.connect()))); kb.recompute_ready(kbc.connect())'
# 期望：先打印 100，随后 PermissionError —— 读失败是因为写了
```
