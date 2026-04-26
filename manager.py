import json
import logging
import os
import sys

from game.attack import AttackCache
from game.reports import ReportCache
from game.simulator import Simulator


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
