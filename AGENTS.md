# Release Coordinator 项目规则

- 独立通用 Python/SQLite 发布协调器；不把使用者项目的技术栈、路径、身份或渠道写成核心默认值。
- 核心合同见 README.md、USAGE.md、INTEGRATION_PROMPT.md；项目私有配置及状态库不提交。
- 验证：`python3 -B -m unittest discover -s tests -v`；状态/锁/恢复改动须覆盖真实并发与响应丢失，不用仅复述实现的断言替代验收。
- 不同项目独立租约，可并行。短数据库事务不跨构建/发布持有；远端操作前后核对 lease 及实际结果。
- 当前版本 macOS/Linux 服务使用 owner-only Unix socket；多机器通过同一发布主机 SSH，不部署各自状态库。
- macOS 用户 LaunchAgent 是登录启动；系统 LaunchDaemon 才是登录前开机启动。未经实际安装验收，不宣称对应能力已上线。
- 本项目首次自部署尚无协调器可用时，允许本次用户明确授权的 bootstrap：本地验证、提交推送源码、安装运行时并验收，保留部署凭证。后续维护遵守全局协调规则；自升级时停止并恢复服务，不取消远端 CI，不删除历史状态和运行时。
- 发布共享源码与安装文档可以直接提交；真实使用者项目是否完成发布，以其适配器的实际渠道验收和共同凭证为准。
