"""Reconcile only membership-paused bots; never restart admin-stopped bots."""

import logging
from db.bots import get_user_bots_by_owner, update_user_bot_status
from db.vip import get_max_bots_for_user
from services import runtime

logger = logging.getLogger(__name__)


async def reconcile_user(user_id):
    maximum = await get_max_bots_for_user(user_id)
    bots = await get_user_bots_by_owner(user_id)
    active = [b for b in bots if b["status"] == "active"]
    for bot in active[maximum:]:
        if runtime.scheduler:
            if not await runtime.scheduler.stop_bot_on_worker(bot["id"]):
                logger.error(
                    "Cannot pause remote bot %s; retrying during reconciliation",
                    bot["id"],
                )
                continue
        if runtime.manager and not runtime.scheduler:
            await runtime.manager.stop_bot(bot["id"])
        await update_user_bot_status(bot["id"], "paused")
    active = [
        b for b in await get_user_bots_by_owner(user_id) if b["status"] == "active"
    ]
    available = max(0, maximum - len(active))
    for bot in [b for b in bots if b["status"] == "paused"][:available]:
        # A failed start must leave membership-paused state intact.
        success = False
        if runtime.scheduler:
            success = bool(await runtime.scheduler.assign_bot_to_worker(bot))
        elif runtime.manager:
            success = await runtime.manager.start_bot(bot)
        if success:
            await update_user_bot_status(bot["id"], "active")


async def reconcile_all():
    from db.bots import get_all_owner_ids

    for user_id in await get_all_owner_ids():
        try:
            await reconcile_user(user_id)
        except Exception:
            logger.exception("Membership reconciliation failed: %s", user_id)
