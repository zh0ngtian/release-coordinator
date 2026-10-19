# Release Coordinator

A self-hosted release coordinator for multiple development tasks. Python 3.9+, Git and Unix; no Python runtime dependencies. macOS and Linux can host it. Each project supplies its own build/publish adapter. It does not choose your language, version scheme, signing identity, deployment environment or distribution channels.

[中文接入规则](USAGE.md) · [可复制接入 prompt](INTEGRATION_PROMPT.md) · [MIT license](LICENSE)

## What it does

- Shared owner-only Unix socket and SQLite task registry across chats/worktrees.
- Independent task queues and failure/recovery chains per project. Release scopes default to repository + application/service + environment; different scopes can publish in parallel. An optional shared `lock_scope` serializes projects that write the same physical target, without joining their queues or recovery chains.
- Immediate release when there are no other active relevant tasks; otherwise a 90-second collection window, extendable to 180 seconds only for tasks explicitly in closing validation. Blocked and stale tasks are excluded.
- Immutable task commit SHAs, isolated integration worktrees, merge-conflict retention and merged validation through the project adapter.
- A frozen batch manifest, final commit, version and target. Late tasks go to the next batch.
- One fenced lease per scope, 10-second heartbeat / 60-second expiry; a stopped owner cannot initiate another step or write state after losing its lease.
- Remote reconciliation before each external step. A lost publish response is recovered by checking actual results, not blindly running the publish again.
- A single shared receipt per successful batch; queued or frozen tasks are not reported complete.
- Persistent SQLite state, logs, checkpoints, retained worktrees and receipts across service restarts.

自 0.1.7 起，已冻结批次的适配器可把只读查询或下载暂时无法确认表示为 `transient-read`，同批次持久等待并优先对账；网络未知不会被解释为缺失工作。详见下方合同与恢复规则。

自 0.1.6 起，整合会先从远端获取每个不同任务的精确 SHA，再按 Git 祖先关系选择不被其他参与任务覆盖的合并头。因此，包含旧任务及其显式冲突解决的后继修复提交可直接参与合并，避免在读取解决提交前重复合并旧任务而再次冲突。彼此独立的任务仍全部合并；任何原始远端 SHA 不可用时仍失败。原始任务列表、manifest、冻结记录及 receipt 保留全部任务/SHA，准备后和最终交付前继续逐项核对祖先关系，后继提交不会代替旧任务来源。

This is cooperative release coordination, not repository access control: authorized humans can still bypass the tool or edit CI. Project rules and CI must direct ordinary tasks through the coordinator. Adapters must implement trustworthy remote checks and idempotent recovery. The supplied demonstration adapter creates local markers only and does **not** prove any real release.

## Install on macOS

```sh
git clone https://github.com/zh0ngtian/release-coordinator.git
cd release-coordinator
python3 -B -m unittest discover -s tests -v
python3 install.py --plan
python3 install.py
~/.local/bin/releasectl health
```

The default installs `local.release-coordinator` as a LaunchAgent: it starts at user login and restarts on failure. Code is copied into an immutable runtime directory; launchd never points into a development worktree. State lives in `~/Library/Application Support/ReleaseCoordinator/`. There is no public network port.

For **system boot before login**, use a LaunchDaemon with the intended non-root service account (replace the username):

```sh
sudo python3 install.py --system --user YOUR_USERNAME --python /absolute/path/to/python3
```

System installation requires administrator authentication. The daemon itself runs as the specified user. Signing keychains and account credentials may be unavailable before login: configure a read-only `preflight`, such as `gh auth status`, to defer processing until credentials become available. The registry can start without those credentials. Login startup and system boot are distinct; confirm `installation.json` and `launchctl print` for the mode actually installed.

## Install on Linux

Install with `python3 -m pip install .`, then adapt [the systemd unit](examples/release-coordinator.service) to the absolute Python executable, state directory, installed module and non-root service account. Use a system unit for boot startup or a user unit for login startup. The macOS installer does not modify Linux services.

## Configure a project

Copy [project.example.json](examples/project.example.json) to a private location, set the real repository root/remote, target, environment and adapter command, then register it:

```sh
~/.local/bin/releasectl project --config /private/path/project.local.json
~/.local/bin/releasectl register --project MY_PROJECT --task MY_TASK
~/.local/bin/releasectl heartbeat --task MY_TASK --state closing
~/.local/bin/releasectl ready --task MY_TASK --sha FULL_40_CHARACTER_SHA --validation /private/path/validation.json
~/.local/bin/releasectl wait --task MY_TASK --timeout 45
~/.local/bin/releasectl status --task MY_TASK
```

`ready` authorizes the configured adapter's automatic release after collection. Register at task start so other tasks are visible; heartbeat at least once a minute while active. A task heartbeat is not the publisher lease heartbeat. Push the development commit to the configured remote before declaring it ready. The service verifies the exact commit in a fresh temporary Git repository; a local unpushed object cannot satisfy this check. Fetch, network, or object-type failures leave readiness unchanged. Identical accepted requests can be retried without remote access; withdrawn requests require verification again. Required validation names must be passed. `--urgent` skips collection; `--separate` releases that task alone. Interrupted non-failed batches resume after lease expiry; immutable failed batches block ordinary ready work only in their own project until a linked repair batch completes. Projects sharing a `lock_scope` still wait for any active batch and live lease in that scope; after a failed owner releases its lease, another project's ordinary ready work can proceed.

## Adapter contract

An adapter is a command argument array (no shell), with optional `{workspace}`, `{repository}` and `{state_dir}` placeholders. It receives:

```text
ADAPTER run prepare --context /private/path/batch.json
ADAPTER check PHASE --context /private/path/batch.json
ADAPTER run PHASE --context /private/path/batch.json
```

Emit exactly one JSON object on stdout; diagnostics go to stderr:

```json
{"done": true, "result": {"version": "1.2.3", "buildNumber": "42"}}
```

已冻结阶段的 `check`，或在执行过程中遇到只读查询/下载故障的幂等 `run`，可返回：

```json
{"done": false, "retryable": {"kind": "transient-read", "reason": "已脱敏的暂时查询失败原因"}}
```

这表示“当前无法确认”，不会触发或重复 `run`。`done` 必须是布尔值；普通响应保留既有 `done/result` 格式，提供的 `result` 必须为对象。暂时故障响应只能包含 `done` 与 `retryable`，后者只能包含上述 `kind` 及非空、最多 2000 字符的脱敏 `reason`；不得同时提供 `result` 或 `done: true`。非法 JSON、矛盾协议、明确构建失败及完整性错误仍进入不可变 `failed`。核心只理解结构化合同，不匹配 EOF 或网络错误字符串。适配器只对确认为只读网络异常的错误输出该响应；其余错误继续抛出。`prepare` 及冻结前整合不适用此恢复合同，仍遵循原有失败规则，不能用它重新分配版本。

`prepare` allocates/reserves the project's version once, applies it and runs necessary merged checks. It must survive retries without allocating a second version. The coordinator commits a sanitized `.release-batch.json` and freezes its introducing commit SHA afterward. `check` reconciles the actual CI/deployment/release, using that frozen SHA and the existing external operation IDs. `run` performs only missing work. A final verification phase must verify every required channel; returning `done: true` means verified completion, not request submission.

The context contains frozen metadata, private project options, prior results and `progressPath`. Write an atomically replaced JSON progress file there to expose CI run IDs while waiting. Results should be shareable status/evidence, not passwords, tokens, provisioning profiles or private keys. Public batch manifests contain only task SHAs, passed check names, version/build and target.

A manifest cannot embed the hash of its own commit. CI should derive the final SHA from its Git event and verify that it is the exact commit introducing `.release-batch.json`. Full checkout history is needed to check task ancestry. A later documentation commit is not a substitute artifact source.

## Recovery and updates

```sh
~/.local/bin/releasectl batch --id BATCH_ID
~/.local/bin/releasectl register --project MY_PROJECT --task REPAIR_TASK --repairs-batch FAILED_BATCH_ID
```

Interrupted integrating/building batches resume automatically after lease expiry; recovery reconciles remote effects first. A failed batch remains immutable and cannot be resumed in place. Register a new task explicitly linked with `--repairs-batch` to that project's latest failed leaf, push its fixed repair SHA, and mark it ready; the child batch includes only its own project's failed task lineage. A failure in another project cannot be a repair target or become an inherited parent, even with a shared `lock_scope`. Never edit a frozen worktree or move a published tag. If an immutable stage is incomplete, the adapter selects a linked repair using a higher version as required by the project. A successful child marks its inherited tasks `recovered` and publishes one receipt including recovery lineage; the original failure record remains available. If an action errors after an external effect, the worker reconciles before marking failure. A verified effect is not repeated; known failures retain diagnostics and block the project's ordinary queue. Another project's successful batch does not change that failure or its tasks.

网络等待使用现有 `recovering` 状态，保留同一 batch、冻结 SHA、版本、外部操作 ID/checkpoint 与已完成结果。`batch`/`status` 中的 `data.recovery` 持久记录 `phase`、`attempts`、`firstFailureAt`、`nextAttemptAt`、脱敏 `reason` 及 `attentionRequired`。同阶段连续失败按 5、10、20、40、60 秒退避，之后封顶 60 秒，各次加 0～1 秒抖动；服务重启仍遵守时间。`data.recoveryEpisodes` 按阶段保留各个尚未解决的故障周期，`data.recovery` 展示当前阻塞阶段；交替阶段发生未知也不会互相覆盖计时。只有该阶段取得明确查询结果才清除自己的故障周期，较早健康阶段的对账不会重置其他阶段的计时。连续 15 分钟无法查询时保留 `recovering` 并设置 `attentionRequired: true`、继续按 60 秒加抖动的最大间隔自动重查；`wait` 可立即返回该状态。`resume --batch BATCH_ID` 可在租约释放后主动重查，不能修改冻结信息、取消远端 CI 或绕过确定性失败。同作用域的新批次继续等待，其他作用域照常执行，租约和 fencing 检查保持生效。

恢复总先 `check`：已完成阶段仅对账，若明确发现结果缺失则失败，不重复其 `run`。若 `run` 普通异常后 `check` 也暂时未知，原写操作错误保存在 `data.pendingActionError` 并等待；随后仅 `check done: true` 能接纳成功，明确 `done: false` 则以原错误进入不可变 `failed`，不能盲目重试写操作。只有 `run` 自身明确返回上述只读故障合同，才允许恢复查询确认缺失后，由适配器幂等补齐工作。普通 `run` 返回 `done: true` 或 `done: false` 后若对账未知，同样禁止在恢复查询明确缺失时再次执行；只有查询确认成功才接纳。核心在执行子进程前持久记录 `data.pendingRun`，覆盖操作已发生、响应落库前负责人死亡的窗口；恢复查明缺失时保守失败，只有 `run` 明确返回结构化 `transient-read` 才解除该限制。运行中再遇只读故障继续保留 checkpoint。升级接入时先安装兼容旧适配器的 0.1.7 核心，再让适配器输出新协议，避免旧核心把未知状态当成缺失工作。

处理整合冲突时，保留原失败 worktree，在独立修复分支中合并当前正式分支与需要覆盖的旧任务，显式解决冲突并提交推送，然后以该 SHA 就绪新的关联修复任务。协调器根据真实提交图选择合并头，且要求所有继承任务仍是最终提交的祖先；真正独立的未解决冲突会继续保留现场并失败。不能用后继 SHA 覆写旧任务记录，也不能 resume 已失败批次；`--corrects-task` 仍只适用于下述已证实远端不可用的 SHA。

If a pre-freeze failure explicitly records `not our ref` for an incorrectly registered SHA, ordinary recovery will inherit that unavailable commit. Use an explicit correction against the latest failed leaf:

```sh
~/.local/bin/releasectl register --project MY_PROJECT --task CORRECTION_TASK \
  --repairs-batch LATEST_FAILED_BATCH_ID --corrects-task ORIGINAL_TASK
~/.local/bin/releasectl ready --task CORRECTION_TASK --sha VERIFIED_PUSHED_SHA \
  --validation /private/path/validation.json
```

The service verifies that the old remote SHA is still unavailable and the replacement is a pushed commit. The new batch excludes only that unavailable task, retains all other participants, and records both task/SHA pairs under `corrections` in its manifest and receipt. Original task SHAs, failure records and worktrees remain unchanged; successful recovery marks the original task `recovered`. Corrections cannot alter a frozen batch or omit a valid remote commit. Concurrent correction registrations are exclusive; withdrawing one does not let it re-enter after another replaces it.

Update by fetching a reviewed revision, running tests and re-running the installer in the already installed startup mode. Old runtime directories, state and historical artifacts remain. Service shutdown terminates its local command groups, preserves recovery state and does not cancel remote CI jobs. Adapters on recovery check remote status again.

[0.1.5 本机升级验收](docs/deployments/2026-10-17-0.1.5.md) records the verified runtime update, retained history and real shared-lock project isolation acceptance.

For multiple machines, run all commands against the same publishing host using authenticated SSH, for example `ssh publisher '~/.local/bin/releasectl status'`. Development SHAs must exist on the shared Git remote, and validation files must be transferred to that host. Do not create separate coordinator databases per machine or put the SQLite database on a network share. This version does not expose a public HTTP API.

## Verification

`python3 -B -m unittest discover -s tests -v` covers independent-project concurrent workers, shared-scope worker serialization, project-specific failure leaves and retained history, scoped atomic ownership, stale-owner fencing, bounded collection, separate releases, immutable batches, late tasks, retained merge conflicts, lost-response reconciliation and Unix-socket restart persistence. Real publishing and deployment depend on the project's adapter and require its own integration acceptance.

0.1.7 的真实 Git/子进程测试还覆盖查询未知时禁止执行、一次外部副作用与操作 ID/冻结版本复用、写响应丢失后的成功/失败对账、跨 Store/Worker 重启的退避、后续阶段的连续故障计时、15 分钟人工关注与 wait/resume、同作用域阻塞和异作用域继续、已完成阶段拒绝重复执行、确定性失败及非法协议封闭失败；本地测试不代表现役服务已升级。

[0.1.6 本地验证记录](docs/validations/2026-10-17-ancestor-merge-repair.md) 还覆盖真实 Git 后继冲突解决、并列独立任务、重复 SHA、已在正式分支中的任务、远端缺失拒绝、逐任务祖先保护与部分整合后的中断恢复；该记录不代表现役服务已升级。

[0.1.6 本机升级验收](docs/deployments/2026-10-17-0.1.6.md) records the verified descendant-repair merge update, unchanged failed history and retained runtime. User-project delivery still requires its own successful receipt.
