# Hermes 缺陷修复与操作顺滑化 — 独立复核报告

- 复核卡：`t_4e357498`（独立复核全部修复与改进，重跑验证并出具接受/返工结论）
- 复核人：辉夜（kaguya），复核权由思远 2026-09-02 授权（worker 执行 → 辉夜复核 → 千花终审）
- 日期：2026-10-21
- 被复核：
  - `t_2d128298`（tsunade）定位巡检缺陷根因并在隔离工作区修复 + 回归
  - `t_071e68d6`（mitsuri）实施状态展示 / 通知 / 对账顺畅化改进并自测
- 基线：live checkout `D:/Program Files (x86)/AI/hermes/hermes-agent`，HEAD `49f72a24119c0c9c8e4a72951c4d876a6f7e5008`（工作树自带 24 个未提交修改 + 25 个未跟踪项，属既有其他工作，不在本次两份交付内）

---

## 0. 判定

**返工（rework required）** —— 五个 P0 缺陷的"现象"确实被掐掉了，五个顺滑化条目也在真实 CLI 上跑通；但修复分支带进来一个**新的 P0 回归**：`cron/scheduler_worker_env.py` 使用了未导入的 `sys`，`_default_spawn` 在模块式起 worker 时直接 `NameError`，等于默认 worker 起不来。这一条必须修完再谈合并。

| 维度 | 结论 |
|---|---|
| P0 缺陷根因是否消除（D1/D2/D5/D6） | 4/4 消除，前后对照可复现（§2.1） |
| D3（pywin32 / 日志轮转） | 机制本身有效，但**引入了 R0 阻塞回归**，且交付脚本不可复现（R2）、"启动硬检查"表述不实（R4） |
| 顺滑化条目（UX-01/02/03/05/09） | 5/5 实测通过；UX-09 有一处显示缺陷（R3） |
| 回归 | 新增测试全绿（40 + 18）；相邻套件 60 + 40 + 55 全绿；交付方自述的 7 文件套件**并非"同样 6 项失败"**——修复树多出 4 项真实回归（R0），基线树对应 4 项通过（§3） |
| 生产零影响 | 未合并、未部署、未重启；live HEAD/工作树/`config.yaml` 未变（§5） |
| 合并相容 | 两分支无同文件改动；combined 树补丁顺放，58 passed + 1 已知环境失败（§6） |
| 遗留 | R0（P0 阻塞）+ R1–R7（§4） |

---

## 1. 复核方法（不复用交付方结论）

1. **独立克隆**（`git clone -s`，不写 live 对象库）：`base-agent` / `fix-agent` / `patch-agent` / `ux-agent` / `combined` / `lcm-base` / `lcm-patch`，全部落在我自己的 workspace `kanban/workspaces/t_4e357498`。
2. **补丁重放一致性**（证明"附件补丁 = 分支内容"，不是另存一份代码）：

| 交付 | 重放结果 | 树哈希比对 |
|---|---|---|
| agent 4 个补丁 → `patch-agent` | `git am --3way` 全部 rc=0 | `git diff --stat HEAD <fix-agent HEAD>` 为空 |
| lcm 1 个补丁 → `lcm-patch` | rc=0 | patch-tree `90214591b1…` == branch-tree `90214591b1…` |
| UX 1 个补丁 → `ux-agent` | rc=0 | patch-tree `35611ee0c9…` == branch-tree `35611ee0c9…` |

3. **不以交付脚本的退出码为准**：D3 加做"生产形状目录"探针（`repro/probe_d3_install_shape.py`，伪造 `<home>/installs/*/environments/*/venv/Lib/site-packages`，用捆绑解释器 + `PYTHONPATH` 启动，不设 `HERMES_SITE`），UX 全部条目用真实 CLI（`python -m hermes_cli.main kanban …`）+ 独立 `HERMES_KANBAN_HOME` 端到端跑（`repro/ux_e2e.py`、`repro/ux_stats_closure.py`）。
4. 所有命令与原始输出落盘在 `kanban/workspaces/t_4e357498/evidence/`（E1–E22，见 §7）。

环境说明：`python` = 仓库自带的 `.venv`（3.11.15，含 pytest 9.1.1 / fastapi / portalocker / ruamel.yaml）；D3/D5 相关另用捆绑解释器 `tools/python-3.14.7+…`。**这一点很关键**：交付方用 `uv --python 3.14`（无 pywin32）跑测试，我用仓库自己的 `.venv`（有真 pywin32）跑，差异直接暴露了 R1。

---

## 2. 逐条证据对照

### 2.1 P0 缺陷（t_2d128298）

| 项 | 基线 before（实测） | 修复后 after（实测） | 新测试 | 结论 |
|---|---|---|---|---|
| **D1** store 关闭后被读 → `AttributeError` | `AttributeError: 'NoneType' object has no attribute 'execute'`，rc=1（E5） | `hermes_lcm.store.StoreClosed: LCM message store is closed (database: …)`，rc=0（E6） | `test_store_closed_lifecycle.py` 10 passed（E11）；基线侧收集即报错（E12） | 通过 |
| **D2** `running` 卡 + 已关闭 run 无人收敛；网关拒答被记成 crash | 全部 sweeper 跑过仍 `task_status=running`、`claim_lock_still_set=true`；booking=`crashed`、`counts_as_failure=true`（E7） | `closed_run_pass_requeued=[t_f253eb72]`、`status=ready`、`claim_lock` 清空；booking=`gateway_refused` / `reclaimed` / `counts_as_failure=false`（E8） | `test_kanban_closed_run_convergence.py` 9 passed；基线 9 failed（E1/E2） | 通过 |
| **D3** 捆绑解释器 `import pywintypes` 失败（`.pth` 未执行） | 无 `activate_runtime_site_packages`；生产形状探针 `marker_at_import=ModuleNotFoundError`（E3） | 生产形状探针 `marker_at_import=from-install-tree`（自动发现，**未设** `HERMES_SITE`）；设 `HERMES_SITE` 后 `pywintypes` + `concurrent_log_handler` 均可导入、rc=0（E4b） | `test_runtime_dependency_selfcheck.py` 8 passed（基线 8 failed），第 9 项见 R1 | 机制通过；**引入 R0 阻塞回归**，交付脚本不可复现（R2），文字不实（R4） |
| **D5** Win32 设备名让路径补全整条挂掉 | 20 failed（`test_complete_path_reserved_names.py`，E2） | 20 passed（E1） | 同名文件 | 通过 |
| **D6** `human_delay.mode` 非法值每次读都告警（日志风暴） | 200 次读 → 200 条告警（E9） | 200 次读 → 1 条，且带类型名与修法（E10） | `test_human_delay_config.py` 4 passed（基线 4 errors） | 通过 |

代码面复核：D1 的 `_conn` 改为 property + `StoreClosed(RuntimeError)`，全仓仅 `store.py` 内 `self._conn =` 一处赋值（带 setter），其它模块的 `_conn` 属各自独立类，不受影响；`close()` 先摘引用后关句柄、幂等、持写锁；compaction 捕获 `StoreClosed` 降级为"本轮不压缩"、其它异常仍按原 sample 抛。D5 的过滤只作用于 Windows 且只作用于目录列举，`_safe_relpath` 兜底不改变正常路径。D6 只去重告警，语义与原来一致。D2 的 claim 路径在 `write_txn` 内同时写 `status='running'` 与 `current_run_id`，不存在"刚领取但 run 还没建"的窗口。

### 2.2 操作顺滑化（t_071e68d6）

| 项 | 基线 before（真实 CLI，E14） | 修复后 after（真实 CLI，E13） | 单测 | 结论 |
|---|---|---|---|---|
| **UX-01** fenced 上下文 `list` 必败 | rc=1，`kanban: delegate_task child contexts cannot mutate Kanban tasks or boards` | rc=0，正常出行 + stderr 一行 `read-only context — ready-closure promotion skipped` | 18 项套件内 | 通过 |
| **UX-02** 崩溃后拿不到 session id / 无恢复命令 | `runs` 无 Session 段；`runs --session` rc=2；`resume` 不是命令（rc=2） | `runs` 末尾 `Session:` + `Resume: hermes --resume <id>`；`runs --session` 只打印命令；`resume` 打印命令 + `--exec` 提示；`--json` 七字段齐；前后 `show --json` 完全一致（只读） | 18 项套件内 | 通过 |
| **UX-03** 无订阅者终态静默 | create 无任何提示；`show` 无 notify 行 | create stderr `✉ no notify subscriber — …`；`show` 有 `notify: none — terminal events will not be delivered`；`kanban.notify_fallback` 解析/落库有测试 | 18 项套件内 | 通过（端到端投递按卡片声明未做，交付方已明示） |
| **UX-05** 统计与归档不闭合 | 无 `total`/`active_total`/`archived_total`；`stats --json` 无这三键 | `total=2 active_total=1 archived_total=1`；`sum(by_status)==active_total`；`list` 末尾 `(1 archived task hidden — hermes kanban list --archived)`（E17） | 18 项套件内 | 通过 |
| **UX-09** 长跑卡无进度 | `--wide` 参数不存在（rc=2） | `● t_… running (unassigned) wide card run=2 hb=5m claim=0s left` | 仅覆盖 attempts/hb/run | **部分通过：`claim=` 恒为 0s（R3）** |
| UX-10 `list --json` 默认带 body | — | 未做（P2，交付方已声明交回总控） | — | 不计入本次验收 |

---

## 3. 复跑结果汇总

| 命令（cwd = 对应克隆） | 结果 | 日志 |
|---|---|---|
| `pytest tests/test_runtime_dependency_selfcheck.py tests/hermes_cli/test_kanban_closed_run_convergence.py tests/gateway/test_complete_path_reserved_names.py tests/gateway/test_human_delay_config.py` | 修复树 **1 failed, 40 passed**（唯一失败 = R1 环境性测试）；基线树 **37 failed, 4 errors**（D3 8 / D2 9 / D5 20 / D6 4） | E1 / E2 |
| `pytest tests/hermes_cli/test_kanban_ux_smoothness.py tests/hermes_cli/test_kanban_cli.py tests/hermes_cli/test_kanban_notify.py tests/hermes_cli/test_kanban_diagnostics.py` | **60 passed** | E15 |
| `pytest tests/hermes_cli/test_kanban_worker_exit_trailer.py test_kanban_reclaim_claim_lock_guard.py test_kanban_review_lifecycle_complete.py tests/gateway/test_kanban_reconcile_orphans.py` | **40 passed** | E16 |
| lcm：`pytest tests/test_store_closed_lifecycle.py tests/test_crash_safe_wal.py tests/test_compaction_telemetry.py` | **55 passed** | E19 |
| UX 测试文件跑在基线树 | **17 failed, 1 passed**（红侧成立） | E18 |
| combined 树（fix 4 提交 + UX 1 提交）跑两套新增测试 | **58 passed, 1 failed**（同一 R1） | E20 |
| `ruff check`（三棵树全部改动文件） | 全部 `All checks passed!` | 见 §7 |

交付方自述的 7 文件套件复跑（`test_kanban_{core_functionality,db,boards,list_undecodable_text,review_surfaces,workflow,promote}.py`）：

| 树 | 结果 | 日志 |
|---|---|---|
| fix-agent | **6 failed, 122 passed, 2 skipped** | E21 |
| base-agent | **6 failed, 122 passed, 2 skipped** | E22 |
| 差分 | 计数相同但**失败集合不同**：修复树独有 4 项（全部是 `_default_spawn` 相关，R0），基线树独有 4 项；仅 2 项两边共有（`test_infrastructure_spawn_refusal_never_charges_the_card`、`test_worktree_workspace_explicit_target_materializes_linked_worktree`） | — |
| 定点复核 | 4 项"修复树独有"用例单独跑：fix **4 failed**（`NameError: name 'sys' is not defined`，`cron/scheduler_worker_env.py:72`），base **4 passed**（9 秒） | E23 |

> 说明：`tests/hermes_cli/test_kanban*.py` 的全量 glob 在本机单轮超过 10 分钟仍未跑完（与 W3-A 记录一致：Windows 上全量套件不稳定），我改成"复跑交付方点名的那 7 个文件 + 对独有失败逐个定点复核"，结论等价且可复现。

---

## 4. 返工清单（具体到可执行）

> 已按人拆成两张返工卡：`t_2b4e30cc`（tsunade，R0/R1/R2/R4）与 `t_a9be51ed`（mitsuri，R3）。R5–R7 为建议项，未单独派卡。

### R0 [P0 阻塞] `cron/scheduler_worker_env.py` 用了没导入的 `sys` —— 默认 worker 起不来

- 代码：新增的 `_dependency_site_packages_on_path()` 第 72 行 `for entry in sys.path:`，而该文件头部只有 `import logging / os / sysconfig`。
- 触发链：`_default_spawn` → `_propagate_module_import_root(cmd, env)` → `pin_hermes_tree_on_pythonpath(env, root)` → `_dependency_site_packages_on_path()`。只要 worker 是模块式 argv（`[sys.executable, "-m", "hermes_cli.main"]`，正是 D3 针对的部署形态），**每一次 spawn 都会 `NameError`**，`subprocess.Popen` 之前就抛。
- 复现（E21/E23）：4 个 spawn 用例在修复树 **4 failed**，traceback 直指 `cron\scheduler_worker_env.py:72: NameError: name 'sys' is not defined`；同一 4 例在基线树 **4 passed**（9 秒）。
- 为什么没被交付方拦住：这 4 项被当成"HEAD 同样失败"，但 HEAD 上它们是绿的——两棵树的失败集合根本不同（§3）；仓库 `[tool.ruff.lint] select` 只开了 `PLW1514/ASYNC*/TID251`，`F821` 不在门内，所以"ruff 通过"在这里没有证明力。
- 修法：`import sys` 一行；并把 `ruff check --select F821 <改动文件>` 纳入提交/复核门（本次三棵树里只有这 1 处是新引入的 F821，其余 78 条为既有）。
- 复验标准：上述 4 例 fix 树 4 passed；`test_kanban_db.py` 的失败集合与基线逐条相同。

### R1 [P1 必须] D3 新测试不 hermetic，交付的"41 passed"不可在仓库自带解释器上复现

- 现象：`tests/test_runtime_dependency_selfcheck.py::test_activation_runs_pth_files_so_pywintypes_resolves`
  `AttributeError: module 'pywintypes' has no attribute 'VALUE'`（E1）。
- 根因：测试用 `.pth` 造了一个假的 `pywintypes`，然后 `import pywintypes` 断言其 `VALUE`；但仓库 `.venv` 里装了真 pywin32，`win32/lib` 已在 `sys.path` 前部，导入的是真模块。交付方用 `uv --python 3.14`（无 pywin32）才 41 passed。
- 修法（任一）：
  1. 断言机制本身而不是模块身份：`assert any(str(fake_site / "win32" / "lib") in p for p in sys.path)`，并用**唯一名**模块（如 `hermes_probe_pywintypes`）验证 `.pth` 确实被执行；
  2. 在测试里 `monkeypatch.syspath_prepend(str(fake_site / "win32" / "lib"))` 后断言，且不依赖环境是否装过 pywin32。
- 复验标准：`.venv\Scripts\python -m pytest tests/test_runtime_dependency_selfcheck.py -q` 与 `uv run --python 3.14 --with pytest --with ruamel.yaml python -m pytest …` **两种解释器都 41 passed**。

### R2 [P2 必须] D3 复现脚本在克隆布局下仍报 "BUG REPRODUCED"

- 现象：`repro_d3_pywin32.py` 的 `argv[2]` 只被记进报告、从未写进 `HERMES_SITE`；`activate_runtime_site_packages()` 按 `<repo>/../installs/...` 自动发现，克隆树（`kanban/workspaces/.../fix-agent`）旁边没有 `installs/`，于是修复树上 `added_entries=[]`、`after_activation=ModuleNotFoundError`、rc=1（E4）。
- 同一脚本加 `HERMES_SITE=<venv site-packages>` 后 rc=0（E4b）——**功能是对的，脚本不自洽**。
- 修法：脚本内 `os.environ.setdefault("HERMES_SITE", site_dir)`，并在输出里打印实际参与探测的候选目录；报告 §3 补一句"克隆布局必须显式给 `HERMES_SITE`"。

### R3 [P2 必须] UX-09 `claim=` 剩余时间恒为 0s（EVIDENCE 样例代码产生不出来）

- 现象：`_fmt_age(t.claim_expires)` 是"过去到现在的年龄"函数（`max(0, now - ts)`），未来时间戳一律夹成 0 → 未过期租约显示 `claim=0s left`；已过期反而显示 `claim=5m left`。实测输出 `run=2 hb=5m claim=0s left`（租约还剩 12 分钟），单元级验证 `_fmt_age(now+720) == '0s'`。
- EVIDENCE.md 里的 `run=7  hb=5m  claim=12m left` 在当前实现下不可能出现——证据需重出。
- 修法：新增 `_fmt_remaining(ts)`（`seconds = int(ts - now)`；`<=0 → "expired"`），`claim={_fmt_remaining(t.claim_expires)} left`；补 `test_list_wide_reports_claim_remaining`（现有测试只断言 `attempts=`/`hb=`/`run=`，`claim=` 无覆盖）。

### R4 [P2 必须] D3"启动硬检查"文字与实现不符

- 现象：`grep -rn "assert_runtime_dependencies"` 只有 `hermes_cli/doctor_platform.py` 一个生产调用点；`hermes_logging` 的 docstring 与交付报告称"Called from the startup self-check / 硬检查"。
- 判断：**当前行为（doctor 报告 + 运行时自动激活）是对的**，裸抛启动断言反而会 brick 启动；错的是文字。
- 修法：docstring 与报告改为"`hermes doctor` 的硬检查 + 运行时自动激活"；若真要启动断言，必须 fail-open（缺依赖只告警）并加开关。

### R5 [P3 建议] D2 基础设施判定的正则过宽，且收敛通道无独立开关

- `_RETRYABLE_INFRA_RE` 含 `connectionclosed(error)?`、`no close frame received or sent`，命中 worker 最后输出的任意位置。若任务本身在处理 websocket（例如 Discord 掉线重连任务），真实失败会被记成 `gateway_refused` → 不记失败 → 该卡可无限重排。
- 建议：只在错误里不含任务自身业务栈迹时命中，或对 `infrastructure` 重排设上限（每卡 N 次后按失败记账）；并在报告里写明该收敛通道仅能随 `reconcile_orphans` 关掉 runless 半边、本身无独立 kill switch。

### R6 [P3 建议] `resume --exec` 继承 worker 环境

- `_exec_resume` 直接 `subprocess.run(argv)`，继承 `os.environ`；在 worker 里执行时子会话会带上 `HERMES_KANBAN_TASK` / `HERMES_KANBAN_RUN_ID`，被当成该卡的 worker 继续跑。
- 修法：用 `agent.delegation_context.scrub_kanban_env(os.environ)` 清洗后传入。

### R7 [P3 建议] D3 fallback 去重是"最后写入者胜"

- 标记文件只记一个解释器/reason；生产上捆绑运行时与 venv 交替运行时每次都会重新告警，"每解释器一次"名不副实。
- 修法：标记文件存 `{interpreter: [reasons]}` 映射。

---

## 5. 生产零影响核验

| 检查 | 结果 |
|---|---|
| live HEAD | `49f72a24119c0c9c8e4a72951c4d876a6f7e5008`（与两份交付的基线一致，未移动） |
| live 工作树 | 本卡开始前 49 条脏项（24 M + 25 ??），与两份交付无关；本卡结束时 50 条，唯一新增是本报告 `docs/hermes_review_20261021.md`（只写文档，不动代码） |
| live `config.yaml` | 无 `kanban.notify_fallback` 键；mtime 16:33:45，早于本卡开始（16:34） |
| 合并 / 部署 / 重启 | 全部未做；两份交付仍在各自 worktree/clone 的分支上（`kanban/t_2d128298-defect-fix`、`kanban/t_2d128298-lcm`、`ux/kanban-smoothness-t_071e68d6`） |
| 生产板 | 我在 fenced 环境下读过一次生产板（`kanban list`，跳过 `recompute_ready`），259 archived 计数与任务 0 审计一致；未发生任务行写入 |
| 交付方自述的"未碰生产" | 与其 workspace 结构相符（各自 `HERMES_HOME`/`HERMES_KANBAN_HOME` 指到临时目录） |

---

## 6. 合并相容性与发布建议

- **文件集合零重叠**：fix 分支动 `hermes_logging.py`、`hermes_bootstrap.py`、`hermes_cli/doctor_platform.py`、`hermes_cli/kanban_db_dispatch.py`、`gateway/run_config_loaders.py`、`cron/scheduler_worker_env.py`、`tui_gateway/methods_complete.py` + 4 个新测试；UX 分支动 `hermes_cli/{kanban,kanban_db,kanban_output,kanban_parser,config_defaults}.py`、`tools/kanban_tools.py`、`docs/…` + 1 个新测试。
- **实测**：`combined` 克隆先 checkout fix 分支再 `git am` UX 补丁，无冲突；两套新增测试 **58 passed + 1 failed（仅 R1）**（E20）。
- 建议顺序：fix 分支 → UX 分支（任意顺序皆可，实测无耦合），但**必须先修 R0**（否则合并即等于让默认 spawn 挂掉），再修 R1/R3，R2/R4 属脚本与文档，同批改完再合并。
- 发布门（Windows 全量 pytest 有既有失败，沿用 W3-A 经验）：以"kanban 子集 + 基线差分失败集合逐条相同"作为闸门，而不是全量零失败。

---

## 7. 证据索引（`kanban/workspaces/t_4e357498/`）

| 文件 | 内容 |
|---|---|
| `evidence/E1_fix_new_tests.log` / `E2_base_new_tests.log` | 4 个新测试：修复树 1F/40P，基线 37F/4E |
| `evidence/E3_d3_base.json` | D3 生产形状探针（基线）：`marker_at_import=ModuleNotFoundError` |
| `evidence/E4_d3_fix.json` | 交付脚本跑修复树（未设 `HERMES_SITE`）：rc=1 —— R2 证据 |
| `evidence/E4_d3_fix_with_site.json` | 同一脚本 + `HERMES_SITE`：rc=0，`pywintypes`/`concurrent_log_handler` 可导入 |
| `evidence/E5_d1_base.log` / `E6_d1_fix.log` | D1 前后对照 |
| `evidence/E7_d2_base.log` / `E8_d2_fix.log` | D2 前后对照（含 booking 分类） |
| `evidence/E9_d6_base.log` / `E10_d6_fix.log` | D6：200 条 → 1 条 |
| `evidence/E11_lcm_newtests_fix.log` / `E12_lcm_newtests_base.log` | lcm 新测试 10 passed / 基线收集失败 |
| `evidence/E13_ux_e2e_fix.log` / `.json` | UX 真实 CLI 端到端（修复树） |
| `evidence/E14_ux_e2e_base.log` / `.json` | 同上（基线树，红侧） |
| `evidence/E15_ux_tests_fix.log` | UX 18 + 相邻 3 套件 = 60 passed |
| `evidence/E16_fix_adjacent_tests.log` | fix 相邻 4 套件 = 40 passed |
| `evidence/E17_ux_stats_closure_fix.json` | UX-05 闭合与归档 footer |
| `evidence/E18_ux_tests_base.log` | UX 测试红侧 17F/1P |
| `evidence/E19_lcm_regression_fix.log` | lcm 回归 55 passed |
| `evidence/E20_combined_tests.log` | 合并树 58P/1F |
| `evidence/E21_fix_claimed_set.log` / `E22_base_claimed_set.log` | 交付方点名的 7 文件套件：两树均 6F/122P/2S，失败集合不同 |
| `evidence/E23_spawn_regression.log` | R0 定点复核：fix 4 failed（NameError `sys`）/ base 4 passed |
| `repro/probe_d3_install_shape.py` | 生产形状自动发现探针（我写的独立验证） |
| `repro/ux_e2e.py`、`repro/ux_stats_closure.py` | UX 端到端与对账探针（我写的独立验证） |
| `repro/repro_d1…/d2…/d3…/d6….py` | 交付方复现脚本副本（未改动；D3 缺陷见 R2） |

---

## 8. 结论一句话

四个缺陷的根因是真的掐掉了（D1/D2/D5/D6），五个顺滑化条目也都拿真实 CLI 跑通了，这部分可以收；但 D3 那一笔在 `cron/scheduler_worker_env.py` 里漏了 `import sys`，把"日志降级"换成了"默认 worker 直接起不来"——而交付方用"HEAD 上同样 6 项失败"盖过去了，实际上失败集合根本不同。先把 R0 修掉、R1/R3 补上，再交千花终审与思远裁决发布；在那之前，这份交付我签不了字。
