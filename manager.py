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

        return {
            "timestamp": now,
            "villages": len(config.get("villages", {})),
            "reports": len(reports),
            "farms": len(attacks),
            "safe_farms": sum(1 for data in attacks.values() if data.get("safe") is not False),
            "high_profile_farms": sum(1 for data in attacks.values() if data.get("high_profile")),
            "low_profile_farms": sum(1 for data in attacks.values() if data.get("low_profile")),
            "analysis_thresholds": {
                "long_gap_seconds": default_wait,
                "low_fill_rate": low_fill_threshold,
                "high_fill_rate": high_fill_threshold,
            },
            "totals": VillageManager._finalize_farm_stats_bucket(totals),
            "sources": finalized_sources,
            "targets": finalized_targets,
        }

    @staticmethod
    def write_farm_stats_snapshot(snapshot):
        FileManager.create_directories(["cache/farm_stats"])
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
