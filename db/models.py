"""SQLAlchemy ORM 模型定义"""
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import Integer, String, BigInteger, Text, UniqueConstraint, Index


class Base(DeclarativeBase):
    """SQLAlchemy 声明式基类"""
    pass


class UserBot(Base):
    """用户 Bot 记录"""
    __tablename__ = 'user_bots'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    bot_token: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    bot_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    bot_username: Mapped[str] = mapped_column(String(255), nullable=True)
    bot_firstname: Mapped[str] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(255), default='active')
    created_at: Mapped[str] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[str] = mapped_column(String(255), nullable=True)
    node_id: Mapped[str] = mapped_column(String(255), default='local')
    forward_mode: Mapped[int] = mapped_column(Integer, default=0)  # 0=默认允许, -1=禁止转发, 1=用户自定义
    auto_delete: Mapped[int] = mapped_column(Integer, default=0)   # 0=不删除, >0=延迟N秒自动删除


class FileMapping(Base):
    """文件映射记录"""
    __tablename__ = 'file_mappings'
    __table_args__ = (UniqueConstraint('bot_db_id', 'file_unique_id', name='uq_file_bot_identity'),
                      Index('idx_file_created_bot', 'bot_db_id', 'created_at'),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    bot_username: Mapped[str] = mapped_column(String(255), nullable=True)
    file_type: Mapped[str] = mapped_column(String(255), nullable=False)
    telegram_file_id: Mapped[str] = mapped_column(Text, nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, default=0)
    file_unique_id: Mapped[str] = mapped_column(String(255), nullable=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[str] = mapped_column(String(255), nullable=True)
    is_valid: Mapped[int] = mapped_column(Integer, default=1)
    bot_db_id: Mapped[int] = mapped_column(Integer, nullable=True)
    source_chat_id: Mapped[str] = mapped_column(String(255), nullable=True)
    source_message_id: Mapped[int] = mapped_column(Integer, nullable=True)


class Collection(Base):
    """集合记录"""
    __tablename__ = 'collections'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    bot_username: Mapped[str] = mapped_column(String(255), nullable=True)
    name: Mapped[str] = mapped_column(String(255), default='')
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    file_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(255), default='open')
    created_at: Mapped[str] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[str] = mapped_column(String(255), nullable=True)
    bot_db_id: Mapped[int] = mapped_column(Integer, nullable=True)


class CollectionItem(Base):
    """集合项"""
    __tablename__ = 'collection_items'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    collection_code: Mapped[str] = mapped_column(String(255), nullable=False)
    file_code: Mapped[str] = mapped_column(String(255), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class UserBlacklist(Base):
    """用户黑名单"""
    __tablename__ = 'user_blacklist'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    reason: Mapped[str] = mapped_column(String(255), default='')
    created_at: Mapped[str] = mapped_column(String(255), nullable=True)


class PlatformSetting(Base):
    """平台设置"""
    __tablename__ = 'platform_settings'

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)


class User(Base):
    """用户 VIP 记录"""
    __tablename__ = 'users'

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    vip_level: Mapped[int] = mapped_column(Integer, default=0)
    vip_expire_at: Mapped[str] = mapped_column(String(255), nullable=True)
    created_at: Mapped[str] = mapped_column(String(255), nullable=True)
    # 当前会员是否为「迁移收到」的：1=不能再生成迁移码（一次性规则），
    # 购买/续费/admin 设置新会员时会重置为 0
    vip_migrated: Mapped[int] = mapped_column(Integer, default=0)


class StarPayment(Base):
    """星星支付记录"""
    __tablename__ = 'star_payments'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    vip_level: Mapped[int] = mapped_column(Integer, nullable=False)
    months: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[str] = mapped_column(String(255), nullable=False)
    telegram_charge_id: Mapped[str] = mapped_column(String(255), nullable=True)
    created_at: Mapped[str] = mapped_column(String(255), nullable=True)


class UserBotPref(Base):
    """用户对某个 Bot 的偏好设置"""
    __tablename__ = 'user_bot_prefs'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    bot_db_id: Mapped[int] = mapped_column(Integer, nullable=False)
    forward_protect: Mapped[int] = mapped_column(Integer, default=0)  # 0=不保护(允许转发), 1=保护(禁止转发)


class GiftClaim(Base):
    """礼物领取码（一码一人，先到先得）"""
    __tablename__ = 'gift_claims'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 领取码（URL 友好，唯一），如 claim_abc123xyz
    code: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    gift_id: Mapped[str] = mapped_column(String(255), nullable=False)
    gift_name: Mapped[str] = mapped_column(String(255), nullable=False)
    star_count: Mapped[int] = mapped_column(Integer, nullable=False)
    upgradeable: Mapped[int] = mapped_column(Integer, default=0)  # 0=不可升级, 1=可升级
    # 状态：pending=待领取, claimed=已领取, expired=已失效
    status: Mapped[str] = mapped_column(String(255), default='pending')
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)  # 生成者(管理员) user_id
    claimant_user_id: Mapped[int] = mapped_column(BigInteger, nullable=True)  # 领取者 user_id
    created_at: Mapped[str] = mapped_column(String(255), nullable=True)
    claimed_at: Mapped[str] = mapped_column(String(255), nullable=True)


class VipMigration(Base):
    """VIP 会员迁移码（一次性，有效期=源会员剩余有效期）"""
    __tablename__ = 'vip_migrations'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 迁移码（URL 友好，唯一），如 mig_abc123xyz，用于 /start mig_xxx 深链
    code: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    from_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)  # 迁出方 user_id
    vip_level: Mapped[int] = mapped_column(Integer, default=0)          # 生成时的等级快照（领取时以实际为准）
    vip_expire_at: Mapped[str] = mapped_column(String(255), nullable=True)   # 生成时的到期快照
    # 状态：pending=待领取, used=已使用, cancelled=已作废（生成新码时旧码作废）
    status: Mapped[str] = mapped_column(String(255), default='pending')
    to_user_id: Mapped[int] = mapped_column(BigInteger, nullable=True)     # 领取方 user_id
    created_at: Mapped[str] = mapped_column(String(255), nullable=True)
    used_at: Mapped[str] = mapped_column(String(255), nullable=True)


class WorkerNode(Base):
    """Worker 节点"""
    __tablename__ = 'worker_nodes'

    node_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    node_url: Mapped[str] = mapped_column(String(255), nullable=False)
    webhook_host: Mapped[str] = mapped_column(String(255), default='')
    max_bots: Mapped[int] = mapped_column(Integer, default=100)
    current_bots: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(255), default='offline')
    last_heartbeat: Mapped[str] = mapped_column(String(255), nullable=True)
    created_at: Mapped[str] = mapped_column(String(255), nullable=True)

class SchemaVersion(Base):
    __tablename__ = 'schema_versions'
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    applied_at: Mapped[str] = mapped_column(String(32))


class PaymentOrder(Base):
    __tablename__ = 'payment_orders'
    payload: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    product: Mapped[str] = mapped_column(String(16))
    level: Mapped[int] = mapped_column(Integer, default=0)
    months: Mapped[int] = mapped_column(Integer)
    amount: Mapped[int] = mapped_column(Integer)
    charge_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default='created', index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_retry: Mapped[int] = mapped_column(BigInteger, default=0)
    error: Mapped[str] = mapped_column(Text, default='')
    created_at: Mapped[str] = mapped_column(String(32))
    updated_at: Mapped[str] = mapped_column(String(32))


class MembershipAudit(Base):
    __tablename__ = 'membership_audit'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_id: Mapped[int] = mapped_column(BigInteger)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    action: Mapped[str] = mapped_column(String(64))
    before: Mapped[str] = mapped_column(Text)
    after: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(String(500), default='')
    created_at: Mapped[str] = mapped_column(String(32))


class UpdateInbox(Base):
    __tablename__ = 'update_inbox'
    bot_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    node_id: Mapped[str] = mapped_column(String(255), index=True)
    body: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(16), default='pending', index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[int] = mapped_column(BigInteger, default=0)
    error: Mapped[str] = mapped_column(Text, default='')
    created_at: Mapped[int] = mapped_column(BigInteger)


class SendJob(Base):
    __tablename__ = 'send_jobs'
    task_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    bot_name: Mapped[str] = mapped_column(String(255), index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    body: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(16), default='pending', index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default='')
    created_at: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[int] = mapped_column(BigInteger)


class BotFileCount(Base):
    __tablename__ = 'bot_file_counts'
    bot_db_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    count: Mapped[int] = mapped_column(BigInteger, default=0)


class RuntimeLease(Base):
    __tablename__ = 'runtime_leases'
    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    owner: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[int] = mapped_column(BigInteger, index=True)


class UserNotice(Base):
    __tablename__ = 'user_notices'
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    body: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(16), default='pending', index=True)
    available_at: Mapped[int] = mapped_column(BigInteger, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
