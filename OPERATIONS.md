# 升级与运维

本次修改分三阶段：安全和支付、消息与任务恢复、性能和运维。三阶段代码在同一版本交付，应一起升级。不会自动连接你的生产 Telegram、执行容器更新或迁移生产数据库。

## 升级步骤

1. 停止旧进程。不要让旧代码与新版同时处理同一 Bot。
2. 备份 `.env` 与数据库。SQLite 使用 `python scripts/backup.py data/backups/before-upgrade.db`，脚本采用 SQLite backup API，并在快照上执行完整性检查；不会覆盖已有文件。
3. Webhook 模式配置 `WEBHOOK_SECRET`（16–256 位，字母、数字、`_`、`-`）。Master/Worker 必须配置 `WORKER_SECRET`；Worker 还需设置 `WORKER_URL`、`MASTER_URL`、`WORKER_WEBHOOK_HOST`。先修改反代，禁止公网访问 `/internal/`。
4. 本地部署执行 `pip install -r requirements.lock`，或 `docker compose up -d --build`。Compose 现在构建本地源码，默认将 HTTP 端口绑定 `127.0.0.1`。反代不在同一主机时，显式设置 `BIND_HOST` 为私网地址，并配置防火墙。
5. 启动时自动执行 `schema_versions` 记录的版本迁移。保留所有历史支付记录及公开文件代码；重复文件身份只保留一条，其余记录的 `file_unique_id` 置空，旧代码仍可使用。首次升级大表需安排维护窗口。
6. 检查 `/health`（进程存活）和 `/ready`（数据库可达、初始化完成），在测试 Bot 验证上传、取回、集合、停止发送、会员到期与恢复。上线后先用测试账号小额核对 Stars 支付。

MySQL 跨机器部署必须共享同一个数据库。迁移账号需要建表、改表、建索引、建触发器的权限。新模型明确了字符串长度，并将 Telegram ID/文件大小改为 BIGINT；SQLite 保留整数的原有兼容性。**不要把同一个 SQLite 文件放在网络文件系统上供多机写入。**

回滚程序前应停止所有新进程并恢复升级前的完整数据库备份，不能只把程序切回旧版；旧版不了解新版任务和订单状态。

## 会员配置

仅管理员可调用：

```text
/setplan
/setplan 2
/setplan 2 max_bots 5 monthly_price 200 yearly_price 1800
/setplan 2 file_limit 100000 forward_mode true auto_delete true
/setplan 2 enabled false
/setplan 2 name 专业版
```

支持等级为 `0` 免费、`4` 基础、`1/2/3` VIP。字段：`name`、`max_bots`、`file_limit`、月价、年价、转发保护、自动删除、是否销售。功能开关接受 `true/false`；文件额度 `0` 表示不限；Bot 数 `0` 表示不能运行。名称使用无空格的短名称。

价格调整对新订单生效，已生成订单在24小时有效期内保留原价。权益和额度调整即时应用于当前会员；降低额度会暂停超额 Bot，提高后只恢复因会员额度暂停的 Bot，不会恢复管理员停止、封禁、冻结或 Token 失效的 Bot。禁售仅阻止新建订单，不取消已支付权益。

同等级续费从当前到期日叠加；换等级按新购时长从购买日起计算，不将低价套餐的剩余时长直接转成高价权益。会员中心和发票会说明。管理员可以补偿剩余天数。永久会员不重复出售相同套餐。月付按30天、年付按360天计算，与历史口径一致。

```text
/setvip 123456789 2 3 客服补偿
/setvip 123456789 3 0 永久会员
/setvip 123456789 0
/setvip info 123456789
/setvip list
/vipadmin days 123456789 7 故障补偿
/vipadmin days 123456789 -3 纠正误加
/vipadmin expires 123456789 2027-01-31 人工约定
/vipadmin history 123456789
```

`/setvip` 是替换等级和有效期，默认12个月，0个月为永久；`days` 在原到期日上加减。每次套餐修改和人工会员修改写入审计表，包含操作者、前后状态和原因。基础版到期保留等级身份但运行额度为0；VIP到期回免费额度。到期提醒按“用户+到期时间”去重并持久化重试。

`/setfree files/basicfiles` 仍可使用，与 `/setplan ... file_limit` 使用同一个实际限额。`FILE_QUOTA_POLICY=retain` 保留旧行为；改为 `reject` 才会拒绝超限新增。不会主动删除用户文件。

## 订单与失败任务

```text
/vipadmin orders
/vipadmin orders 123456789
/vipadmin retry order_...
/vipadmin resolve order_... completed 已核对Telegram确认发放
/vipadmin resolve order_... refunded 已核对Telegram确认退款
/tasks
```

订单状态包含 `created`、`received`、`verifying`、`retry`、`sending`、`completed`、`refund_pending`、`refunded`、`review`。重放相同交易不会再次增加会员或再次赠送 Premium。旧版已经完成的支付账本保留；升级瞬间已付款的旧发票回调可以导入新版订单，仍需要官方交易核验；未付款的旧发票需重新购买。

核验检查付款人、订单 payload、金额、交易类型，并按页查询官方 Stars 账本；临时失败会指数退避，8次失败后转人工核查。VIP 入账与发放在同一事务内完成。Premium 发放是外部消费：超时或中断意味着结果不确定，进入 `review`，不会盲目重发或自动退款。明确的 API 拒绝可退款；通知发送失败不会触发退款。`resolve` **只登记人工对账结果**，不会执行发放或退款；必要的外部动作需要管理员自行核对和处理。

发送任务以数据库为准，不依赖 Redis 才能恢复。旧 Redis 列表逐条写入数据库后再移除；数据库写入失败时不会先清空旧列表。Bot 启动会主动恢复待发送任务，不需要用户发送新消息。每用户和每 Bot 的队列有上限，触发限流有重试预算，部分失败进入失败记录，避免反复重发整个批次。`/queue` 查看正在发送和等待数量，`/stop` 取消用户自己的任务。

Webhook 请求在更新持久化后返回200，`(bot_key, update_id)` 去重；处理失败会重试，超过预算保留为失败。Worker 切换会转移待处理更新；数据库运行租约阻止两个进程同时托管同一个 Bot。**消息发送无法提供跨 Telegram 和数据库的严格仅一次保证：若 Telegram 已发送但进程在记录确认前退出，恢复时可能重复。部分失败和结果不确定的业务需要核对，不能直接全量补发。** Polling 的上游接收仍使用 Telegram 库的轮询机制；需要更新持久化确认时使用 Webhook。

## 监控、清理与备份

- `/health` 是存活检查，`/ready` 是就绪检查，两种接收模式均提供。
- `/internal/metrics` 需 `X-Worker-Secret`，返回消息、发送和订单的各状态计数。单机如需读取指标也要配置 `WORKER_SECRET`。建议监控失败任务、`review` 订单、待处理量持续增长和 readiness 失败。
- 成功或取消任务按 `DATA_RETENTION_DAYS` 分批清理；失败任务、支付账本和审计不会自动删除。安排数据库磁盘和备份空间监控。
- SQLite 建议每日运行备份脚本，保留异机副本。MySQL 使用受保护的 defaults 配置文件配合 `mysqldump --single-transaction --routines --triggers`；不要把密码直接放在命令参数或日志中。定期在隔离实例恢复备份，并验证文件/集合记录、会员到期日和订单数量。
- Docker 常规版及保护版构建前都运行测试；保护版构建包含新增 `services` 包。依赖固定在 `requirements.lock`，更新依赖须重新跑测试。

本次本地验证包含 SQLite 迁移、备份恢复、HTTP路由、支付和任务状态测试；本机 Docker daemon 未启动，容器、真实 MySQL 和真实 Telegram 发放需要在部署环境继续验证。CI 另外提供 MySQL/Redis 集成测试，避免将编译通过误当作数据库联调通过。
