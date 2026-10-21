# Hermes 缺陷修复与操作顺滑化 — 总验收与收口

- 收口卡：`t_4d76d5f8`（总验收：判定发布条件、沉淀遗留事项）
- 终审人：千花（正宫终审权，思远 2026-09-02 授权：worker 执行 → 辉夜复核 → 千花终审）
- 时间：2026-10-21 17:16–17:35 +08:00（本报告所有实测时间戳均在此窗口）
- 输入：
  - 独立复核报告 `docs/hermes_review_20261021.md`（辉夜，判定 rework required）
  - 只读巡检 `docs/hermes_audit_20261021.md`（D1–D13）
  - 走查卡点 `docs/hermes_ux_friction_20261021.md`（UX-01–UX-15）
  - 复核证据 `kanban/workspaces/t_4e357498/evidence/`（E1–E23）

> 本报告不复述复核结论：凡下表中标"我的复核"一列，都是我自己重新跑命令/读原始日志/读分支源码得到的，不是引用辉夜的数字。

---

## 0. 判定

**验收结论：部分接受（partial accept）**

| 范围 | 结论 |
|---|---|
| D1 / D2 / D5 / D6（4 条 P0/P1 缺陷） | **接受** |
| UX-01 / UX-02 / UX-03 / UX-05（4 条顺滑化） | **接受** |
| D3（pywin32 / 日志轮转） | **不接受当前提交**：机制有效，但同一提交带进 R0 阻塞回归，另有 R1/R2/R4 |
| UX-09（长跑卡进度） | **不接受当前提交**：`claim=` 剩余时间恒 `0s`（R3） |

**发布决定：冻结（不发布）**

依据三条，任一条都足以冻结：
1. **R0 是 P0 阻塞且仍未修**。fix 分支 HEAD `7975d417d1` 的 `cron/scheduler_worker_env.py` 第 72 行用 `sys.path`，文件头只有 `import logging / os / sysconfig`，`^import sys$` 不存在 → 模块式 worker 每次 spawn 在 `subprocess.Popen` 之前就 `NameError`。**合并即等于让默认 worker 起不来**，这不是"带点小毛病发布"。
2. **两个返工卡仍在运行、未落地**。`t_2b4e30cc`（tsunade，R0/R1/R2/R4）与 `t_a9be51ed`（mitsuri，R3）17:16 时仍为 `running`，fix 分支 HEAD 与 UX clone HEAD 均未移动（`7975d417d1` / `a04164c730`）。没有返工产物，就没有可验收的对象。
3. **发布本身需要正式审批，本卡无此授权**。`t_4d76d5f8` 的 completion_contract 为 `local-only`；合并 + 部署属生产变更，须思远书面批准。已建 `t_34c76731`「[发布门·待思远批准]」承载该闸门。

本卡**未**做：未合并、未部署、未重启任何服务、未改 `config.yaml`、未写生产库。只新增本文档。

---

## 1. 逐条验收（证据引用 + 我的独立复核）

### 1.1 缺陷修复（t_2d128298）

| 项 | 验收 | 复核报告证据 | 我的复核（自己跑的） |
|---|---|---|---|
| D1 lcm use-after-close | **接受** | E5 基线 `AttributeError` rc=1 → E6 修复 `StoreClosed` rc=0；E11 新增 10 passed，E12 基线收集即错 | lcm worktree `kanban/workspaces/t_2d128298/hermes-lcm` 分支 `kanban/t_2d128298-lcm` HEAD `0bf028a`；`store.py:262` 确有 `class StoreClosed(RuntimeError)`；E11 日志尾部 `10 passed in 0.62s` |
| D2 卡/run 不收敛 + 网关拒答被记账 | **接受** | E7 基线 4 个 sweeper 都漏 → E8 `closed_run_pass_requeued=[t_f253eb72]`、`booking=gateway_refused`、`counts_as_failure=false`；新增 9 passed | 修复分支 HEAD 含 `ff2e0b08e6 fix(kanban): converge task/run pairs and stop charging control-plane refusals (D2)`；E1 中该套件无失败项 |
| D3 pywin32 / 日志轮转降级 | **不接受（返工中）** | 生产形状探针 E3 `ModuleNotFoundError` → E4 `from-install-tree`（未设 `HERMES_SITE` 自动发现成功）；E4b 设 `HERMES_SITE` 后 `pywintypes`+`concurrent_log_handler` 均可导入 | **R0 实测复现**：fix 树 `^import sys$` NOT FOUND，`:72 for entry in sys.path:`；E23 原始日志 `4 failed in 9.92s`（fix）vs `4 passed in 8.85s`（base）；E1 尾部 `1 failed, 40 passed`，唯一失败为 R1 |
| D5 Win32 设备名 | **接受** | E2 基线 20 failed → E1 20 passed | E1/E2 尾部一致（E2 `37 failed, 4 errors`）；分支含 `149158a28f ... (D5)` |
| D6 human_delay 告警风暴 | **接受** | E9 200 次读 → 200 条 → E10 200 次读 → 1 条 | 修复树 `gateway/run_config_loaders.py:42-55` 有 `_warn_invalid_human_delay_mode_once()` 进程级去重；分支含 `7975d417d1 ... (D6)` |

### 1.2 操作顺滑化（t_071e68d6）

| 项 | 验收 | 复核报告证据 | 我的复核 |
|---|---|---|---|
| UX-01 fenced 上下文 `list` 必败 | **接受** | E14 基线 rc=1 + fenced 拒绝文案 → E13 rc=0 + `read-only context — ready-closure promotion skipped` | 读 E13 原始 JSON：`fenced_list_rc=0`、`fenced_list_exit0=true`、`fenced_list_note=true`、`fenced_list_has_icon=true` |
| UX-02 崩溃后无 session id / 无恢复命令 | **接受** | E14 无 Session 段、`runs --session` rc=2 → E13 `Session:`/`Resume:` 段、`resume --json` 七字段齐、只读 | E13 JSON：`runs_session_rc=0`、`resume_rc=0`、`resume_read_only=true`、`resume_json_keys` 共 7 项、`resume_json_session_matches=true` |
| UX-03 无订阅者终态静默 | **接受**（端到端投递按卡片声明未做，不冒充） | E13 create 警告 + show notify none | E13 JSON：`create_warns_no_subscriber=true`、`show_notify_none=true` |
| UX-05 统计与归档不闭合 | **接受** | E17 `total=2 active_total=1 archived_total=1`，`sum(by_status)==active_total`，archive footer | E13 JSON：`stats_json_keys=[active_total, archived_total, total]`、`stats_json_closes=true`、`stats_total_line=true` |
| UX-09 长跑卡进度 | **不接受（返工中）** | E13 输出 `run=2 hb=5m claim=0s left`（租约实剩 12 分钟）→ R3 | **R3 源头确认**：UX clone HEAD `a04164c730`，`hermes_cli/kanban_output.py:94` 仍是 `f"claim={_fmt_age(t.claim_expires)} left"`，`_fmt_remaining` NOT FOUND；E13 原始 JSON 的 `list_wide_text` 两张卡都是 `claim=0s left` |

### 1.3 回归与相容性（我复核过的原始日志）

| 套件 | 报告数字 | 我从日志尾部实读 |
|---|---|---|
| 4 个新增缺陷测试（fix / base） | fix 1F40P / base 37F4E | E1 `1 failed, 40 passed in 14.83s`（RC=1）；E2 `37 failed, 4 errors in 13.94s` |
| UX + 相邻 3 套件（fix） | 60 passed | E15 `60 passed in 102.63s` |
| fix 相邻 4 套件 | 40 passed | E16 `40 passed in 56.12s` |
| UX 测试文件跑基线树 | 17F/1P（红侧成立） | E18 `17 failed, 1 passed in 32.54s` |
| lcm 回归（fix） | 55 passed | E19 `55 passed in 22.09s` |
| combined 树（fix + UX） | 58P/1F | E20 `1 failed, 58 passed in 48.86s`（唯一失败为 R1，同一根因） |
| 交付方点名的 7 文件套件 | 两树均 6F/122P/2S 但失败集合不同 | E21 `6 failed, 122 passed, 2 skipped`；E22 同计数 |
| R0 定点复核 | fix 4 failed / base 4 passed | E23 原文：`4 failed in 9.92s` / `4 passed in 8.85s` |

R0 定性我同意复核报告：这不是"HEAD 上也一样挂"，而是**修复树独有的 4 项回归**——基线树对应 4 例全绿，失败集合根本不重叠。

---

## 2. 生产零影响核验（我亲自跑的）

| 检查 | 结果 |
|---|---|
| live repo HEAD | `49f72a24119c0c9c8e4a72951c4d876a6f7e5008`（与两份交付的基线一致，未移动） |
| live 工作树 | 51 条脏项（26 M + 25 ??），全部为既有的其他在途工作（D8 本地补丁）；本轮新增只有 `docs/` 下三份文档 |
| `config.yaml` | sha256 `4de5139166929408c57cadcbcc75538497357667418e116e3f7c57c990724fec`，mtime `2026-10-21 16:33:45.359`（早于复核卡开工）；无 `kanban.notify_fallback` 键 |
| 分支位置 | fix 在 `kanban/t_2d128298-defect-fix`（worktree，HEAD `7975d417d1`，工作树干净）；lcm 在 `kanban/t_2d128298-lcm`（HEAD `0bf028a`）；UX 在独立 clone `C:/Users/JoJo/kanban-clones/t_071e68d6`（HEAD `a04164c730`）。**均未进 main** |
| 合并 / 部署 / 重启 | 全部未发生；live 服务未重启 |

---

## 3. 发布闸门（G1–G8，全绿才允许合并）

1. **G1 [P0]** R0 修复：补 `import sys`，4 个 spawn 用例 fix 树 4 passed（基线 4 passed）。
2. **G2 [P1]** R1 hermetic：`test_runtime_dependency_selfcheck.py` 在仓库 `.venv`（真 pywin32）与 `uv --python 3.14`（无 pywin32）**两种解释器都 41 passed**。
3. **G3 [P2]** R2：`repro_d3_pywin32.py` 内 `os.environ.setdefault("HERMES_SITE", site_dir)`，克隆布局 exit 0，报告补写"克隆须显式给 `HERMES_SITE`"。
4. **G4 [P2]** R3：新增 `_fmt_remaining()`（`<=0 → "expired"`），补 `test_list_wide_reports_claim_remaining`，EVIDENCE.md 样例重出。
5. **G5 [P2]** R4：docstring 与报告改为"`hermes doctor` 硬检查 + 运行时自动激活"，不改成裸抛（裸抛会 brick 启动）。
6. **G6** 辉夜对返工后分支复验签字（E23 类定点复核重跑）。
7. **G7** 发布闸门 = `kanban` 子集测试 + **与基线树失败集合逐条相同的差分**；不以 Windows 全量零失败为门（沿用 W3-A 经验）。
8. **G8** 合并顺序 fix → UX，改完重跑 combined 套件；合并前建回滚点，闸门失败即回滚。

**当前状态：G1–G6 未达成，G7/G8 无从执行 → 冻结。** 批准与执行载体：`t_34c76731`（blocked，待思远批准，parents = 两张返工卡）。

---

## 4. 遗留事项台账（负责人 + 期限）

### A. 本日必须闭环（返工）

| ID | 事项 | 负责人 | 期限 | 载体 |
|---|---|---|---|---|
| R0 | `cron/scheduler_worker_env.py` 补 `import sys`（P0 阻塞） | tsunade | 2026-10-21 当日 | `t_2b4e30cc`（running） |
| R1 | D3 新测试 hermetic，两种解释器都 41 passed | tsunade | 2026-10-21 当日 | `t_2b4e30cc` |
| R2 | `repro_d3_pywin32.py` 自设 `HERMES_SITE` + 报告补注 | tsunade | 2026-10-21 当日 | `t_2b4e30cc` |
| R4 | D3"启动硬检查"表述更正 | tsunade | 2026-10-21 当日 | `t_2b4e30cc` |
| R3 | `claim=` 剩余时间 + `test_list_wide_reports_claim_remaining` + EVIDENCE 重出 | mitsuri | 2026-10-21 当日 | `t_a9be51ed`（running） |

### B. 复验与发布

| ID | 事项 | 负责人 | 期限 | 载体 |
|---|---|---|---|---|
| G6 | 返工后独立复验（不复用交付方结论） | 辉夜 kaguya | 2026-10-22 | 新建（复验通过后派） |
| G1–G8 | 合并两分支 + 跑发布闸门 + 部署 | 千花（执行）／思远（批准） | 批准后 24h 内 | `t_34c76731`（blocked） |

### C. P3 硬化（不阻塞发布）

| ID | 事项 | 负责人 | 期限 | 载体 |
|---|---|---|---|---|
| R5 | D2 基础设施判定收窄 + 重排上限 + 说明无独立 kill switch | tsunade | 2026-10-28 | `t_b9266816` |
| R6 | `resume --exec` 用 `scrub_kanban_env` 清洗环境 | tsunade | 2026-10-28 | `t_b9266816` |
| R7 | D3 fallback 去重改 `{interpreter: [reasons]}` | tsunade | 2026-10-28 | `t_b9266816` |

### D. 需思远拍板的决策项（未批准前不动生产库）

| ID | 事项 | 负责人 | 期限 | 说明 |
|---|---|---|---|---|
| D4 | kanban 账本失真：173 条 review run `ended_at` 全 NULL、456 条 `budget_stopped` 无 summary、1121/1258 张卡 `max_runtime_seconds` NULL | 千花（脚本）／思远（口径） | 请 2026-10-24 前给方向 | 需一次性脚本打生产库，属写入，必须单独授权；口径未定前不执行 |
| D7 | 投递失败静默丢弃（16 条 `delivery_fail`）：outbox / 重试 / 语义签核 | 千花（设计）／思远（语义） | 请 2026-10-24 前给方向 | 涉及"失败是否算卡失败"的产品语义，不能由执行方自定 |

### E. 运维与容量

| ID | 事项 | 负责人 | 期限 | 说明 |
|---|---|---|---|---|
| D9 | 容量与维护窗口：state.db 6.49GB（+365MB/5d）、lcm.db 2.66GB、memory.sqlite3 594MB | 千花 | 2026-11-04 | 走维护窗口脚本 + marker，不裸停 serve |
| D10 | 空壳 DB 误导巡检路径（`kanban/kanban.db`、`boards/default/kanban.db` 均 0 字节，真库 5.2MB） | 千花 | 2026-10-24 | 只做标注/软链修正，不删库 |
| D12 | 兜底 provider 端点 `127.0.0.1:18434` connect timeout | 千花 | 2026-10-24 | 换端点或降级声明，改链前先报 |
| D13 | kaguya cron 噪音（524/206 failed、205 incidents） | 辉夜 | 2026-10-28 | 清噪音，不动 cron 业务逻辑 |

### F. UX 未实施项（mitsuri 交付时已声明交回总控）

| ID | 事项 | 负责人 | 期限 |
|---|---|---|---|
| UX-06 | 分解根卡 body 为空仍被当 implementation 派发（**正在阻塞 `t_49f4ed0c`**） | mitsuri | 2026-10-22 |
| UX-04 / UX-07 / UX-08 / UX-10 / UX-11 / UX-13 / UX-14 | 展示/通知/对账剩余项，其中 UX-11、UX-14 需编排侧决策 | mitsuri（UX-11/14 先由千花给口径） | 2026-10-28 起分批 |

---

## 5. 最终收口说明

**本轮实际收获**：四条缺陷（D1/D2/D5/D6）的根因确实被掐掉，四条顺滑化（UX-01/02/03/05）在真实 CLI 上跑通，这部分可以收；两条交付（fix / UX）文件集合零重叠、combined 树可顺放。这些都有"改前红 → 改后绿"的对照，不是自述。

**本轮不能收的原因**：D3 这一笔用"日志降级修复"换来了"默认 worker 起不来"的新 P0，而且是被"HEAD 上也一样失败"这种错误归因盖过去的——两棵树的失败集合根本不重叠。同类风险还体现在 UX-09：`claim=0s left` 这条假数据出现在交付方自己的 EVIDENCE 里，说明样例没有经过真实渲染。

**对流程的两条硬结论**（已在本报告落盘，后续沿用）：
1. `ruff` 默认 select 不含 `F821`，"ruff 全绿"不能证明没有未定义名——R0 就是这么漏过去的。改动文件必须加跑 `ruff check --select F821`。
2. 回归判定只认"基线树与修复树的失败集合逐条比对"，不认两边失败**计数**相同。计数相同、集合不同，正是这次差点放行的原因。

**本卡动作清单**：读三份报告 + 全部 E1–E23 原始日志；亲自核 live HEAD / `config.yaml` 哈希与 mtime / 三分支 HEAD / `import sys` 缺失 / `_fmt_age` 源码 / E13 原始 JSON；新建 `t_34c76731`（发布门，blocked）与 `t_b9266816`（P3 硬化）；本报告落 `docs/hermes_closeout_20261021.md`。

**一句话结论**：D1/D2/D5/D6 + UX-01/02/03/05 验收通过；D3 与 UX-09 退回返工（R0 阻塞必须先行）；**发布冻结**，闸门交 `t_34c76731`，等思远书面批准。
