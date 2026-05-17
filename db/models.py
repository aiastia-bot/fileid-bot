"""SQLAlchemy ORM 模型定义"""
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import Integer, String


class Base(DeclarativeBase):
    """SQLAlchemy 声明式基类"""
    pass


class UserBot(Base):
    """用户 Bot 记录"""
    __tablename__ = 'user_bots'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False)
    bot_token: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    bot_id: Mapped[int] = mapped_column(Integer, nullable=True)
    bot_username: Mapped[str] = mapped_column(String, nullable=True)
    bot_firstname: Mapped[str] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default='active')
    created_at: Mapped[str] = mapped_column(String, nullable=True)
    updated_at: Mapped[str] = mapped_column(String, nullable=True)
    node_id: Mapped[str] = mapped_column(String, default='local')
    forward_mode: Mapped[int] = mapped_column(Integer, default=0)  # 0=默认允许, -1=禁止转发, 1=用户自定义
    auto_delete: Mapped[int] = mapped_column(Integer, default=0)   # 0=不删除, >0=延迟N秒自动删除


class FileMapping(Base):
    """文件映射记录"""
    __tablename__ = 'file_mappings'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    bot_username: Mapped[str] = mapped_column(String, nullable=True)
    file_type: Mapped[str] = mapped_column(String, nullable=False)
    telegram_file_id: Mapped[str] = mapped_column(String, nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    file_unique_id: Mapped[str] = mapped_column(String, nullable=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=True)
    is_valid: Mapped[int] = mapped_column(Integer, default=1)
    bot_db_id: Mapped[int] = mapped_column(Integer, nullable=True)
    source_chat_id: Mapped[str] = mapped_column(String, nullable=True)
    source_message_id: Mapped[int] = mapped_column(Integer, nullable=True)


class Collection(Base):
    """集合记录"""
    __tablename__ = 'collections'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    bot_username: Mapped[str] = mapped_column(String, nullable=True)
    name: Mapped[str] = mapped_column(String, default='')
    user_id: Mapped[int] = mapped_column(Integer, nullable=True)
    file_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String, default='open')
    created_at: Mapped[str] = mapped_column(String, nullable=True)
    updated_at: Mapped[str] = mapped_column(String, nullable=True)
    bot_db_id: Mapped[int] = mapped_column(Integer, nullable=True)


class CollectionItem(Base):
    """集合项"""
    __tablename__ = 'collection_items'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    collection_code: Mapped[str] = mapped_column(String, nullable=False)
    file_code: Mapped[str] = mapped_column(String, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class UserBlacklist(Base):
    """用户黑名单"""
    __tablename__ = 'user_blacklist'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    reason: Mapped[str] = mapped_column(String, default='')
    created_at: Mapped[str] = mapped_column(String, nullable=True)


class PlatformSetting(Base):
    """平台设置"""
    __tablename__ = 'platform_settings'

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String, nullable=False)


class User(Base):
    """用户 VIP 记录"""
    __tablename__ = 'users'

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    vip_level: Mapped[int] = mapped_column(Integer, default=0)
    vip_expire_at: Mapped[str] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=True)


class StarPayment(Base):
    """星星支付记录"""
    __tablename__ = 'star_payments'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    vip_level: Mapped[int] = mapped_column(Integer, nullable=False)
    months: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[str] = mapped_column(String, nullable=False)
    telegram_charge_id: Mapped[str] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=True)


class UserBotPref(Base):
    """用户对某个 Bot 的偏好设置"""
    __tablename__ = 'user_bot_prefs'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    bot_db_id: Mapped[int] = mapped_column(Integer, nullable=False)
    forward_protect: Mapped[int] = mapped_column(Integer, default=0)  # 0=不保护(允许转发), 1=保护(禁止转发)


class WorkerNode(Base):
    """Worker 节点"""
    __tablename__ = 'worker_nodes'

    node_id: Mapped[str] = mapped_column(String, primary_key=True)
    node_url: Mapped[str] = mapped_column(String, nullable=False)
    webhook_host: Mapped[str] = mapped_column(String, default='')
    max_bots: Mapped[int] = mapped_column(Integer, default=100)
    current_bots: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String, default='offline')
    last_heartbeat: Mapped[str] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=True)