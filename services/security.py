import hmac
from config import WORKER_SECRET, WEBHOOK_SECRET


def internal_authorized(request):
    return bool(WORKER_SECRET) and hmac.compare_digest(
        request.headers.get("X-Worker-Secret", ""), WORKER_SECRET
    )


def webhook_authorized(request):
    return bool(WEBHOOK_SECRET) and hmac.compare_digest(
        request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), WEBHOOK_SECRET
    )
