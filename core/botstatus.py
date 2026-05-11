import time

from core.filemanager import FileManager


class BotStatus:
    status_path = "cache/bot_status.json"

    @staticmethod
    def _write(running, state, reason, message="", pid=None):
        FileManager.create_directories(["cache"])
        payload = {
            "running": bool(running),
            "state": state,
            "reason": reason,
            "message": message,
            "pid": pid,
            "updated_at": int(time.time()),
        }
        FileManager.save_json_file(payload, BotStatus.status_path)
        return payload

    @staticmethod
    def read():
        return FileManager.load_json_file(BotStatus.status_path) or {
            "running": False,
            "state": "inactive",
            "reason": "unknown",
            "message": "",
            "pid": None,
            "updated_at": 0,
        }

    @staticmethod
    def mark_started(pid=None):
        return BotStatus._write(
            True,
            "active",
            "started",
            "Bot process is active",
            pid=pid,
        )

    @staticmethod
    def mark_stopped(reason="stopped", message="Bot process is inactive", pid=None):
        return BotStatus._write(
            False,
            "inactive",
            reason,
            message,
            pid=pid,
        )

    @staticmethod
    def mark_bot_protection(method, url, status=None, pid=None):
        return BotStatus.mark_stopped(
            reason="bot_protection",
            message=(
                "Bot protection hit on %s %s%s; bot stopped, restart manually"
                % (
                    method,
                    url,
                    " [%s]" % status if status is not None else "",
                )
            ),
            pid=pid,
        )
