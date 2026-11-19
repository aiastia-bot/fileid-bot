"""Bounded housekeeping. Failed tasks, orders and audits remain for inspection."""

import logging
import time
from sqlalchemy import delete
from config import DATA_RETENTION_DAYS
from db.core import get_session
from db.models import UpdateInbox, SendJob

logger = logging.getLogger(__name__)

CLEANUP_BATCH = 5000  # 单批删除上限：控制单次写锁时长


async def cleanup_records() -> int:
    """删除超过保留期（DATA_RETENTION_DAYS 天）的终态行，返回删除总数。

    循环分批清到无剩余、每批单独提交以限制写锁时长。旧实现单日上限
    1000 行，追不上日增量，update_inbox 曾因此积到 9.6 万行
    （2026-11-19 CPU 100% 事故的燃料）。failed 行保留供排查，不在此清理。
    """
    cutoff = int(time.time()) - DATA_RETENTION_DAYS * 86400
    removed = 0
    async with get_session() as session:
        for model, key, age, terminal in (
            (
                UpdateInbox,
                (UpdateInbox.bot_key, UpdateInbox.update_id),
                UpdateInbox.created_at,
                ["completed"],
            ),
            (
                SendJob,
                (SendJob.task_id,),
                SendJob.updated_at,
                ["completed", "cancelled"],
            ),
        ):
            from sqlalchemy import select, tuple_

            while True:
                rows = (
                    await session.execute(
                        select(*key)
                        .where(age < cutoff, model.state.in_(terminal))
                        .limit(CLEANUP_BATCH)
                    )
                ).all()
                if not rows:
                    break
                await session.execute(delete(model).where(tuple_(*key).in_(rows)))
                await session.commit()
                removed += len(rows)
    if removed:
        logger.info(
            "历史清理: 删除 %s 条超期终态行 (保留 %s 天)", removed, DATA_RETENTION_DAYS
        )
    return removed


async def auto_purge_dead_bots() -> int:
    """死亡超过宽限期的 Bot 自动清理文件记录并移出备份组

    覆盖 revoked（Token 被撤销）/ deleted（主人删除）/ banned（被封禁）；
    admin_stopped / paused 属临时状态不清理。宽限期内主人 /updatetoken
    或恢复 Bot 会改回 active，自然跳过。返回清理的 Bot 数。
    """
    from config import AUTO_PURGE_DEAD_BOT_DAYS, ADMIN_IDS
    if AUTO_PURGE_DEAD_BOT_DAYS <= 0:
        return 0
    from db import (get_dead_bots_past_grace, purge_bot_files,
                    remove_bot_from_backup, get_platform_setting,
                    set_platform_setting)
    from services.notices import enqueue

    # 部署基线：首次启用时记录当前时间，只清理「上线之后」才死亡的 Bot，
    # 部署前就已死亡的存量 Bot 一律不动（留给管理员 /purge 手动处理）
    baseline = await get_platform_setting('auto_purge_baseline')
    if not baseline:
        from datetime import datetime
        baseline = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        await set_platform_setting('auto_purge_baseline', baseline)
        logger.info("死亡 Bot 自动清理基线已设定: %s", baseline)
        return 0

    purged = 0
    for bot in await get_dead_bots_past_grace(AUTO_PURGE_DEAD_BOT_DAYS,
                                              not_before=baseline):
        bot_db_id = bot['id']
        username = bot.get('bot_username') or str(bot_db_id)
        owner_id = bot.get('owner_id')
        try:
            result = await purge_bot_files(bot_db_id)
            await remove_bot_from_backup(bot_db_id)
        except Exception:
            logger.exception("自动清理 Bot %d (@%s) 失败", bot_db_id, username)
            continue
        purged += 1
        body = (
            f"🧹 Bot @{username} 已失效超过 {AUTO_PURGE_DEAD_BOT_DAYS} 天，"
            f"文件记录已自动清理（{result['deleted_files']} 条），并已移出备份组。\n"
            "如需继续使用：更新 Token 后重新上传/回填文件。"
        )
        if owner_id:
            await enqueue(f'autopurge:{bot_db_id}:{result["deleted_files"]}',
                          owner_id, body)
        for admin_id in ADMIN_IDS:
            await enqueue(f'autopurge_admin:{bot_db_id}:{admin_id}',
                          admin_id, f"🧹 自动清理：@{username} (主人 {owner_id}) "
                          f"文件记录 {result['deleted_files']} 条")
        logger.info("自动清理死亡 Bot @%s (db_id=%s): %s", username, bot_db_id, result)
    return purged
