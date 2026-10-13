# Release Coordinator

A self-hosted release coordinator for multiple development tasks. Python 3.9+, Git and Unix; no Python runtime dependencies. macOS and Linux can host it. Each project supplies its own build/publish adapter. It does not choose your language, version scheme, signing identity, deployment environment or distribution channels.

[中文接入规则](USAGE.md) · [可复制接入 prompt](INTEGRATION_PROMPT.md) · [MIT license](LICENSE)

## What it does

- Shared owner-only Unix socket and SQLite task registry across chats/worktrees.
- Independent release scopes: repository + application/service + environment. Different projects can publish in parallel. An optional `lock_scope` handles an explicitly shared physical target.
- Immediate release when there are no other active relevant tasks; otherwise a 90-second collection window, extendable to 180 seconds only for tasks explicitly in closing validation. Blocked and stale tasks are excluded.
- Immutable task commit SHAs, isolated integration worktrees, merge-conflict retention and merged validation through the project adapter.
- A frozen batch manifest, final commit, version and target. Late tasks go to the next batch.
- One fenced lease per scope, 10-second heartbeat / 60-second expiry; a stopped owner cannot initiate another step or write state after losing its lease.
- Remote reconciliation before each external step. A lost publish response is recovered by checking actual results, not blindly running the publish again.
- A single shared receipt per successful batch; queued or frozen tasks are not reported complete.
- Persistent SQLite state, logs, checkpoints, retained worktrees and receipts across service restarts.

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

`ready` authorizes the configured adapter's automatic release after collection. Register at task start so other tasks are visible; heartbeat at least once a minute while active. A task heartbeat is not the publisher lease heartbeat. Push the development commit to the configured remote before declaring it ready. The service verifies the exact commit in a fresh temporary Git repository; a local unpushed object cannot satisfy this check. Fetch, network, or object-type failures leave readiness unchanged. Identical accepted requests can be retried without remote access; withdrawn requests require verification again. Required validation names must be passed. `--urgent` skips collection; `--separate` releases that task alone. Interrupted non-failed batches resume after lease expiry; immutable failed batches block only their scope until a linked repair batch completes. Unrelated projects continue.

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

`prepare` allocates/reserves the project's version once, applies it and runs necessary merged checks. It must survive retries without allocating a second version. The coordinator commits a sanitized `.release-batch.json` and freezes its introducing commit SHA afterward. `check` reconciles the actual CI/deployment/release, using that frozen SHA and the existing external operation IDs. `run` performs only missing work. A final verification phase must verify every required channel; returning `done: true` means verified completion, not request submission.

The context contains frozen metadata, private project options, prior results and `progressPath`. Write an atomically replaced JSON progress file there to expose CI run IDs while waiting. Results should be shareable status/evidence, not passwords, tokens, provisioning profiles or private keys. Public batch manifests contain only task SHAs, passed check names, version/build and target.

A manifest cannot embed the hash of its own commit. CI should derive the final SHA from its Git event and verify that it is the exact commit introducing `.release-batch.json`. Full checkout history is needed to check task ancestry. A later documentation commit is not a substitute artifact source.

## Recovery and updates

```sh
~/.local/bin/releasectl batch --id BATCH_ID
~/.local/bin/releasectl register --project MY_PROJECT --task REPAIR_TASK --repairs-batch FAILED_BATCH_ID
```

Interrupted integrating/building batches resume automatically after lease expiry; recovery reconciles remote effects first. A failed batch remains immutable and cannot be resumed in place. Register a new task explicitly linked with `--repairs-batch`, push its fixed repair SHA, and mark it ready; the child batch includes the failed task lineage. Resolve an integration conflict in its retained worktree and use that resolution commit in the linked repair task. Never edit a frozen worktree or move a published tag. If an immutable stage is incomplete, the adapter selects a linked repair using a higher version as required by the project. A successful child marks its inherited tasks `recovered` and publishes one receipt including recovery lineage; the original failure record remains available. If an action errors after an external effect, the worker reconciles once before marking failure. A verified effect is not repeated; a persistent error retains diagnostics and blocks its scope instead of automatically looping.

If a pre-freeze failure explicitly records `not our ref` for an incorrectly registered SHA, ordinary recovery will inherit that unavailable commit. Use an explicit correction against the latest failed leaf:

```sh
~/.local/bin/releasectl register --project MY_PROJECT --task CORRECTION_TASK \
  --repairs-batch LATEST_FAILED_BATCH_ID --corrects-task ORIGINAL_TASK
~/.local/bin/releasectl ready --task CORRECTION_TASK --sha VERIFIED_PUSHED_SHA \
  --validation /private/path/validation.json
```

The service verifies that the old remote SHA is still unavailable and the replacement is a pushed commit. The new batch excludes only that unavailable task, retains all other participants, and records both task/SHA pairs under `corrections` in its manifest and receipt. Original task SHAs, failure records and worktrees remain unchanged; successful recovery marks the original task `recovered`. Corrections cannot alter a frozen batch or omit a valid remote commit. Concurrent correction registrations are exclusive; withdrawing one does not let it re-enter after another replaces it.

Update by fetching a reviewed revision, running tests and re-running the installer in the already installed startup mode. Old runtime directories, state and historical artifacts remain. Service shutdown terminates its local command groups, preserves recovery state and does not cancel remote CI jobs. Adapters on recovery check remote status again.

For multiple machines, run all commands against the same publishing host using authenticated SSH, for example `ssh publisher '~/.local/bin/releasectl status'`. Development SHAs must exist on the shared Git remote, and validation files must be transferred to that host. Do not create separate coordinator databases per machine or put the SQLite database on a network share. This version does not expose a public HTTP API.

## Verification

`python3 -B -m unittest discover -s tests -v` covers independent-project concurrent workers, scoped atomic ownership, stale-owner fencing, bounded collection, separate releases, immutable batches, late tasks, retained merge conflicts, lost-response reconciliation and Unix-socket restart persistence. Real publishing and deployment depend on the project's adapter and require its own integration acceptance.
