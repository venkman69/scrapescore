import logging

import sdnotify

logger = logging.getLogger(__name__)


class SystemdNotifier:
    """Thin wrapper around sdnotify.SystemdNotifier.

    All calls are no-ops when not running under systemd
    (sdnotify checks NOTIFY_SOCKET internally).
    """

    def __init__(self):
        self._notifier = sdnotify.SystemdNotifier()

    def notify_ready(self):
        logger.debug("Sending READY=1 to systemd")
        self._notifier.notify("READY=1")

    def notify_status(self, message: str):
        logger.debug("Sending STATUS=%s to systemd", message)
        self._notifier.notify(f"STATUS={message}")

    def notify_watchdog(self):
        logger.debug("Sending WATCHDOG=1 to systemd")
        self._notifier.notify("WATCHDOG=1")

    def notify_stopping(self):
        logger.debug("Sending STOPPING=1 to systemd")
        self._notifier.notify("STOPPING=1")
