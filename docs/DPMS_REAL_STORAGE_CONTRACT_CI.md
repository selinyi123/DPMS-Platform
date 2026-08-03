# DPMS 真实存储契约 CI

`real-contracts.yml` 在隔离的 MySQL 8.0 与 Redis 7 服务上持续执行两组默认
单元测试无法覆盖的契约：

- 全量 Schema 初始化、正向迁移、生产 Schema 验证及回滚后重新升级；
- Worker 以运行时最小权限核对当前镜像携带的完整迁移版本与 checksum
  账本，拒绝缺失、漂移或额外版本；
- 策略索引 drift 拒绝、账号滚动风险状态触发器，以及超时取消后 MySQL 连接池恢复；
- Redis Stream 多 consumer-group 终态保留、Lua 原子操作与 ACL 拒绝语义；
- 以项目实际 Redis entrypoint 启动第二个隔离实例，验证固定 group bootstrap、
  bootstrap/default 用户禁用、Core/Worker 精确 scope preflight、health 最小权限和
  group-admin 只读治理边界。

CI 使用名称以 `dpms_contract_` 开头的临时数据库。准备脚本同时要求：

1. `DPMS_MYSQL_INTEGRATION=1`；
2. `DPMS_CONTRACT_DATABASE_BOOTSTRAP=1`；
3. `DATABASE_URL` 必须指向 loopback，或名称以
   `dpms-contract-mysql-` 开头、且不含点号的单标签隔离容器；
4. 数据库名称必须以 `dpms_contract_` 开头；
5. 如需在默认开启 binlog 的 MySQL 8 官方镜像中创建迁移触发器，
   `DPMS_CONTRACT_DATABASE_ADMIN_URL` 必须指向同一已校验 host/port 的
   `mysql` 系统库。准备脚本仅用该一次性管理员连接启用触发器 DDL，随后仍以
   Schema 作用域的 `DATABASE_URL` 执行全部迁移和验证。
6. workflow 在 6380 端口启动的 managed Redis 使用 `/data`、`/tmp` 临时文件系统，
   测试结束无论成功失败都强制移除；它不复用 6379 的通用 retention 测试实例。

这些限制用于防止把测试 Bootstrap 或回滚契约误用于共享、预发布或生产数据库。
该工作流不访问四个外部平台，也不读取真实账号凭据。

本地等价验证应使用一次性 MySQL/Redis 容器，并显式设置上述环境变量；不得把
管理员 URL 指向另一个容器、端口或普通业务 Schema：

```text
python scripts/prepare_contract_database.py
python -m unittest -v core/tests/test_mysql_migration_contract.py core/tests/test_redis_stream_retention_integration.py
```
