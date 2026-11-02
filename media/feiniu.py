"""fnOS media adapter, verified against trim.media 0.9.8-1."""

import hashlib
import json
import logging
import math
import secrets
import threading
import time
from pathlib import PurePosixPath
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit

import requests

from abstract_classes import Media, MediaException

logger = logging.getLogger(__name__)

# Public signing constants shipped in the fnOS media web client, version 631.
_API_KEY = "16CCEB3D-AB42-077D-36A1-F355324E4237"
_SIGN_SALT = "NDzZTVxnRKP8Z0jXg1VAMonaG8akvh"
_API = "/v/api/v1"
_WEB_CLIENT_VERSION = "631"


def signed_headers(method, path, token, payload=None):
    url = urlsplit(path)
    if method.upper() == "GET":
        params = {k: v for k, v in parse_qsl(url.query, keep_blank_values=True)
                  if v not in ("null", "undefined")}
        content = unquote(urlencode(sorted(params.items()), quote_via=quote))
    else:
        content = payload.decode("utf-8") if payload is not None else ""
    content_hash = hashlib.md5(content.encode("utf-8")).hexdigest()
    nonce = str(secrets.randbelow(900000) + 100000)
    timestamp = str(int(time.time() * 1000))
    source = "_".join((_SIGN_SALT, url.path, nonce, timestamp, content_hash, _API_KEY))
    signature = hashlib.md5(source.encode("utf-8")).hexdigest()
    return {
        "Authorization": token,
        "X-Trim-Client": "web",
        "X-Trim-Client-Version": _WEB_CLIENT_VERSION,
        "authx": f"nonce={nonce}&timestamp={timestamp}&sign={signature}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


class PlaybackRecords:
    """Separate changing playback positions from metadata and continuous progress."""

    def __init__(self, idle_timeout, repeat_timeout):
        self.idle_timeout = idle_timeout
        self.repeat_timeout = repeat_timeout
        self.previous = None
        self.activity = {}
        self.dispatched = {}

    def reset_baseline(self):
        self.previous = None

    @staticmethod
    def _fingerprint(record):
        return record["media_guid"], record.get("video_guid"), record["ts"]

    def update(self, records, now):
        if not isinstance(records, list):
            raise MediaException("Feiniu play/list did not return a list")
        current = {}
        for row in records:
            if not isinstance(row, dict):
                raise MediaException("Invalid Feiniu playback record")
            guid, media_guid, position = row.get("guid"), row.get("media_guid"), row.get("ts")
            if (not isinstance(guid, str) or not guid
                    or not isinstance(media_guid, str) or not media_guid
                    or isinstance(position, bool) or not isinstance(position, (int, float))
                    or not math.isfinite(position) or position < 0 or guid in current):
                raise MediaException("Invalid Feiniu playback record fields")
            current[guid] = dict(row)
        previous, self.previous = self.previous, current
        if previous is None:
            return []
        candidates = []
        for guid, row in current.items():
            old = previous.get(guid)
            if old is not None and self._fingerprint(old) == self._fingerprint(row):
                continue
            # Completion and manual/automatic watched changes are not play requests.
            if row["ts"] == 0 or (old is not None and old.get("watched") != row.get("watched")):
                continue
            state = self.activity.get(guid)
            if (state is None or now - state["changed_at"] >= self.idle_timeout
                    or state["media_guid"] != row["media_guid"]):
                state = {"handled": False, "media_guid": row["media_guid"]}
                self.activity[guid] = state
            state["changed_at"] = now
            if not state["handled"]:
                state["handled"] = True
                last = self.dispatched.get(guid)
                if last is None or now - last >= self.repeat_timeout:
                    self.dispatched[guid] = now
                    candidates.append(row)
        retention = max(self.idle_timeout, self.repeat_timeout)
        self.activity = {guid: state for guid, state in self.activity.items()
                         if guid in current or now - state["changed_at"] < retention}
        self.dispatched = {guid: at for guid, at in self.dispatched.items()
                           if now - at < self.repeat_timeout}
        return candidates


class Feiniu(Media):
    def __init__(self, player, tv, av, config):
        super().__init__(player, tv, av, config)
        self._host = str(config.get("Host") or "").rstrip("/")
        url = urlsplit(self._host)
        if (url.scheme not in ("http", "https") or not url.hostname
                or url.username is not None or url.password is not None
                or url.path or url.query or url.fragment):
            raise MediaException("Feiniu Host must be a service root, e.g. http://192.168.1.10:8005")
        self._username = config.get("Username")
        self._password = config.get("Password")
        if not isinstance(self._username, str) or not self._username:
            raise MediaException("Feiniu Username is required")
        if not isinstance(self._password, str):
            raise MediaException("Feiniu Password must be a string")
        self._poll_interval = self._number(config, "PollInterval", 2, 0.5)
        self._offline_interval = self._number(config, "OfflineCheckInterval", 5, 1)
        self._request_timeout = self._number(config, "RequestTimeout", 10, 1)
        self._startup_timeout = self._number(config, "StartupTimeout", 60, 1)
        idle = self._number(config, "ActivityIdleTimeout", 90, 30)
        repeat = self._number(config, "RepeatFilterTimeout", 120, 0)
        self._only_when_online = config.get("OnlyWhenPlayerOnline", True)
        if not isinstance(self._only_when_online, bool):
            raise MediaException("Feiniu OnlyWhenPlayerOnline must be a boolean")
        excluded = config.get("ExcludeVideoExt") or []
        if not isinstance(excluded, list) or not all(isinstance(ext, str) for ext in excluded):
            raise MediaException("Feiniu ExcludeVideoExt must be a list of extensions")
        self._excluded = {ext.lower().lstrip(".") for ext in excluded}
        self._records = PlaybackRecords(idle, repeat)
        self._access_token = None
        self._auth_lock = threading.Lock()
        self._play_lock = threading.RLock()
        self._active = None
        self._polling_online = None
        self._stop_event = threading.Event()
        self._thread = None

    @staticmethod
    def _number(config, key, default, minimum):
        value = config.get(key, default)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < minimum):
            raise MediaException(f"Feiniu {key} must be a finite number >= {minimum}")
        return value

    def _token(self):
        with self._auth_lock:
            if self._access_token is not None:
                return self._access_token
            headers = {
                "X-Emby-Authorization": (
                    'MediaBrowser Client="BlurayPoster Feiniu", Device="BlurayPoster", '
                    'DeviceId="blurayposter-feiniu", Version="1.0.0"'
                ),
                "Accept": "application/json",
            }
            try:
                response = requests.post(
                    self._host + "/Users/AuthenticateByName", headers=headers,
                    json={"Username": self._username, "Pw": self._password},
                    timeout=self._request_timeout, allow_redirects=False,
                )
            except requests.RequestException as error:
                raise MediaException(f"Feiniu login transport failed ({type(error).__name__})") from None
            result = self._json(response, "login")
            token = result.get("AccessToken") if isinstance(result, dict) else None
            if not isinstance(token, str) or not token:
                raise MediaException("Feiniu login did not return an access token")
            self._access_token = token
            logger.info("Feiniu login successful")
            return token

    @staticmethod
    def _json(response, route):
        if response.status_code != 200:
            raise MediaException(f"Feiniu {route} returned HTTP {response.status_code}")
        if "json" not in response.headers.get("Content-Type", "").lower():
            raise MediaException(f"Feiniu {route} did not return JSON; check the media service port")
        try:
            return response.json()
        except ValueError:
            raise MediaException(f"Feiniu {route} returned invalid JSON") from None

    def _native_request(self, method, path, body=None):
        payload = None if body is None else json.dumps(
            body, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        for attempt in range(2):
            token = self._token()
            headers = signed_headers(method, path, token, payload)
            try:
                response = requests.request(
                    method, self._host + path, data=payload, headers=headers,
                    timeout=self._request_timeout, allow_redirects=False,
                )
            except requests.RequestException as error:
                raise MediaException(f"Feiniu request transport failed ({type(error).__name__})") from None
            if response.status_code in (401, 403) and attempt == 0:
                with self._auth_lock:
                    if self._access_token == token:
                        self._access_token = None
                continue
            result = self._json(response, path.split("?")[0])
            if not isinstance(result, dict) or not isinstance(result.get("code"), int):
                raise MediaException("Feiniu API response has no business status code")
            if result["code"] == 0:
                return result.get("data")
            # Some releases return authentication failures inside HTTP 200.
            # A failed read can safely be retried after one fresh login.
            if method == "GET" and attempt == 0:
                with self._auth_lock:
                    if self._access_token == token:
                        self._access_token = None
                continue
            raise MediaException(f"Feiniu API {path.split('?')[0]} failed, code {result['code']}")

    def _resolve_item(self, record):
        files = self._native_request("GET", _API + "/media/itemfile/" + quote(record["guid"], safe=""))
        if not isinstance(files, list):
            raise MediaException("Feiniu itemfile did not return a list")
        matches = [item for item in files if isinstance(item, dict)
                   and item.get("media_guid") == record["media_guid"]
                   and item.get("item_guid", record["guid"]) == record["guid"]]
        if len(matches) != 1:
            raise MediaException("Feiniu selected media version has no unique matching file")
        path = matches[0].get("file_path")
        if not isinstance(path, str) or not path.startswith("/"):
            raise MediaException("Feiniu selected file has no absolute NAS path")
        file_path = PurePosixPath(path)
        if file_path.name.lower() in ("index.bdmv", "movieobject.bdmv") and file_path.parent.name.lower() == "bdmv":
            path, container = str(file_path.parent.parent), "bluray"
        elif file_path.name.lower() == "bdmv":
            path, container = str(file_path.parent), "bluray"
        else:
            extension = file_path.suffix.lower().lstrip(".")
            containers = {"iso", "mkv", "mp4", "m4v", "avi", "mov", "wmv",
                          "m2ts", "mts", "ts", "mpg", "mpeg", "flv", "webm"}
            if extension in containers:
                container = extension
            else:
                # Release names can contain dots. Confirm the selected version's
                # disc structure through the native file browser.
                children = self._native_request("POST", _API + "/server/path", {"path": path})
                bdmv = [child for child in children if isinstance(child, dict)
                        and str(child.get("filename", "")).lower() == "bdmv"
                        and child.get("is_dir") is True] if isinstance(children, list) else []
                if len(bdmv) != 1:
                    raise MediaException("Feiniu cannot identify the selected disc directory")
                contents = self._native_request("POST", _API + "/server/path", {
                    "path": str(file_path / bdmv[0]["filename"]),
                })
                if not isinstance(contents, list) or not any(
                        isinstance(child, dict)
                        and str(child.get("filename", "")).lower() == "index.bdmv"
                        and child.get("is_dir") is False for child in contents):
                    raise MediaException("Feiniu cannot identify the selected disc directory")
                container = "bluray"
        return {"Id": record["guid"], "Path": path, "Container": container,
                "watched": bool(record.get("watched")), "started": False}

    def _dispatch_record(self, record):
        if not self._check_player_online():
            return
        with self._play_lock:
            if self._active is not None:
                logger.info("Feiniu selection ignored while player is busy, item: %s", record["guid"])
                return
        item = self._resolve_item(record)
        extension = PurePosixPath(item["Path"]).suffix.lower().lstrip(".")
        if extension in self._excluded:
            logger.info("Feiniu video excluded by extension, item: %s", item["Id"])
            return
        # Resolution can take time; the player may have gone offline meanwhile.
        if not self._check_player_online():
            return
        if self._player is None:
            raise MediaException("Feiniu has no configured player")
        with self._play_lock:
            self._active = item
            item["requested_at"] = time.monotonic()

        def message(header, text):
            self.on_message(header, text)
            if str(header).lower() in ("error", "notification"):
                with self._play_lock:
                    if self._active is item and not item["started"]:
                        self._active = None

        try:
            result = self._player.play(
                item["Path"], item["Container"], message,
                lambda **kw: self.on_play_begin(context=item, **kw),
                lambda **kw: self.on_play_in_progress(context=item, **kw),
                lambda **kw: self.on_play_end(context=item, **kw),
            )
            with self._play_lock:
                if self._active is item and not item["started"]:
                    if result is False:
                        self._active = None
                    else:
                        item["requested_at"] = time.monotonic()
        except Exception:
            with self._play_lock:
                if self._active is item:
                    self._active = None
            raise MediaException("Feiniu player dispatch failed") from None

    def _check_player_online(self):
        online = True
        if self._only_when_online:
            probe = getattr(self._player, "is_online", None)
            if not callable(probe):
                raise MediaException("Feiniu player has no online probe; configure a supported player")
            try:
                online = probe()
                if online is None:
                    legacy_probe = getattr(self._player, "_is_port_open", None)
                    if callable(legacy_probe):
                        online = legacy_probe()
            except Exception as error:
                logger.debug("Feiniu player online probe failed (%s)", type(error).__name__)
                online = False
            if not isinstance(online, bool):
                raise MediaException("Feiniu player does not support online detection; set OnlyWhenPlayerOnline: false")
        if self._polling_online is not online:
            self._records.reset_baseline()
            self._polling_online = online
            logger.info("Feiniu playback polling %s", "resumed with a new baseline" if online else "paused: player offline")
        return online

    def _poll_once(self):
        if not self._check_player_online():
            return
        with self._play_lock:
            if (self._active is not None and not self._active["started"]
                    and time.monotonic() - self._active["requested_at"] >= self._startup_timeout):
                logger.warning("Feiniu player did not confirm startup before timeout")
                self._active = None
        rows = self._native_request("GET", _API + "/play/list")
        candidates = self._records.update(rows, time.monotonic())
        if len(candidates) > 1:
            logger.warning("Feiniu has multiple simultaneous playback changes; skipping ambiguous selection")
        elif candidates:
            try:
                self._dispatch_record(candidates[0])
            except MediaException as error:
                logger.error("%s", error)

    def _poll_loop(self):
        failures = 0
        while not self._stop_event.is_set():
            try:
                self._poll_once()
                failures = 0
            except Exception as error:
                self._records.reset_baseline()
                failures += 1
                # Never log response bodies, request headers or credentials.
                detail = str(error) if isinstance(error, MediaException) else type(error).__name__
                logger.warning("Feiniu polling failed (%s); next success establishes a new baseline", detail)
            delay = (self._offline_interval if self._polling_online is False
                     else min(60, self._poll_interval * 2 ** min(failures, 5)))
            self._stop_event.wait(delay)

    def _set_watched(self, item):
        if item["watched"]:
            return
        try:
            self._native_request("POST", _API + "/item/watched", {"item_guid": item["Id"]})
            item["watched"] = True
            logger.info("Feiniu marked watched, item: %s", item["Id"])
        except MediaException as error:
            logger.error("Feiniu could not mark watched, item: %s (%s)", item["Id"], error)

    def _notify_devices(self, event):
        for device in (self._tv, self._av):
            if device is not None:
                try:
                    getattr(device, event)(self.on_message)
                except Exception as error:
                    logger.error("Feiniu device callback failed (%s)", type(error).__name__)

    def on_message(self, header, message):
        logger.info("Feiniu player %s: %s", header, message)

    def on_play_begin(self, **kwargs):
        item = kwargs.get("context")
        with self._play_lock:
            if item is None or self._active is not item or item["started"]:
                return
            item["started"] = True
            self._set_watched(item)
            self._notify_devices("play_begin")

    def on_play_in_progress(self, **kwargs):
        # Do not write Pioneer progress into the records used as our input.
        pass

    def on_play_end(self, **kwargs):
        item = kwargs.get("context")
        with self._play_lock:
            if item is None or self._active is not item:
                return
            if item["started"]:
                position, total = kwargs.get("position_ticks", 0), kwargs.get("total_ticks", 0)
                if total >= 6000000000 and position >= total * 0.7:
                    self._set_watched(item)
                self._notify_devices("play_end")
            self._active = None

    def start_before(self, **kwargs):
        for device in (self._player, self._tv, self._av):
            if device is not None:
                device.start_before()

    def start(self, **kwargs):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._records.reset_baseline()
        self._polling_online = None
        self._thread = threading.Thread(target=self._poll_loop, name="feiniu-poll", daemon=True)
        self._thread.start()
        logger.info("Feiniu playback polling started, OnlyWhenPlayerOnline: %s", self._only_when_online)

    def stop(self):
        self._stop_event.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=self._request_timeout * 4 + 1)
