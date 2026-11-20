"""Reconcile only membership-paused bots; never restart admin-stopped bots."""

import logging
from db.bots import get_user_bot_by_id, get_user_bots_by_owner, update_user_bot_status
from db.vip import get_max_bots_for_user
from services import runtime

logger = logging.getLogger(__name__)


async def reconcile_user(user_id):
    async with runtime.operation_lock(('reconcile-member', user_id)):
        await _reconcile_user(user_id)


async def _reconcile_user(user_id):
    maximum = await get_max_bots_for_user(user_id)
    bots = await get_user_bots_by_owner(user_id)
    active = [b for b in bots if b["status"] == "active"]
    for bot in active[maximum:]:
        bot_id = bot['id']
        async with runtime.operation_lock(('bot', bot_id)):
            current = await get_user_bot_by_id(bot_id)
            if not current or current['status'] != 'active' or current['owner_id'] != user_id:
                continue
            if runtime.scheduler:
                if not await runtime.scheduler.stop_bot_on_worker(bot_id):
                    logger.error("Cannot pause remote bot %s; retrying during reconciliation", bot_id)
                    continue
            elif runtime.manager:
                await runtime.manager.stop_bot(bot_id)
            await update_user_bot_status(bot_id, "paused")
    active = [
        b for b in await get_user_bots_by_owner(user_id) if b["status"] == "active"
    ]
    available = max(0, maximum - len(active))
    for bot in [b for b in bots if b["status"] == "paused"][:available]:
        # A failed start must leave membership-paused state intact.
        bot_id = bot['id']
        async with runtime.operation_lock(('bot', bot_id)):
            current = await get_user_bot_by_id(bot_id)
            if not current or current['status'] != 'paused' or current['owner_id'] != user_id:
                continue
            success = False
            if runtime.scheduler:
                success = bool(await runtime.scheduler.assign_bot_to_worker(current))
            elif runtime.manager:
                success = await runtime.manager.start_bot(current)
            if success:
                await update_user_bot_status(bot_id, "active")


async def reconcile_all():
    from db.bots import get_all_owner_ids

    for user_id in await get_all_owner_ids():
        try:
            await reconcile_user(user_id)
        except Exception:
            logger.exception("Membership reconciliation failed: %s", user_id)
