import hashlib
import json
import logging
import os
import sys
import time

from game.attack import AttackCache
from game.reports import ReportCache
from game.simulator import Simulator
from core.filemanager import FileManager


class VillageManager:
    @staticmethod
    def report_loot_total(extra):
        total = 0
        for amount in extra.get("loot", {}).values():
            total += int(amount)
        return total

    @staticmethod
    def report_capacity(extra):
        capacity = 0
        for unit, amount in extra.get("units_sent", {}).items():
            unit_data = Simulator.pool.get(unit)
            if not unit_data:
                continue
            capacity += int(amount) * int(unit_data.get("load", 0))
        return capacity

    @staticmethod
    def report_fill_rate(report_loot, report_capacity):
        if report_capacity <= 0:
            return 0
        return min(1.0, report_loot / report_capacity)

    @staticmethod
    def report_unit_total(extra, key):
        total = 0
        for amount in extra.get(key, {}).values():
            total += int(amount)
        return total

    @staticmethod
    def _farm_stats_bucket():
        return {
            "reports": 0,
            "loot": 0,
            "capacity": 0,
            "filled_capacity": 0,
            "sent_units": 0,
            "lost_units": 0,
        }

    @staticmethod
    def _median(values):
        if not values:
            return 0
        ordered = sorted(values)
        middle = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[middle]
        return (ordered[middle - 1] + ordered[middle]) / 2

    @staticmethod
    def _finalize_farm_stats_bucket(bucket):
        reports = bucket["reports"]
        capacity = bucket["capacity"]
        sent_units = bucket["sent_units"]
        bucket["avg_loot"] = round(bucket["loot"] / reports, 2) if reports else 0
        bucket["fill_rate"] = (
            round(bucket["filled_capacity"] / capacity, 4)
            if capacity > 0 else 0
        )
        bucket["loss_percentage"] = (
            round(bucket["lost_units"] / sent_units * 100, 2)
            if sent_units > 0 else 0
        )
        return bucket

    @staticmethod
    def _hash_file(path):
        if not os.path.exists(path):
            return None
        digest = hashlib.sha1()
        with open(path, "rb") as source:
            for chunk in iter(lambda: source.read(8192), b""):
                digest.update(chunk)
        return digest.hexdigest()[:12]

    @staticmethod
    def build_config_profile(config):
        farms = config.get("farms", {})
        bot = config.get("bot", {})
        map_config = config.get("map", {})
        world = config.get("world", {})
        villages = config.get("villages", {})

        farm_keys = [
            "farm",
            "min_points",
            "max_points",
            "find_player_owned",
            "search_radius",
            "target_cache_max_age_hours",
            "default_away_time",
            "full_loot_away_time",
            "low_loot_away_time",
            "priority_ratio",
            "farm_exploration_ratio",
            "farm_exploration_min_targets",
            "max_farms",
            "attack_higher_points",
            "force_scout_if_available",
            "farm_scout_amount",
            "night_bonus_start_hour",
            "night_bonus_end_hour",
            "night_bonus_troop_multiplier",
        ]
        bot_keys = [
            "active_delay",
            "inactive_delay",
            "village_delay_min",
            "village_delay_max",
            "delay_factor",
            "attack_delay_factor",
        ]
        world_keys = ["game_speed", "unit_speed"]
        map_keys = [
            "discovery_enabled",
            "discovery_radius",
            "discovery_step",
            "max_discovery_requests_per_run",
            "discovery_refresh_hours",
            "fetch_delay_hours",
        ]
        village_keys = [
            "managed",
            "units",
            "building",
            "scout_first",
            "additional_farms",
            "gather_enabled",
            "gather_selection",
            "advanced_gather",
        ]

        village_profiles = {}
        template_names = set()
        default_units = config.get("units", {}).get("default")
        if default_units:
            template_names.add(default_units)

        village_template = config.get("village_template", {})
        for key in ["units", "building"]:
            if village_template.get(key):
                template_names.add(village_template[key])

        for village_id, village_config in sorted(villages.items()):
            entry = {}
            for key in village_keys:
                value = village_config.get(key)
                if key == "additional_farms":
                    entry["additional_farms_count"] = len(value or [])
                    entry["additional_farms"] = sorted(str(item) for item in (value or []))
                    continue
                entry[key] = value
                if key in ["units", "building"] and value:
                    template_names.add(value)
            village_profiles[str(village_id)] = entry

        template_hashes = {}
        for name in sorted(template_names):
            hashes = {}
            troop_hash = VillageManager._hash_file(f"templates/troops/{name}.txt")
            builder_hash = VillageManager._hash_file(f"templates/builder/{name}.txt")
            if troop_hash:
                hashes["troops"] = troop_hash
            if builder_hash:
                hashes["builder"] = builder_hash
            if hashes:
                template_hashes[name] = hashes

        profile_payload = {
            "farms": {key: farms.get(key) for key in farm_keys if key in farms},
            "bot": {key: bot.get(key) for key in bot_keys if key in bot},
            "map": {key: map_config.get(key) for key in map_keys if key in map_config},
            "world": {key: world.get(key) for key in world_keys if key in world},
            "villages": village_profiles,
            "template_hashes": template_hashes,
        }
        profile_id = hashlib.sha1(
            json.dumps(profile_payload, sort_keys=True).encode("utf-8")
        ).hexdigest()[:12]
        profile_payload["profile_id"] = profile_id
        return profile_payload

    @staticmethod
    def _farm_window_bucket():
        bucket = VillageManager._farm_stats_bucket()
        bucket.update({
            "scouts": 0,
            "active_targets": set(),
            "new_targets": set(),
        })
        return bucket

    @staticmethod
    def _add_report_to_window(bucket, report, is_attack, is_scout, first_attack_at, window_start):
        extra = report.get("extra", {})
        report_loot = VillageManager.report_loot_total(extra) if is_attack else 0
        report_capacity = VillageManager.report_capacity(extra) if is_attack else 0
        sent_units = VillageManager.report_unit_total(extra, "units_sent")
        lost_units = VillageManager.report_unit_total(extra, "units_losses")

        bucket["scouts"] += 1 if is_scout else 0
        if not is_attack:
            bucket["sent_units"] += sent_units
            bucket["lost_units"] += lost_units
            return

        bucket["reports"] += 1
        target = str(report.get("dest") or "unknown")
        bucket["active_targets"].add(target)
        if first_attack_at and first_attack_at >= window_start:
            bucket["new_targets"].add(target)
        bucket["loot"] += report_loot
        bucket["capacity"] += report_capacity
        bucket["filled_capacity"] += min(report_loot, report_capacity)
        bucket["sent_units"] += sent_units
        bucket["lost_units"] += lost_units

    @staticmethod
    def _finalize_farm_window(bucket, seconds):
        active_targets = bucket.pop("active_targets", set())
        new_targets = bucket.pop("new_targets", set())
        bucket = VillageManager._finalize_farm_stats_bucket(bucket)
        hours = seconds / 3600
        bucket["active_targets"] = len(active_targets)
        bucket["new_targets"] = len(new_targets)
        bucket["loot_per_hour"] = round(bucket["loot"] / hours, 2) if hours else 0
        bucket["attacks_per_hour"] = round(bucket["reports"] / hours, 2) if hours else 0
        bucket["scouts_per_hour"] = round(bucket["scouts"] / hours, 2) if hours else 0
        return bucket

    @staticmethod
    def build_recent_farm_windows(reports, reference_time):
        report_rows = []
        first_attack_by_target = {}
        latest_report_at = 0

        for report in reports.values():
            extra = report.get("extra", {})
            when = int(extra.get("when", 0) or 0)
            if not when:
                continue
            latest_report_at = max(latest_report_at, when)
            report_type = report.get("type")
            is_attack = report_type == "attack"
            is_scout = report_type == "scout"
            if not is_attack and not is_scout:
                continue
            if is_attack:
                target = str(report.get("dest") or "unknown")
                first_attack_by_target[target] = min(
                    first_attack_by_target.get(target, when),
                    when,
                )
            report_rows.append((when, report, is_attack, is_scout))

        reference_time = max(reference_time, latest_report_at)
        windows = {}
        for label, seconds in {"6h": 21600, "24h": 86400, "72h": 259200}.items():
            window_start = reference_time - seconds
            total_bucket = VillageManager._farm_window_bucket()
            source_buckets = {}
            for when, report, is_attack, is_scout in report_rows:
                if when < window_start or when > reference_time:
                    continue
                origin = str(report.get("origin") or "unknown")
                first_attack_at = first_attack_by_target.get(str(report.get("dest") or "unknown"))
                VillageManager._add_report_to_window(
                    total_bucket, report, is_attack, is_scout, first_attack_at, window_start
                )
                VillageManager._add_report_to_window(
                    source_buckets.setdefault(origin, VillageManager._farm_window_bucket()),
                    report,
                    is_attack,
                    is_scout,
                    first_attack_at,
                    window_start,
                )

            windows[label] = {
                "seconds": seconds,
                "totals": VillageManager._finalize_farm_window(total_bucket, seconds),
                "sources": {
                    source: VillageManager._finalize_farm_window(bucket, seconds)
                    for source, bucket in sorted(source_buckets.items())
                },
            }

        return {
            "reference_time": reference_time,
            "windows": windows,
        }

    @staticmethod
    def _load_latest_farm_stats_snapshot():
        stats_dir = "cache/farm_stats"
        if not os.path.isdir(stats_dir):
            return None

        latest = None
        for filename in os.listdir(stats_dir):
            if not filename.endswith(".jsonl"):
                continue
            path = os.path.join(stats_dir, filename)
            try:
                with open(path, "r", encoding="utf-8") as stats_file:
                    for line in stats_file:
                        line = line.strip()
                        if not line:
                            continue
                        snapshot = json.loads(line)
                        if latest is None or int(snapshot.get("timestamp", 0) or 0) > int(latest.get("timestamp", 0) or 0):
                            latest = snapshot
            except (OSError, ValueError):
                continue
        return latest

    @staticmethod
    def _diff_farm_stats_bucket(current, previous):
        bucket = VillageManager._farm_stats_bucket()
        for key in bucket:
            bucket[key] = max(
                0,
                int(current.get(key, 0) or 0) - int(previous.get(key, 0) or 0),
            )
        return VillageManager._finalize_farm_stats_bucket(bucket)

    @staticmethod
    def build_snapshot_delta(previous, current):
        if not previous:
            return None
        previous_timestamp = int(previous.get("timestamp", 0) or 0)
        current_timestamp = int(current.get("timestamp", 0) or 0)
        if previous_timestamp <= 0 or current_timestamp <= previous_timestamp:
            return None

        delta_seconds = current_timestamp - previous_timestamp
        delta_hours = delta_seconds / 3600
        totals = VillageManager._diff_farm_stats_bucket(
            current.get("totals", {}),
            previous.get("totals", {}),
        )
        totals["loot_per_hour"] = round(totals["loot"] / delta_hours, 2) if delta_hours else 0
        totals["attacks_per_hour"] = round(totals["reports"] / delta_hours, 2) if delta_hours else 0
        if "scouts" in current and "scouts" in previous:
            totals["scouts"] = max(
                0,
                int(current.get("scouts", 0) or 0) - int(previous.get("scouts", 0) or 0),
            )
        else:
            totals["scouts"] = 0
        totals["scouts_per_hour"] = round(totals["scouts"] / delta_hours, 2) if delta_hours else 0

        sources = {}
        for source in sorted(
                set(current.get("sources", {}).keys())
                | set(previous.get("sources", {}).keys())
        ):
            source_delta = VillageManager._diff_farm_stats_bucket(
                current.get("sources", {}).get(source, {}),
                previous.get("sources", {}).get(source, {}),
            )
            source_delta["loot_per_hour"] = (
                round(source_delta["loot"] / delta_hours, 2) if delta_hours else 0
            )
            source_delta["attacks_per_hour"] = (
                round(source_delta["reports"] / delta_hours, 2) if delta_hours else 0
            )
            if source_delta["reports"] or source_delta["loot"]:
                sources[source] = source_delta

        previous_profile = previous.get("config_profile", {}).get("profile_id")
        current_profile = current.get("config_profile", {}).get("profile_id")
        return {
            "from_timestamp": previous_timestamp,
            "to_timestamp": current_timestamp,
            "seconds": delta_seconds,
            "hours": round(delta_hours, 2),
            "profile_changed": previous_profile != current_profile,
            "previous_profile_id": previous_profile,
            "profile_id": current_profile,
            "totals": totals,
            "sources": sources,
        }

    @staticmethod
    def build_farm_stats_snapshot(config, attacks, reports, now=None):
        now = now or int(time.time())
        totals = VillageManager._farm_stats_bucket()
        targets = {}
        target_observations = {}
        sources = {}
        farm_config = config.get("farms", {})
        default_wait = int(farm_config.get("default_away_time", 3600) or 3600)
        low_fill_threshold = 0.25
        high_fill_threshold = 0.85

        for farm, data in sorted(attacks.items(), key=lambda item: str(item[0])):
            targets[str(farm)] = {
                **VillageManager._farm_stats_bucket(),
                "safe": data.get("safe") is not False,
                "high_profile": bool(data.get("high_profile")),
                "low_profile": bool(data.get("low_profile")),
                "last_attack": int(data.get("last_attack", 0) or 0),
                "latest_report_at": int(data.get("latest_report_at", 0) or 0),
                "latest_resources_total": sum(
                    int(value or 0)
                    for value in data.get("latest_resources", {}).values()
                ),
                "source_reports": {},
            }
            target_observations[str(farm)] = []

        for report in reports.values():
            if report.get("type") != "attack":
                continue
            dest = str(report.get("dest"))
            if dest not in targets:
                continue

            extra = report.get("extra", {})
            report_loot = VillageManager.report_loot_total(extra)
            report_capacity = VillageManager.report_capacity(extra)
            sent_units = VillageManager.report_unit_total(extra, "units_sent")
            lost_units = VillageManager.report_unit_total(extra, "units_losses")
            filled_capacity = min(report_loot, report_capacity)
            report_fill_rate = VillageManager.report_fill_rate(
                report_loot, report_capacity
            )
            origin = str(report.get("origin") or "unknown")
            when = int(extra.get("when", 0) or 0)

            for bucket in (
                    totals,
                    targets[dest],
                    sources.setdefault(origin, VillageManager._farm_stats_bucket()),
            ):
                bucket["reports"] += 1
                bucket["loot"] += report_loot
                bucket["capacity"] += report_capacity
                bucket["filled_capacity"] += filled_capacity
                bucket["sent_units"] += sent_units
                bucket["lost_units"] += lost_units

            source_reports = targets[dest]["source_reports"]
            source_reports[origin] = source_reports.get(origin, 0) + 1
            if when:
                target_observations[dest].append({
                    "when": when,
                    "origin": origin,
                    "loot": report_loot,
                    "capacity": report_capacity,
                    "fill_rate": report_fill_rate,
                })

        for target, observations in target_observations.items():
            observations = sorted(observations, key=lambda item: item["when"])
            if not observations:
                continue

            gaps = []
            long_gap_reports = 0
            low_fill_after_long_gap = 0
            full_fill_after_long_gap = 0
            for previous, current in zip(observations, observations[1:]):
                gap = current["when"] - previous["when"]
                if gap < 0:
                    continue
                gaps.append(gap)
                if gap >= default_wait:
                    long_gap_reports += 1
                    if current["fill_rate"] < low_fill_threshold:
                        low_fill_after_long_gap += 1
                    if current["fill_rate"] > high_fill_threshold:
                        full_fill_after_long_gap += 1

            latest = observations[-1]
            target_bucket = targets[target]
            target_bucket["first_report_at"] = observations[0]["when"]
            target_bucket["last_report_at"] = latest["when"]
            target_bucket["last_observed_origin"] = latest["origin"]
            target_bucket["last_observed_loot"] = latest["loot"]
            target_bucket["last_observed_capacity"] = latest["capacity"]
            target_bucket["last_observed_fill_rate"] = round(
                latest["fill_rate"], 4
            )
            target_bucket["avg_gap_seconds"] = (
                round(sum(gaps) / len(gaps), 2) if gaps else 0
            )
            target_bucket["median_gap_seconds"] = round(
                VillageManager._median(gaps), 2
            )
            target_bucket["long_gap_seconds"] = default_wait
            target_bucket["long_gap_reports"] = long_gap_reports
            target_bucket["low_fill_after_long_gap"] = low_fill_after_long_gap
            target_bucket["full_fill_after_long_gap"] = full_fill_after_long_gap
            target_bucket["low_fill_after_long_gap_rate"] = (
                round(low_fill_after_long_gap / long_gap_reports, 4)
                if long_gap_reports else 0
            )

        finalized_targets = {
            target: VillageManager._finalize_farm_stats_bucket(bucket)
            for target, bucket in targets.items()
        }
        finalized_sources = {
            source: VillageManager._finalize_farm_stats_bucket(bucket)
            for source, bucket in sorted(sources.items())
        }

        recent = VillageManager.build_recent_farm_windows(reports, now)

        return {
            "timestamp": now,
            "villages": len(config.get("villages", {})),
            "reports": len(reports),
            "scouts": sum(1 for report in reports.values() if report.get("type") == "scout"),
            "farms": len(attacks),
            "safe_farms": sum(1 for data in attacks.values() if data.get("safe") is not False),
            "high_profile_farms": sum(1 for data in attacks.values() if data.get("high_profile")),
            "low_profile_farms": sum(1 for data in attacks.values() if data.get("low_profile")),
            "analysis_thresholds": {
                "long_gap_seconds": default_wait,
                "low_fill_rate": low_fill_threshold,
                "high_fill_rate": high_fill_threshold,
            },
            "config_profile": VillageManager.build_config_profile(config),
            "recent": recent,
            "totals": VillageManager._finalize_farm_stats_bucket(totals),
            "sources": finalized_sources,
            "targets": finalized_targets,
        }

    @staticmethod
    def write_farm_stats_snapshot(snapshot):
        FileManager.create_directories(["cache/farm_stats"])
        previous = VillageManager._load_latest_farm_stats_snapshot()
        delta = VillageManager.build_snapshot_delta(previous, snapshot)
        if delta:
            snapshot["since_previous"] = delta
        day = time.strftime("%Y-%m-%d", time.localtime(snapshot["timestamp"]))
        path = f"cache/farm_stats/{day}.jsonl"
        with open(path, "a", encoding="utf-8") as stats_file:
            stats_file.write(json.dumps(snapshot, sort_keys=True) + "\n")
        return path

    @staticmethod
    def farm_manager(verbose=False, clean_reports=False):
        logger = logging.getLogger("FarmManager")
        with open("config.json", "r") as f:
            config = json.load(f)

        if verbose:
            logger.info("Villages: %d", len(config["villages"]))
        attacks = AttackCache.cache_grab()
        reports = ReportCache.cache_grab()
        high_fill_rate_threshold = 0.85
        low_fill_rate_threshold = 0.25

        if verbose:
            logger.info("Reports: %d", len(reports))
            logger.info("Farms: %d", len(attacks))
        t = {"wood": 0, "iron": 0, "stone": 0}
        for farm in attacks:
            data = attacks[farm]

            num_attack = []
            loot = {"wood": 0, "iron": 0, "stone": 0}
            total_loss_count = 0
            total_sent_count = 0
            latest_report_at = 0
            last_loot = 0
            last_fill_rate = 0
            total_capacity = 0
            filled_capacity = 0
            for rep in reports:
                report = reports[rep]
                if str(report.get("dest")) == str(farm) and report.get("type") == "attack":
                    extra = report.get("extra", {})
                    for unit in extra.get("units_sent", {}):
                        total_sent_count += int(extra["units_sent"][unit])
                    for unit in extra.get("units_losses", {}):
                        total_loss_count += int(extra["units_losses"][unit])
                    try:
                        res = extra["loot"]
                        report_loot = VillageManager.report_loot_total(extra)
                        report_capacity = VillageManager.report_capacity(extra)
                        report_fill_rate = (
                            min(1.0, report_loot / report_capacity)
                            if report_capacity > 0 else 0
                        )
                        for r in res:
                            amount = int(res[r])
                            loot[r] = loot[r] + amount
                            t[r] = t[r] + amount
                        total_capacity += report_capacity
                        filled_capacity += min(report_loot, report_capacity)
                        latest_report_at = max(
                            latest_report_at,
                            int(extra.get("when", 0) or 0),
                        )
                        if int(extra.get("when", 0) or 0) >= latest_report_at:
                            last_loot = report_loot
                            last_fill_rate = report_fill_rate
                        num_attack.append(report)
                    except:
                        pass
            percentage_lost = 0

            if total_sent_count > 0:
                percentage_lost = total_loss_count / total_sent_count * 100

            perf = ""
            if data["high_profile"]:
                perf = "High Profile "
            if "low_profile" in data and data["low_profile"]:
                perf = "Low Profile "
            if verbose:
                logger.info(
                    "%sFarm village %s attacked %d times - Total loot: %s - Total units lost: %d (%.2f)",
                    perf, farm, len(num_attack), str(loot), total_loss_count, percentage_lost
                )
            if len(num_attack):
                total = 0
                for k in loot:
                    total += loot[k]
                avg_loot = int(total / len(num_attack))
                data["total_loot"] = total
                data["avg_loot"] = avg_loot
                data["last_loot"] = last_loot
                data["loot_reports"] = len(num_attack)
                data["loss_percentage"] = round(percentage_lost, 2)
                avg_fill_rate = (
                    round(filled_capacity / total_capacity, 4)
                    if total_capacity > 0 else 0
                )
                data["total_capacity"] = total_capacity
                data["filled_capacity"] = filled_capacity
                data["avg_fill_rate"] = avg_fill_rate
                data["last_fill_rate"] = round(last_fill_rate, 4)
                if len(num_attack) > 3:
                    if avg_fill_rate < low_fill_rate_threshold:
                        if verbose:
                            logger.info(
                                "Farm %s has low haul fill rate (%.0f%%), extending farm time",
                                farm, avg_fill_rate * 100
                            )
                        data["low_profile"] = True
                        data["high_profile"] = False
                        AttackCache.set_cache(farm, data)
                    elif avg_fill_rate > high_fill_rate_threshold:
                        if verbose:
                            logger.info(
                                "Farm %s has high haul fill rate (%.0f%%), setting to high profile",
                                farm, avg_fill_rate * 100
                            )
                        data["high_profile"] = True
                        data["low_profile"] = False
                        AttackCache.set_cache(farm, data)
                    else:
                        data["high_profile"] = False
                        data["low_profile"] = False
                        AttackCache.set_cache(farm, data)
                else:
                    AttackCache.set_cache(farm, data)

            if percentage_lost > 20 and not data["low_profile"]:
                logger.warning(f"Dangerous {percentage_lost} percentage lost units! Extending farm time")
                data["low_profile"] = True
                data["high_profile"] = False
                AttackCache.set_cache(farm, data)
            if percentage_lost > 50 and len(num_attack) > 10:
                logger.critical("Farm seems too dangerous/ unprofitable to farm. Setting safe to false!")
                data["safe"] = False
                AttackCache.set_cache(farm, data)

        if verbose:
            logger.info("Total loot: %s" % t)

        try:
            snapshot = VillageManager.build_farm_stats_snapshot(
                config, attacks, reports
            )
            stats_path = VillageManager.write_farm_stats_snapshot(snapshot)
            if verbose:
                logger.info("Farm stats snapshot saved to %s", stats_path)
        except Exception:
            logger.exception("Unable to write farm stats snapshot")

        if clean_reports:
            list_of_files = sorted(["./cache/reports/" + f for f in os.listdir("./cache/reports/")],
                                   key=os.path.getctime)

            logger.info(f"Found {len(list_of_files)} files")

            while len(list_of_files) > clean_reports:
                oldest_file = list_of_files.pop(0)
                logger.info(f"Delete old report ({oldest_file})")
                os.remove(os.path.abspath(oldest_file))


if __name__ == "__main__":
    logging.basicConfig(stream=sys.stdout)
    VillageManager.farm_manager(verbose=True)
