"""Bot API routing shared by live applications and temporary admin clients."""

from telegram import Bot
from telegram.ext import ApplicationBuilder

import config


def _custom_endpoint(token: str) -> str:
    bot_id = token.partition(':')[0]
    selected = config.TELEGRAM_BOT_API_BOT_IDS
    if selected == {'all'} or bot_id in selected:
        return config.TELEGRAM_BOT_API_URL
    return ''


def application_builder(token: str) -> ApplicationBuilder:
    builder = ApplicationBuilder().token(token)
    if endpoint := _custom_endpoint(token):
        builder.base_url(f'{endpoint}/bot').base_file_url(f'{endpoint}/file/bot')
    return builder


def create_bot(token: str) -> Bot:
    if endpoint := _custom_endpoint(token):
        return Bot(token=token, base_url=f'{endpoint}/bot', base_file_url=f'{endpoint}/file/bot')
    return Bot(token=token)
