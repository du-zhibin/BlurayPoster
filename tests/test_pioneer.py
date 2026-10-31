"""
player.pioneer.Pioneer 多目录支持测试。

配置方式与 player.oppo.Oppo 完全一致：MappingPath（多条）+ Auth + NFSPrefer。
用本地 HTTP 桩模拟播放机的 Browser.GetDevices / Browser.NetShareLogin /
Browser.NetShareGetFiles / Playback.PlayFile，不需要真实设备。

运行（在 BlurayPoster 根目录）：
    python -m unittest discover -s tests -p "test_pioneer.py" -v
"""

import base64
import json
import threading
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from player.pioneer import Pioneer


# 播放机看到的设备列表：一个 SMB 主机和一个 NFS 主机
DEVICES = [
    {"dev_idx": 1, "dev_type": 4, "label": "NAS466C", "path": "/smb", "update": 0},
    {"dev_idx": 2, "dev_type": 3, "label": "192.168.1.10", "path": "/nfs", "update": 0},
]


def entry(dev_idx, dev_type, f_idx, label, file_path, f_class):
    return {"dev_idx": dev_idx, "dev_type": dev_type, "f_class": f_class,
            "f_idx": f_idx, "file": file_path, "label": label}


# 目录树：键是 (host, path)，对应 NetShareLogin / NetShareGetFiles 的请求字段
TREE = {
    # SMB 主机 -> 两个共享（对应 NAS 上的两个共享文件夹）
    ("NAS466C", "/smb"): [
        entry(1, 4, 0, "Video", "/smb/NAS466C/Video", 96),
        entry(1, 4, 1, "Video2", "/smb/NAS466C/Video2", 96),
    ],
    ("/smb/NAS466C/Video", "Video"): [
        entry(1, 4, 0, "Movie", "/smb/NAS466C/Video/Movie", 1),
    ],
    ("/smb/NAS466C/Video/Movie", "Movie"): [
        entry(1, 4, 0, "Dune.iso", "/smb/NAS466C/Video/Movie/Dune.iso", 80),
        entry(1, 4, 1, "Dune2.iso", "/smb/NAS466C/Video/Movie/Dune2.iso", 80),
    ],
    # 播放机源列表里没有该主机时，代码会用 path="" 直接登录主机
    ("NAS466C", ""): [
        entry(1, 4, 0, "Video", "/smb/NAS466C/Video", 96),
    ],
    ("/smb/NAS466C/Video2", "Video2"): [
        entry(1, 4, 0, "Dune.iso", "/smb/NAS466C/Video2/Dune.iso", 80),
    ],
    # NFS 主机 -> 一个导出
    ("192.168.1.10", "/nfs"): [
        entry(2, 3, 0, "Series", "/nfs/192.168.1.10/Series", 96),
    ],
    ("/nfs/192.168.1.10/Series", "Series"): [
        entry(2, 3, 0, "S01", "/nfs/192.168.1.10/Series/S01", 1),
    ],
    ("/nfs/192.168.1.10/Series/S01", "S01"): [
        entry(2, 3, 0, "E01.mkv", "/nfs/192.168.1.10/Series/S01/E01.mkv", 8),
    ],
}


class MockPlayer:
    """记录收到的 JSON-RPC 请求，并按 TREE 返回结果。"""

    def __init__(self, reject_users=(), devices=None, tree=None):
        self.requests = []
        self.play_requests = []
        self.login_requests = []
        self.reject_users = set(reject_users)
        self.devices = DEVICES if devices is None else devices
        self.tree = TREE if tree is None else tree
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def _handler(outer):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                outer.requests.append(body)
                result = outer.dispatch(body)
                payload = json.dumps({"jsonrpc": "2.0", "id": body.get("id", "1"), "result": result})
                data = payload.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        return Handler

    @staticmethod
    def _decode(value):
        try:
            return base64.b64decode(value or "").decode("utf-8")
        except Exception:
            return ""

    def dispatch(self, body):
        method = body.get("method")
        params = body.get("params") or {}
        if method == "Browser.GetDevices":
            return {"devices": self.devices,
                    "limits": {"start": 1, "end": 32, "total": len(self.devices)}}
        if method == "Browser.NetShareLogin":
            self.login_requests.append(params)
            if self._decode(params.get("user")) in self.reject_users:
                return {"files": [], "limits": {"start": 0, "end": 63, "total": 0}}
            entries = self.tree.get((params.get("host"), params.get("path")), [])
            return {"files": entries, "limits": {"start": 0, "end": 63, "total": len(entries)}}
        if method == "Browser.NetShareGetFiles":
            entries = self.tree.get((params.get("host"), params.get("path")), [])
            start, end = params["limits"]["start"], params["limits"]["end"]
            return {"files": entries[start:end],
                    "limits": {"start": start, "end": min(end, len(entries)), "total": len(entries)}}
        if method == "Playback.PlayFile":
            self.play_requests.append(params)
            return "0"
        if method == "Playback.GetPlayingStatus":
            return {
                "elapsetime": {"hours": 0, "minutes": 1, "seconds": 0, "milliseconds": 0},
                "totaltime": {"hours": 2, "minutes": 0, "seconds": 0, "milliseconds": 0},
            }
        return {}


class PioneerMultiDirectoryTest(unittest.TestCase):

    def setUp(self):
        self.player_stub = MockPlayer()
        self.messages = []

    def tearDown(self):
        self.player_stub.stop()

    def build(self, mapping, auth=None, extra=None):
        config = {
            "IP": "127.0.0.1",
            "MappingPath": mapping,
            "Auth": auth if auth is not None else [{"Username": "user1", "Password": "pass1"}],
        }
        config.update(extra or {})
        player = Pioneer(config)
        # 把端点指向本地桩，避开 Pioneer 里写死的 8090
        player._port = self.player_stub.port
        player._http_host = "http://127.0.0.1:{}/jsonrpc".format(self.player_stub.port)
        player._play_status = -1
        return player

    def play(self, player, media_path, container):
        player.play(media_path, container, self.on_message, lambda **k: None,
                    lambda **k: None, lambda **k: None)

    def on_message(self, header, message):
        self.messages.append((header, message))

    def only_play(self):
        self.assertEqual(len(self.player_stub.play_requests), 1, self.player_stub.play_requests)
        return self.player_stub.play_requests[0]

    # --- 用例 -------------------------------------------------------------

    def test_two_smb_shares_same_config_style_as_oppo(self):
        # 与 oppo 完全相同的配置结构：一个 MappingPath 条目对应一个共享目录
        player = self.build([
            {"Media": "/mnt/Video", "SMB": "/NAS466C/Video", "NFS": "/192.168.1.10/Video"},
            {"Media": "/mnt/Video2", "SMB": "/NAS466C/Video2", "NFS": "/192.168.1.10/Video2"},
        ], extra={"NFSPrefer": False})
        self.play(player, "/mnt/Video/Movie/Dune.iso", "bluray")

        self.assertEqual(self.messages, [])
        params = self.only_play()
        self.assertEqual(params["file"], "/smb/NAS466C/Video/Movie/Dune.iso")
        self.assertEqual(params["dev_idx"], 1)
        self.assertEqual(params["dev_type"], 4)
        self.assertEqual(params["f_class"], 80)
        self.assertEqual(params["repeat"], 80)

    def test_second_share_also_plays(self):
        player = self.build([
            {"Media": "/mnt/Video", "SMB": "/NAS466C/Video", "NFS": "/192.168.1.10/Video"},
            {"Media": "/mnt/Video2", "SMB": "/NAS466C/Video2", "NFS": "/192.168.1.10/Video2"},
        ], extra={"NFSPrefer": False})
        self.play(player, "/mnt/Video2/Dune.iso", "bluray")

        params = self.only_play()
        self.assertEqual(params["file"], "/smb/NAS466C/Video2/Dune.iso")
        self.assertEqual(params["dev_idx"], 1)
        self.assertEqual(params["dev_type"], 4)

    def test_nfs_mapping_and_video_class(self):
        player = self.build([
            {"Media": "/mnt/Video3", "SMB": "/NAS466C/Series", "NFS": "/192.168.1.10/Series"},
        ])
        self.play(player, "/mnt/Video3/S01/E01.mkv", "mkv")

        params = self.only_play()
        self.assertEqual(params["file"], "/nfs/192.168.1.10/Series/S01/E01.mkv")
        self.assertEqual(params["dev_idx"], 2)
        self.assertEqual(params["dev_type"], 3)
        self.assertEqual(params["f_class"], 8)
        # repeat 跟随 f_class，与 App 行为一致
        self.assertEqual(params["repeat"], 8)

    def test_auth_list_falls_back_to_next_account(self):
        self.player_stub.stop()
        self.player_stub = MockPlayer(reject_users={"baduser"})
        player = self.build([
            {"Media": "/mnt/Video", "SMB": "/NAS466C/Video", "NFS": "/192.168.1.10/Video"},
        ], auth=[
            {"Username": "baduser", "Password": "x"},
            {"Username": "gooduser", "Password": "y"},
        ], extra={"NFSPrefer": False})
        self.play(player, "/mnt/Video/Movie/Dune.iso", "bluray")

        self.assertEqual(self.messages, [])
        params = self.only_play()
        self.assertEqual(params["file"], "/smb/NAS466C/Video/Movie/Dune.iso")
        users = [login["user"] for login in self.player_stub.login_requests]
        self.assertEqual(users[0], base64.b64encode(b"baduser").decode())
        self.assertEqual(users[1], base64.b64encode(b"gooduser").decode())

    def test_legacy_mode_keeps_single_share_behaviour(self):
        player = self.build([
            {"Media": "/mnt/Video", "SMB": "/NAS466C/Video", "NFS": "/192.168.1.10/Video"},
        ], extra={"NFSPrefer": False, "UseNetShareLogin": False})
        self.play(player, "/mnt/Video/Movie/Dune.iso", "bluray")

        # 旧的单共享行为：去掉服务器名后拼 /mnt/cifs/
        self.assertEqual(self.only_play()["file"], "/mnt/cifs/Video/Movie/Dune.iso")

    def test_missing_mapping_reports_error(self):
        config = {"IP": "127.0.0.1"}
        player = Pioneer(config)
        player._play_status = -1
        self.play(player, "/mnt/Video/Movie/Dune.iso", "bluray")
        self.assertEqual(len(self.player_stub.play_requests), 0)
        self.assertEqual(self.messages[0][0], "Error")

    def test_unresolvable_file_reports_error(self):
        player = self.build([
            {"Media": "/mnt/Video", "SMB": "/NAS466C/Video", "NFS": "/192.168.1.10/Video"},
        ], extra={"NFSPrefer": False})
        self.play(player, "/mnt/Video/Movie/Missing.mkv", "mkv")
        self.assertEqual(len(self.player_stub.play_requests), 0)
        self.assertEqual(self.messages[0][0], "Error")
        self.assertIn("Missing.mkv", self.messages[0][1])

    def test_nas_absent_from_device_list_still_plays(self):
        # 播放机源列表为空：不再报错，而是按协议直接构造源登录
        self.player_stub.stop()
        self.player_stub = MockPlayer(devices=[])
        player = self.build([
            {"Media": "/mnt/Video", "SMB": "/NAS466C/Video", "NFS": "/192.168.1.10/Video"},
        ], extra={"NFSPrefer": False})
        self.play(player, "/mnt/Video/Movie/Dune.iso", "bluray")

        self.assertEqual(self.messages, [])
        params = self.only_play()
        self.assertEqual(params["file"], "/smb/NAS466C/Video/Movie/Dune.iso")
        # 起播用的是浏览结果里的真实身份
        self.assertEqual(params["dev_type"], 4)
        # 登录用的是按配置协议合成的源
        login = self.player_stub.login_requests[0]
        self.assertEqual(login["dev_idx"], 0)
        self.assertEqual(login["dev_type"], 4)
        self.assertEqual(login["host"], "NAS466C")

    def test_login_is_cached_between_plays(self):
        player = self.build([
            {"Media": "/mnt/Video", "SMB": "/NAS466C/Video", "NFS": "/192.168.1.10/Video"},
        ], extra={"NFSPrefer": False})
        self.play(player, "/mnt/Video/Movie/Dune.iso", "bluray")
        player._play_status = -1  # 允许第二次播放
        self.play(player, "/mnt/Video/Movie/Dune2.iso", "bluray")

        self.assertEqual(len(self.player_stub.play_requests), 2)
        self.assertEqual(self.player_stub.play_requests[1]["file"],
                         "/smb/NAS466C/Video/Movie/Dune2.iso")
        # 同一主机的登录与目录列表只做一次
        self.assertEqual(len(self.player_stub.login_requests), 1)

    def test_target_directory_after_first_page_is_found_and_cached(self):
        tree = dict(TREE)
        key = ("/nfs/192.168.1.10/Series", "Series")
        tree[key] = [entry(2, 3, i, f"Folder{i}", f"/unused/{i}", 1) for i in range(70)]
        tree[key] += TREE[key]
        self.player_stub.tree = tree
        player = self.build([
            {"Media": "/mnt/Video3", "NFS": "/192.168.1.10/Series"},
        ])
        self.play(player, "/mnt/Video3/S01/E01.mkv", "mkv")
        self.assertEqual(self.messages, [])
        self.assertEqual(self.only_play()["file"], "/nfs/192.168.1.10/Series/S01/E01.mkv")
        requests = [r["params"]["limits"] for r in self.player_stub.requests
                    if r["method"] == "Browser.NetShareGetFiles" and r["params"]["path"] == "Series"]
        self.assertEqual(requests, [{"start": 0, "end": 63}, {"start": 63, "end": 126}])
        cached = player._listing_cache[key][1]
        self.assertEqual(len(cached), 71)

    def test_missing_share_does_not_browse_an_unrelated_share(self):
        player = self.build([
            {"Media": "/mnt/Video", "SMB": "/NAS466C/Unknown"},
        ], extra={"NFSPrefer": False})
        self.play(player, "/mnt/Video/Dune.iso", "bluray")
        self.assertEqual(self.player_stub.play_requests, [])
        self.assertFalse(any(r["method"] == "Browser.NetShareGetFiles"
                             for r in self.player_stub.requests))
        self.assertIn("Unknown", self.messages[0][1])

    def test_repeated_page_stops_without_caching_incomplete_results(self):
        player = self.build([])
        node = TREE[("192.168.1.10", "/nfs")][0]
        page = {"files": TREE[("/nfs/192.168.1.10/Series", "Series")],
                "limits": {"total": 10}}
        with patch.object(player, "_rpc", return_value=page) as rpc:
            with self.assertRaisesRegex(RuntimeError, "分页未前进"):
                player._list_net_share(node, {})
        self.assertEqual(rpc.call_count, 2)
        self.assertEqual(player._listing_cache, {})

    def test_empty_page_before_total_stops_without_caching(self):
        player = self.build([])
        node = TREE[("192.168.1.10", "/nfs")][0]
        with patch.object(player, "_rpc", return_value={"files": [], "limits": {"total": 5}}):
            with self.assertRaisesRegex(RuntimeError, "分页提前结束"):
                player._list_net_share(node, {})
        self.assertEqual(player._listing_cache, {})

    def test_feiniu_nfs_export_label_with_old_mount_hint(self):
        self.player_stub.tree = {
            ("192.168.1.10", "/nfs"): [
                entry(2, 3, 3, "-vol4-1000-tv", "/mnt/cifs/-vol4-1000-tv", 96),
            ],
            ("/mnt/cifs/-vol4-1000-tv", "-vol4-1000-tv"): [
                entry(2, 3, 0, "E01.mkv", "/mnt/cifs/-vol4-1000-tv/E01.mkv", 8),
            ],
        }
        player = self.build([
            {"Media": "/vol4/1000/tv", "NFS": "/192.168.1.10/-vol4-1000-tv"},
        ], extra={"ForceMountPath": "fs/1000/nfs"})
        self.play(player, "/vol4/1000/tv/E01.mkv", "mkv")
        self.assertEqual(self.messages, [])
        self.assertEqual(self.only_play()["file"], "/mnt/cifs/-vol4-1000-tv/E01.mkv")


if __name__ == "__main__":
    unittest.main(verbosity=2)
