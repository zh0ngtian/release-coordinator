# 共用写锁项目的失败隔离实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** 失败恢复按发布项目隔离，显式共用 `lock_scope` 的项目继续互斥写入同一目标。

**Architecture:** `due` 与 `create_batch` 只查当前项目及当前 scope 的失败叶节点；运行中批次检查和 fenced lease 仍按整个 scope 检查。外项目失败不被恢复、不被继承、不被覆盖；同项目失败仍须显式关联修复。没有 schema 迁移和通道降级。

**Tech Stack:** 现有 Python/SQLite、Unix socket 服务、macOS LaunchAgent 与不可变运行时安装器。

## 用户授权与问题证据

用户已明确授权上述隔离修复并继续仅 iOS 发布。现役两个项目共享 main 写锁；默认双端项目失败叶节点阻止 OTA 项目调度，但 `register --repairs-batch` 又只允许原项目恢复，形成永久阻塞。当前没有运行中批次或有效租约；原失败历史必须完整保留。

## 任务一：项目失败链与共用锁分离

**文件职责：**
- `release_coordinator/store.py`：两处失败叶节点查询加入当前 project 过滤；active batch 与 lease 的 scope 互斥不变。
- `tests/test_coordinator.py` 或独立 `tests/test_shared_scope_failures.py`：用真实 Store/Worker 和受控适配器测试跨项目失败、当前项目自身失败链、同锁串行、并发竞争以及历史不可变。
- `release_coordinator/__init__.py`：版本升级为 0.1.5。
- `README.md`、`USAGE.md`、`INTEGRATION_PROMPT.md`：区分项目队列/失败链与 scope 写锁；历史版本记录保留。

- [ ] 基线 `python3 -B -m unittest discover -s tests -v` 必须通过。
- [ ] 先加入真实行为 RED：A、B 共用写锁，A 失败且释放租约后，B 的普通 ready 应可 due/create_batch，B 不继承 A 的任务和恢复链，A 的失败记录保持原样。
- [ ] 加入双失败链 RED：A、B 均失败时，A 的新任务继续被 A 失败阻挡，A 显式关联自己的最新失败叶节点后能恢复，不能误取时间更晚的 B 失败。
- [ ] 覆盖同 scope 的活动批次和真实并发：B 可恢复独立调度，但 A/B 不能并行写同一个目标；窗口与旧 owner fencing 合同不变。保留既有响应丢失、不可用提交更正和中断恢复全量回归。
- [ ] 最小修复两处 SQL 查询；不要改数据库历史、lease 互斥或用户项目的构建/分发策略。
- [ ] 全量测试、`git diff --check`、自审、提交；不自行安装服务或发客户端。
- [ ] 依序独立规格审查、质量审查；必要修复后复审。

## 任务二：现役升级与客户端交付

- [ ] 复核没有活动批次或有效 lease；保存旧安装凭证、CLI/launchd 指向及 SQLite backup（仓库外、私有权限）。
- [ ] 提交推送审查后的固定 SHA，保持既有启动模式，先 `install.py --plan`，再运行现有安装器停止/恢复现役服务；不另建数据库、不删除旧 runtime、不取消远端 CI。
- [ ] 验证 CLI/服务版本 0.1.5、runtime 内容散列与源代码一致、启动模式不变、旧失败批次和任务历史不变。OTA 任务应无需关联外项目失败即可自动进入自己的批次；原双端项目的失败链仍被保留。
- [ ] 等待 Codex Mobile 已推送任务 `ios-composer-web-controls-20261017` 的 OTA-only 批次成功；实际 receipt 核对 main、iosbuild、sign、ota、ipa、verify，不存在 GitHub Release/APK/软件源阶段。
- [ ] 独立完整下载 LAN IPA 与 HTTPS OTA，核对版本、大小、SHA-256 和升级签名身份连续性，再交付固定安装链接。
- [ ] 记录实际升级证据并提交推送文档；清理本任务隔离工作区，保留失败、历史产物与凭证。

## 验证证据位置

协调器修复：`~/Library/Application Support/ReleaseCoordinator/validations/project-failure-isolation-20261017/`。

客户端现有 1517 项回归、8 项 iPhone 17 Pro 正常字号竖屏真实 UI 和审查证据：`~/Library/Application Support/CodexMobile/validation/ios-composer-web-controls-20261017/`；客户端源码已经提交推送，不为本次协调器修复重复改动。
