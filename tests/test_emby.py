"""Emby watched-state updates and their WebSocket feedback."""

import unittest
from unittest.mock import Mock, patch

import requests

from media.emby import Emby


class EmbyWatchedTests(unittest.TestCase):
    def setUp(self):
        self.player = Mock()
        self.emby = Emby(self.player, None, None, {
            "Host": "http://emby.test:8096",
            "RepeatFilterTimeout": 0,
        })
        self.emby._user_id = "user-1"
        self.item = {
            "Id": "movie-1",
            "Path": "/movie/example.iso",
            "Container": "iso",
            "IsFolder": False,
            "UserData": {"Played": False},
        }
        self.emby._play_item = self.item

    def event(self, played=True, item_id="movie-1", user_id="user-1"):
        return {
            "MessageType": "UserDataChanged",
            "Data": {
                "UserId": user_id,
                "UserDataList": [{"ItemId": item_id, "Played": played}],
            },
        }

    @patch("media.emby.requests.post")
    def test_marks_only_after_player_begin_callback(self, post):
        post.return_value = Mock(status_code=200)
        self.emby._run_player()
        post.assert_not_called()
        on_begin = self.player.play.call_args.args[3]
        on_begin()
        self.assertEqual(post.call_args.kwargs["url"],
                         "http://emby.test:8096/emby/Users/user-1/PlayedItems/movie-1")
        self.assertEqual(post.call_args.kwargs["timeout"], 10)
        self.assertTrue(self.item["UserData"]["Played"])

    @patch("media.emby.requests.post")
    def test_accepts_legacy_204_response(self, post):
        post.return_value = Mock(status_code=204)
        self.assertTrue(self.emby._set_if_watched(True))

    @patch("media.emby.requests.post")
    def test_own_event_before_http_response_does_not_replay(self, post):
        def respond(**kwargs):
            self.emby._handle_msg(self.event())
            return Mock(status_code=200)

        post.side_effect = respond
        with patch.object(self.emby, "_query_item") as query:
            self.emby.on_play_begin()
        query.assert_not_called()
        self.player.play.assert_not_called()
        self.assertIsNone(self.emby._pending_watched_update)

    @patch("media.emby.requests.post")
    def test_own_event_after_http_response_does_not_replay(self, post):
        post.return_value = Mock(status_code=200)
        self.emby.on_play_begin()
        with patch.object(self.emby, "_query_item") as query:
            self.emby._handle_msg(self.event())
        query.assert_not_called()

    @patch("media.emby.requests.post")
    def test_disabled_setting_keeps_end_threshold(self, post):
        self.emby = Emby(self.player, None, None, {
            "Host": "http://emby.test:8096",
            "MarkWatchedOnStart": False,
        })
        self.emby._play_item = self.item
        self.emby.on_play_begin()
        post.assert_not_called()
        post.return_value = Mock(status_code=200)
        self.emby.on_play_end(position_ticks=5000000000, total_ticks=7000000000)
        post.assert_called_once()

    @patch("media.emby.requests.post")
    def test_no_duplicate_mark_at_end(self, post):
        post.return_value = Mock(status_code=200)
        self.emby.on_play_begin()
        self.emby.on_play_end(position_ticks=5000000000, total_ticks=7000000000)
        post.assert_called_once()

    @patch("media.emby.requests.post")
    def test_already_watched_does_not_increment_play_count(self, post):
        self.item["UserData"]["Played"] = True
        self.emby.on_play_begin()
        post.assert_not_called()

    @patch("media.emby.requests.post")
    def test_failure_does_not_set_watched_or_break_callback(self, post):
        for failure in (Mock(status_code=500, text="error"), requests.Timeout()):
            with self.subTest(failure=failure):
                post.side_effect = None
                if isinstance(failure, Exception):
                    post.side_effect = failure
                else:
                    post.return_value = failure
                self.emby.on_play_begin()
                self.assertFalse(self.item["UserData"]["Played"])
                self.assertIsNone(self.emby._pending_watched_update)

    def test_expired_feedback_guard_allows_later_events(self):
        self.emby._pending_watched_update = ("movie-1", True, 0)
        with patch.object(self.emby, "_query_item", return_value=None) as query:
            self.emby._handle_msg(self.event())
        query.assert_called_once_with("movie-1")

    def test_guard_does_not_hide_other_users_or_other_items(self):
        pending = ("movie-1", True, float("inf"))
        self.emby._pending_watched_update = pending
        with patch.object(self.emby, "_query_item", return_value=None) as query:
            self.emby._handle_msg(self.event(user_id="other-user"))
            query.assert_not_called()
            self.emby._handle_msg(self.event(item_id="movie-2"))
            query.assert_called_once_with("movie-2")
        self.assertIs(self.emby._pending_watched_update, pending)


if __name__ == "__main__":
    unittest.main()
