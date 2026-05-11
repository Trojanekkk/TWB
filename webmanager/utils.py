import collections
import datetime
import glob
import json
import os
import signal
import subprocess
import tempfile
import threading

import psutil

from core.botstatus import BotStatus


_CONFIG_LOCK = threading.Lock()
BUILDINGS = ["main", "barracks", "stable", "watchtower", "smith", "garage", "place", "statue", "market", "wood",
             "stone", "iron", "farm", "storage", "hide", "wall", "snob", "church"]
UNITS = ["spear", "sword", "axe", "archer", "spy", "light", "marcher", "heavy", "ram", "catapult", "knight", "snob"]


def _atomic_write_json(path, data):
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, 'w') as tmpfile:
            json.dump(data, tmpfile, indent=2, sort_keys=False)
            tmpfile.flush()
            os.fsync(tmpfile.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


class DataReader:
    @staticmethod
    def _migrate_reporting_to_logging(config):
        if "reporting" not in config:
            return config, False

        migrated_config = collections.OrderedDict()
        for section, value in config.items():
            if section == "reporting":
                if "logging" not in config:
                    migrated_config["logging"] = value
                continue
            migrated_config[section] = value

        return migrated_config, True

    @staticmethod
    def cache_grab(cache_location):
        output = {}
        c_path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "cache",
            cache_location
        )
        if not os.path.isdir(c_path):
            return output
        for existing in os.listdir(c_path):
            existing = str(existing)
            if not existing.endswith(".json"):
                continue
            t_path = os.path.join(os.path.dirname(__file__), "..", "cache", cache_location, existing)
            with open(t_path, 'r') as f:
                try:
                    output[existing.replace('.json', '')] = json.load(f)
                except Exception as e:
                    print("Cache read error for %s: %s. Removing broken entry" % (t_path, str(e)))
                    f.close()
                    os.remove(t_path)

        return output

    @staticmethod
    def template_grab(template_location):
        output = []
        template_location = template_location.replace('.', '/')
        c_path = os.path.join(os.path.dirname(__file__), "..", template_location)
        for existing in os.listdir(c_path):
            existing = str(existing)
            if not existing.endswith(".txt"):
                continue
            output.append(existing.split('.')[0])
        return output

    @staticmethod
    def config_grab():
        config_file_path = os.path.join(os.path.dirname(__file__), "..", "config.json")
        with _CONFIG_LOCK:
            with open(config_file_path, 'r') as f:
                config = json.load(f, object_pairs_hook=collections.OrderedDict)
            config, migrated = DataReader._migrate_reporting_to_logging(config)
            if migrated:
                _atomic_write_json(config_file_path, config)
            return config

    @staticmethod
    def config_set(parameter, value):
        try:
            value = json.loads(value)
        except:
            pass
        config_file_path = os.path.join(os.path.dirname(__file__), "..", "config.json")
        with _CONFIG_LOCK:
            with open(config_file_path, 'r') as config_file:
                template = json.load(config_file, object_pairs_hook=collections.OrderedDict)
            template, _ = DataReader._migrate_reporting_to_logging(template)
            if parameter.startswith("reporting."):
                parameter = parameter.replace("reporting.", "logging.", 1)
            elif parameter == "reporting":
                parameter = "logging"
            if "." in parameter:
                section, param = parameter.split('.')
                template[section][param] = value
            else:
                template[parameter] = value
            _atomic_write_json(config_file_path, template)
            print("Deployed new configuration file")
            return True

    @staticmethod
    def village_config_set(village_id, parameter, value):
        config_file_path = os.path.join(os.path.dirname(__file__), "..", "config.json")
        with _CONFIG_LOCK:
            with open(config_file_path, 'r') as config_file:
                template = json.load(config_file, object_pairs_hook=collections.OrderedDict)
            if village_id not in template['villages']:
                return False
            try:
                template['villages'][str(village_id)][parameter] = json.loads(value)
            except json.decoder.JSONDecodeError:
                template['villages'][str(village_id)][parameter] = value
            _atomic_write_json(config_file_path, template)
            print("Deployed new configuration file")
            return True

    @staticmethod
    def get_session():
        c_path = os.path.join(os.path.dirname(__file__), "..", "cache", "session.json")
        if not os.path.exists(c_path):
            return {"raw": "", "endpoint": "None", "server": "None", "world": "None"}
        with open(c_path, 'r') as session_file:
            session_data = json.load(session_file)
            cookies = []
            for c in session_data['cookies']:
                cookies.append("%s=%s" % (c, session_data['cookies'][c]))
            session_data['raw'] = ';'.join(cookies)
            return session_data

    @staticmethod
    def set_session_cookies(raw):
        cookies = {}
        for item in raw.split(';'):
            item = item.strip()
            if not item or '=' not in item:
                continue
            k, _, v = item.partition('=')
            k = k.strip()
            if k:
                cookies[k] = v.strip()
        if not cookies:
            return False
        c_path = os.path.join(os.path.dirname(__file__), "..", "cache", "session.json")
        with _CONFIG_LOCK:
            if os.path.exists(c_path):
                with open(c_path, 'r') as session_file:
                    session_data = json.load(session_file, object_pairs_hook=collections.OrderedDict)
            else:
                session_data = collections.OrderedDict()
            session_data['cookies'] = cookies
            _atomic_write_json(c_path, session_data)
        return True


class LogReader:
    @staticmethod
    def _root_path():
        return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    @staticmethod
    def _resolve_path(path):
        if os.path.isabs(path):
            return path
        return os.path.join(LogReader._root_path(), path)

    @staticmethod
    def _format_timestamp(value):
        try:
            return datetime.datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError, OSError, OverflowError):
            return value

    @staticmethod
    def _timestamp_value(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            pass

        for fmt in ("%Y-%m-%d %H:%M:%S,%f", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.datetime.strptime(value, fmt).timestamp()
            except (TypeError, ValueError):
                continue
        return 0

    @staticmethod
    def _is_epoch_timestamp(value):
        try:
            float(value)
            return True
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _parse_line(line):
        line = line.strip()
        if not line:
            return None

        if line.startswith("Starting bot at "):
            timestamp = line.replace("Starting bot at ", "", 1)
            return {
                "timestamp": LogReader._format_timestamp(timestamp),
                "sort_ts": LogReader._timestamp_value(timestamp),
                "village_id": "",
                "action": "BOT_START",
                "data": "Starting bot",
                "raw": line,
            }

        parts = line.split(" - ", 3)
        if len(parts) == 4:
            if not LogReader._is_epoch_timestamp(parts[0]):
                return {
                    "timestamp": parts[0].split(",", 1)[0],
                    "sort_ts": LogReader._timestamp_value(parts[0]),
                    "village_id": parts[1],
                    "action": parts[2],
                    "data": parts[3],
                    "raw": line,
                }

            return {
                "timestamp": LogReader._format_timestamp(parts[0]),
                "sort_ts": LogReader._timestamp_value(parts[0]),
                "village_id": parts[1],
                "action": parts[2],
                "data": parts[3],
                "raw": line,
            }

        return {
            "timestamp": "",
            "sort_ts": 0,
            "village_id": "",
            "action": "RAW",
            "data": line,
            "raw": line,
        }

    @staticmethod
    def from_config(config):
        logging_config = config.get("logging", {})
        connection_string = logging_config.get("connection_string", "")
        log_data = {
            "enabled": bool(logging_config.get("enabled", False)),
            "file_logging": connection_string.startswith("file://"),
            "connection_string": connection_string,
            "files": [],
            "entries": [],
        }

        if not log_data["enabled"] or not log_data["file_logging"]:
            return log_data

        log_pattern = connection_string.split("://", 1)[1].replace("{ts}", "*")
        log_pattern = LogReader._resolve_path(log_pattern)
        log_paths = [path for path in glob.glob(log_pattern) if os.path.isfile(path)]
        log_paths.sort(key=os.path.getmtime, reverse=True)

        root_path = LogReader._root_path()
        for log_path in log_paths:
            entries = []
            error = None
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as log_file:
                    for line in log_file:
                        entry = LogReader._parse_line(line)
                        if entry:
                            entries.append(entry)
            except OSError as exc:
                error = str(exc)

            rel_path = os.path.relpath(log_path, root_path)
            file_info = {
                "name": os.path.basename(log_path),
                "path": rel_path,
                "updated_at": LogReader._format_timestamp(os.path.getmtime(log_path)),
                "entries": entries,
                "error": error,
            }
            log_data["files"].append(file_info)
            for entry in entries:
                entry_with_file = dict(entry)
                entry_with_file["file"] = rel_path
                log_data["entries"].append(entry_with_file)

        log_data["entries"].sort(key=lambda entry: entry["sort_ts"], reverse=True)
        return log_data


class BuildingTemplateManager:
    base_path = os.path.join(os.path.dirname(__file__), "..", "templates", "builder")

    @staticmethod
    def template_path(template):
        plain = os.path.basename(template)
        if not plain.endswith(".txt"):
            plain = "%s.txt" % plain
        return os.path.join(BuildingTemplateManager.base_path, plain)

    @staticmethod
    def template_cache_list():
        output = {}
        for existing in os.listdir(BuildingTemplateManager.base_path):
            if not existing.endswith(".txt"):
                continue
            with open(BuildingTemplateManager.template_path(existing), 'r') as template_file:
                output[existing] = BuildingTemplateManager.template_to_dict(
                    [x.strip() for x in template_file.readlines()])
        return output

    @staticmethod
    def template_to_dict(t_list):
        rows = []

        for entry in t_list:
            if entry.startswith('#') or ':' not in entry:
                continue
            building, next_level = entry.split(':', 1)
            rows.append({
                'order': len(rows) + 1,
                'building': building,
                'level': int(next_level),
                'entry': "%s:%d" % (building, int(next_level)),
            })

        return rows

    @staticmethod
    def save_template(template, rows):
        clean_rows = []
        for row in rows:
            building = str(row.get("building", "")).strip()
            if building not in BUILDINGS:
                continue
            try:
                level = int(row.get("level", 0))
            except (TypeError, ValueError):
                continue
            if level <= 0:
                continue
            clean_rows.append("%s:%d" % (building, level))

        path = BuildingTemplateManager.template_path(template)
        with open(path, 'w') as template_file:
            template_file.write("# %s\n" % os.path.basename(path).replace(".txt", ""))
            template_file.write("# Managed from the web UI. Format: building:target_level\n\n")
            template_file.write("\n".join(clean_rows))
            if clean_rows:
                template_file.write("\n")
        return clean_rows


class UnitTemplateTools:
    @staticmethod
    def dict_to_text(data):
        if not data:
            return ""
        parts = []
        for key in sorted(data.keys()):
            parts.append("%s=%s" % (key, data[key]))
        return ", ".join(parts)

    @staticmethod
    def text_to_dict(raw, allowed=None):
        allowed = allowed or UNITS
        output = {}
        for item in str(raw or "").replace("\n", ",").split(","):
            item = item.strip()
            if not item or "=" not in item:
                continue
            key, _, value = item.partition("=")
            key = key.strip()
            if key not in allowed:
                continue
            try:
                amount = int(value.strip())
            except ValueError:
                continue
            if amount > 0:
                output[key] = amount
        return output


class TroopTemplateManager:
    base_path = os.path.join(os.path.dirname(__file__), "..", "templates", "troops")

    @staticmethod
    def template_path(template):
        plain = os.path.basename(template)
        if not plain.endswith(".txt"):
            plain = "%s.txt" % plain
        return os.path.join(TroopTemplateManager.base_path, plain)

    @staticmethod
    def template_cache_list():
        output = {}
        for existing in os.listdir(TroopTemplateManager.base_path):
            if not existing.endswith(".txt"):
                continue
            with open(TroopTemplateManager.template_path(existing), 'r') as template_file:
                try:
                    output[existing] = TroopTemplateManager.template_to_rows(json.load(template_file))
                except Exception:
                    output[existing] = []
        return output

    @staticmethod
    def template_to_rows(template):
        rows = []
        for entry in template or []:
            build = entry.get("build", {})
            farm = []
            for farm_entry in entry.get("farm", []):
                farm.append(UnitTemplateTools.dict_to_text(farm_entry))
            rows.append({
                "order": len(rows) + 1,
                "building": entry.get("building", "barracks"),
                "level": int(entry.get("level", 1)),
                "barracks": UnitTemplateTools.dict_to_text(build.get("barracks", {})),
                "stable": UnitTemplateTools.dict_to_text(build.get("stable", {})),
                "garage": UnitTemplateTools.dict_to_text(build.get("garage", {})),
                "upgrades": UnitTemplateTools.dict_to_text(entry.get("upgrades", {})),
                "farm": "; ".join(farm),
            })
        return rows

    @staticmethod
    def save_template(template, rows):
        output = []
        for row in rows:
            building = str(row.get("building", "barracks")).strip()
            if building not in BUILDINGS:
                continue
            try:
                level = int(row.get("level", 1))
            except (TypeError, ValueError):
                continue
            if level <= 0:
                continue

            entry = {"building": building, "level": level}
            build = {}
            for recruit_building in ["barracks", "stable", "garage"]:
                units = UnitTemplateTools.text_to_dict(row.get(recruit_building, ""))
                if units:
                    build[recruit_building] = units
            if build:
                entry["build"] = build

            upgrades = UnitTemplateTools.text_to_dict(row.get("upgrades", ""))
            if upgrades:
                entry["upgrades"] = upgrades

            farm = []
            for farm_entry in str(row.get("farm", "") or "").split(";"):
                units = UnitTemplateTools.text_to_dict(farm_entry)
                if units:
                    farm.append(units)
            if farm:
                entry["farm"] = farm
            output.append(entry)

        with open(TroopTemplateManager.template_path(template), 'w') as template_file:
            json.dump(output, template_file, indent=2)
            template_file.write("\n")
        return output


class OffensiveTemplateManager:
    base_path = os.path.join(os.path.dirname(__file__), "..", "templates", "offensive")

    @staticmethod
    def template_path(template):
        plain = os.path.basename(template)
        if not plain.endswith(".txt"):
            plain = "%s.txt" % plain
        return os.path.join(OffensiveTemplateManager.base_path, plain)

    @staticmethod
    def template_cache_list():
        output = {}
        for existing in os.listdir(OffensiveTemplateManager.base_path):
            if not existing.endswith(".txt"):
                continue
            with open(OffensiveTemplateManager.template_path(existing), 'r') as template_file:
                try:
                    output[existing] = OffensiveTemplateManager.template_to_rows(json.load(template_file))
                except Exception:
                    output[existing] = {"village": "any", "groups": []}
        return output

    @staticmethod
    def template_to_rows(template):
        rows = []
        for group in template.get("groups", []):
            rows.append({
                "order": len(rows) + 1,
                "units": UnitTemplateTools.dict_to_text(group.get("units", {})),
                "await": bool(group.get("await", False)),
            })
        return {"village": template.get("village", "any"), "groups": rows}

    @staticmethod
    def save_template(template, village, rows):
        output = {"village": village or "any", "groups": []}
        for row in rows:
            units = UnitTemplateTools.text_to_dict(row.get("units", ""))
            if not units:
                continue
            output["groups"].append({
                "units": units,
                "await": bool(row.get("await", False)),
            })
        with open(OffensiveTemplateManager.template_path(template), 'w') as template_file:
            json.dump(output, template_file, indent=2)
            template_file.write("\n")
        return output


class MapBuilder:

    @staticmethod
    def build(villages, current_village=None, size=None):
        out_map = {}
        min_x = 999
        max_x = 0
        min_y = 999
        max_y = 0

        current_location = None
        grid_vils = {}
        extra_data = {}

        for v in villages:
            vdata = villages[v]
            x, y = vdata['location']
            if x < min_x:
                min_x = x
            if x > max_x:
                max_x = x

            if y < min_y:
                min_y = y
            if y > max_y:
                max_y = y
            if current_village and vdata['id'] == current_village:
                current_location = vdata['location']
                extra_data['owner'] = vdata['owner']
                extra_data['tribe'] = vdata['tribe']
            grid_vils["%d:%d" % (x, y)] = vdata

        if current_location and size:
            min_x = current_location[0] - size
            min_y = current_location[1] - size
            max_x = current_location[0] + size
            max_y = current_location[1] + size

        for location_x in range(min_x, max_x):
            if location_x not in out_map:
                out_map[location_x - min_x] = {}
            ylocs = {}
            for location_y in range(min_y, max_y):
                location = "%d:%d" % (location_x, location_y)
                if location in grid_vils:
                    ylocs[location_y - min_y] = grid_vils[location]
                else:
                    ylocs[location_y - min_y] = None
            out_map[location_x - min_x] = ylocs

        return {"grid": out_map, "extra": extra_data}


class BotManager:
    pid = None

    @staticmethod
    def _pid_is_twb(pid):
        try:
            process = psutil.Process(int(pid))
            cmdline = " ".join(process.cmdline())
            return "twb.py" in cmdline
        except (psutil.NoSuchProcess, psutil.AccessDenied, TypeError, ValueError):
            return False

    def is_running(self):
        if not self.pid:
            status = BotStatus.read()
            status_pid = status.get("pid")
            if status.get("running") and self._pid_is_twb(status_pid):
                self.pid = int(status_pid)
                return True
            if status.get("running"):
                BotStatus.mark_stopped(
                    reason="process_exited",
                    message="Bot process exited",
                    pid=status_pid,
                )
            return False
        if self._pid_is_twb(self.pid):
            return True
        self.pid = False
        status = BotStatus.read()
        if status.get("running"):
            BotStatus.mark_stopped(
                reason="process_exited",
                message="Bot process exited",
            )
        return False

    def status(self):
        running = self.is_running()
        status = BotStatus.read()
        if running:
            status["running"] = True
            status["state"] = "active"
            status["pid"] = self.pid
        else:
            status["running"] = False
            status["state"] = "inactive"
        return status

    def start(self):
        if self.is_running():
            return self.status()
        wd = os.path.join(os.path.dirname(__file__), "..")
        proc = subprocess.Popen(["python3", "twb.py"], cwd=wd)
        self.pid = proc.pid
        BotStatus.mark_started(proc.pid)
        print("Bot started successfully")
        return self.status()

    def stop(self):
        if self.is_running():
            pid = self.pid
            os.kill(self.pid, signal.SIGTERM)
            try:
                psutil.Process(self.pid).wait(timeout=10)
            except (psutil.NoSuchProcess, psutil.TimeoutExpired):
                pass
            self.pid = None
            BotStatus.mark_stopped(
                reason="manual_stop",
                message="Bot stopped manually from web interface",
                pid=pid,
            )
            print("Bot stopped successfully")
        else:
            BotStatus.mark_stopped(
                reason="manual_stop",
                message="Bot is already inactive",
            )
        return self.status()

    def restart(self):
        was_running = self.is_running()
        if was_running:
            self.stop()
            self.start()
        return was_running
