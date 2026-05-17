"""
Map management, pls don't read this code.
"""
import logging
import math
import time

from core.extractors import Extractor
from core.filemanager import FileManager


class Map:
    """
    Class to manage the world around you
    """
    wrapper = None
    village_id = None
    map_data = []
    villages = {}
    my_location = None
    map_pos = {}
    last_fetch = 0
    fetch_delay = 8
    discovery_enabled = False
    discovery_radius = 0
    discovery_step = 20
    discovery_max_requests_per_run = 0
    discovery_refresh_hours = 24

    def __init__(self, wrapper=None, village_id=None):
        """
        Creates the map files
        """
        self.wrapper = wrapper
        self.village_id = village_id
        self.map_data = []
        self.villages = {}
        self.my_location = None
        self.map_pos = {}
        self.last_fetch = 0
        self.fetch_delay = 8
        self.discovery_enabled = False
        self.discovery_radius = 0
        self.discovery_step = 20
        self.discovery_max_requests_per_run = 0
        self.discovery_refresh_hours = 24
        self.load_cached_villages()

    def configure_discovery(self, config):
        """
        Configures optional wider map discovery. This only visits map screens and
        fills cache/villages; farming still uses farms.search_radius as its limit.
        """
        config = config or {}
        self.fetch_delay = float(config.get("fetch_delay_hours", self.fetch_delay) or 8)
        self.discovery_enabled = bool(config.get("discovery_enabled", False))
        self.discovery_radius = max(0, int(config.get("discovery_radius", 0) or 0))
        self.discovery_step = max(1, int(config.get("discovery_step", 20) or 20))
        self.discovery_max_requests_per_run = max(
            0,
            int(config.get("max_discovery_requests_per_run", 0) or 0),
        )
        self.discovery_refresh_hours = max(
            1,
            int(config.get("discovery_refresh_hours", 24) or 24),
        )

    def get_map(self):
        """
        Fetch the map every 24ish hours and update the cache entries
        """
        if self.last_fetch + (self.fetch_delay * 3600) > time.time():
            return
        self.last_fetch = time.time()
        res = self.fetch_map_screen()
        if not self.parse_map_response(res):
            return False
        self.discover_wider_map()
        return True

    def fetch_map_screen(self, center=None):
        url = f"game.php?village={self.village_id}&screen=map"
        if center:
            url = f"{url}&x={int(center[0])}&y={int(center[1])}"
        return self.wrapper.get_url(url)

    def parse_map_response(self, res):
        game_state = Extractor.game_state(res)
        self.map_data = Extractor.map_data(res)
        if self.map_data:
            self.parse_map_data(game_state)
        if not self.map_data or not self.villages:
            return self.get_map_old(game_state=game_state)
        return True

    def parse_map_data(self, game_state):
        for tile in self.map_data:
            data = tile["data"]
            x = int(data["x"])
            y = int(data["y"])
            vdata = data["villages"]
            # Fix broken parsing
            if type(vdata) is dict:
                cdata = [{} for _ in range(20)]
                for k, v in vdata.items():
                    if type(v) is not dict:
                        cdata[int(k)] = {0: item[0:] for item in v}
                    else:
                        cdata[int(k)] = v
                vdata = cdata
            for lon, val in enumerate(vdata):
                if not val:
                    continue
                # Force dict type to iterate properly
                if type(val) != dict:
                    val = {i: val[i] for i in range(0, len(val))}
                for lat, entry in val.items():
                    if not lat:
                        continue
                    coords = [x + int(lon), y + int(lat)]
                    if entry[0] == str(self.village_id):
                        self.my_location = coords

                    self.build_cache_entry(location=coords, entry=entry)
            if not self.my_location:
                self.my_location = [
                    game_state["village"]["x"],
                    game_state["village"]["y"],
                ]

    def load_cached_villages(self):
        try:
            files = FileManager.list_directory("cache/villages", ends_with=".json")
        except Exception:
            return

        for filename in files:
            try:
                entry = FileManager.load_json_file(f"cache/villages/{filename}")
            except Exception:
                continue
            if not isinstance(entry, dict):
                continue
            vid = str(entry.get("id") or filename.rsplit(".", 1)[0])
            location = entry.get("location")
            if not vid or not location:
                continue
            self.villages[vid] = entry
            self.map_pos[vid] = location
            if vid == str(self.village_id):
                self.my_location = location

    def discover_wider_map(self):
        if (
                not self.discovery_enabled
                or self.discovery_radius <= 0
                or self.discovery_max_requests_per_run <= 0
                or not self.my_location
        ):
            return

        cache = self.get_discovery_cache()
        centers = self.discovery_centers(cache)
        if not centers:
            return

        fetched = 0
        now = int(time.time())
        for center in centers[:self.discovery_max_requests_per_run]:
            key = self.discovery_center_key(center)
            try:
                res = self.fetch_map_screen(center=center)
                if self.parse_map_response(res):
                    fetched += 1
            except Exception:
                logging.exception(
                    "Unable to discover map center %s for village %s",
                    key,
                    self.village_id,
                )
            cache["centers"][key] = now

        cache["last_run"] = now
        self.set_discovery_cache(cache)
        if fetched:
            logging.info(
                "Discovered %d wider map centers for village %s",
                fetched,
                self.village_id,
            )

    def discovery_center_key(self, center):
        return "%d,%d" % (int(center[0]), int(center[1]))

    def discovery_centers(self, cache):
        origin = self.my_location
        now = int(time.time())
        refresh_after = self.discovery_refresh_hours * 3600
        known_centers = cache.get("centers", {})
        if not isinstance(known_centers, dict):
            known_centers = {}
        unseen = []
        stale = []
        for dx in range(-self.discovery_radius, self.discovery_radius + 1, self.discovery_step):
            for dy in range(-self.discovery_radius, self.discovery_radius + 1, self.discovery_step):
                if dx == 0 and dy == 0:
                    continue
                distance = math.sqrt(dx ** 2 + dy ** 2)
                if distance > self.discovery_radius:
                    continue
                center = [origin[0] + dx, origin[1] + dy]
                key = self.discovery_center_key(center)
                last_seen = int(known_centers.get(key, 0) or 0)
                entry = (distance, last_seen, center)
                if not last_seen:
                    unseen.append(entry)
                elif last_seen + refresh_after <= now:
                    stale.append(entry)

        unseen.sort(key=lambda item: (item[0], item[2][0], item[2][1]))
        stale.sort(key=lambda item: (item[1], item[0], item[2][0], item[2][1]))
        return [item[2] for item in unseen + stale]

    def get_discovery_cache(self):
        data = FileManager.load_json_file(self.discovery_cache_path())
        if not isinstance(data, dict):
            data = {}
        if not isinstance(data.get("centers"), dict):
            data["centers"] = {}
        return data

    def set_discovery_cache(self, data):
        FileManager.create_directories(["cache/map_discovery"])
        FileManager.save_json_file(data, self.discovery_cache_path())

    def discovery_cache_path(self):
        return f"cache/map_discovery/{self.village_id}.json"

    def get_map_old(self, game_state):
        """
        Old method of parsing the map, might work, might not, who knows
        """
        if self.map_data:
            for tile in self.map_data:
                data = tile["data"]
                x = int(data["x"])
                y = int(data["y"])
                vdata = data["villages"]
                for lon, lon_val in enumerate(vdata):
                    try:
                        for lat in vdata[lon]:
                            coords = [x + int(lon), y + int(lat)]
                            entry = vdata[lon][lat]
                            if entry[0] == str(self.village_id):
                                self.my_location = coords

                            self.build_cache_entry(location=coords, entry=entry)
                    except:
                        raise
            if not self.my_location:
                self.my_location = [
                    game_state["village"]["x"],
                    game_state["village"]["y"],
                ]
        if not self.map_data or not self.villages:
            logging.warning(
                "Error reading map state for village %s, farming might not work properly",
                self.village_id
            )
            return False
        return True

    def build_cache_entry(self, location, entry):
        """
        Builds a cache entry based on their weird data structure
        """
        vid = entry[0]
        name = entry[2]
        try:
            points = int(entry[3].replace(".", ""))
        except ValueError:
            # Breaks farming logic on event villages
            return
        player = entry[4]
        bonus = entry[6]
        clan = entry[11]
        structure = {
            "id": vid,
            "name": name,
            "location": location,
            "bonus": bonus,
            "points": points,
            "safe": False,
            "scout": False,
            "tribe": clan,
            "owner": player,
            "buildings": {},
            "resources": {},
            "last_seen": int(time.time()),
        }
        self.map_pos[vid] = location
        cached = self.in_cache(vid)
        if not cached:
            MapCache.set_cache(village_id=vid, entry=structure)
        if cached and cached != structure:
            MapCache.set_cache(village_id=vid, entry=structure)
        self.villages[vid] = structure

    def in_cache(self, vid):
        """
        Checks if a village is already in the village cache
        """
        entry = MapCache.get_cache(village_id=vid)
        return entry

    def get_dist(self, ext_loc):
        """
        Calculates distance from current village to coords
        """
        distance = math.sqrt(
            ((self.my_location[0] - ext_loc[0]) ** 2)
            + ((self.my_location[1] - ext_loc[1]) ** 2)
        )
        return distance


class MapCache:
    """
    Holds a cache of all found villages within a certain distance
    """
    @staticmethod
    def get_cache(village_id):
        """
        Get data from the cache
        """
        return FileManager.load_json_file(f"cache/villages/{village_id}.json")

    @staticmethod
    def set_cache(village_id, entry):
        """
        Creates or updates a cache entry
        """
        FileManager.save_json_file(entry, f"cache/villages/{village_id}.json")
