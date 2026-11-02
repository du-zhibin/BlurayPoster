"""
Copyright (C) 2025 whitebrise

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version
"""

import time
import socket
import logging
import base64
import requests
import json
import threading
import urllib.parse
from abstract_classes import Player, PlayerException


logger = logging.getLogger(__name__)


class Pioneer(Player):
    # Browser.GetDevices 返回的设备类型
    DEV_TYPE_NFS = 3
    DEV_TYPE_SMB = 4
    # 播放机返回的可播放文件类别，命中时沿用其原始 f_class
    PLAYABLE_CLASSES = (8, 48, 80)

    @property
    def BDMV(self):  # play_type
        return 80

    @property
    def VIDEO(self):  # play_type
        return 8

    def __init__(self, config: dict):
        super().__init__(config)
        try:
            self._ip = self._config.get('IP', '127.0.0.1')
            self._port = 8090
            self._use_nfs = self._config.get('NFSPrefer', True)
            self._http_host = f"http://{self._ip}:{self._port}/jsonrpc"
            self._mapping_path_list = self._config.get('MappingPath')
            self._force_mount_path = self._config.get('ForceMountPath')
            # 与 oppo 相同的多用户认证列表，网络共享登录时按顺序尝试
            self._auth = list(self._config.get('Auth', []) or [])
            self._play_start_timeout = self._config.get('PlayStartTimeout', 5)
            self._play_end_timeout = self._config.get('PlayEndTimeout', 5)
            self._device_list = []

            # 通过播放机自身的网络共享接口登录并浏览目标共享，配置方式与 oppo 一致。
            # 关闭后回到旧的单共享行为（依赖 StartupKeySequence 手动进入目录）。
            self._use_netshare = self._config.get('UseNetShareLogin', True)
            self._netshare_timeout = self._config.get('NetShareTimeout', 8)
            self._netshare_cache = self._config.get('NetShareCache', 300)
            self._share_cache = {}
            # 按 server 缓存登录结果（entries + 用过的账号），避免同一共享的后续影片重复登录
            self._login_cache = {}
            # 按目录节点缓存子项列表，避免重复播放时逐层重新浏览
            self._listing_cache = {}
            self._device_cache = {'time': 0, 'devices': []}

            self._startup_key_sequence = self._config.get('StartupKeySequence', [])
            self._startup_wait = self._config.get('StartupWait', 5)
            self._online_status = 1
            self._offline_count = 0

            self._on_message = None
            self._on_play_begin = None
            self._on_play_in_progress = None
            self._on_play_end = None
            self._headers = {
                "Authorization": "Basic dmlzaW86dmlzaW8=",
                "Content-Type": "application/json",
            }

            self._control_keys = {
                "left": 258048,
                "right": 258049,
                "up": 258050,
                "down": 258051,
                "ok": 323584,
                "return": 651267,
                "home": 585730,
            }

            self._position_ticks = 0
            self._total_ticks = 0
            self._play_status = -1

            # 先完成所有变量初始化，再启动线程
            thread = threading.Thread(target=self._track_online_status)
            thread.daemon = True
            thread.start()
        except Exception as e:
            raise PlayerException(e)

    @staticmethod
    def dict_to_url_encoded_json(data: dict) -> str:
        return urllib.parse.quote(json.dumps(data))

    @staticmethod
    def extract_path_parts(path):
        # 替换路径分隔符
        normalized_path = path.replace('\\\\', '\\').replace('\\', '/').replace("//", "/")
        # 分割路径
        parts = normalized_path.split('/')
        # 提取路径部分
        server = parts[1]
        filename = parts[-1]
        folder = '/'.join(parts[2:-1])
        return server, folder, filename

    @staticmethod
    def _b64(value):
        # 播放机的网络共享登录要求用户名、密码分别 Base64 编码
        return base64.b64encode(str(value or '').encode('utf-8')).decode('ascii')

    def _get_play_info(self):
        """
        获取全局信息
        :return:
        """
        try:
            request_body = {"method": "Playback.GetPlayingStatus", "id": "1", "jsonrpc": "2.0"}
            res = requests.post(self._http_host, headers=self._headers, data=json.dumps(request_body), timeout=2)
            if res.status_code == 200:
                result = res.json()
                if "result" in result:
                    return result
        except Exception as e:
            logger.debug(f"get play info failed, error: {e}")
        return None

    def _is_port_open(self) -> bool:
        """
        检测主机的指定端口是否开放。
        :return: 如果端口开放返回 True，否则返回 False
        """
        try:
            conn = socket.create_connection((self._ip, self._port), timeout=1)
            conn.close()  # 确保关闭连接
            return True
        except (socket.timeout, ConnectionRefusedError):
            return False
        except Exception as e:
            logger.debug(f"检测端口时出错: {e}")
            return False

    def _send_control_key(self, key):
        """
        发送控制按键
        :return:
            bool
        """
        if not key:
            return True

        logger.debug(f"send control key: {key}")
        try:
            request_body = {"method": "Remoter.SendKey", "params": {"key": self._control_keys[key]}, "id": "1", "jsonrpc": "2.0"}
            res = requests.post(self._http_host, headers=self._headers, data=json.dumps(request_body), timeout=1)
            if res.status_code == 200:
                return True
        except Exception as e:
            logger.debug(f"send control key failed, error: {e}")
            return False
        return None

    def _send_control_sequence(self, keys):
        logger.debug(f"send control sequence: {keys}")
        for key in keys:
            self._send_control_key(key)
            time.sleep(1)

    def _track_online_status(self):
        logger.debug("track online status")
        while True:
            time.sleep(2)
            online = self._is_port_open()
            if online:
                # 状态如果是离线，则切换为在线，并执行开机序列
                if self._online_status == 0:
                    self._online_status = 1
                    self._offline_count = 0
                    logger.debug("set online status to 1")
                    time.sleep(self._startup_wait)
                    self._send_control_sequence(self._startup_key_sequence)
                else:
                    # 如果已经是在线状态，确保计数器重置
                    if self._offline_count > 0:
                        self._offline_count = 0
            else:
                # 状态如果是在线，则累加离线计数
                if self._online_status == 1:
                    self._offline_count += 1
                    logger.debug(f"offline count: {self._offline_count}")
                    if self._offline_count > 5:
                        self._online_status = 0
                        self._offline_count = 0
                        logger.debug("set online status to 0")

    def _track_play_status(self):
        """
        跟踪播放进度
        :return:
        """

        self._play_status = 0
        last_qry_time = time.time()
        last_report_time = 0
        timeout = 15

        while True:
            time.sleep(1)
            play_info = self._get_play_info()
            logger.debug(play_info)
            if play_info is None:
                if time.time() - last_qry_time > timeout:
                    break
                else:
                    continue
            if self._play_status == 0:
                if "elapsetime" in play_info["result"]:
                    self._play_status = 1
                    logger.debug("set playing status to 1")
                    self._on_play_begin()
                elif time.time() - last_qry_time > timeout:
                    break
                else:
                    continue
            elif self._play_status == 1:
                if "elapsetime" in play_info["result"]:
                    if time.time() - last_report_time > 60:
                        elapse_hours = play_info["result"]["elapsetime"]["hours"]
                        elapse_minutes = play_info["result"]["elapsetime"]["minutes"]
                        elapse_seconds = play_info["result"]["elapsetime"]["seconds"]
                        elapse_milliseconds = play_info["result"]["elapsetime"]["milliseconds"]

                        total_hours = play_info["result"]["totaltime"]["hours"]
                        total_minutes = play_info["result"]["totaltime"]["minutes"]
                        total_seconds = play_info["result"]["totaltime"]["seconds"]
                        total_milliseconds = play_info["result"]["totaltime"]["milliseconds"]

                        self._position_ticks = elapse_hours * 3600000000 + elapse_minutes * 60000000 + elapse_seconds * 1000000 + elapse_milliseconds * 1000
                        self._total_ticks = total_hours * 3600000000 + total_minutes * 60000000 + total_seconds * 1000000 + total_milliseconds * 1000

                        self._on_play_in_progress(position_ticks=self._position_ticks, total_ticks=self._total_ticks)
                        last_report_time = time.time()
                        logger.debug("update play position ticks: {}".format(self._position_ticks))

                else:
                    logger.debug("play end break")
                    break
            last_qry_time = time.time()
        # 报告已结束
        self._on_play_end(position_ticks=self._position_ticks, total_ticks=self._total_ticks)
        logger.debug("play end")
        self._position_ticks = 0
        self._total_ticks = 0
        self._play_status = -1

    def _play(self, nfs_prefer, path, play_type):
        """
        播放文件（单共享回退路径，把文件直接拼到 /mnt/cifs/ 下）
        :param nfs_prefer:
        :param path:
        :return:
        """
        try:
            logger.debug("play {}".format(path))

            # 设置请求的 URL 和头部信息
            url = self._http_host

            request_body = {
                "method": "Playback.PlayFile",
                "params": {
                    "dev_idx": 0,
                    "dev_type": 0,
                    "f_class": play_type,
                    "f_idx": 0,
                    "file": "/mnt/cifs/"+path,
                    "list_type": 0,
                    "mode": 0,
                    "repeat": 80
                }
            }

            # 发起 POST 请求
            res = requests.post(url, headers=self._headers, data=json.dumps(request_body))

            if res.status_code == 200:
                result = res.json()
                if "result" in result and result["result"] == "0":
                    return True
                logger.error("play file failed, reason: {}, path: {}".format(res.text, request_body))
        except Exception as e:
            logger.error(f"play file exception, error: {e}")
            return False

    # --- 网络共享：登录、浏览、定位文件 ------------------------------------

    def _rpc(self, method, params=None, timeout=None):
        body = {"method": method, "id": "1", "jsonrpc": "2.0"}
        if params is not None:
            body["params"] = params
        res = requests.post(self._http_host, headers=self._headers, data=json.dumps(body),
                            timeout=timeout or self._netshare_timeout)
        if res.status_code != 200:
            raise RuntimeError(f"{method} http {res.status_code}: {res.text}")
        result = res.json()
        if 'error' in result:
            raise RuntimeError(f"{method} error: {result['error']}")
        return result.get('result')

    def _get_devices(self):
        now = time.time()
        if now - self._device_cache['time'] < 60 and self._device_cache['devices']:
            return self._device_cache['devices']
        result = self._rpc('Browser.GetDevices', {"limits": {"start": 1, "end": 32}})
        devices = (result or {}).get('devices') or []
        self._device_cache = {'time': now, 'devices': devices}
        return devices

    def _find_device(self, server):
        """在播放机的源列表里找这台主机。

        找不到时不报错，而是按配置的协议构造一个源直接登录——
        这样 NAS 是否出现在播放机的源列表里不再是硬性前提。
        """
        wanted = self.DEV_TYPE_NFS if self._use_nfs else self.DEV_TYPE_SMB
        try:
            devices = self._get_devices()
        except Exception as e:
            logger.error(f"Browser.GetDevices 失败: {e}")
            devices = []
        network = [d for d in devices if d.get('dev_type') in (self.DEV_TYPE_NFS, self.DEV_TYPE_SMB)]
        logger.debug("播放机源列表: {}".format(
            [(d.get('dev_type'), d.get('label'), d.get('path')) for d in devices]))
        for device in network:
            if device.get('label') == server or device.get('path') == server:
                return device
        for device in network:
            if device.get('dev_type') == wanted:
                return device
        if network:
            return network[0]
        logger.warning(
            "播放机未列出任何网络源，直接按协议构造源登录: dev_type={} host={}（播放机当前源: {}）".format(
                wanted, server, [(d.get('dev_type'), d.get('label')) for d in devices]))
        return {"dev_idx": 0, "dev_type": wanted, "label": server, "path": ""}

    def _login_with_fallback(self, device):
        """先按配置协议的 dev_type 登录，失败再换另一种协议设备类型重试一次。"""
        try:
            return self._login_net_share(device)
        except Exception as primary:
            alternate = (self.DEV_TYPE_SMB if device.get('dev_type') == self.DEV_TYPE_NFS
                         else self.DEV_TYPE_NFS)
            if alternate == device.get('dev_type'):
                raise
            logger.warning("登录失败(dev_type={}): {}；改用 dev_type={} 重试".format(
                device.get('dev_type'), primary, alternate))
            return self._login_net_share(dict(device, dev_type=alternate))

    def _login_net_share(self, device):
        """用 Auth 列表依次尝试登录网络共享，成功后把用过的账号移到队尾。"""
        candidates = list(self._auth) if self._auth else [{"Username": "", "Password": ""}]
        last_error = None
        for auth_item in candidates:
            try:
                result = self._rpc('Browser.NetShareLogin', {
                    "dev_idx": device.get('dev_idx', 0),
                    "dev_type": device.get('dev_type', 0),
                    "host": device.get('label', ''),
                    "path": device.get('path', ''),
                    "port": 0,
                    "user": self._b64(auth_item.get("Username", "")),
                    "passwd": self._b64(auth_item.get("Password", "")),
                    "limits": {"start": 0, "end": 63},
                })
            except Exception as e:
                last_error = e
                logger.error(f"login net share failed with user {auth_item.get('Username')}: {e}")
                continue
            files = (result or {}).get('files')
            if files:
                if self._auth:
                    try:
                        self._auth.remove(auth_item)
                        self._auth.append(auth_item)
                    except ValueError:
                        pass
                return files, auth_item
            last_error = RuntimeError("网络共享登录返回空列表")
        raise RuntimeError(f"网络共享登录失败: {last_error}")

    def _list_net_share(self, node, auth_item):
        cache_key = (node.get('file') or node.get('path'), node.get('label'))
        cached = self._listing_cache.get(cache_key)
        if cached and time.time() - cached[0] < self._netshare_cache:
            return cached[1]
        result = self._rpc('Browser.NetShareGetFiles', {
            "dev_idx": node.get('dev_idx', 0),
            "dev_type": node.get('dev_type', 0),
            "host": node.get('file') or node.get('path') or '',
            "path": node.get('label') or '',
            "port": 0,
            "user": self._b64(auth_item.get("Username", "")),
            "passwd": self._b64(auth_item.get("Password", "")),
            "f_class": node.get('f_class', 1),
            "f_idx": node.get('f_idx', 0),
            "limits": {"start": 0, "end": 63},
        })
        children = (result or {}).get('files') or []
        self._listing_cache[cache_key] = (time.time(), children)
        return children

    @staticmethod
    def _force_mount_split(folder, force_mount_path):
        """按 ForceMountPath 切出「共享名提示」和「共享内路径」。"""
        if not force_mount_path:
            return None, folder
        prefix = str(force_mount_path).strip('/')
        if not prefix:
            return None, folder
        hint = prefix.split('/')[-1]
        if folder == prefix:
            return hint, ''
        if folder.startswith(prefix + '/'):
            return hint, folder[len(prefix) + 1:]
        if prefix in folder:
            return hint, folder.replace(prefix, "").strip('/')
        return hint, folder

    def _select_share(self, entries, within, hint):
        """共享名可能跨多段，按最长前缀匹配；再退回 ForceMountPath 提示。"""
        segments = [s for s in within.split('/') if s]
        for length in range(len(segments), 0, -1):
            candidate = '/'.join(segments[:length])
            for entry in entries:
                if entry.get('label') == candidate:
                    return entry, segments[length:]
        if hint:
            for entry in entries:
                if entry.get('label') == hint or entry.get('file') == hint:
                    return entry, segments
        if not entries:
            raise RuntimeError("播放机未返回任何共享，请检查 Auth 账号密码")
        return entries[0], segments

    def _resolve_entry(self, server, folder, filename):
        """让播放机登录共享并浏览到目标文件，返回播放机给的条目。"""
        cache_key = (server, folder, filename)
        cached = self._share_cache.get(cache_key)
        if cached and time.time() - cached[0] < self._netshare_cache:
            return cached[1]

        # 登录（含挂载）是最贵的一步，按 server 缓存，后续影片直接复用
        login_cached = self._login_cache.get(server)
        if login_cached and time.time() - login_cached[0] < self._netshare_cache:
            entries, auth_item = login_cached[1], login_cached[2]
            logger.debug(f"reuse cached net share login for {server}")
        else:
            device = self._find_device(server)
            entries, auth_item = self._login_with_fallback(device)
            self._login_cache[server] = (time.time(), entries, auth_item)
        hint, within = self._force_mount_split(folder, self._force_mount_path)
        node, remaining = self._select_share(entries, within, hint)
        logger.debug(f"selected share: {node.get('label')}, remaining: {remaining}")

        for segment in list(remaining) + [filename]:
            children = self._list_net_share(node, auth_item)
            hit = None
            for child in children:
                if child.get('label') == segment or child.get('file') == segment:
                    hit = child
                    break
            if hit is None:
                raise RuntimeError(
                    f"在共享 {node.get('label')} 中找不到 {segment}，"
                    f"当前层有: {[c.get('label') for c in children]}")
            node = hit

        logger.debug(f"resolved media via player browse: {node}")
        self._share_cache[cache_key] = (time.time(), node)
        now = time.time()
        for store in (self._share_cache, self._login_cache, self._listing_cache):
            for stale in [k for k, v in store.items() if now - v[0] >= self._netshare_cache]:
                store.pop(stale, None)
        return node

    def _play_entry(self, entry, play_type):
        """用播放机返回的真实路径与设备身份下发 Playback.PlayFile。"""
        entry_class = entry.get('f_class')
        f_class = entry_class if entry_class in self.PLAYABLE_CLASSES else play_type
        file_path = entry.get('file') or entry.get('path')
        request_body = {
            "method": "Playback.PlayFile",
            "params": {
                "dev_idx": entry.get('dev_idx', 0),
                "dev_type": entry.get('dev_type', 0),
                "f_class": f_class,
                "f_idx": entry.get('f_idx', 0),
                "file": file_path,
                "list_type": 0,
                "mode": 0,
                "repeat": f_class,
            }
        }
        logger.debug(f"play resolved file: {request_body}")
        try:
            res = requests.post(self._http_host, headers=self._headers, data=json.dumps(request_body),
                                timeout=self._netshare_timeout)
            if res.status_code == 200:
                result = res.json()
                if "result" in result and result["result"] == "0":
                    return True
                logger.error("play resolved file failed, reason: {}, path: {}".format(res.text, file_path))
            else:
                logger.error(f"play resolved file http {res.status_code}: {res.text}")
        except Exception as e:
            logger.error(f"play resolved file exception, error: {e}")
        return False

    def start_before(self, **kwargs):
        """
        启动前
        :return:
        """
        pass

    def play(self, media_path: str, container, on_message, on_play_begin, on_play_in_progress, on_play_end, **kwargs):
        """
        播放影片
        :param media_path:
        :param container:
        :param on_message:
        :param on_play_begin:
        :param on_play_in_progress:
        :param on_play_end:
        :param kwargs:
        :return:
        """
        if self._play_status >= 0:
            return on_message("Notification", "movie is playing or prepare to playing, wait!")
        if not self._mapping_path_list:
            return on_message("Error", "no MappingPath configured")

        # 转换目录
        media_path = (media_path.replace('\\\\', '\\').
                      replace("\\", "/").replace("//", "/"))
        real_path = media_path
        for mapping_path in self._mapping_path_list:
            real_path = real_path.replace(mapping_path["Media"], mapping_path["NFS"], 1) if self._use_nfs \
                else real_path.replace(mapping_path["Media"], mapping_path["SMB"], 1)
        real_path = real_path.replace("//", "/")
        logger.debug("transfer path, from: {}, to: {}".format(media_path, real_path))
        sever, folder, file = self.extract_path_parts(real_path)
        logger.debug("curt path, sever: {}, folder:  {}, file: {}".format(sever, folder, file))

        if container != "bluray" and not file.lower().endswith(".iso"):
            play_type = self.VIDEO
        else:
            play_type = self.BDMV

        if self._use_netshare:
            # 与 oppo 一致：按映射把文件定位到某个共享，由播放机自己登录并浏览
            try:
                entry = self._resolve_entry(sever, folder, file)
            except Exception as e:
                logger.error(f"resolve share failed: {e}")
                return on_message("Error", "cannot resolve net share, {}".format(e))
            if not self._play_entry(entry, play_type):
                return on_message("Error", "cannot play file, {}".format(media_path))
        else:
            if not self._play(self._use_nfs, folder+"/"+file, play_type):
                return on_message("Error", "cannot play bdmv folder, {}".format(media_path))

        # 开始播放并监控播放进度
        self._on_message = on_message
        self._on_play_begin = on_play_begin
        self._on_play_in_progress = on_play_in_progress
        self._on_play_end = on_play_end
        thread = threading.Thread(target=self._track_play_status)
        thread.daemon = True
        thread.start()
