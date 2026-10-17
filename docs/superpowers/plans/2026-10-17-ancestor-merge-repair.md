# 祖先任务合并恢复实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking. 本任务已明确采用当前会话逐项执行，不再委派子代理。

**Goal:** 已显式解决旧任务与当前正式分支冲突的后继修复提交，能够通过关联子批次整合，同时完整保留旧任务来源、失败现场和交付核验。

**Architecture:** `Worker.integrate` 先获取每个不同任务的精确远端 SHA，再用 Git 提交图计算不被其他任务覆盖的 merge heads，并依次合并这些 heads。仅优化 Git 合并输入，原始 tasks、manifest、冻结信息、receipt 和两次逐任务祖先核验继续使用所有来源。有效远端提交不能被更正机制省略，真实独立冲突仍失败并保留现场。

**Tech Stack:** Python 3.9+ 标准库、Git、SQLite、现有 Worker 与项目适配器。

## 规格与授权

- 用户已经授权修复旧客户端失败批次、合入原任务功能并继续双端发布。本上游任务仅负责通用协调器修复、验证与源码推送；不安装服务、不修改使用者现役数据库或失败 worktree。
- `0.1.5` 的根因：`integrate` 对 `tasks` 逐个 fetch 后立即 merge。继承的旧 SHA 与当前 main 冲突时，包含旧任务及人工冲突解决的后继 SHA 尚未被读取；普通关联修复会重复相同失败。
- 成功条件：后继提交确实包含每个覆盖任务；独立任务全部合并；每个原始 SHA 仍是冻结提交和交付提交的祖先。不得依赖提交时间、项目名称、私有目录或无条件冲突策略。
- 失败历史不改写，失败批次不能原地 resume。后继修复属于新关联批次，不能代替旧任务的来源记录。`--corrects-task` 继续仅适用于已证实远端不存在的 SHA。
- 外部仓库不能通过当前任务的原生 worktree 工具指定，因此使用 Git fallback，在仓库外隔离分支开发。

## 文件职责

- `tests/test_ancestor_merge_repair.py`：真实 Git 仓库、Store、Worker 和受控适配器回归，覆盖后继冲突解决、并列任务、重复/已整合任务、远端缺失、真实冲突及中断恢复。
- `release_coordinator/engine.py`：仅调整 fetch 与 merge 输入选择，不改变状态机和审核来源。
- `release_coordinator/__init__.py`、`pyproject.toml`：源码版本一致升级为 `0.1.6`。
- `README.md`、`USAGE.md`：说明实际合并语义、关联恢复及不可变来源。
- `docs/validations/2026-10-17-ancestor-merge-repair.md`：本地实际 RED/GREEN/全量验证记录，明确尚未部署。

## 任务一：复现与回归

- [x] 读取 AGENTS、README、USAGE，确认 main 为 `5059883` / `0.1.5`，工作区干净；隔离 worktree 基线 `python3.11 -B -m unittest discover -s tests -v` 为 35/35 通过。
- [x] 新增真实冲突测试：base → 旧任务修改 `file`；main 独立修改同一文件；原批次真实失败。新 repair 分支合并旧任务、显式解决冲突并推送，关联失败批次后要求成功。核对旧失败整行、worktree 的冲突内容和 MERGE_HEAD 完全不变，原任务在同一新凭证中 recovered。
- [x] 运行 `python3.11 -B -m unittest discover -s tests -p test_ancestor_merge_repair.py -v`，确认旧实现因先 merge 旧 SHA 导致预期断言失败，保存 RED 日志。
- [x] 覆盖并列独立提交均被合入，重复 SHA 去重但任务审计不去重，已在 main 的任务仍出现在 manifest/receipt；缺失远端 SHA 不能因其他任务而被跳过，实际冲突仍保留现场且不调用适配器。
- [x] 用已有部分合并的中断批次检查恢复；保留现有真实并发锁、失效 owner fencing、响应丢失与更正任务回归。

## 任务二：最小实现

- [x] 将原逐任务合并改为以下步骤，仍通过 `self.git` 执行以保留租约防护、超时和进程终止约束：

```python
task_shas=list(dict.fromkeys(task['sha'] for task in data['tasks']))
for sha in task_shas:
    self.git(repo,'fetch','origin',sha)
heads=self.git(work,'merge-base','--independent',*task_shas).splitlines()
for sha in heads:
    self.git(work,'merge','--no-ff','--no-edit',sha)
```

- [x] 逐项检查 `prepare` 和交付末尾的 `merge-base --is-ancestor` 仍遍历全部 `data['tasks']`，manifest/receipt 不改为仅 heads。
- [x] 运行上述定向命令取得 GREEN；运行 `python3.11 -B -m unittest discover -s tests -v`，要求全部测试通过。

## 任务三：文档、版本与交付

- [x] 将两个版本声明从 `0.1.5` 改为 `0.1.6`，保留历史部署文档内容。
- [x] README 与 USAGE 写明先获取所有任务 SHA、按 Git 祖先关系选合并头，以及新关联恢复、历史不可变、来源完整保留与缺失 SHA 继续拒绝。
- [x] 将真实验证结果写入本地验证文档；原始日志位于 `~/Library/Application Support/ReleaseCoordinator/validations/ancestor-merge-repair-20261017/`，不得声称服务已经升级。
- [x] 执行 `git diff --check`、全量测试与暂存检查，按中文 Conventional Commit 提交；正文含“新增功能”和“主要修改”。推送 `codex/ancestor-merge-repair-20261017` 后报告精确 SHA、验证结果与后续安装责任。

## 边界

当前计划无数据库迁移、服务安装、适配器发布策略、main 推进或客户端构建操作。主任务在独立审查后负责现役升级及真实渠道交付。
