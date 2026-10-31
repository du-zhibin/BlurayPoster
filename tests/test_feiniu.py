"""Playback detection, selected-file routing, and watched-state feedback."""

import copy
import threading
import unittest
from unittest.mock import Mock, patch

import requests

from abstract_classes import MediaException
from bluray_poster import initialize_components
from media.feiniu import Feiniu, PlaybackRecords, signed_headers


def record(guid="movie-1", ts=10, **kwargs):
    return {"guid": guid, "media_guid": "file-" + guid, "video_guid": "video-" + guid,
            "ts": ts, "watched": 0, "duration": 7200, **kwargs}


def response(data=None, status=200, content_type="application/json"):
    return Mock(status_code=status, headers={"Content-Type": content_type},
                json=Mock(return_value=data))


class PlaybackRecordTests(unittest.TestCase):
    def setUp(self):
        self.tracker = PlaybackRecords(90, 120)
        self.tracker.update([record()], 0)

    def test_initial_and_unchanged_history_do_not_play(self):
        self.assertEqual(self.tracker.update([record()], 1), [])

    def test_progress_or_new_positive_record_is_a_candidate(self):
        self.assertEqual(self.tracker.update([record(ts=11)], 2)[0]["guid"], "movie-1")
        self.assertEqual(self.tracker.update([record("movie-2")], 4)[0]["guid"], "movie-2")

    def test_hours_of_continuous_progress_only_trigger_once(self):
        for now in range(2, 7200, 15):
            candidates = self.tracker.update([record(ts=now)], now)
            self.assertEqual(len(candidates), int(now == 2))

    def test_metadata_and_watched_feedback_do_not_play(self):
        for now, extra in enumerate((
            {"is_favorite": 1}, {"watched": 1}, {"watched": 0, "watched_ts": 200},
            {"title": "New title"},
        ), 1):
            self.assertEqual(self.tracker.update([record(**extra)], now), [])
        self.assertEqual(self.tracker.update([record(ts=7200, watched=1)], 10), [])

    def test_zero_progress_completion_and_live_records_do_not_play(self):
        self.assertEqual(self.tracker.update([record(ts=0), record("live", ts=0)], 2), [])

    def test_idle_and_repeat_time_both_apply(self):
        self.assertEqual(len(self.tracker.update([record(ts=11)], 2)), 1)
        self.assertEqual(self.tracker.update([record(ts=12)], 93), [])
        self.assertEqual(self.tracker.update([record(ts=13)], 130), [])
        self.assertEqual(len(self.tracker.update([record(ts=14)], 221)), 1)

    def test_removed_and_reappearing_watched_record_does_not_replay(self):
        self.tracker.update([record(ts=11)], 2)
        self.tracker.update([], 4)
        self.assertEqual(self.tracker.update([record(ts=12, watched=1)], 6), [])

    def test_recovery_uses_a_new_baseline(self):
        self.tracker.reset_baseline()
        self.assertEqual(self.tracker.update([record(ts=300)], 20), [])
        self.assertEqual(len(self.tracker.update([record(ts=310)], 22)), 1)

    def test_recovery_preserves_already_handled_activity(self):
        self.tracker.update([record(ts=11)], 2)
        self.tracker.reset_baseline()
        self.tracker.update([record(ts=12)], 4)
        self.assertEqual(self.tracker.update([record(ts=13)], 6), [])

    def test_version_changes_can_be_recognized_after_repeat_filter(self):
        self.tracker.update([record(ts=11)], 2)
        self.assertEqual(len(self.tracker.update(
            [record(ts=11, media_guid="alternate-file")], 130)), 1)

    def test_malformed_rows_fail_without_replacing_baseline(self):
        before = copy.deepcopy(self.tracker.previous)
        for rows in (None, {}, [None], [record(ts=float("nan"))], [record(ts=True)],
                     [record(ts=-1)], [record(media_guid="")], [record(), record()]):
            with self.subTest(rows=rows), self.assertRaises(MediaException):
                self.tracker.update(rows, 2)
            self.assertEqual(self.tracker.previous, before)


class FeiniuTests(unittest.TestCase):
    def setUp(self):
        self.player, self.tv, self.av = Mock(), Mock(), Mock()
        self.player.play.return_value = None
        self.config = {"Host": "http://feiniu.test:8005", "Username": "user", "Password": "secret"}
        self.media = Feiniu(self.player, self.tv, self.av, self.config)
        self.media._access_token = "test-token"
        self.row = record()
        self.item = {"Id": "movie-1", "Path": "/vol1/movies/Movie.iso",
                     "Container": "iso", "watched": False, "started": False}

    def dispatch(self):
        with patch.object(self.media, "_resolve_item", return_value=self.item):
            self.media._dispatch_record(self.row)
        return self.player.play.call_args.args

    @patch("media.feiniu.requests.request")
    def test_watched_only_after_actual_begin_and_no_duplicate_at_end(self, request):
        request.return_value = response({"code": 0, "data": None})
        args = self.dispatch()
        request.assert_not_called()
        args[3]()
        request.assert_called_once()
        call = request.call_args
        self.assertEqual(call.args, ("POST", "http://feiniu.test:8005/v/api/v1/item/watched"))
        self.assertEqual(call.kwargs["data"], b'{"item_guid":"movie-1"}')
        self.assertEqual(call.kwargs["timeout"], 10)
        self.assertFalse(call.kwargs["allow_redirects"])
        self.assertEqual(call.kwargs["headers"]["Authorization"], "test-token")
        self.tv.play_begin.assert_called_once()
        args[5](position_ticks=6000000000, total_ticks=7000000000)
        request.assert_called_once()
        self.av.play_end.assert_called_once()
        self.assertIsNone(self.media._active)

    def test_failed_start_end_callback_does_not_mark_or_switch_devices(self):
        args = self.dispatch()
        with patch.object(self.media, "_set_watched") as watched:
            args[5](position_ticks=6000000000, total_ticks=7000000000)
        watched.assert_not_called()
        self.tv.play_begin.assert_not_called()
        self.tv.play_end.assert_not_called()
        self.assertIsNone(self.media._active)

    def test_synchronous_player_error_releases_pending_item(self):
        self.player.play.side_effect = lambda *args: args[2]("Error", "No share")
        self.dispatch()
        self.assertIsNone(self.media._active)

    def test_player_exception_releases_pending_item(self):
        self.player.play.side_effect = RuntimeError("private detail")
        with self.assertRaisesRegex(MediaException, "dispatch failed"):
            self.dispatch()
        self.assertIsNone(self.media._active)

    def test_busy_player_does_not_replace_original_context(self):
        self.dispatch()
        with patch.object(self.media, "_resolve_item") as resolve:
            self.media._dispatch_record(record("movie-2"))
        resolve.assert_not_called()
        self.assertIs(self.media._active, self.item)
        self.player.play.assert_called_once()

    def test_timeout_discards_late_callbacks(self):
        args = self.dispatch()
        self.item["requested_at"] = 0
        with patch("media.feiniu.time.monotonic", return_value=100), \
                patch.object(self.media, "_native_request", return_value=[]), \
                patch.object(self.media, "_set_watched") as watched:
            self.media._poll_once()
            args[3]()
            args[5](position_ticks=6000000000, total_ticks=7000000000)
        watched.assert_not_called()
        self.tv.play_begin.assert_not_called()
        self.assertIsNone(self.media._active)

    def test_old_callbacks_cannot_mark_new_selection(self):
        args = self.dispatch()
        self.media._active = dict(self.item, Id="movie-2")
        with patch.object(self.media, "_set_watched") as watched:
            args[3]()
            args[5](position_ticks=6000000000, total_ticks=7000000000)
        watched.assert_not_called()
        self.assertEqual(self.media._active["Id"], "movie-2")

    @patch("media.feiniu.requests.request")
    def test_watched_failure_does_not_interrupt_device_callbacks(self, request):
        request.return_value = response({"code": 1234})
        args = self.dispatch()
        args[3]()
        self.assertFalse(self.item["watched"])
        self.tv.play_begin.assert_called_once()
        self.assertIs(self.media._active, self.item)

    def test_disabled_mark_on_start_retains_end_threshold(self):
        self.media._mark_watched = False
        args = self.dispatch()
        with patch.object(self.media, "_set_watched") as watched:
            args[3]()
            watched.assert_not_called()
            args[5](position_ticks=6000000000, total_ticks=7000000000)
            watched.assert_called_once_with(self.item)

    @patch("media.feiniu.requests.request")
    def test_already_watched_is_not_written_again(self, request):
        self.item["watched"] = True
        self.dispatch()[3]()
        request.assert_not_called()

    def test_dry_run_does_not_initialize_devices_play_or_write(self):
        self.media._dry_run = True
        self.media.start_before()
        with patch.object(self.media, "_resolve_item", return_value=self.item), \
                patch.object(self.media, "_native_request") as request, \
                self.assertLogs("media.feiniu", level="INFO") as logs:
            self.media._dispatch_record(self.row)
        self.assertIn("DryRun candidate", logs.output[0])
        self.player.start_before.assert_not_called()
        self.tv.start_before.assert_not_called()
        self.player.play.assert_not_called()
        request.assert_not_called()

    def test_application_dry_run_does_not_construct_device_control_threads(self):
        settings = Mock()
        settings.get.side_effect = {
            "Media": {**self.config, "Executor": "media.feiniu.Feiniu", "DryRun": True},
        }.get
        with patch("bluray_poster.initialize_component") as initialize:
            media = initialize_components(settings)
            media.start_before()
        self.assertIsInstance(media, Feiniu)
        initialize.assert_not_called()
        self.assertIsNone(media._player)

    def test_application_real_mode_still_initializes_devices(self):
        settings = Mock()
        settings.get.side_effect = {
            "Media": {**self.config, "Executor": "media.feiniu.Feiniu", "DryRun": False},
        }.get
        with patch("bluray_poster.initialize_component", return_value=self.player) as initialize:
            media = initialize_components(settings)
        self.assertEqual(initialize.call_count, 3)
        self.assertIs(media._player, self.player)

    def test_excluded_extension_not_sent_to_player(self):
        self.media._excluded = {"mkv"}
        with patch.object(self.media, "_resolve_item", return_value=dict(self.item, Path="/a.mkv")):
            self.media._dispatch_record(self.row)
        self.player.play.assert_not_called()

    def test_simultaneous_candidates_are_not_guessed(self):
        with patch.object(self.media, "_native_request", side_effect=[
            [], [record(), record("movie-2")],
        ]), patch.object(self.media, "_dispatch_record") as dispatch:
            self.media._poll_once()
            self.media._poll_once()
        dispatch.assert_not_called()

    def test_exact_media_version_is_used(self):
        files = [
            {"media_guid": "other", "file_path": "/wrong.iso"},
            {"media_guid": self.row["media_guid"], "item_guid": self.row["guid"],
             "file_path": "/correct.iso"},
        ]
        with patch.object(self.media, "_native_request", return_value=files):
            self.assertEqual(self.media._resolve_item(self.row)["Path"], "/correct.iso")
        for files in (files[:1], files[1:] * 2):
            with patch.object(self.media, "_native_request", return_value=files), \
                    self.assertRaises(MediaException):
                self.media._resolve_item(self.row)

    def test_bdmv_entry_becomes_disc_root(self):
        for file_path in ("/disc/BDMV/index.bdmv", "/disc/BDMV/MovieObject.bdmv", "/disc/BDMV"):
            with self.subTest(file_path=file_path), patch.object(
                self.media, "_native_request",
                return_value=[{"media_guid": self.row["media_guid"], "file_path": file_path}],
            ):
                item = self.media._resolve_item(self.row)
                self.assertEqual((item["Path"], item["Container"]), ("/disc", "bluray"))

    @patch("media.feiniu.requests.get")
    def test_dotted_disc_directory_uses_matching_compat_metadata(self, get):
        path = "/Movie.2024.2160p"
        get.return_value = response({"Path": path, "Container": "bluray"})
        with patch.object(self.media, "_native_request", return_value=[
            {"media_guid": self.row["media_guid"], "file_path": path},
        ]):
            self.assertEqual(self.media._resolve_item(self.row)["Container"], "bluray")
            get.return_value = response({"Path": "/different-version", "Container": "bluray"})
            with self.assertRaises(MediaException):
                self.media._resolve_item(self.row)

    @patch("media.feiniu.requests.post")
    @patch("media.feiniu.requests.request")
    def test_expired_token_is_refreshed_once(self, request, post):
        post.return_value = response({"AccessToken": "refreshed"})
        request.side_effect = [response(status=401), response({"code": 0, "data": []})]
        self.assertEqual(self.media._native_request("GET", "/v/api/v1/play/list"), [])
        post.assert_called_once()
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args.kwargs["headers"]["Authorization"], "refreshed")

    @patch("media.feiniu.requests.post")
    @patch("media.feiniu.requests.request")
    def test_http200_read_auth_failure_gets_only_one_refresh(self, request, post):
        post.return_value = response({"AccessToken": "refreshed"})
        request.return_value = response({"code": 5000})
        with self.assertRaisesRegex(MediaException, "code 5000"):
            self.media._native_request("GET", "/v/api/v1/play/list")
        post.assert_called_once()
        self.assertEqual(request.call_count, 2)

    @patch("media.feiniu.requests.request")
    def test_html_is_not_a_valid_empty_record_list(self, request):
        request.return_value = response(content_type="text/html")
        with self.assertRaisesRegex(MediaException, "did not return JSON"):
            self.media._native_request("GET", "/v/api/v1/play/list")

    @patch("media.feiniu.requests.request", side_effect=requests.Timeout("secret-detail"))
    def test_transport_errors_do_not_expose_exception_credentials(self, request):
        with self.assertRaises(MediaException) as caught:
            self.media._native_request("GET", "/v/api/v1/play/list")
        self.assertNotIn("secret-detail", str(caught.exception))

    def test_poll_loop_resets_baseline_after_failure_and_stops(self):
        with patch.object(self.media, "_poll_once", side_effect=MediaException("network")), \
                patch.object(self.media._records, "reset_baseline") as reset, \
                patch.object(self.media._stop_event, "wait", side_effect=lambda _: self.media.stop()):
            self.media._poll_loop()
        reset.assert_called_once()

    def test_worker_can_be_stopped_and_started_only_once(self):
        entered = threading.Event()
        with patch.object(self.media, "_poll_once", side_effect=lambda: entered.set()):
            self.media.start()
            self.assertTrue(entered.wait(2))
            worker = self.media._thread
            self.media.start()
            self.assertIs(worker, self.media._thread)
            self.media.stop()
            self.assertFalse(worker.is_alive())

    def test_invalid_configuration_fails_early(self):
        for extra in (
            {"Host": "http://user:password@nas:8005"}, {"Host": "http://nas:5666/v"},
            {"PollInterval": 0}, {"ActivityIdleTimeout": 1}, {"RequestTimeout": float("inf")},
            {"DryRun": "false"}, {"ExcludeVideoExt": "mkv"}, {"Password": 1234},
        ):
            with self.subTest(extra=extra), self.assertRaises(MediaException):
                Feiniu(self.player, None, None, {**self.config, **extra})


class SignatureTests(unittest.TestCase):
    @patch("media.feiniu.secrets.randbelow", return_value=234567)
    @patch("media.feiniu.time.time", return_value=1700000000)
    def test_get_matches_probe_reference(self, clock, nonce):
        # This vector follows the installed web client's URLSearchParams rules.
        headers = signed_headers("GET", "/v/api/v1/play/list", "token")
        self.assertEqual(headers["authx"],
                         "nonce=334567&timestamp=1700000000000&sign=d141903fb5d1a1136e6e00aed4f9a291")

    @patch("media.feiniu.secrets.randbelow", return_value=234567)
    @patch("media.feiniu.time.time", return_value=1700000000)
    def test_post_signs_the_exact_utf8_body_sent_to_server(self, clock, nonce):
        headers = signed_headers("POST", "/v/api/v1/item/watched", "token",
                                 b'{"item_guid":"movie-1"}')
        self.assertEqual(headers["authx"],
                         "nonce=334567&timestamp=1700000000000&sign=e13125e8645a3f3249976fe29422d1bd")


if __name__ == "__main__":
    unittest.main()
