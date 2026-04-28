"""
Class for using one generic cookie jar, emulating a single tab
"""

import requests

from core.filemanager import FileManager
from core.notification import Notification

import logging
import re
import sys
import time
import random
import uuid
from urllib.parse import urljoin, urlencode

from core.reporter import ReporterObject


class WebWrapper:
    """
    WebWrapper object for sending HTTP requests
    """
    web = None
    headers = {
        'user-agent': 'Mozilla/5.0 (Windows NT 6.3; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/78.0.3904.97 Safari/537.36',
        'upgrade-insecure-requests': '1'
    }
    endpoint = None
    logger = logging.getLogger("Requests")
    server = None
    last_response = None
    last_h = None
    priority_mode = False
    auth_endpoint = None
    reporter = None
    delay = 1.0
    request_rate_limit_enabled = True
    idle_backoff_on_error_seconds = 300
    pause_on_bot_protection = True
    min_request_interval = 1.0
    last_request_at = 0
    humanizer_enabled = False
    humanizer_ambient_chance = 0.05
    humanizer_idle_chance = 0.0
    humanizer_idle_gap_min = 300
    humanizer_idle_gap_max = 900
    humanizer_warmup_after_seconds = 1800
    humanizer_last_action_at = 0
    humanizer_last_idle_at = 0
    humanizer_ambient_screens = ["overview", "map", "ranking", "report"]

    def __init__(self, url, server=None, endpoint=None, reporter_enabled=False, reporter_constr=None):
        """
        Construct the session and detect variables
        """
        self.web = requests.session()
        self.headers = dict(self.headers)
        self.auth_endpoint = url
        self.server = server
        self.endpoint = endpoint
        self.reporter = ReporterObject(enabled=reporter_enabled, connection_string=reporter_constr)
        self.last_request_at = 0
        self.humanizer_last_action_at = 0
        self.humanizer_last_idle_at = 0

    def configure_from_bot_config(self, bot_config):
        """
        Applies conservative request hygiene settings from config.
        """
        self.request_rate_limit_enabled = bot_config.get(
            "request_rate_limit_enabled", True
        )
        self.idle_backoff_on_error_seconds = bot_config.get(
            "idle_backoff_on_error_seconds", 300
        )
        self.pause_on_bot_protection = bot_config.get(
            "pause_on_bot_protection", True
        )
        self.humanizer_enabled = bot_config.get("humanizer_enabled", False)
        self.humanizer_ambient_chance = bot_config.get(
            "humanizer_ambient_chance", 0.05
        )
        self.humanizer_idle_chance = bot_config.get(
            "humanizer_idle_chance", 0.0
        )
        self.humanizer_idle_gap_min = bot_config.get(
            "humanizer_idle_gap_min", 300
        )
        self.humanizer_idle_gap_max = bot_config.get(
            "humanizer_idle_gap_max", 900
        )
        self.humanizer_warmup_after_seconds = bot_config.get(
            "humanizer_warmup_after_seconds", 1800
        )

    def _sleep_before_request(self):
        if self.priority_mode:
            return
        if self.humanizer_enabled:
            delay = random.gauss(5 * self.delay, 1.5 * self.delay)
            time.sleep(max(0.5, delay))
        else:
            time.sleep(random.randint(int(3 * self.delay), int(7 * self.delay)))
        if not self.request_rate_limit_enabled:
            return
        elapsed = time.time() - self.last_request_at
        if elapsed < self.min_request_interval:
            time.sleep(self.min_request_interval - elapsed)

    def _record_request_event(self, method, url, status=None, event="request"):
        FileManager.create_directories(["cache/request_events"])
        payload = {
            "when": int(time.time()),
            "method": method,
            "url": url,
            "status": status,
            "event": event,
        }
        path = "cache/request_events/%d_%s.json" % (
            payload["when"],
            uuid.uuid4().hex[:8],
        )
        FileManager.save_json_file(payload, path)

    def _humanizer_get(self, url, event):
        if self.priority_mode:
            return None
        self.headers['Origin'] = (self.endpoint if self.endpoint else self.auth_endpoint).rstrip('/')
        self._sleep_before_request()
        full_url = urljoin(self.endpoint if self.endpoint else self.auth_endpoint, url)
        try:
            response = self.web.get(url=full_url, headers=self.headers)
            self.logger.debug("Humanizer GET %s [%d]", full_url, response.status_code)
            self.post_process(response)
            self._record_request_event("GET", full_url, response.status_code, event)
            self._handle_bot_protection("GET", full_url, response)
            self._backoff_after_unusual_response("GET", full_url, response)
            self.humanizer_last_action_at = time.time()
            return response
        except Exception as e:
            self.logger.warning("Humanizer GET %s: %s", full_url, str(e))
            self._record_request_event("GET", full_url, None, "humanizer_exception")
            return None

    def maybe_humanize(self, village_id=None, warmup=False):
        if not self.humanizer_enabled or self.priority_mode:
            return False

        now = time.time()
        if (
                warmup
                and self.last_request_at
                and now - self.last_request_at > int(self.humanizer_warmup_after_seconds)
        ):
            url = "game.php?screen=overview"
            if village_id:
                url = f"game.php?village={village_id}&screen=overview"
            self.logger.info("Humanizer warm-up visit: overview")
            self._humanizer_get(url, "humanizer_warmup")
            return True

        if (
                self.humanizer_idle_chance > 0
                and now - self.humanizer_last_idle_at > int(self.humanizer_idle_gap_min)
                and random.random() < float(self.humanizer_idle_chance)
        ):
            idle_min = int(self.humanizer_idle_gap_min)
            idle_max = max(idle_min, int(self.humanizer_idle_gap_max))
            wait = random.randint(
                idle_min,
                idle_max,
            )
            self.logger.info("Humanizer idle gap for %d seconds", wait)
            self._record_request_event("SLEEP", "humanizer_idle", None, "humanizer_idle")
            time.sleep(wait)
            self.humanizer_last_idle_at = time.time()
            return True

        if random.random() >= float(self.humanizer_ambient_chance):
            return False

        screen = random.choice(self.humanizer_ambient_screens)
        url = f"game.php?screen={screen}"
        if village_id:
            url = f"game.php?village={village_id}&screen={screen}"
        self.logger.info("Humanizer ambient visit: %s", screen)
        self._humanizer_get(url, "humanizer_ambient")
        return True

    def _backoff_after_unusual_response(self, method, url, response):
        if not response or response.status_code < 400:
            return
        self._record_request_event(method, url, response.status_code, "unusual_response")
        if response.status_code in [429, 500, 502, 503, 504]:
            wait = int(self.idle_backoff_on_error_seconds)
            self.logger.warning(
                "%s %s returned %d, backing off for %d seconds",
                method, url, response.status_code, wait
            )
            time.sleep(wait)

    def _handle_bot_protection(self, method, url, response):
        if not response or 'data-bot-protect="forced"' not in response.text:
            return False
        self.logger.warning("Bot protection hit; stopping bot")
        self._record_request_event(method, url, response.status_code, "bot_protection")
        self.reporter.report(
            0,
            "TWB_RECAPTCHA",
            "Bot protection hit; stopping bot, restart manually",
        )
        Notification.send("Bot protection hit; bot stopped, restart manually")
        if self.pause_on_bot_protection:
            sys.exit(1)
        time.sleep(int(self.idle_backoff_on_error_seconds))
        return True

    def post_process(self, response):
        """
        Post-processes all requests and stores data used for the next request
        """
        xsrf = re.search('<meta content="(.+?)" name="csrf-token"', response.text)
        if xsrf:
            self.headers['x-csrf-token'] = xsrf.group(1)
            self.logger.debug("Set CSRF token")
        elif 'x-csrf-token' in self.headers:
            del self.headers['x-csrf-token']
        self.headers['Referer'] = response.url
        self.last_response = response
        get_h = re.search(r'&h=(\w+)', response.text)
        if get_h:
            self.last_h = get_h.group(1)
        self.last_request_at = time.time()

    def get_url(self, url, headers=None):
        """
        Fetches a URL using a basic GET request
        """
        self.headers['Origin'] = (self.endpoint if self.endpoint else self.auth_endpoint).rstrip('/')
        self._sleep_before_request()
        url = urljoin(self.endpoint if self.endpoint else self.auth_endpoint, url)
        if not headers:
            headers = self.headers
        try:
            res = self.web.get(url=url, headers=headers)
            self.logger.debug("GET %s [%d]", url, res.status_code)
            self.post_process(res)
            self._record_request_event("GET", url, res.status_code)
            self._handle_bot_protection("GET", url, res)
            self._backoff_after_unusual_response("GET", url, res)
            return res
        except Exception as e:
            self.logger.warning("GET %s: %s", url, str(e))
            self._record_request_event("GET", url, None, "request_exception")
            time.sleep(int(self.idle_backoff_on_error_seconds))
            return None

    def post_url(self, url, data, headers=None):
        """
        Sends a basic POST request with urlencoded postdata
        """
        self._sleep_before_request()
        self.headers['Origin'] = (self.endpoint if self.endpoint else self.auth_endpoint).rstrip('/')
        url = urljoin(self.endpoint if self.endpoint else self.auth_endpoint, url)
        enc = urlencode(data)
        if not headers:
            headers = self.headers
        try:
            res = self.web.post(url=url, data=data, headers=headers)
            self.logger.debug("POST %s %s [%d]", url, enc, res.status_code)
            self.post_process(res)
            self._record_request_event("POST", url, res.status_code)
            self._handle_bot_protection("POST", url, res)
            self._backoff_after_unusual_response("POST", url, res)
            return res
        except Exception as e:
            self.logger.warning("POST %s %s: %s", url, enc, str(e))
            self._record_request_event("POST", url, None, "request_exception")
            time.sleep(int(self.idle_backoff_on_error_seconds))
            return None

    def start(self, ):
        """
        Start the bot and verify whether the last session is still valid
        """
        session_data = FileManager.load_json_file("cache/session.json")
        if session_data:
            self.web.cookies.update(session_data['cookies'])
            get_test = self.get_url("game.php?screen=overview")
            if "game.php" in get_test.url:
                return True
            self.logger.warning("Current session cache not valid")

        self.web.cookies.clear()
        cinp = input("Enter browser cookie string> ")
        cookies = {}
        cinp = cinp.strip()
        for itt in cinp.split(';'):
            itt = itt.strip()
            kvs = itt.split("=")
            k = kvs[0]
            v = '='.join(kvs[1:])
            cookies[k] = v
        self.web.cookies.update(cookies)
        self.logger.info("Game Endpoint: %s", self.endpoint)

        for c in self.web.cookies:
            cookies[c.name] = c.value

        FileManager.save_json_file({
            'endpoint': self.endpoint,
            'server': self.server,
            'cookies': cookies
        }, "cache/session.json")

    def get_action(self, village_id, action):
        """
        Runs an action on a specific village
        """
        url = "game.php?village=%s&screen=%s" % (village_id, action)
        response = self.get_url(url)
        return response

    def get_api_data(self, village_id, action, params={}):

        custom = dict(self.headers)
        custom['accept'] = "application/json, text/javascript, */*; q=0.01"
        custom['x-requested-with'] = "XMLHttpRequest"
        custom['tribalwars-ajax'] = "1"
        req = {
            'ajax': action,
            'village': village_id,
            'screen': 'api'
        }
        req.update(params)
        payload = f"game.php?{urlencode(req)}"
        url = urljoin(self.endpoint, payload)
        res = self.get_url(url, headers=custom)
        if res.status_code == 200:
            try:
                return res.json()
            except:
                return res

    def post_api_data(self, village_id, action, params={}, data={}):
        """
        Simulates an API request
        """
        custom = dict(self.headers)
        custom['accept'] = "application/json, text/javascript, */*; q=0.01"
        custom['x-requested-with'] = "XMLHttpRequest"
        custom['tribalwars-ajax'] = "1"
        req = {
            'ajax': action,
            'village': village_id,
            'screen': 'api'
        }
        req.update(params)
        payload = f"game.php?{urlencode(req)}"
        url = urljoin(self.endpoint, payload)
        if 'h' not in data:
            data['h'] = self.last_h
        res = self.post_url(url, data=data, headers=custom)
        if res.status_code == 200:
            try:
                return res.json()
            except:
                return res

    def get_api_action(self, village_id, action, params={}, data={}):
        """
        Simulates an API action being triggered
        """
        custom = dict(self.headers)
        custom['Accept'] = "application/json, text/javascript, */*; q=0.01"
        custom['X-Requested-With'] = "XMLHttpRequest"
        custom['TribalWars-Ajax'] = "1"
        req = {
            'ajaxaction': action,
            'village': village_id,
            'screen': 'api'
        }
        req.update(params)
        payload = f"game.php?{urlencode(req)}"
        url = urljoin(self.endpoint, payload)
        if 'h' not in data:
            data['h'] = self.last_h
        res = self.post_url(url, data=data, headers=custom)
        if res.status_code == 200:
            try:
                return res.json()
            except:
                return res
        return None
