import glob
import json
import os
import time


class StatsBuilder:
    resources = ["wood", "stone", "iron"]
    unit_loads = {
        "spear": 25,
        "sword": 15,
        "axe": 10,
        "archer": 10,
        "spy": 0,
        "light": 80,
        "marcher": 50,
        "heavy": 50,
        "knight": 100,
        "ram": 0,
        "catapult": 0,
        "snob": 0,
    }

    @staticmethod
    def _empty_loot():
        return {resource: 0 for resource in StatsBuilder.resources}

    @staticmethod
    def _empty_rollup():
        return {
            "loot": StatsBuilder._empty_loot(),
            "loot_total": 0,
            "reports": 0,
            "attacks": 0,
            "scouts": 0,
            "losses": 0,
            "sent": 0,
            "capacity": 0,
            "filled_capacity": 0,
            "avg_loot": 0,
            "fill_rate": 0,
            "loss_percentage": 0,
        }

    @staticmethod
    def _add_loot(target, loot):
        for resource in StatsBuilder.resources:
            target[resource] += int(loot.get(resource, 0) or 0)

    @staticmethod
    def _total(resources):
        return sum(int(resources.get(resource, 0) or 0) for resource in StatsBuilder.resources)

    @staticmethod
    def _report_when(report):
        return int(report.get("extra", {}).get("when", 0) or 0)

    @staticmethod
    def _unit_total(units):
        return sum(int(value or 0) for value in (units or {}).values())

    @staticmethod
    def _capacity(units):
        total = 0
        for unit, amount in (units or {}).items():
            total += StatsBuilder.unit_loads.get(unit, 0) * int(amount or 0)
        return total

    @staticmethod
    def _report_kind(report):
        extra = report.get("extra", {})
        report_type = report.get("type")
        has_loot = bool(extra.get("loot"))
        has_scout_data = bool(
            extra.get("resources")
            or extra.get("buildings")
            or extra.get("defence_units")
        )
        is_scout = report_type == "scout"
        is_attack = report_type == "attack" or (has_loot and not is_scout)
        return is_attack, is_scout, has_scout_data

    @staticmethod
    def _add_report(rollup, report, is_attack, is_scout):
        extra = report.get("extra", {})
        loot = extra.get("loot", {}) or {}
        sent = extra.get("units_sent", {}) or {}
        losses = extra.get("units_losses", {}) or report.get("losses", {}) or {}
        loot_total = StatsBuilder._total(loot)
        capacity = StatsBuilder._capacity(sent)

        rollup["reports"] += 1
        rollup["attacks"] += 1 if is_attack else 0
        rollup["scouts"] += 1 if is_scout else 0
        rollup["losses"] += StatsBuilder._unit_total(losses)
        rollup["sent"] += StatsBuilder._unit_total(sent)
        rollup["capacity"] += capacity
        rollup["filled_capacity"] += min(loot_total, capacity)
        StatsBuilder._add_loot(rollup["loot"], loot)

    @staticmethod
    def _finalize_rollup(rollup):
        rollup["loot_total"] = StatsBuilder._total(rollup["loot"])
        rollup["avg_loot"] = (
            round(rollup["loot_total"] / rollup["attacks"], 2)
            if rollup["attacks"] else 0
        )
        rollup["fill_rate"] = (
            round(rollup["filled_capacity"] / rollup["capacity"], 4)
            if rollup["capacity"] else 0
        )
        rollup["loss_percentage"] = (
            round((rollup["losses"] / rollup["sent"]) * 100, 2)
            if rollup["sent"] else 0
        )
        return rollup

    @staticmethod
    def _median(values):
        if not values:
            return 0
        values = sorted(values)
        middle = len(values) // 2
        if len(values) % 2:
            return values[middle]
        return (values[middle - 1] + values[middle]) / 2

    @staticmethod
    def _village_name(village_id, managed):
        village = managed.get(str(village_id), {})
        public = village.get("public", {})
        return village.get("name") or public.get("name") or str(village_id)

    @staticmethod
    def _snapshot_dir():
        return os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "cache", "farm_stats")
        )

    @staticmethod
    def _load_snapshots(limit=96):
        snapshot_dir = StatsBuilder._snapshot_dir()
        if not os.path.isdir(snapshot_dir):
            return {"count": 0, "latest": None, "history": []}

        snapshots = []
        for path in glob.glob(os.path.join(snapshot_dir, "*.jsonl")):
            try:
                with open(path, "r", encoding="utf-8") as snapshot_file:
                    for line in snapshot_file:
                        line = line.strip()
                        if not line:
                            continue
                        snapshots.append(json.loads(line))
            except (OSError, ValueError):
                continue

        snapshots.sort(key=lambda item: int(item.get("timestamp", 0) or 0))
        history = snapshots[-limit:]
        latest = history[-1] if history else None
        profile_summaries = {}
        for item in snapshots:
            profile = item.get("config_profile", {}) or {}
            profile_id = profile.get("profile_id") or "legacy"
            summary = profile_summaries.setdefault(profile_id, {
                "profile_id": profile_id,
                "snapshots": 0,
                "intervals": 0,
                "hours": 0,
                "loot": 0,
                "attacks": 0,
                "scouts": 0,
                "capacity": 0,
                "filled_capacity": 0,
                "lost_units": 0,
                "sent_units": 0,
                "latest_timestamp": 0,
                "latest_label": "",
                "farms": profile.get("farms", {}),
                "template_hashes": profile.get("template_hashes", {}),
            })
            summary["snapshots"] += 1
            summary["latest_timestamp"] = max(
                summary["latest_timestamp"],
                int(item.get("timestamp", 0) or 0),
            )
            interval = item.get("since_previous", {}) or {}
            if interval.get("profile_id") != profile_id or interval.get("profile_changed"):
                continue
            totals = interval.get("totals", {}) or {}
            summary["intervals"] += 1
            summary["hours"] += float(interval.get("hours", 0) or 0)
            summary["loot"] += int(totals.get("loot", 0) or 0)
            summary["attacks"] += int(totals.get("reports", 0) or 0)
            summary["scouts"] += int(totals.get("scouts", 0) or 0)
            summary["capacity"] += int(totals.get("capacity", 0) or 0)
            summary["filled_capacity"] += int(totals.get("filled_capacity", 0) or 0)
            summary["lost_units"] += int(totals.get("lost_units", 0) or 0)
            summary["sent_units"] += int(totals.get("sent_units", 0) or 0)

        config_profiles = []
        for summary in profile_summaries.values():
            hours = summary["hours"]
            capacity = summary["capacity"]
            sent_units = summary["sent_units"]
            latest_timestamp = summary["latest_timestamp"]
            summary["loot_per_hour"] = round(summary["loot"] / hours, 2) if hours else 0
            summary["attacks_per_hour"] = round(summary["attacks"] / hours, 2) if hours else 0
            summary["scouts_per_hour"] = round(summary["scouts"] / hours, 2) if hours else 0
            summary["fill_rate"] = (
                round(summary["filled_capacity"] / capacity, 4)
                if capacity else 0
            )
            summary["loss_percentage"] = (
                round(summary["lost_units"] / sent_units * 100, 2)
                if sent_units else 0
            )
            summary["latest_label"] = (
                time.strftime("%m-%d %H:%M", time.localtime(latest_timestamp))
                if latest_timestamp else ""
            )
            config_profiles.append(summary)

        config_profiles.sort(
            key=lambda item: int(item.get("latest_timestamp", 0) or 0),
            reverse=True,
        )

        return {
            "count": len(snapshots),
            "latest": latest,
            "history": [
                {
                    "timestamp": item.get("timestamp", 0),
                    "label": time.strftime(
                        "%m-%d %H:%M",
                        time.localtime(int(item.get("timestamp", 0) or 0)),
                    ),
                    "loot": item.get("totals", {}).get("loot", 0),
                    "fill_rate": item.get("totals", {}).get("fill_rate", 0),
                    "loss_percentage": item.get("totals", {}).get("loss_percentage", 0),
                    "low_profile_farms": item.get("low_profile_farms", 0),
                    "profile_id": (item.get("config_profile", {}) or {}).get("profile_id", "legacy"),
                    "loot_per_hour": (
                        item.get("recent", {})
                        .get("windows", {})
                        .get("24h", {})
                        .get("totals", {})
                        .get("loot_per_hour", 0)
                    ),
                }
                for item in history
            ],
            "config_profiles": config_profiles,
        }

    @staticmethod
    def _chart_buckets(reference_time, reports, hours=24):
        bucket_seconds = 3600
        chart_end = reference_time - (reference_time % bucket_seconds) + bucket_seconds
        chart_start = chart_end - (hours * bucket_seconds)
        buckets = []
        for index in range(hours):
            start = chart_start + (index * bucket_seconds)
            buckets.append({
                "start": start,
                "label": time.strftime("%H:%M", time.localtime(start)),
                **StatsBuilder._empty_rollup(),
            })

        for report in reports.values():
            when = StatsBuilder._report_when(report)
            if when < chart_start or when >= chart_end:
                continue
            is_attack, is_scout, _ = StatsBuilder._report_kind(report)
            if not is_attack and not is_scout:
                continue
            index = int((when - chart_start) / bucket_seconds)
            if 0 <= index < len(buckets):
                StatsBuilder._add_report(buckets[index], report, is_attack, is_scout)

        max_loot = 0
        max_attacks = 0
        for bucket in buckets:
            StatsBuilder._finalize_rollup(bucket)
            max_loot = max(max_loot, bucket["loot_total"])
            max_attacks = max(max_attacks, bucket["attacks"])

        for bucket in buckets:
            bucket["loot_height"] = (
                max(3, int(bucket["loot_total"] / max_loot * 100))
                if max_loot else 0
            )
            bucket["attack_height"] = (
                max(3, int(bucket["attacks"] / max_attacks * 100))
                if max_attacks else 0
            )

        return {
            "hours": hours,
            "max_loot": max_loot,
            "max_attacks": max_attacks,
            "buckets": buckets,
        }

    @staticmethod
    def build(reports, farms, managed, config=None):
        now = int(time.time())
        config = config or {}
        latest_report_at = 0
        for report in reports.values():
            latest_report_at = max(latest_report_at, StatsBuilder._report_when(report))
        reference_time = max(now, latest_report_at)
        windows = {
            "24h": {
                "since": reference_time - 86400,
                **StatsBuilder._empty_rollup(),
            },
            "7d": {
                "since": reference_time - 604800,
                **StatsBuilder._empty_rollup(),
            },
            "all": {
                "since": 0,
                **StatsBuilder._empty_rollup(),
            },
        }
        farm_config = config.get("farms", {})
        default_wait = int(farm_config.get("default_away_time", 3600) or 3600)
        farm_totals = {}
        target_observations = {}
        village_totals = {}
        village_windows = {}

        for report_id, report in reports.items():
            when = StatsBuilder._report_when(report)
            is_attack, is_scout, _ = StatsBuilder._report_kind(report)

            if not is_attack and not is_scout:
                continue

            dest = str(report.get("dest") or "unknown")
            if dest not in farm_totals:
                farm_totals[dest] = {
                    "target_vid": dest,
                    **StatsBuilder._empty_rollup(),
                    "last_report_at": 0,
                    "source_reports": {},
                }
            StatsBuilder._add_report(farm_totals[dest], report, is_attack, is_scout)
            farm_totals[dest]["last_report_at"] = max(farm_totals[dest]["last_report_at"], when)
            origin = str(report.get("origin") or "unknown")
            farm_totals[dest]["source_reports"][origin] = (
                farm_totals[dest]["source_reports"].get(origin, 0) + 1
            )

            for label, window in windows.items():
                if not when and window["since"] > 0:
                    continue
                if when and when < window["since"]:
                    continue
                StatsBuilder._add_report(window, report, is_attack, is_scout)

                if origin not in village_windows:
                    village_windows[origin] = {
                        label: StatsBuilder._empty_rollup()
                        for label in windows.keys()
                    }
                StatsBuilder._add_report(
                    village_windows[origin][label], report, is_attack, is_scout
                )

            if origin not in village_totals:
                village_totals[origin] = StatsBuilder._empty_rollup()
            StatsBuilder._add_report(village_totals[origin], report, is_attack, is_scout)

            if is_attack and when:
                extra = report.get("extra", {})
                loot_total = StatsBuilder._total(extra.get("loot", {}) or {})
                capacity = StatsBuilder._capacity(extra.get("units_sent", {}) or {})
                fill_rate = min(1.0, loot_total / capacity) if capacity else 0
                target_observations.setdefault(dest, []).append({
                    "when": when,
                    "loot": loot_total,
                    "capacity": capacity,
                    "fill_rate": fill_rate,
                })

        high_profile = 0
        low_profile = 0
        unsafe = 0
        draining = 0
        for farm_id, farm in farms.items():
            if farm.get("high_profile"):
                high_profile += 1
            if farm.get("low_profile"):
                low_profile += 1
            if farm.get("safe") is False:
                unsafe += 1
            if StatsBuilder._total(farm.get("latest_resources", {}) or {}) > 0:
                draining += 1
            if farm_id not in farm_totals:
                farm_totals[farm_id] = {
                    "target_vid": farm_id,
                    **StatsBuilder._empty_rollup(),
                    "last_report_at": int(farm.get("latest_report_at", 0) or 0),
                    "source_reports": {},
                }
            farm_totals[farm_id]["safe"] = farm.get("safe") is not False
            farm_totals[farm_id]["high_profile"] = bool(farm.get("high_profile"))
            farm_totals[farm_id]["low_profile"] = bool(farm.get("low_profile"))
            farm_totals[farm_id]["latest_resources_total"] = StatsBuilder._total(
                farm.get("latest_resources", {}) or {}
            )

        for target_id, observations in target_observations.items():
            observations = sorted(observations, key=lambda item: item["when"])
            gaps = []
            long_gap_reports = 0
            low_after_long_gap = 0
            for previous, current in zip(observations, observations[1:]):
                gap = current["when"] - previous["when"]
                if gap < 0:
                    continue
                gaps.append(gap)
                if gap >= default_wait:
                    long_gap_reports += 1
                    if current["fill_rate"] < 0.25:
                        low_after_long_gap += 1

            farm_totals[target_id]["avg_gap_seconds"] = (
                round(sum(gaps) / len(gaps), 2) if gaps else 0
            )
            farm_totals[target_id]["median_gap_seconds"] = round(
                StatsBuilder._median(gaps), 2
            )
            farm_totals[target_id]["long_gap_reports"] = long_gap_reports
            farm_totals[target_id]["low_fill_after_long_gap"] = low_after_long_gap
            farm_totals[target_id]["low_fill_after_long_gap_rate"] = (
                round(low_after_long_gap / long_gap_reports, 4)
                if long_gap_reports else 0
            )

        top_farms = sorted(
            farm_totals.values(),
            key=lambda item: StatsBuilder._total(item["loot"]),
            reverse=True,
        )[:20]
        for farm in top_farms:
            StatsBuilder._finalize_rollup(farm)

        for window in windows.values():
            StatsBuilder._finalize_rollup(window)

        village_rows = []
        for village_id, rollup in village_totals.items():
            village_window_data = village_windows.get(village_id, {})
            row = {
                "village_id": village_id,
                "name": StatsBuilder._village_name(village_id, managed),
                "resources": managed.get(village_id, {}).get("resources", {}),
                "troops": managed.get(village_id, {}).get("troops", {}),
                **StatsBuilder._finalize_rollup(rollup),
                "windows": {
                    label: StatsBuilder._finalize_rollup(
                        village_window_data.get(label, StatsBuilder._empty_rollup())
                    )
                    for label in windows.keys()
                },
            }
            village_rows.append(row)
        village_rows.sort(key=lambda item: item["loot_total"], reverse=True)

        return {
            "generated_at": reference_time,
            "generated_at_label": time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(reference_time)
            ),
            "latest_report_at": latest_report_at,
            "latest_report_label": time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(latest_report_at)
            ) if latest_report_at else "",
            "windows": windows,
            "farms": {
                "total": len(farms),
                "high_profile": high_profile,
                "low_profile": low_profile,
                "unsafe": unsafe,
                "draining": draining,
            },
            "villages": {
                "managed": len(managed),
            },
            "village_totals": village_rows,
            "top_farms": top_farms,
            "chart": StatsBuilder._chart_buckets(reference_time, reports),
            "snapshots": StatsBuilder._load_snapshots(),
        }
