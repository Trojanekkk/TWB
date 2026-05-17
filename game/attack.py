"""
Attack manager
Sounds dangerous but it just sends farms
"""

from core.extractors import Extractor
import logging
import time
from datetime import datetime
from datetime import timedelta

from core.filemanager import FileManager
from game.simulator import Simulator


class AttackManager:
    """
    Attackmanager class
    """
    map = None
    village_id = None
    troopmanager = None
    wrapper = None
    targets = {}
    logger = logging.getLogger("Attacks")
    max_farms = 15
    template = {}
    extra_farm = []
    repman = None
    target_high_points = False
    target_player_owned = False
    farm_radius = 50
    farm_minpoints = 0
    farm_maxpoints = 1000
    ignored = []

    # Configures the amount of spies used to detect if villages are safe to farm
    scout_farm_amount = 5

    forced_peace_time = None

    # blocks villages which cannot be attacked at the moment (too low points, beginners protection etc..)
    _unknown_ignored = []

    # Don't mess with these they are in the config file
    farm_high_prio_wait = 1200
    farm_default_wait = 3600
    farm_low_prio_wait = 7200
    farm_priority_ratio = 50
    farm_high_loot_threshold = 500
    farm_low_loot_threshold = 100
    farm_exploration_ratio = 0.25
    farm_exploration_min_targets = 2
    attack_delay_factor = None

    # Night bonus protection: defenders get +200% defence during night-bonus
    # hours, so a normal-sized farm bleeds troops. Instead of skipping, the
    # bot multiplies the troop template by `night_bonus_troop_multiplier`
    # for any attack whose ARRIVAL lands inside the night-bonus window.
    night_bonus_start_hour = 23
    night_bonus_end_hour = 7
    night_bonus_troop_multiplier = 3.0

    # World speed parameters (used to estimate arrival time locally so we
    # can decide BEFORE sending whether the attack lands in night bonus).
    # Effective unit speed = game_speed * unit_speed. For a default world
    # both are 1.0; on this user's world (1.25 game / 0.8 unit) effective
    # is 1.0 too, so units travel at simulator base speeds.
    game_speed = 1.0
    unit_speed = 1.0

    # Scout-first policy. When False (default), the bot will blind-attack
    # any target that has no recent report. When True, the bot scouts new
    # targets first (provided spies are available) and skips them this
    # cycle. A target with a loss-report is ALWAYS skipped regardless.
    scout_first = False

    def __init__(self, wrapper=None, village_id=None, troopmanager=None, map=None):
        """
        Create the attack manager
        """
        self.wrapper = wrapper
        self.village_id = village_id
        self.troopmanager = troopmanager
        self.map = map

    def enough_in_village(self, units):
        """
        Checks if there are enough troops in a village.
        Rejects requests with non-positive amounts so we never send
        an empty/invalid attack.
        """
        for unit in units:
            wanted = units[unit]
            if not isinstance(wanted, int) or wanted <= 0:
                return f"{unit} (invalid amount: {wanted})"
            if unit not in self.troopmanager.troops:
                return f"{unit} (0/{wanted})"
            available = int(self.troopmanager.troops[unit])
            if wanted > available:
                return f"{unit} ({available}/{wanted})"
        return False

    def _is_night_bonus_hour(self, hour):
        """
        True if the given hour falls inside the configured night-bonus window.
        Handles wrap-around (e.g. 23-7).
        """
        start = self.night_bonus_start_hour
        end = self.night_bonus_end_hour
        if start == end:
            return False
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    def _estimate_travel_seconds(self, template, distance):
        """
        Estimate one-way travel time in seconds for `template` at given distance.
        Uses the slowest unit's base speed (min/field) from the simulator,
        adjusted by world game_speed * unit_speed.
        """
        speeds = []
        for unit in template:
            entry = Simulator.pool.get(unit)
            if entry and "speed" in entry:
                speeds.append(entry["speed"])
        if not speeds:
            return None
        slowest_min_per_field = max(speeds)
        effective = max(self.game_speed * self.unit_speed, 0.0001)
        return (distance * slowest_min_per_field * 60.0) / effective

    def _arrival_in_night_bonus(self, template, distance):
        """
        True if a farm sent right now with `template` over `distance` fields
        would land inside the night-bonus window.
        """
        seconds = self._estimate_travel_seconds(template, distance)
        if seconds is None:
            return False
        arrival = datetime.now() + timedelta(seconds=seconds)
        return self._is_night_bonus_hour(arrival.hour)

    def _scale_template(self, template, multiplier):
        """
        Scale every troop count in `template` by `multiplier`, rounded up.
        Knights are clamped to 1 (only one knight per village exists).
        Empty/zero entries are dropped.
        """
        if multiplier <= 1.0:
            return dict(template)
        scaled = {}
        for unit, amount in template.items():
            if not isinstance(amount, int) or amount <= 0:
                continue
            if unit == "knight":
                scaled[unit] = 1
                continue
            scaled[unit] = max(1, int(round(amount * multiplier)))
        return scaled

    def run(self):
        """
        Run the farming logic
        """
        if not self.troopmanager.can_attack or self.troopmanager.troops == {}:
            # Disable farming is disabled in config or no troops available
            return False
        self.get_targets()
        ignored = []
        sent = 0
        # Limits the amount of villages that are farmed from the current village
        for target in self.targets:
            if sent >= self.max_farms:
                break
            if type(self.template) == list:
                f = False
                for template in self.template:
                    if template in ignored:
                        continue
                    out_res = self.send_farm(target, template)
                    if out_res == 1:
                        f = True
                        sent += 1
                        break
                    elif out_res == -1:
                        ignored.append(template)
                if not f:
                    continue
            else:
                out_res = self.send_farm(target, self.template)
                if out_res == 1:
                    sent += 1
                if out_res == -1:
                    break

    def send_farm(self, target, template):
        """
        Send a farming run.

        If the predicted arrival lands during the night-bonus window the
        template is scaled up by `night_bonus_troop_multiplier` to overpower
        the +200% defence; if the village cannot supply the scaled count we
        return -1 so the caller falls through to the next farm template.
        """
        target_village, distance = target[0], target[1]
        send_template = dict(template)
        scaled_for_night = False

        if (
            self.night_bonus_troop_multiplier > 1.0
            and self._arrival_in_night_bonus(template, distance)
        ):
            send_template = self._scale_template(
                template, self.night_bonus_troop_multiplier
            )
            scaled_for_night = True
            self.logger.info(
                "Night-bonus arrival predicted for %s -> %s, scaling troops x%.2f: %s",
                self.village_id, target_village["id"],
                self.night_bonus_troop_multiplier, str(send_template)
            )

        missing = self.enough_in_village(send_template)
        if missing:
            if scaled_for_night:
                self.logger.debug(
                    "Not enough troops for night-scaled template (%s); trying next",
                    missing
                )
                return -1
            self.logger.debug(
                "Not sending additional farm because not enough units: %s", missing
            )
            return -1

        cached = self.can_attack(vid=target_village["id"], clear=False)
        if not cached:
            return 0

        attack_result = self.attack(target_village["id"], troops=send_template)
        if attack_result == "forced_peace":
            return 0
        self.logger.info(
            "Attacking %s -> %s (%s)",
            self.village_id, target_village["id"], str(send_template)
        )
        self.wrapper.reporter.report(
            self.village_id,
            "TWB_FARM",
            "Attacking %s -> %s (%s)"
            % (self.village_id, target_village["id"], str(send_template)),
        )
        if attack_result:
            for u in send_template:
                self.troopmanager.troops[u] = str(
                    int(self.troopmanager.troops[u]) - send_template[u]
                )
            self.attacked(
                target_village["id"],
                scout=True,
                safe=True,
                high_profile=cached["high_profile"]
                if type(cached) == dict
                else False,
                low_profile=cached["low_profile"]
                if type(cached) == dict and "low_profile" in cached
                else False,
            )
            return 1
        self.logger.debug(
            "Ignoring target %s because unable to attack", target_village["id"]
        )
        self._unknown_ignored.append(target_village["id"])
        return 0

    def farm_priority_score(self, vid, distance):
        """
        Higher scores are farmed first. The priority ratio is the amount of
        average loot a target must gain per extra field of distance to outrank
        a closer farm.
        """
        cache_entry = AttackCache.get_cache(vid)
        if not cache_entry:
            return 50 - (distance * self.farm_priority_ratio)
        if cache_entry.get("safe") is False and cache_entry.get("scout"):
            return -100000

        score = float(cache_entry.get("avg_loot", 0) or 0)
        if cache_entry.get("high_profile"):
            score += self.farm_high_loot_threshold
        if cache_entry.get("low_profile"):
            score -= self.farm_low_loot_threshold
        if cache_entry.get("latest_resources"):
            score += min(
                250,
                sum(int(value or 0) for value in cache_entry["latest_resources"].values()) / 10,
            )
        return score - (distance * self.farm_priority_ratio)

    def exploration_sort_key(self, target):
        """
        Prefer targets this village has attacked least recently, then least often.
        This keeps farming from collapsing onto the same high-score targets forever.
        """
        village, distance, score = target
        cache_entry = AttackCache.get_cache(village["id"]) or {}
        source_entry = cache_entry.get("sources", {}).get(str(self.village_id), {})
        last_attack = int(
            source_entry.get("last_attack")
            or cache_entry.get("last_attack")
            or 0
        )
        attacks = int(source_entry.get("attacks", 0) or 0)
        return last_attack, attacks, distance, -score

    def diversify_targets(self, targets):
        """
        Keep most slots score-driven, but reserve a slice for exploration.
        The returned order matters because run() stops after max_farms sends.
        """
        if self.max_farms <= 1 or self.farm_exploration_ratio <= 0:
            return targets

        target_slots = min(len(targets), self.max_farms)
        if target_slots <= 1:
            return targets

        explore_slots = int(round(target_slots * self.farm_exploration_ratio))
        explore_slots = max(self.farm_exploration_min_targets, explore_slots)
        explore_slots = min(explore_slots, target_slots - 1)
        if explore_slots <= 0:
            return targets

        exploit_slots = max(1, target_slots - explore_slots)
        priority_targets = targets[:exploit_slots]
        remaining_targets = targets[exploit_slots:]
        exploration_targets = sorted(
            remaining_targets,
            key=self.exploration_sort_key,
        )[:explore_slots]
        exploration_ids = {
            target[0]["id"] for target in exploration_targets
        }
        rest = [
            target for target in remaining_targets
            if target[0]["id"] not in exploration_ids
        ]

        mixed_targets = []
        priority_queue = list(priority_targets)
        exploration_queue = list(exploration_targets)
        interval = max(
            1,
            len(priority_queue) // max(1, len(exploration_queue)),
        )
        while priority_queue or exploration_queue:
            for _ in range(interval):
                if priority_queue:
                    mixed_targets.append(priority_queue.pop(0))
            if exploration_queue:
                mixed_targets.append(exploration_queue.pop(0))

        self.logger.info(
            "Farm target mix: %d priority, %d exploration, %d overflow",
            len(priority_targets), len(exploration_targets), len(rest)
        )
        return mixed_targets + rest

    def get_targets(self):
        """
        Gets all possible farming targets based on distance
        """
        output = []
        my_village = (
            self.map.villages[self.village_id]
            if self.village_id in self.map.villages
            else None
        )
        allowed_player_farms = {str(farm) for farm in self.extra_farm}
        for vid in self.map.villages:
            village = self.map.villages[vid]
            if village["owner"] != "0":
                if not self.target_player_owned:
                    if vid not in self.ignored:
                        self.logger.debug(
                            "Ignoring village %s because player-owned farming is disabled",
                            vid
                        )
                        self.ignored.append(vid)
                    continue
                if str(vid) not in allowed_player_farms:
                    if vid not in self.ignored:
                        self.logger.debug(
                            "Ignoring village %s because player owned, add to additional_farms to auto attack",
                            vid
                        )
                        self.ignored.append(vid)
                    continue
            if village["owner"] == "0" and str(vid) in allowed_player_farms:
                if vid not in self.ignored:
                    self.logger.debug(
                        "Village %s is listed in additional_farms but is currently barbarian; treating as normal farm",
                        vid
                    )
            if my_village and "points" in my_village and "points" in village:
                if village["points"] >= self.farm_maxpoints:
                    if vid not in self.ignored:
                        self.logger.debug(
                            "Ignoring village %s because points %d exceeds limit %d",
                            vid, village["points"], self.farm_maxpoints
                        )
                        self.ignored.append(vid)
                    continue
                if village["points"] <= self.farm_minpoints:
                    if vid not in self.ignored:
                        self.logger.debug(
                            "Ignoring village %s because points %d below limit %d",
                            vid, village["points"], self.farm_minpoints
                        )
                        self.ignored.append(vid)
                    continue
                if (
                        village["points"] >= my_village["points"]
                        and not self.target_high_points
                ):
                    if vid not in self.ignored:
                        self.logger.debug(
                            "Ignoring village %s because of higher points %d -> %d",
                            vid, my_village["points"], village["points"]
                        )
                        self.ignored.append(vid)
                    continue
                if vid in self._unknown_ignored:
                    continue
            if village["owner"] != "0":
                get_h = time.localtime().tm_hour
                if get_h in range(0, 8) or get_h == 23:
                    self.logger.debug(
                        "Village %s will be ignored because it is player owned and attack between 23h-8h", vid
                    )
                    continue
            distance = self.map.get_dist(village["location"])
            if distance > self.farm_radius:
                if vid not in self.ignored:
                    self.logger.debug(
                        "Village %s will be ignored because it is too far away: distance is %f, max is %d",
                        vid, distance, self.farm_radius
                    )
                    self.ignored.append(vid)
                continue
            if vid in self.ignored:
                self.logger.debug("Removed %s from farm ignore list", vid)
                self.ignored.remove(vid)

            output.append([village, distance, self.farm_priority_score(vid, distance)])
        self.logger.info(
            "Farm targets: %d Ignored targets: %d", len(output), len(self.ignored)
        )
        ranked_targets = sorted(output, key=lambda x: (-x[2], x[1]))
        self.targets = self.diversify_targets(ranked_targets)

    def attacked(
            self,
            vid,
            scout=False,
            high_profile=False,
            safe=True,
            low_profile=False,
            attack_type="attack",
    ):
        """
        The farm was sent and this is a callback on what happened
        """
        AttackCache.set_cache(
            vid,
            {
                "scout": scout,
                "safe": safe,
                "high_profile": high_profile,
                "low_profile": low_profile,
            },
            source_village_id=self.village_id,
            action=attack_type,
            repman=self.repman,
        )

    def scout(self, vid):
        """
        Attempt to send scouts to a farm
        """
        if not self.troopmanager.can_scout:
            self.logger.debug(
                "Skipping scout for %s because farm scouting is disabled", vid
            )
            return False
        if self.scout_farm_amount <= 0:
            self.logger.debug(
                "Skipping scout for %s because scout amount is disabled", vid
            )
            return False
        if (
                "spy" not in self.troopmanager.troops
                or int(self.troopmanager.troops["spy"]) < self.scout_farm_amount
        ):
            self.logger.debug(
                "Cannot scout %s at the moment because insufficient unit: spy", vid
            )
            return False
        troops = {"spy": self.scout_farm_amount}
        if self.attack(vid, troops=troops):
            self.attacked(vid, scout=True, safe=False, attack_type="scout")
            return True
        return False

    def can_attack(self, vid, clear=False):
        """
        Checks if it is safe en engage
        If not an amount of 5 scouts will be sent
        """
        cache_entry = AttackCache.get_cache(vid)

        if cache_entry and cache_entry["last_attack"]:
            last_attack = datetime.fromtimestamp(cache_entry["last_attack"])
            now = datetime.now()
            if (
                    last_attack < now - timedelta(hours=12)
                    and self.scout_first
                    and self.troopmanager.can_scout
            ):
                self.logger.debug(
                    "Attacked long ago %s, trying scout attack", last_attack
                )
                if self.scout(vid):
                    return False

        if not cache_entry:
            status = self.repman.safe_to_engage(vid)
            if status == 1:
                return True
            if status == 0:
                # Existing report shows we lost troops on this target.
                # Never attack blind, even with scout_first off.
                self.logger.debug(
                    "Skipping %s: previous report shows losses, not engaging",
                    vid
                )
                return False
            # status == -1: no intel for this target.
            if self.scout_first and self.troopmanager.can_scout:
                self.scout(vid)
                return False
            # Policy: blind attack with normal troops on first contact.
            return True

        if not cache_entry["safe"] or clear:
            if cache_entry["scout"] and self.repman:
                status = self.repman.safe_to_engage(vid)
                if status == -1:
                    self.logger.info(
                        "Checking %s: scout report not yet available", vid
                    )
                    return False
                if status == 0:
                    if cache_entry["last_attack"] + self.farm_low_prio_wait * 2 > int(time.time()):
                        if self.scout_first and self.troopmanager.can_scout:
                            self.logger.info(
                                "%s: Old scout report found (%s), re-scouting",
                                vid, cache_entry["last_attack"]
                            )
                            self.scout(vid)
                        return False
                    else:
                        self.logger.info(
                            "%s: scout report noted enemy units, ignoring", vid
                        )
                        return False
                self.logger.info(
                    "%s: scout report noted no enemy units, attacking", vid
                )
                return True

            self.logger.debug(
                "%s will be ignored for attack because unsafe, set safe:true to override", vid
            )
            return False

        if not cache_entry["scout"] and self.troopmanager.can_scout:
            self.scout(vid)
            return False
        min_time = self.farm_default_wait
        if cache_entry["high_profile"]:
            min_time = self.farm_high_prio_wait
        if "low_profile" in cache_entry and cache_entry["low_profile"]:
            min_time = self.farm_low_prio_wait

        if cache_entry and self.repman:
            res_left, res = self.repman.has_resources_left(vid)
            total_loot = 0
            for x in res:
                total_loot += int(res[x])

            if res_left and total_loot > 100:
                self.logger.debug(f"Draining farm of resources! Sending attack to get {res}.")
                min_time = int(self.farm_high_prio_wait / 2)

        if cache_entry["last_attack"] + min_time > int(time.time()):
            self.logger.debug(
                "%s will be ignored because of previous attack (%d sec delay between attacks)",
                vid, min_time
            )
            return False
        return cache_entry

    def has_troops_available(self, troops):
        for t in troops:
            if (
                    t not in self.troopmanager.troops
                    or int(self.troopmanager.troops[t]) < troops[t]
            ):
                return False
        return True

    def attack(self, vid, troops=None):
        """
        Send a TW attack
        """
        original_delay = self.wrapper.delay
        if self.attack_delay_factor is not None:
            try:
                self.wrapper.delay = float(self.attack_delay_factor)
            except (TypeError, ValueError):
                self.logger.warning(
                    "Invalid attack_delay_factor %s, using global delay %s",
                    self.attack_delay_factor,
                    original_delay,
                )

        try:
            return self._attack_with_current_delay(vid, troops=troops)
        finally:
            self.wrapper.delay = original_delay

    def _attack_with_current_delay(self, vid, troops=None):
        """
        Send a TW attack using the wrapper's currently configured delay.
        """
        url = f"game.php?village={self.village_id}&screen=place&target={vid}"
        pre_attack = self.wrapper.get_url(url)
        pre_data = {}
        for u in Extractor.attack_form(pre_attack):
            k, v = u
            pre_data[k] = v
        if troops:
            pre_data.update(troops)
        else:
            pre_data.update(self.troopmanager.troops)

        if vid not in self.map.map_pos:
            return False

        x, y = self.map.map_pos[vid]
        post_data = {"x": x, "y": y, "target_type": "coord", "attack": "Aanvallen"}
        pre_data.update(post_data)

        confirm_url = f"game.php?village={self.village_id}&screen=place&try=confirm"
        conf = self.wrapper.post_url(url=confirm_url, data=pre_data)
        if '<div class="error_box">' in conf.text:
            return False
        duration = Extractor.attack_duration(conf)
        if self.forced_peace_time:
            now = datetime.now()
            if now + timedelta(seconds=duration) > self.forced_peace_time:
                self.logger.info("Attack would arrive after the forced peace timer, not sending attack!")
                return "forced_peace"

        self.logger.info(
            "[Attack] %s -> %s duration %f.1 h", self.village_id, vid, duration / 3600
        )

        confirm_data = {}
        for u in Extractor.attack_form(conf):
            k, v = u
            if k == "support":
                continue
            confirm_data[k] = v
        new_data = {"building": "main", "h": self.wrapper.last_h}
        confirm_data.update(new_data)
        # The extractor doesn't like the empty cb value, and mistakes its value for x. So I add it here.
        if "x" not in confirm_data:
            confirm_data["x"] = x

        result = self.wrapper.get_api_action(
            village_id=self.village_id,
            action="popup_command",
            params={"screen": "place"},
            data=confirm_data,
        )

        return result


class AttackCache:
    schema_version = 2
    cache_dir = "cache/farms"
    legacy_cache_dir = "cache/attacks"

    @staticmethod
    def _now():
        return int(time.time())

    @staticmethod
    def _base_entry(village_id):
        return {
            "schema_version": AttackCache.schema_version,
            "target_vid": str(village_id),
            "scout": False,
            "safe": False,
            "high_profile": False,
            "low_profile": False,
            "last_attack": 0,
            "last_scout": 0,
            "last_attacker": None,
            "latest_resources": {},
            "latest_buildings": {},
            "latest_defence_units": {},
            "latest_report_at": 0,
            "last_loot": 0,
            "total_loot": 0,
            "avg_loot": 0,
            "loot_reports": 0,
            "total_capacity": 0,
            "filled_capacity": 0,
            "avg_fill_rate": 0,
            "last_fill_rate": 0,
            "loss_percentage": 0,
            "sources": {},
        }

    @staticmethod
    def _merge_report_intel(entry, repman):
        if not repman:
            return entry

        newest = None
        for report_id in repman.last_reports:
            report = repman.last_reports[report_id]
            if str(report.get("dest")) != str(entry["target_vid"]):
                continue
            when = int(report.get("extra", {}).get("when", 0) or 0)
            if newest is None or when > int(newest.get("extra", {}).get("when", 0) or 0):
                newest = report

        if not newest:
            return entry

        extra = newest.get("extra", {})
        entry["latest_report_at"] = int(extra.get("when", 0) or 0)
        if extra.get("resources") is not None:
            entry["latest_resources"] = extra.get("resources", {})
        if extra.get("buildings") is not None:
            entry["latest_buildings"] = extra.get("buildings", {})
        if extra.get("defence_units") is not None:
            entry["latest_defence_units"] = extra.get("defence_units", {})
        return entry

    @staticmethod
    def _normalize(village_id, entry):
        normalized = AttackCache._base_entry(village_id)
        if not entry:
            return None

        if entry.get("schema_version") == AttackCache.schema_version:
            normalized.update(entry)
            normalized["target_vid"] = str(normalized.get("target_vid") or village_id)
            normalized.setdefault("sources", {})
            for key in [
                "last_loot",
                "total_loot",
                "avg_loot",
                "loot_reports",
                "total_capacity",
                "filled_capacity",
                "avg_fill_rate",
                "last_fill_rate",
                "loss_percentage",
            ]:
                normalized[key] = normalized.get(key, 0)
            return normalized

        last_attack = int(entry.get("last_attack", 0) or 0)
        normalized.update({
            "scout": bool(entry.get("scout", False)),
            "safe": bool(entry.get("safe", False)),
            "high_profile": bool(entry.get("high_profile", False)),
            "low_profile": bool(entry.get("low_profile", False)),
            "last_attack": last_attack,
            "last_scout": last_attack if entry.get("scout") and not entry.get("safe") else 0,
            "last_attacker": entry.get("last_attacker"),
        })
        if normalized["last_attacker"]:
            normalized["sources"][str(normalized["last_attacker"])] = {
                "last_attack": last_attack,
                "last_scout": normalized["last_scout"],
                "attacks": 1 if last_attack else 0,
                "scouts": 1 if normalized["last_scout"] else 0,
                "safe": normalized["safe"],
            }
        return normalized

    @staticmethod
    def get_cache(village_id):
        FileManager.create_directories([AttackCache.cache_dir])
        current = FileManager.load_json_file(
            f"{AttackCache.cache_dir}/{village_id}.json"
        )
        if current:
            return AttackCache._normalize(village_id, current)

        legacy = FileManager.load_json_file(
            f"{AttackCache.legacy_cache_dir}/{village_id}.json"
        )
        normalized = AttackCache._normalize(village_id, legacy)
        if normalized:
            FileManager.save_json_file(
                normalized,
                f"{AttackCache.cache_dir}/{village_id}.json"
            )
        return normalized

    @staticmethod
    def get_legacy_cache(village_id):
        return AttackCache._normalize(
            village_id,
            FileManager.load_json_file(f"{AttackCache.legacy_cache_dir}/{village_id}.json")
        )

    @staticmethod
    def set_cache(village_id, entry, source_village_id=None, action="update", repman=None):
        now = AttackCache._now()
        current = AttackCache.get_cache(village_id) or AttackCache._base_entry(village_id)
        source = str(source_village_id) if source_village_id else None

        current.update({
            "schema_version": AttackCache.schema_version,
            "target_vid": str(village_id),
            "scout": bool(entry.get("scout", current.get("scout", False))),
            "safe": bool(entry.get("safe", current.get("safe", False))),
            "high_profile": bool(entry.get("high_profile", current.get("high_profile", False))),
            "low_profile": bool(entry.get("low_profile", current.get("low_profile", False))),
        })
        for key in [
            "last_loot",
            "total_loot",
            "avg_loot",
            "loot_reports",
            "total_capacity",
            "filled_capacity",
            "avg_fill_rate",
            "last_fill_rate",
            "loss_percentage",
        ]:
            if key in entry:
                current[key] = entry[key]
        if action in ["attack", "scout"]:
            current["last_attack"] = now
            current["last_attacker"] = source
            if action == "scout":
                current["last_scout"] = now

        if source and action in ["attack", "scout"]:
            source_entry = current["sources"].get(source, {
                "last_attack": 0,
                "last_scout": 0,
                "attacks": 0,
                "scouts": 0,
                "safe": False,
            })
            source_entry["last_attack"] = now
            source_entry["attacks"] = int(source_entry.get("attacks", 0)) + 1
            source_entry["safe"] = current["safe"]
            if action == "scout":
                source_entry["last_scout"] = now
                source_entry["scouts"] = int(source_entry.get("scouts", 0)) + 1
            current["sources"][source] = source_entry

        current = AttackCache._merge_report_intel(current, repman)
        FileManager.create_directories([AttackCache.cache_dir])
        return FileManager.save_json_file(
            current,
            f"{AttackCache.cache_dir}/{village_id}.json"
        )

    @staticmethod
    def cache_grab():
        output = {}

        FileManager.create_directories([AttackCache.cache_dir, AttackCache.legacy_cache_dir])

        for existing in FileManager.list_directory(AttackCache.legacy_cache_dir, ends_with=".json"):
            village_id = existing.replace(".json", "")
            output[village_id] = AttackCache.get_cache(village_id)

        for existing in FileManager.list_directory(AttackCache.cache_dir, ends_with=".json"):
            village_id = existing.replace(".json", "")
            output[village_id] = AttackCache.get_cache(village_id)
        return output
