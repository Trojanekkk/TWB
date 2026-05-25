import datetime
import json
import logging
import os
import re
import time

from core.filemanager import FileManager


class GarbageCollector:
    """
    Removes stale runtime artifacts from cache directories.

    Known cache files often carry the event timestamp in their filename or JSON
    payload. Use that first so copied/imported cache keeps its original age;
    fall back to filesystem mtime for generic files.
    """

    timestamp_keys = {
        "when",
        "timestamp",
        "last_attack",
        "last_scout",
        "latest_report_at",
        "last_report_at",
        "first_report_at",
        "last_seen",
        "last_run",
    }

    min_epoch = 946684800  # 2000-01-01

    def __init__(
            self,
            retention_days=14,
            roots=None,
            enabled=True,
            active_paths=None,
            now=None,
    ):
        self.retention_days = int(retention_days or 14)
        self.roots = roots or ["cache"]
        self.enabled = enabled
        self.active_paths = {
            os.path.abspath(path)
            for path in (active_paths or [])
            if path
        }
        self.now = int(now or time.time())
        self.logger = logging.getLogger("GarbageCollector")

    @classmethod
    def from_config(cls, config, active_paths=None):
        config = config or {}
        bot_config = (config or {}).get("bot", {})
        roots = bot_config.get("cache_gc_roots", ["cache"])
        if isinstance(roots, str):
            roots = [roots]
        roots = list(roots)

        logging_config = config.get("logging", {})
        connection_string = logging_config.get("connection_string")
        if connection_string and connection_string.startswith("file://"):
            log_path = connection_string.split("://", 1)[1]
            log_path = log_path.replace("{ts}", str(int(time.time())))
            log_dir = os.path.dirname(log_path)
            if log_dir and log_dir not in roots:
                roots.append(log_dir)

        return cls(
            retention_days=bot_config.get("cache_retention_days", 14),
            roots=roots,
            enabled=bot_config.get("cache_gc_enabled", True),
            active_paths=active_paths,
        )

    def collect(self):
        if not self.enabled or self.retention_days <= 0:
            return {"deleted": 0, "errors": 0, "bytes": 0}

        cutoff = self.now - self.retention_days * 86400
        deleted = 0
        errors = 0
        deleted_bytes = 0

        for root in self.roots:
            root_path = FileManager.get_path(root)
            if not os.path.isdir(root_path):
                continue

            for directory, _, files in os.walk(root_path):
                for filename in files:
                    path = os.path.join(directory, filename)
                    if os.path.abspath(path) in self.active_paths:
                        continue

                    try:
                        file_time = self.file_time(path)
                        if file_time is None or file_time >= cutoff:
                            continue

                        deleted_bytes += os.path.getsize(path)
                        os.remove(path)
                        deleted += 1
                    except OSError:
                        errors += 1
                        self.logger.exception("Unable to remove stale file %s", path)

        if deleted or errors:
            self.logger.info(
                "Garbage collection finished: removed %d files (%d bytes), errors=%d",
                deleted,
                deleted_bytes,
                errors,
            )

        return {"deleted": deleted, "errors": errors, "bytes": deleted_bytes}

    def file_time(self, path):
        filename_time = self.time_from_filename(os.path.basename(path))
        if filename_time:
            return filename_time

        json_time = self.time_from_json(path)
        if json_time:
            return json_time

        return int(os.path.getmtime(path))

    def time_from_filename(self, filename):
        date_match = re.search(r"(\d{4}-\d{2}-\d{2})", filename)
        if date_match:
            try:
                value = datetime.datetime.strptime(
                    date_match.group(1),
                    "%Y-%m-%d",
                )
                return int(value.replace(tzinfo=datetime.timezone.utc).timestamp())
            except ValueError:
                pass

        epoch_match = (
            re.search(r"^twb_(\d{10,13})", filename)
            or re.search(r"^(\d{10,13})_[0-9a-fA-F]{8}\.json$", filename)
        )
        if not epoch_match:
            return None

        value = int(epoch_match.group(1))
        if value > 9999999999:
            value = int(value / 1000)
        if self.valid_epoch(value):
            return value
        return None

    def time_from_json(self, path):
        if not path.endswith(".json"):
            return None

        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None

        values = []
        self.collect_json_times(data, values)
        return max(values) if values else None

    def collect_json_times(self, value, output):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in self.timestamp_keys:
                    ts = self.normalize_epoch(item)
                    if ts:
                        output.append(ts)
                if isinstance(item, (dict, list)):
                    self.collect_json_times(item, output)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, (dict, list)):
                    self.collect_json_times(item, output)

    def normalize_epoch(self, value):
        try:
            value = int(float(value))
        except (TypeError, ValueError):
            return None
        if value > 9999999999:
            value = int(value / 1000)
        if self.valid_epoch(value):
            return value
        return None

    def valid_epoch(self, value):
        return self.min_epoch <= value <= self.now + 86400
