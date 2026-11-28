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

若分批更新通知功能，先更新负责投递通知的 Master，再更新 Worker；旧 Master 会把新版 Worker 生成的 HTML 通知当作纯文本发送。

回滚程序前应停止所有新进程并恢复升级前的完整数据库备份，不能只把程序切回旧版；旧版不了解新版任务和订单状态。

## 管理面板（Telegram Mini App，只读）

面板挂在与 webhook 相同的 HTTPS 域名下（默认 `https://<WEBHOOK_HOST>/panel/`，`PANEL_PATH` 可改或置空禁用）。反代需把该路径转发到应用端口——现有反代按整域转发时无需改动。

- **进入方式**：`/platform` Bot 列表和 `/setvip`/`/vipadmin` 帮助回复里的「🖥 管理面板」按钮；启动时还会给每位管理员私聊设置菜单按钮（同时完成 Telegram 的 Mini App 域名关联，不设全局默认菜单）。
- **安全模型**：API 用 Telegram `initData` HMAC 验签（Bot Token 参与），且要求发起人 ID 在 `ADMIN_IDS` 内；无凭据、过期（24 小时）或非管理员的请求一律 401。静态资源不含数据。
- **内容**：平台统计、Bot 列表（搜索 + 状态筛选 + 分页，点击 Bot 展开文件码与集合浏览，支持类型筛选和排序）、用户查询（会员信息 + 名下 Bot 分类统计）、兑换码管理与生成/作废、会员设置、付费会员与订单列表。
- **前端结构**：`static/panel/` 下 `index.html` + `styles.css` + `app.js`（Vue 3 全局构建随仓库分发在 `vendor/`，无 CDN、无构建步骤）。
- Worker 角色不挂面板；standalone 轮询模式同样可用。

## 接入自建 Telegram Bot API

先部署 [Telegram Bot API Server](https://github.com/tdlib/telegram-bot-api)，确认 FileID Bot 容器能访问它。用 Docker 部署时可以将两个服务接入同一个私有网络。跨主机部署时给 API 服务配置受控的 HTTPS 入口。

在 `.env` 里配置服务地址与要试用的 Telegram Bot ID：

```dotenv
TELEGRAM_BOT_API_URL=http://telegram-bot-api:8081
TELEGRAM_BOT_API_BOT_IDS=123456789
```

Bot ID 是 Token 冒号前的数字，也可在 `/platform bots` 中看到。填逗号分隔的 ID 可选择多个 Bot，显式填 `all` 会将主 Bot 和所有子 Bot 都指向自建服务。清空两项时使用官方 API。分布式部署时，Master 和可能接管这些 Bot 的 Worker 使用相同的路由配置。Webhook 仍使用对应节点的现有 Webhook 地址，自建服务必须能访问该地址。

已有 Bot 切换前，停止其旧客户端，然后在官方 Bot API 为该 Bot 调用 [`logOut`](https://core.telegram.org/bots/api#logout)。成功后应用新配置并启动。如果之前用 `/stopbot` 停了测试 Bot，应用配置后用 `/startbot` 启动它。测试 `/start`、上传与代码取回。应用只按配置选路，不会自动迁移或临时切回官方 API。调用 `logOut` 后有 10 分钟无法重新登录官方 API，回切时先停用本地客户端并登出自建服务，再恢复官方配置。

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

同等级续费从当前到期日叠加。**升级自动补差价**：购买更高等级时，当前会员的剩余价值（剩余天数 × 实付日均价，按最近一笔同等级星星支付折算，无支付记录按月付价兜底）自动抵扣目标价款；剩余价值 ≥ 目标价时该档位禁购，请管理员用 `/vipadmin days` 补偿或人工处理。**降级禁止**：会员有效期内不能购买更低等级（按钮禁用）。剩余天数向下取整，不足一天舍去。**兑换码**：同等级叠加；升级码从当前到期日续接（剩余时长并入新等级）；有效期内拒兑低等级码（码退回不消耗）。管理员 `/setvip` 与面板设置不走抵扣，替换原有效期。会员中心与发票会说明。永久会员不重复出售相同套餐。月付按30天、年付按360天计算，与历史口径一致。

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


## 统一 API 限流与 429

所有主 Bot 和用户 Bot 的发送及管理 `ExtBot` 请求接入同一个按 Bot 身份共享的调度器；轮询 `getUpdates` 由 Telegram 库自行管理。直接 `send_message`、按钮回复、进度编辑、删除、支付和管理接口也经过入口；Bot 的普通包装器只重试网络错误，不再自己循环处理429。每个 Bot 原本只运行在一个 Worker 上，限流状态在该进程内共享。

- `BOT_MIN_API_INTERVAL` 保留原有 API 最小间隔（默认0.2秒）。
- `API_MESSAGES_PER_SECOND=20` 控制发送消息的加权预算；相册和批量复制/转发按媒体或消息条数计。
- `API_PRIVATE_INTERVAL=1.1`、`API_GROUP_INTERVAL=3.2` 分别控制私聊和群组的逐消息节奏。频道用户名按群组节奏保守处理；应尽量统一使用数字聊天 ID，避免同一聊天的用户名/数字别名形成不同预算。
- `API_EDIT_INTERVAL=2` 控制同一条消息的编辑频率；按钮答复不占私聊发送预算，但仍受 API 总速率和 Bot 冷却约束。
- `API_MAX_INFLIGHT=8` 允许不同聊天的网络请求并行。等待聊天预算时不持有网络并发名额，慢请求也不持有调度锁。
- 遇到429，数字或 `timedelta` 形式的 `retry_after` 都会转成秒，并将完整等待时间发布为此 Bot 的统一冷却。其他 Bot 继续工作。`API_429_RETRIES=2` 限定自动重试次数；超过 `RETRY_AFTER_MAX_WAIT` 时不在请求内等待，但完整冷却仍被保留。预算耗尽的文件任务记录为失败，队列不再叠加一轮429重试。
- 发送队列会跳过聊天自身预算尚未恢复的任务，先发送其他就绪用户的任务。自动删除改用每批最多100条的 `delete_messages`，减少请求数量。

新版 `SEND_MIN_INTERVAL` 默认0，`SEND_BATCH_DELAY` 默认0.05，实际限速交给统一入口。旧 `.env` 中显式设置的值仍生效；如果仍为1或1.5秒，吞吐会继续受额外延迟限制，可以按上述默认值调整。

`/internal/metrics` 的 `api_rate_limits` 提供每 Bot 的请求次数、429次数、累计排队毫秒数、重试耗尽次数、当前冷却和并发数。计数是当前进程生命周期内的数据，不是跨重启的历史统计。配置是保守起点，Telegram 的实际限制可能变化；根据429和排队指标逐步调整，不能承诺零429或固定发送速度。
