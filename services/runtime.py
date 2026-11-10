"""Process services configured by the entry point, without importing __main__."""

manager = None
scheduler = None
master_app = None


def configure(*, bot_manager=None, bot_scheduler=None, application=None):
    global manager, scheduler, master_app
    manager, scheduler, master_app = bot_manager, bot_scheduler, application
