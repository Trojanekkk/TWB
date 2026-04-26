import time


class StatsBuilder:
    resources = ["wood", "stone", "iron"]

    @staticmethod
    def _empty_loot():
        return {resource: 0 for resource in StatsBuilder.resources}

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
    def build(reports, farms, managed):
        now = int(time.time())
        windows = {
            "24h": {
                "since": now - 86400,
                "loot": StatsBuilder._empty_loot(),
                "attacks": 0,
                "scouts": 0,
                "losses": 0,
                "sent": 0,
            },
            "7d": {
                "since": now - 604800,
                "loot": StatsBuilder._empty_loot(),
                "attacks": 0,
                "scouts": 0,
                "losses": 0,
                "sent": 0,
            },
            "all": {
                "since": 0,
                "loot": StatsBuilder._empty_loot(),
                "attacks": 0,
                "scouts": 0,
                "losses": 0,
                "sent": 0,
            },
        }
        farm_totals = {}

        for report_id, report in reports.items():
            extra = report.get("extra", {})
            when = StatsBuilder._report_when(report)
            loot = extra.get("loot", {}) or {}
            sent = extra.get("units_sent", {}) or {}
            losses = extra.get("units_losses", {}) or report.get("losses", {}) or {}
            sent_total = sum(int(value or 0) for value in sent.values())
            loss_total = sum(int(value or 0) for value in losses.values())
            is_scout = bool(extra.get("resources") or extra.get("buildings") or extra.get("defence_units"))
            is_attack = report.get("type") == "attack" or bool(loot) or is_scout

            if not is_attack:
                continue

            dest = str(report.get("dest") or "unknown")
            if dest not in farm_totals:
                farm_totals[dest] = {
                    "target_vid": dest,
                    "loot": StatsBuilder._empty_loot(),
                    "attacks": 0,
                    "scouts": 0,
                    "losses": 0,
                    "sent": 0,
                    "last_report_at": 0,
                }
            farm_totals[dest]["attacks"] += 1
            farm_totals[dest]["scouts"] += 1 if is_scout else 0
            farm_totals[dest]["losses"] += loss_total
            farm_totals[dest]["sent"] += sent_total
            farm_totals[dest]["last_report_at"] = max(farm_totals[dest]["last_report_at"], when)
            StatsBuilder._add_loot(farm_totals[dest]["loot"], loot)

            for window in windows.values():
                if not when and window["since"] > 0:
                    continue
                if when and when < window["since"]:
                    continue
                window["attacks"] += 1
                window["scouts"] += 1 if is_scout else 0
                window["losses"] += loss_total
                window["sent"] += sent_total
                StatsBuilder._add_loot(window["loot"], loot)

        for window in windows.values():
            window["loot_total"] = StatsBuilder._total(window["loot"])
            window["loss_percentage"] = (
                round((window["losses"] / window["sent"]) * 100, 2)
                if window["sent"] else 0
            )

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
                    "loot": StatsBuilder._empty_loot(),
                    "attacks": 0,
                    "scouts": 0,
                    "losses": 0,
                    "sent": 0,
                    "last_report_at": int(farm.get("latest_report_at", 0) or 0),
                }

        top_farms = sorted(
            farm_totals.values(),
            key=lambda item: StatsBuilder._total(item["loot"]),
            reverse=True,
        )[:20]
        for farm in top_farms:
            farm["loot_total"] = StatsBuilder._total(farm["loot"])
            farm["loss_percentage"] = (
                round((farm["losses"] / farm["sent"]) * 100, 2)
                if farm["sent"] else 0
            )

        return {
            "generated_at": now,
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
            "top_farms": top_farms,
        }
