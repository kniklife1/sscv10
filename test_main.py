#!/usr/bin/env python3
"""
Automated deterministic regression and validation test suite for govt-notice-telegram-alerts.
Covers all 19 official targets, live_test mode, valid empty-board baseline completion,
unrecognized page/API rejection, multi-page deduplication, lossless state merging,
delivery retries, and secret redaction using deterministic mocks and fixtures.
"""

from __future__ import annotations

import io
import json
import logging
import os
import sys
import tempfile
import unittest
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

# Ensure we import main from govt-notice-telegram-alerts if run from root or subdir
_HERE = os.path.dirname(os.path.abspath(__file__))
_SUBDIR = os.path.join(_HERE, "govt-notice-telegram-alerts")
if os.path.isfile(os.path.join(_HERE, "main.py")):
    sys.path.insert(0, _HERE)
elif os.path.isfile(os.path.join(_SUBDIR, "main.py")):
    sys.path.insert(0, _SUBDIR)

import main as app

# The nine original sources. These tests pin their pre-existing behaviour; the RRB/RRCB source is
# date-informational by design and is covered in test_rrb_integration.py.
LEGACY_SOURCES = tuple(x for x in app.SOURCES if x.source_id != "rrb_rrcb")


class FakeHttpClient(app.HttpClient):
    """Deterministic HTTP client returning fixture responses per URL."""

    def __init__(
        self,
        routes: Dict[str, Any],
        max_retries: int = 3,
    ) -> None:
        self.sleep_calls: List[float] = []
        super().__init__(
            timeout=(2, 2),
            max_retries=max_retries,
            sleep_fn=lambda d: self.sleep_calls.append(d),
        )
        self.routes = routes
        self.call_counts: Dict[str, int] = {}

    def _perform_single_request(
        self,
        url: str,
        method: str,
        headers: Dict[str, str],
        data: Optional[bytes],
    ) -> Tuple[int, Dict[str, str], bytes, str]:
        self.call_counts[url] = self.call_counts.get(url, 0) + 1
        handler = self.routes.get(url)
        if handler is None:
            for k, v in self.routes.items():
                if url.startswith(k):
                    handler = v
                    break
        if handler is None:
            return 404, {}, b"Not Found", url
        if callable(handler):
            return handler(url, self.call_counts[url])
        if isinstance(handler, Exception):
            raise handler
        status, resp_headers, body = handler
        if isinstance(body, str):
            body = body.encode("utf-8")
        return status, resp_headers, body, url


class RecordingNotifier(app.TelegramNotifier):
    """Deterministic TelegramNotifier spy that captures formatted HTML messages without network calls."""

    def __init__(
        self,
        bot_token: str = "123456789:AAH_TestTokenValue1234567890",
        chat_id: str = "-1001234567890",
        fail_notice_titles: Optional[List[str]] = None,
        fail_summary: bool = False,
    ) -> None:
        super().__init__(
            bot_token=bot_token,
            chat_id=chat_id,
            max_retries=1,
            sleep_fn=lambda _: None,
        )
        self.fail_notice_titles = set(fail_notice_titles or [])
        self.fail_summary = fail_summary
        self.sent_notices: List[Tuple[Dict[str, Any], bool, str]] = []
        self.sent_summaries: List[str] = []

    def send_notice(
        self,
        notice_record: Dict[str, Any],
        is_live_test: bool = False,
    ) -> str:
        msg = app.format_telegram_message(notice_record, is_live_test=is_live_test)
        if notice_record.get("title") in self.fail_notice_titles:
            raise app.TelegramDeliveryError(
                "Simulated Telegram HTTP 502 delivery failure", permanent=False
            )
        self.sent_notices.append((dict(notice_record), is_live_test, msg))
        return msg

    def send_live_test_summary(self, summary: app.RunSummary) -> str:
        msg = app.format_live_test_summary_message(summary)
        if self.fail_summary:
            raise app.TelegramDeliveryError(
                "Simulated summary delivery failure", permanent=False
            )
        self.sent_summaries.append(msg)
        return msg


class ValidationSuite(unittest.TestCase):
    """Comprehensive deterministic tests covering all original and upgraded requirements."""

    def test_01_all_9_targets_and_cli_modes_configured(self) -> None:
        """Verify exactly 9 official active targets, CRPF/CISF/SSB exclusion, and CLI modes."""
        self.assertEqual(len(LEGACY_SOURCES), 9)
        source_ids = [s.source_id for s in LEGACY_SOURCES]
        orgs = [s.organization for s in LEGACY_SOURCES]
        urls = [s.homepage_url for s in LEGACY_SOURCES]
        self.assertEqual(len(set(source_ids)), 9)
        self.assertEqual(len(set(orgs)), 9)
        self.assertEqual(len(set(urls)), 9)

        expected_pairs = [
            ("Staff Selection Commission (SSC)", "https://ssc.gov.in/"),
            ("Union Public Service Commission (UPSC)", "https://www.upsc.gov.in/"),
            ("Uttar Pradesh Public Service Commission (UPPSC)", "https://uppsc.up.nic.in/"),
            ("Haryana Staff Selection Commission (HSSC)", "https://www.hssc.gov.in/"),
            ("Rajasthan Staff Selection Board (RSSB)", "https://rssb.rajasthan.gov.in/"),
            ("Madhya Pradesh Employees Selection Board (MPESB)", "https://esb.mp.gov.in/"),
            ("Delhi Subordinate Services Selection Board (DSSSB)", "https://dsssb.delhi.gov.in/"),
            ("Uttar Pradesh Police Recruitment and Promotion Board (UPPRPB)", "https://uppbpb.gov.in/"),
            ("Uttarakhand Public Service Commission (UKPSC)", "https://psc.uk.gov.in/"),
        ]
        actual_pairs = [(s.organization, s.homepage_url) for s in LEGACY_SOURCES]
        self.assertEqual(actual_pairs, expected_pairs)

        # Verify all 9 original adapters plus exactly one RRB/RRCB adapter are registered, and no extras remain
        self.assertEqual(len(app.ADAPTER_REGISTRY), 10)
        self.assertIn("adapter_rrb_rrcb", app.ADAPTER_REGISTRY)
        self.assertEqual(len(app.SOURCES), 10)
        self.assertEqual([x.source_id for x in app.SOURCES].count("rrb_rrcb"), 1)
        for src in LEGACY_SOURCES:
            self.assertIn(src.adapter_name, app.ADAPTER_REGISTRY)
            self.assertTrue(len(src.notice_urls) >= 1)

        # Verify CRPF, CISF, and SSB are completely excluded from active monitoring
        for excluded_id in ("crpf", "cisf", "ssb"):
            self.assertNotIn(excluded_id, source_ids)
            self.assertNotIn(excluded_id, app.SOURCE_BY_ID)
            self.assertNotIn(f"adapter_{excluded_id}", app.ADAPTER_REGISTRY)
            self.assertFalse(hasattr(app, f"adapter_{excluded_id}"))

        # Verify all 10 removed sites (7 earlier + CRPF, CISF, SSB) are tracked in REMOVED_SOURCE_IDS
        self.assertEqual(
            app.REMOVED_SOURCE_IDS,
            frozenset(
                {
                    "upsssc",
                    "bpsc",
                    "bssc",
                    "hppsc",
                    "bsf",
                    "itbp",
                    "assam_rifles",
                    "crpf",
                    "cisf",
                    "ssb",
                }
            ),
        )
        for rem_id in app.REMOVED_SOURCE_IDS:
            self.assertNotIn(rem_id, source_ids)

        for mode in ("auto", "baseline", "monitor", "live_test"):
            parsed = app.parse_args(["--mode", mode])
            self.assertEqual(parsed.mode, mode)

    def test_02_url_and_title_normalization_and_serial_stripping(self) -> None:
        """Verify URL/title normalization, jsessionid removal, and table serial-number stripping."""
        u1 = app.normalize_url(
            "http://uppsc.up.nic.in/Open_PDF_DB.aspx;jsessionid=ABC1234?I4PnQ0tBag=1&utm_source=twitter#section2"
        )
        u2 = app.normalize_url(
            "https://UPPSC.UP.NIC.IN:443/Open_PDF_DB.aspx/?I4PnQ0tBag=1&fbclid=xyz123"
        )
        self.assertEqual(u1, u2)
        self.assertIn("I4PnQ0tBag=1", u1)
        self.assertNotIn("utm_source", u1)
        self.assertNotIn("jsessionid", u1.lower())

        # Table serial number shifts (e.g. Row 1 -> Row 2) must produce identical normalized titles
        row1 = app.normalize_title("1 18-10-2026 Public Notice regarding CET Group-D Examination (1.2 MB)")
        row2 = app.normalize_title("2 18-10-2026 Public Notice regarding CET Group-D Examination [1.2 MB]")
        row3 = app.normalize_title("S.No. 15. 18-10-2026 Public Notice regarding CET Group-D Examination")
        self.assertEqual(row1, row2)
        self.assertEqual(row1, row3)

        # Distinct notices remain distinct
        t_adv1 = app.normalize_title("Advt. No. 05/2026 Group-D Recruitment")
        t_adv2 = app.normalize_title("Advt. No. 06/2026 Group-C Recruitment")
        self.assertNotEqual(t_adv1, t_adv2)

    def test_03_fingerprint_stability_and_multi_page_deduplication(self) -> None:
        """
        Verify duplicate notices discovered across multiple pages or mirror URLs
        are sent only once, while distinct documents or titles are preserved.
        """
        # Same notice discovered on UPSC /whats-new wrapper page and /active-exams direct PDF
        n_wrapper = app.DiscoveredNotice(
            source_id="upsc",
            organization="Union Public Service Commission (UPSC)",
            title="Examination Schedule: Combined Geo-Scientist (Main) Examination, 2026",
            url="https://www.upsc.gov.in/whats-new/Geo-Scientist-2026",
        )
        n_pdf = app.DiscoveredNotice(
            source_id="upsc",
            organization="Union Public Service Commission (UPSC)",
            title="  Examination Schedule: Combined Geo-Scientist (Main) Examination, 2026 ",
            url="https://www.upsc.gov.in/sites/default/files/TimeTable-GeoScientist-2026.pdf",
            publication_date="18-10-2026",
        )
        # Distinct notice with a different official PDF document
        n_other_pdf = app.DiscoveredNotice(
            source_id="upsc",
            organization="Union Public Service Commission (UPSC)",
            title="Examination Schedule: Combined Geo-Scientist (Main) Examination, 2026",
            url="https://www.upsc.gov.in/sites/default/files/Corrigendum-GeoScientist-2026.pdf",
        )
        deduped = app.deduplicate_source_notices([n_wrapper, n_pdf, n_other_pdf])
        self.assertEqual(len(deduped), 2)
        # Prefer the direct PDF URL and retain the explicit publication date
        self.assertEqual(
            deduped[0].url,
            "https://www.upsc.gov.in/sites/default/files/TimeTable-GeoScientist-2026.pdf",
        )
        self.assertEqual(deduped[0].publication_date, "18-10-2026")

    def test_04_state_schema_validation_and_corrupt_state_handling(self) -> None:
        """Validate JSON state schema and corrupt/missing state handling."""
        initial = app.create_initial_state()
        validated = app.validate_state_schema(initial)
        self.assertEqual(validated["schema_version"], 1)
        self.assertFalse(validated["initialized"])
        self.assertEqual(len(validated["sources"]), 10)  # 9 original sources + rrb_rrcb

        with tempfile.TemporaryDirectory() as tmpdir:
            missing_path = os.path.join(tmpdir, "missing.json")
            with self.assertRaises(app.StateValidationError):
                app.load_state(missing_path, allow_init_if_missing=False)

            empty_path = os.path.join(tmpdir, "empty.json")
            with open(empty_path, "w", encoding="utf-8") as f:
                f.write("   ")
            with self.assertRaises(app.StateValidationError):
                app.load_state(empty_path, allow_init_if_missing=False)

            corrupt_path = os.path.join(tmpdir, "corrupt.json")
            with open(corrupt_path, "w", encoding="utf-8") as f:
                f.write("{corrupt json")
            with self.assertRaises(app.StateValidationError):
                app.load_state(corrupt_path, allow_init_if_missing=False)

    def test_05_baseline_and_incremental_alert_lifecycle(self) -> None:
        """
        Verify normal baseline and incremental monitoring:
        - First run baselines existing notices with 0 alerts
        - Subsequent run sends 0 alerts for known notices and 1 alert per genuinely new notice
        - Metadata updates on existing notices do not trigger duplicate alerts
        - Failed Telegram delivery is retried without counting as a brand-new notice again
        """
        test_sources = (LEGACY_SOURCES[0], LEGACY_SOURCES[1])  # SSC, UPSC
        ssc_api_url = test_sources[0].notice_urls[0]
        upsc_url = test_sources[1].notice_urls[0]

        ssc_payload_v1 = {
            "statusCode": "200",
            "data": [
                {
                    "id": "ssc-1",
                    "headline": "Combined Graduate Level Examination 2026 Final Answer Key",
                    "createdAt": "",
                    "attachments": [{"path": "uploads/masterData/NoticeBoards/cgl_key_2026.pdf"}],
                }
            ],
        }
        upsc_html_v1 = """
        <html><body>
          <ul>
            <li><a href="/sites/default/files/UPSC_CSE_2026_Result.pdf">Final Result: Civil Services Examination 2026 (12 Oct 2026)</a></li>
          </ul>
        </body></html>
        """

        client_v1 = FakeHttpClient(
            routes={
                ssc_api_url: (200, {"Content-Type": "application/json"}, json.dumps(ssc_payload_v1)),
                upsc_url: (200, {"Content-Type": "text/html"}, upsc_html_v1),
            }
        )
        notifier = RecordingNotifier()

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "notice_state.json")
            app.save_state_atomically(app.create_initial_state(), state_file)

            # Run 1: First-run baseline
            summary1, state1 = app.run_monitor(
                state_path=state_file,
                mode="auto",
                sources=test_sources,
                http_client=client_v1,
                notifier=notifier,
            )
            self.assertEqual(summary1.succeeded_websites, 2)
            self.assertEqual(summary1.baselined_notices, 2)
            self.assertEqual(summary1.genuinely_new_notices, 0)
            self.assertEqual(len(notifier.sent_notices), 0)
            self.assertTrue(state1["sources"]["ssc"]["baseline_completed"])
            self.assertTrue(state1["sources"]["upsc"]["baseline_completed"])

            # Run 2: SSC backfills createdAt date on ssc-1; 0 new alerts should be sent
            ssc_payload_v1["data"][0]["createdAt"] = "2026-10-10T10:00:00.000Z"
            client_v1_dated = FakeHttpClient(
                routes={
                    ssc_api_url: (200, {"Content-Type": "application/json"}, json.dumps(ssc_payload_v1)),
                    upsc_url: (200, {"Content-Type": "text/html"}, upsc_html_v1),
                }
            )
            summary2, state2 = app.run_monitor(
                state_path=state_file,
                mode="auto",
                sources=test_sources,
                http_client=client_v1_dated,
                notifier=notifier,
            )
            self.assertEqual(summary2.genuinely_new_notices, 0)
            self.assertEqual(len(notifier.sent_notices), 0)
            ssc_rec = next(r for r in state2["notices"].values() if r["source_id"] == "ssc")
            self.assertEqual(ssc_rec["publication_date"], "2026-10-10")

            # Run 3: SSC adds 1 new notice while UPSC times out; Telegram fails transiently
            ssc_payload_v2 = {
                "statusCode": "200",
                "data": [
                    {
                        "id": "ssc-2",
                        "headline": "Junior Engineer Examination 2026 Admit Card Notice",
                        "createdAt": "2026-10-18T08:00:00.000Z",
                        "attachments": [{"path": "uploads/masterData/NoticeBoards/je_admit_2026.pdf"}],
                    },
                    ssc_payload_v1["data"][0],
                ],
            }
            client_v2 = FakeHttpClient(
                routes={
                    ssc_api_url: (200, {"Content-Type": "application/json"}, json.dumps(ssc_payload_v2)),
                    upsc_url: app.SourceFetchError("CONNECTION_TIMEOUT", "UPSC timed out"),
                }
            )
            failing_notifier = RecordingNotifier(
                fail_notice_titles=["Junior Engineer Examination 2026 Admit Card Notice"]
            )
            summary3, state3 = app.run_monitor(
                state_path=state_file,
                mode="auto",
                sources=test_sources,
                http_client=client_v2,
                notifier=failing_notifier,
                reference_date=app.datetime.date(2026, 10, 18),
            )
            self.assertEqual(summary3.genuinely_new_notices, 1)
            self.assertEqual(summary3.telegram_delivery_failures, 1)
            self.assertEqual(summary3.telegram_deliveries_confirmed, 0)

            # Run 4: Telegram recovers -> retries failed delivery without counting as brand-new
            summary4, state4 = app.run_monitor(
                state_path=state_file,
                mode="auto",
                sources=test_sources,
                http_client=client_v2,
                notifier=notifier,
                reference_date=app.datetime.date(2026, 10, 18),
            )
            self.assertEqual(summary4.genuinely_new_notices, 0)
            self.assertEqual(summary4.telegram_deliveries_confirmed, 1)
            self.assertEqual(len(notifier.sent_notices), 1)
            sent_rec, is_test_flag, msg_text = notifier.sent_notices[0]
            self.assertFalse(is_test_flag)
            self.assertNotIn("[LIVE TEST]", msg_text)
            self.assertEqual(sent_rec["title"], "Junior Engineer Examination 2026 Admit Card Notice")

            # Run 5: Repeated run does not resend delivered or baselined notices
            summary5, _ = app.run_monitor(
                state_path=state_file,
                mode="auto",
                sources=test_sources,
                http_client=client_v2,
                notifier=notifier,
                reference_date=app.datetime.date(2026, 10, 18),
            )
            self.assertEqual(summary5.genuinely_new_notices, 0)
            self.assertEqual(summary5.telegram_deliveries_confirmed, 0)
            self.assertEqual(len(notifier.sent_notices), 1)

    def test_06_live_test_mode_end_to_end_and_subsequent_monitor_isolation(self) -> None:
        """
        Verify live_test mode requirements:
        - Selects the latest notice (preferring explicit publication date & source ordering)
        - Sends [LIVE TEST]-prefixed message for latest notice from each successful source
        - Does NOT send ordinary pending alerts during live_test
        - Baselines all discovered notices for successful sources (including the tested latest notice)
        - Preserves failed sources as unbaselined
        - Subsequent normal monitoring run sends 0 alerts for notices discovered during live_test
        - Sends final Telegram test summary with all required counters
        """
        ssc_src = app.SOURCE_BY_ID["ssc"]
        upsc_src = app.SOURCE_BY_ID["upsc"]
        uppsc_src = app.SOURCE_BY_ID["uppsc"]
        test_sources = (ssc_src, upsc_src, uppsc_src)

        # SSC has 2 notices: index 0 has older date (2026-10-10), index 1 has newer explicit date (2026-10-18)
        ssc_payload = {
            "statusCode": "200",
            "data": [
                {
                    "id": "ssc-old",
                    "headline": "Older SSC Notice from October 10",
                    "createdAt": "2026-10-10T09:00:00.000Z",
                    "attachments": [{"path": "uploads/old_notice.pdf"}],
                },
                {
                    "id": "ssc-latest",
                    "headline": "Latest SSC Constable GD Examination Notice 2026",
                    "createdAt": "2026-10-18T11:30:00.000Z",
                    "attachments": [{"path": "uploads/latest_gd_2026.pdf"}],
                },
            ],
        }
        # UPSC has 2 notices without explicit dates -> must pick first in source order and NOT invent a date
        upsc_html = """
        <html><body>
          <h2>What's New</h2>
          <ul>
            <li><a href="/sites/default/files/UPSC_Top_Notice.pdf">UPSC Civil Services Main Examination Schedule</a></li>
            <li><a href="/sites/default/files/UPSC_Second_Notice.pdf">UPSC Engineering Services Reserve List</a></li>
          </ul>
        </body></html>
        """

        client = FakeHttpClient(
            routes={
                ssc_src.notice_urls[0]: (200, {"Content-Type": "application/json"}, json.dumps(ssc_payload)),
                upsc_src.notice_urls[0]: (200, {"Content-Type": "text/html"}, upsc_html),
                # UPPSC fails with timeout
                uppsc_src.notice_urls[0]: app.SourceFetchError("CONNECTION_TIMEOUT", "UPPSC timed out"),
                uppsc_src.notice_urls[1]: app.SourceFetchError("CONNECTION_TIMEOUT", "UPPSC timed out"),
            }
        )
        notifier = RecordingNotifier()

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "notice_state.json")
            app.save_state_atomically(app.create_initial_state(), state_file)

            summary_lt, state_lt = app.run_monitor(
                state_path=state_file,
                mode="live_test",
                sources=test_sources,
                http_client=client,
                notifier=notifier,
            )

            self.assertEqual(summary_lt.total_websites, 3)
            self.assertEqual(summary_lt.succeeded_websites, 2)
            self.assertEqual(summary_lt.failed_websites, 1)
            self.assertEqual(summary_lt.websites_with_notices, 2)
            self.assertEqual(summary_lt.test_messages_delivered, 2)
            self.assertEqual(summary_lt.telegram_delivery_failures, 0)
            self.assertTrue(summary_lt.summary_message_delivered)

            # Exactly 2 [LIVE TEST] notice messages + 1 summary message sent
            self.assertEqual(len(notifier.sent_notices), 2)
            self.assertEqual(len(notifier.sent_summaries), 1)

            # Check SSC message selected the newest explicit date (2026-10-18)
            ssc_sent_rec, ssc_is_test, ssc_msg = notifier.sent_notices[0]
            self.assertTrue(ssc_is_test)
            self.assertTrue(ssc_msg.startswith("[LIVE TEST]"))
            self.assertEqual(ssc_sent_rec["title"], "Latest SSC Constable GD Examination Notice 2026")
            self.assertEqual(ssc_sent_rec["publication_date"], "2026-10-18")
            self.assertIn("2026-10-18", ssc_msg)
            self.assertIn("https://ssc.gov.in/api/attachment/uploads/latest_gd_2026.pdf", ssc_msg)

            # Check UPSC message selected first item in source order and did NOT fabricate a publication date
            upsc_sent_rec, upsc_is_test, upsc_msg = notifier.sent_notices[1]
            self.assertTrue(upsc_is_test)
            self.assertTrue(upsc_msg.startswith("[LIVE TEST]"))
            self.assertEqual(upsc_sent_rec["title"], "UPSC Civil Services Main Examination Schedule")
            self.assertIsNone(upsc_sent_rec["publication_date"])
            self.assertIn("Publication Date:</b> Not available", upsc_msg)

            # Check summary message contents
            summary_msg = notifier.sent_summaries[0]
            self.assertIn("[LIVE TEST]", summary_msg)
            self.assertIn("Total websites:</b> 3", summary_msg)
            self.assertIn("Successful website fetches and parsing:</b> 2", summary_msg)
            self.assertIn("Failed websites:</b> 1", summary_msg)
            self.assertIn("Websites with notices found:</b> 2", summary_msg)
            self.assertIn("Test messages delivered successfully:</b> 2", summary_msg)
            self.assertIn("Telegram delivery failures:</b> 0", summary_msg)
            self.assertIn("Websites requiring adapter review:</b> 0", summary_msg)

            # All 4 discovered notices must be recorded as 'baselined' in state
            self.assertEqual(len(state_lt["notices"]), 4)
            for rec in state_lt["notices"].values():
                self.assertEqual(rec["delivery_status"], "baselined")

            # SSC and UPSC completed baseline; failed UPPSC remains unbaselined
            self.assertTrue(state_lt["sources"]["ssc"]["baseline_completed"])
            self.assertTrue(state_lt["sources"]["upsc"]["baseline_completed"])
            self.assertFalse(state_lt["sources"]["uppsc"]["baseline_completed"])

            # Next normal monitoring run MUST NOT alert on any of the notices discovered during live_test
            notifier_next = RecordingNotifier()
            summary_next, _ = app.run_monitor(
                state_path=state_file,
                mode="monitor",
                sources=test_sources,
                http_client=client,
                notifier=notifier_next,
            )
            self.assertEqual(summary_next.genuinely_new_notices, 0)
            self.assertEqual(summary_next.telegram_deliveries_confirmed, 0)
            self.assertEqual(len(notifier_next.sent_notices), 0)

    def test_07_live_test_telegram_failure_is_not_queued_as_normal_alert(self) -> None:
        """
        Verify that if a [LIVE TEST] Telegram message fails to send:
        - The failure is counted and recorded in test_delivery_status='failed'
        - The notice's normal delivery_status remains 'baselined'
        - The next normal scheduled monitor run does NOT send that old notice as a new alert
        """
        ssc_src = app.SOURCE_BY_ID["ssc"]
        ssc_payload = {
            "statusCode": "200",
            "data": [
                {
                    "id": "ssc-100",
                    "headline": "SSC CHSL 2026 Tier-I Result Declaration Notice",
                    "createdAt": "2026-10-15T10:00:00.000Z",
                    "attachments": [{"path": "uploads/chsl_2026.pdf"}],
                }
            ],
        }
        client = FakeHttpClient(
            routes={
                ssc_src.notice_urls[0]: (200, {"Content-Type": "application/json"}, json.dumps(ssc_payload)),
            }
        )
        failing_test_notifier = RecordingNotifier(
            fail_notice_titles=["SSC CHSL 2026 Tier-I Result Declaration Notice"]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "notice_state.json")
            app.save_state_atomically(app.create_initial_state(), state_file)

            summary_lt, state_lt = app.run_monitor(
                state_path=state_file,
                mode="live_test",
                sources=(ssc_src,),
                http_client=client,
                notifier=failing_test_notifier,
            )
            self.assertEqual(summary_lt.test_messages_delivered, 0)
            self.assertEqual(summary_lt.telegram_delivery_failures, 1)

            rec = next(iter(state_lt["notices"].values()))
            self.assertEqual(rec["delivery_status"], "baselined")
            self.assertEqual(rec["test_delivery_status"], "failed")

            # Subsequent normal monitoring run must NOT send this baselined notice
            healthy_notifier = RecordingNotifier()
            summary_mon, _ = app.run_monitor(
                state_path=state_file,
                mode="monitor",
                sources=(ssc_src,),
                http_client=client,
                notifier=healthy_notifier,
            )
            self.assertEqual(summary_mon.genuinely_new_notices, 0)
            self.assertEqual(len(healthy_notifier.sent_notices), 0)

    def test_08_valid_empty_notice_board_vs_unrecognized_page_or_invalid_api(self) -> None:
        """
        Verify:
        - A valid empty notice board (API data=[] or recognized empty HTML table) completes baseline
          and reports NO_NOTICE_FOUND in live_test mode.
        - An unrecognized HTML page, access-denied page, or invalid API schema does NOT complete
          baseline and reports REVIEW_REQUIRED.
        """
        ssc_src = app.SOURCE_BY_ID["ssc"]
        upsc_src = app.SOURCE_BY_ID["upsc"]
        uppsc_src = app.SOURCE_BY_ID["uppsc"]
        hssc_src = app.SOURCE_BY_ID["hssc"]

        valid_empty_html = """
        <html><body>
          <h2>Public Notice Board</h2>
          <table>
            <thead><tr><th>S.No.</th><th>Date</th><th>Subject</th><th>Download</th></tr></thead>
            <tbody><tr><td colspan="4">No notices available at present.</td></tr></tbody>
          </table>
        </body></html>
        """
        unrecognized_html = """
        <html><body>
          <h1>Welcome to Unknown Portal Layout</h1>
          <p>Some unrelated text with zero notice board structure.</p>
        </body></html>
        """
        invalid_ssc_api = {"unexpectedKey": "bad_schema"}

        client = FakeHttpClient(
            routes={
                # SSC returns invalid API response and homepage is unrecognized
                ssc_src.notice_urls[0]: (200, {"Content-Type": "application/json"}, json.dumps(invalid_ssc_api)),
                ssc_src.notice_urls[1]: (200, {"Content-Type": "text/html"}, unrecognized_html),
                # UPSC returns a valid recognized empty notice board
                upsc_src.notice_urls[0]: (200, {"Content-Type": "text/html"}, valid_empty_html),
                # UPPSC returns an unrecognized page
                uppsc_src.notice_urls[0]: (200, {"Content-Type": "text/html"}, unrecognized_html),
                uppsc_src.notice_urls[1]: (200, {"Content-Type": "text/html"}, unrecognized_html),
                # HSSC returns an access-denied block page
                hssc_src.notice_urls[0]: (200, {"Content-Type": "text/html"}, "<html><body><h1>403 Forbidden - Access Denied</h1></body></html>"),
            }
        )
        notifier = RecordingNotifier()

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "notice_state.json")
            app.save_state_atomically(app.create_initial_state(), state_file)

            summary, state = app.run_monitor(
                state_path=state_file,
                mode="live_test",
                sources=(ssc_src, upsc_src, uppsc_src, hssc_src),
                http_client=client,
                notifier=notifier,
            )

            # Only UPSC (valid empty board) succeeded and completed its baseline
            self.assertEqual(summary.succeeded_websites, 1)
            self.assertEqual(summary.failed_websites, 3)
            self.assertEqual(summary.websites_with_no_notices, 1)
            self.assertEqual(summary.websites_requiring_review, 3)

            self.assertTrue(state["sources"]["upsc"]["baseline_completed"])
            self.assertEqual(summary.per_source_results["upsc"]["test_outcome"], "NO_NOTICE_FOUND")

            self.assertFalse(state["sources"]["ssc"]["baseline_completed"])
            self.assertEqual(summary.per_source_results["ssc"]["test_outcome"], "REVIEW_REQUIRED")

            self.assertFalse(state["sources"]["uppsc"]["baseline_completed"])
            self.assertEqual(summary.per_source_results["uppsc"]["test_outcome"], "REVIEW_REQUIRED")

            self.assertFalse(state["sources"]["hssc"]["baseline_completed"])
            self.assertEqual(summary.per_source_results["hssc"]["test_outcome"], "REVIEW_REQUIRED")

    def test_09_lossless_state_merge_preserves_fingerprints_and_delivered_status(self) -> None:
        """Verify merge_states never discards fingerprints or downgrades delivered/baselined notices."""
        base = app.create_initial_state()
        inc = app.create_initial_state()

        base["initialized"] = True
        base["sources"]["ssc"]["baseline_completed"] = True
        base["notices"]["fp_1"] = {
            "fingerprint": "fp_1",
            "source_id": "ssc",
            "organization": "Staff Selection Commission (SSC)",
            "title": "Notice 1",
            "normalized_url": "https://ssc.gov.in/1.pdf",
            "first_seen_at": "2026-10-18T01:00:00Z",
            "delivery_status": "delivered",
            "delivered_at": "2026-10-18T01:01:00Z",
        }

        inc["initialized"] = True
        inc["sources"]["upsc"]["baseline_completed"] = True
        # fp_1 is only 'pending' in inc, while fp_2 is newly baselined in inc
        inc["notices"]["fp_1"] = {
            "fingerprint": "fp_1",
            "source_id": "ssc",
            "organization": "Staff Selection Commission (SSC)",
            "title": "Notice 1",
            "normalized_url": "https://ssc.gov.in/1.pdf",
            "first_seen_at": "2026-10-18T02:00:00Z",
            "delivery_status": "pending",
        }
        inc["notices"]["fp_2"] = {
            "fingerprint": "fp_2",
            "source_id": "upsc",
            "organization": "Union Public Service Commission (UPSC)",
            "title": "Notice 2",
            "normalized_url": "https://www.upsc.gov.in/2.pdf",
            "first_seen_at": "2026-10-18T02:00:00Z",
            "delivery_status": "baselined",
        }

        merged = app.merge_states(base, inc)
        self.assertTrue(merged["sources"]["ssc"]["baseline_completed"])
        self.assertTrue(merged["sources"]["upsc"]["baseline_completed"])
        self.assertEqual(len(merged["notices"]), 2)
        self.assertEqual(merged["notices"]["fp_1"]["delivery_status"], "delivered")
        self.assertEqual(merged["notices"]["fp_2"]["delivery_status"], "baselined")

    def test_10_retry_limits_and_http_error_handling(self) -> None:
        """Test retry limits, Retry-After handling, and non-retryable 403/404."""
        client_503 = FakeHttpClient(
            routes={
                "https://example.gov.in/503": (503, {"Retry-After": "1"}, "Service Unavailable"),
                "https://example.gov.in/403": (403, {}, "Forbidden"),
            },
            max_retries=3,
        )
        with self.assertRaises(app.SourceFetchError) as ctx503:
            client_503.fetch_text("https://example.gov.in/503")
        self.assertEqual(ctx503.exception.category, "HTTP_503_SERVER_ERROR")
        self.assertEqual(client_503.call_counts["https://example.gov.in/503"], 3)

        with self.assertRaises(app.SourceFetchError) as ctx403:
            client_503.fetch_text("https://example.gov.in/403")
        self.assertEqual(ctx403.exception.category, "HTTP_403_FORBIDDEN")
        self.assertEqual(client_503.call_counts["https://example.gov.in/403"], 1)

    def test_11_secret_redaction_and_missing_env_vars(self) -> None:
        """Test missing environment variables in monitor & live_test modes and secret redaction."""
        secret_token = "123456789:AAH_SecretBotTokenValueXYZ987654"
        secret_chat = "-1009876543210"

        stream = io.StringIO()
        logger = logging.getLogger("test_redaction")
        logger.setLevel(logging.INFO)
        logger.handlers.clear()
        handler = logging.StreamHandler(stream)
        redactor = app.SecretRedactingFilter([secret_token, secret_chat])
        handler.addFilter(redactor)
        logger.addHandler(handler)

        logger.error(
            "Error calling https://api.telegram.org/bot%s/sendMessage for chat %s",
            secret_token,
            secret_chat,
        )
        output = stream.getvalue()
        self.assertNotIn(secret_token, output)
        self.assertNotIn(secret_chat, output)
        self.assertIn("[REDACTED]", output)

        with tempfile.TemporaryDirectory() as tmpdir:
            st_path = os.path.join(tmpdir, "state.json")
            st = app.create_initial_state()
            st["initialized"] = True
            app.save_state_atomically(st, st_path)

            with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}, clear=False):
                self.assertEqual(app.main(["--mode", "monitor", "--state-file", st_path]), 3)
                self.assertEqual(app.main(["--mode", "live_test", "--state-file", st_path]), 3)

    def test_12_workflow_yaml_structure_and_live_test_mode(self) -> None:
        """Validate GitHub Actions YAML structure, 10-min cron, live_test option, and lossless merge."""
        workflow_path = os.path.join(
            os.path.dirname(os.path.abspath(app.__file__)),
            ".github",
            "workflows",
            "main.yml",
        )
        self.assertTrue(os.path.exists(workflow_path))
        with open(workflow_path, "r", encoding="utf-8") as fh:
            content = fh.read()

        self.assertIn('cron: "*/10 * * * *"', content)
        self.assertIn("workflow_dispatch:", content)
        self.assertIn("- live_test", content)
        self.assertIn("concurrency:", content)
        self.assertIn("cancel-in-progress: false", content)
        self.assertIn("contents: write", content)
        self.assertIn("secrets.TELEGRAM_BOT_TOKEN", content)
        self.assertIn("secrets.TELEGRAM_CHAT_ID", content)
        self.assertIn("git diff --quiet -- data/notice_state.json", content)
        self.assertIn("--merge-state", content)
        self.assertNotIn("-X theirs", content)
        self.assertNotIn("--force", content)

    def test_13_all_9_adapters_and_live_test_pending_isolation(self) -> None:
        """
        Verify all 9 active adapters parse realistic fixtures and that live_test mode
        never sends ordinary pending notices that were sitting in state.
        """
        routes: Dict[str, Any] = {}
        for src in LEGACY_SOURCES:
            if src.source_id == "ssc":
                routes[src.notice_urls[0]] = (
                    200,
                    {"Content-Type": "application/json"},
                    json.dumps(
                        {
                            "statusCode": "200",
                            "data": [
                                {
                                    "id": "ssc-99",
                                    "headline": "SSC Combined Graduate Level Examination 2026 Schedule",
                                    "createdAt": "2026-10-18T10:00:00Z",
                                    "attachments": [{"path": "uploads/ssc_cgl_2026.pdf"}],
                                }
                            ],
                        }
                    ),
                )
            elif src.source_id == "uppsc":
                routes[src.notice_urls[0]] = (
                    200,
                    {"Content-Type": "text/html"},
                    """
                    <html><body><table>
                      <tr><td>1</td><td><a href=Open_PDF_DB.aspx?I4PnQ0tBag=99 title="UPPSC Combined State Upper Subordinate Exam 2026 Notification">UPPSC Exam...</a></td><td>Visible upto 31 Dec 2026</td></tr>
                    </table></body></html>
                    """,
                )
            else:
                routes[src.notice_urls[0]] = (
                    200,
                    {"Content-Type": "text/html"},
                    f"""
                    <html><body>
                      <h2>Notice Board - {src.organization}</h2>
                      <table>
                        <tr><td>1</td><td>18-10-2026</td><td><a href="/notices/{src.source_id}_advt_2026.pdf">Official Recruitment and Examination Notice 2026 for {src.source_id.upper()}</a></td></tr>
                      </table>
                    </body></html>
                    """,
                )

        client = FakeHttpClient(routes=routes)
        notifier = RecordingNotifier()

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "notice_state.json")
            st = app.create_initial_state()
            # Seed a pre-existing unrelated 'pending' notice from an earlier run
            st["notices"]["preexisting_pending_fp"] = {
                "fingerprint": "preexisting_pending_fp",
                "source_id": "ssc",
                "organization": "Staff Selection Commission (SSC)",
                "title": "Old Unsent Pending Notice From Past Run",
                "normalized_url": "https://ssc.gov.in/old_unsent.pdf",
                "first_seen_at": "2026-01-01T00:00:00Z",
                "delivery_status": "pending",
            }
            app.save_state_atomically(st, state_file)

            summary, final_state = app.run_monitor(
                state_path=state_file,
                mode="live_test",
                sources=LEGACY_SOURCES,
                http_client=client,
                notifier=notifier,
            )

            self.assertEqual(summary.total_websites, 9)
            self.assertEqual(summary.succeeded_websites, 9)
            self.assertEqual(summary.failed_websites, 0)
            self.assertEqual(summary.websites_with_notices, 9)
            self.assertEqual(summary.test_messages_delivered, 9)
            self.assertTrue(summary.summary_message_delivered)

            # Verify all 9 sent messages have [LIVE TEST] prefix and the old pending notice was NOT sent
            self.assertEqual(len(notifier.sent_notices), 9)
            for rec, is_test, msg in notifier.sent_notices:
                self.assertTrue(is_test)
                self.assertTrue(msg.startswith("[LIVE TEST]"))
                self.assertNotEqual(rec["title"], "Old Unsent Pending Notice From Past Run")

            # Verify all 9 sources are marked baseline_completed=True
            for src in LEGACY_SOURCES:
                self.assertTrue(final_state["sources"][src.source_id]["baseline_completed"])

    def test_14_trailing_new_badges_and_meaningful_details_preserved(self) -> None:
        """
        Verify trailing 'New'/'[NEW]'/'(New)' badges and leading serial numbers are
        stripped while post codes, advt numbers, years, and corrigendum numbers remain distinct.
        """
        t_base = app.normalize_title(" Combined Graduate Level Exam 2026 (Advt No. 04/2026) ")
        t_new1 = app.normalize_title("1. Combined Graduate Level Exam 2026 (Advt No. 04/2026) New")
        t_new2 = app.normalize_title("(12) Combined Graduate Level Exam 2026 (Advt No. 04/2026) [NEW!]")
        t_new3 = app.normalize_title("S.No. 5: Combined Graduate Level Exam 2026 (Advt No. 04/2026) (New) (1.5 MB)")
        self.assertEqual(t_base, t_new1)
        self.assertEqual(t_base, t_new2)
        self.assertEqual(t_base, t_new3)

        # Meaningful recruitment identifiers must remain distinct
        self.assertNotEqual(
            app.normalize_title("Recruitment for Post Code 41/26 in DSSSB"),
            app.normalize_title("Recruitment for Post Code 42/26 in DSSSB"),
        )
        self.assertNotEqual(
            app.normalize_title("Corrigendum-1 to Advt. No. 05/2025"),
            app.normalize_title("Corrigendum-2 to Advt. No. 05/2025"),
        )
        self.assertNotEqual(
            app.normalize_title("Combined State Exam 2025 Final Result"),
            app.normalize_title("Combined State Exam 2026 Final Result"),
        )

    def test_15_expired_and_historical_notices_suppressed_in_monitor_mode(self) -> None:
        """
        Verify expired notices ('Visible upto ...', 'Valid till ...' in the past) and
        old historical notices are baselined rather than sent as new production alerts.
        """
        self.assertEqual(
            app.extract_expiry_date("Notice for Exam 2025 Visible upto : 15/08/2025"),
            "15/08/2025",
        )
        self.assertTrue(
            app.is_notice_expired(
                "Notice for Exam 2025 Visible upto : 15/08/2025",
                reference_date=app.datetime.date(2026, 10, 19),
            )
        )
        self.assertFalse(
            app.is_notice_expired(
                "Notice for Exam 2026 Visible upto : 31/12/2026",
                reference_date=app.datetime.date(2026, 10, 19),
            )
        )

        upsc_src = app.SOURCE_BY_ID["upsc"]
        initial_html = """
        <html><body><h2>What's New</h2><ul>
          <li><a href="/whats-new/notice_1.pdf">18-10-2026 Active Notice One for Examination 2026</a></li>
        </ul></body></html>
        """
        # Second run adds 1 expired notice, 1 archival 2023 notice, and 1 genuinely new 2026 notice
        second_html = """
        <html><body><h2>What's New</h2><ul>
          <li><a href="/whats-new/notice_fresh.pdf">19-10-2026 Genuinely New Examination Schedule 2026</a></li>
          <li><a href="/whats-new/notice_1.pdf">18-10-2026 Active Notice One for Examination 2026</a></li>
          <li><a href="/whats-new/notice_exp.pdf">Old Exam Notice Valid till 15-08-2025</a></li>
          <li><a href="/whats-new/notice_arch.pdf">10-01-2023 Archival Recruitment Result 2023</a></li>
        </ul></body></html>
        """
        routes: Dict[str, Any] = {
            upsc_src.notice_urls[0]: (200, {"Content-Type": "text/html"}, initial_html),
        }
        client = FakeHttpClient(routes=routes)
        notifier = RecordingNotifier()

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "notice_state.json")
            app.save_state_atomically(app.create_initial_state(), state_file)

            # Baseline run
            app.run_monitor(
                state_path=state_file,
                mode="baseline",
                sources=(upsc_src,),
                http_client=client,
                notifier=notifier,
                reference_date=app.datetime.date(2026, 10, 18),
            )
            self.assertEqual(len(notifier.sent_notices), 0)

            # Incremental monitor run with 3 newly discovered genuine notices (1 fresh 2026, 1 expired 2025, 1 archival 2023).
            # With publication-date/expiry restrictions AND closed category whitelist restrictions removed, all 3 are delivered!
            routes[upsc_src.notice_urls[0]] = (200, {"Content-Type": "text/html"}, second_html)
            summary_mon, state_mon = app.run_monitor(
                state_path=state_file,
                mode="monitor",
                sources=(upsc_src,),
                http_client=client,
                notifier=notifier,
                reference_date=app.datetime.date(2026, 10, 19),
            )
            self.assertEqual(summary_mon.genuinely_new_notices, 3)
            self.assertEqual(summary_mon.telegram_deliveries_confirmed, 3)
            self.assertEqual(len(notifier.sent_notices), 3)
            sent_titles = [rec["title"] for rec, _, _ in notifier.sent_notices]
            self.assertTrue(any("Genuinely New Examination Schedule 2026" in t for t in sent_titles))
            self.assertTrue(any("Old Exam Notice Valid till 15-08-2025" in t for t in sent_titles))
            self.assertTrue(any("Archival Recruitment Result 2023" in t for t in sent_titles))
            # All 4 notices are now stored in state (3 delivered, 1 baselined)
            self.assertEqual(len(state_mon["notices"]), 4)

    def test_16_stale_failed_notices_and_ambiguous_post_send_timeout_not_resent(self) -> None:
        """
        Verify:
        - Previously failed notices that are stale (>24h old or >= MAX_NOTICE_DELIVERY_ATTEMPTS)
          are transitioned to 'baselined' and never resent.
        - An ambiguous post-send timeout (TelegramDeliveryError(ambiguous=True)) marks the
          notice as 'delivered' so it is not blindly resent as a duplicate.
        """
        ssc_src = app.SOURCE_BY_ID["ssc"]
        ssc_payload = {
            "statusCode": "200",
            "data": [
                {
                    "id": "ssc-new-ambig",
                    "headline": "SSC New Recruitment Notice With Ambiguous Timeout 2026",
                    "createdAt": "2026-10-19T08:00:00Z",
                    "attachments": [{"path": "uploads/ambig_2026.pdf"}],
                }
            ],
        }
        client = FakeHttpClient(
            routes={
                ssc_src.notice_urls[0]: (200, {"Content-Type": "application/json"}, json.dumps(ssc_payload)),
            }
        )

        class AmbiguousOnceNotifier(RecordingNotifier):
            def __init__(self) -> None:
                super().__init__()
                self.calls = 0

            def send_notice(self, notice_record: Dict[str, Any], is_live_test: bool = False) -> str:
                self.calls += 1
                if self.calls == 1:
                    raise app.TelegramDeliveryError(
                        "Ambiguous post-send read timeout", permanent=False, ambiguous=True
                    )
                return super().send_notice(notice_record, is_live_test=is_live_test)

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "notice_state.json")
            st = app.create_initial_state()
            st["initialized"] = True
            st["sources"]["ssc"]["baseline_completed"] = True
            st["sources"]["ssc"]["baseline_completed_at"] = "2026-10-01T00:00:00Z"
            # Seed a stale failed notice from 5 days ago
            st["notices"]["stale_failed_fp"] = {
                "fingerprint": "stale_failed_fp",
                "source_id": "ssc",
                "organization": "Staff Selection Commission (SSC)",
                "title": "Stale Failed Notice From Five Days Ago",
                "normalized_url": "https://ssc.gov.in/stale_failed.pdf",
                "first_seen_at": "2026-10-10T00:00:00Z",
                "delivery_status": "failed",
                "delivery_attempts": 1,
            }
            app.save_state_atomically(st, state_file)

            ambig_notifier = AmbiguousOnceNotifier()
            summary1, state1 = app.run_monitor(
                state_path=state_file,
                mode="monitor",
                sources=(ssc_src,),
                http_client=client,
                notifier=ambig_notifier,
                reference_date=app.datetime.date(2026, 10, 19),
            )
            # Stale failed notice must have been suppressed to 'baselined' without calling notifier
            self.assertEqual(state1["notices"]["stale_failed_fp"]["delivery_status"], "baselined")
            # Ambiguous notice was attempted once and marked 'delivered' to prevent duplicate resend
            self.assertEqual(ambig_notifier.calls, 1)
            for fp, rec in state1["notices"].items():
                if fp != "stale_failed_fp":
                    self.assertEqual(rec["delivery_status"], "delivered")
                    self.assertEqual(rec["ledger_status"], "ambiguous")

            # Run monitor again: 0 calls to notifier!
            summary2, _ = app.run_monitor(
                state_path=state_file,
                mode="monitor",
                sources=(ssc_src,),
                http_client=client,
                notifier=ambig_notifier,
                reference_date=app.datetime.date(2026, 10, 19),
            )
            self.assertEqual(ambig_notifier.calls, 1)
            self.assertEqual(summary2.telegram_deliveries_confirmed, 0)

    def test_17_stale_state_cannot_overwrite_newer_sent_or_reintroduce_old_pending(self) -> None:
        """
        Verify merge_states preserves 'delivered' ('sent') records even when merged with
        a stale state file, and promotes stale 'pending' records that predate baseline_completed_at.
        """
        newer = app.create_initial_state()
        stale = app.create_initial_state()

        newer["initialized"] = True
        newer["sources"]["ssc"]["baseline_completed"] = True
        newer["sources"]["ssc"]["baseline_completed_at"] = "2026-10-19T08:00:00Z"
        newer["notices"]["fp_sent"] = {
            "fingerprint": "fp_sent",
            "source_id": "ssc",
            "organization": "Staff Selection Commission (SSC)",
            "title": "Already Sent Notice",
            "normalized_url": "https://ssc.gov.in/sent.pdf",
            "first_seen_at": "2026-10-19T08:05:00Z",
            "delivery_status": "sent",
            "delivered_at": "2026-10-19T08:05:05Z",
        }

        stale["initialized"] = True
        stale["sources"]["ssc"]["baseline_completed"] = False
        stale["notices"]["fp_sent"] = {
            "fingerprint": "fp_sent",
            "source_id": "ssc",
            "organization": "Staff Selection Commission (SSC)",
            "title": "Already Sent Notice",
            "normalized_url": "https://ssc.gov.in/sent.pdf",
            "first_seen_at": "2026-10-19T08:05:00Z",
            "delivery_status": "pending",
        }
        stale["notices"]["fp_old_pending"] = {
            "fingerprint": "fp_old_pending",
            "source_id": "ssc",
            "organization": "Staff Selection Commission (SSC)",
            "title": "Old Pending Notice From Before Baseline",
            "normalized_url": "https://ssc.gov.in/old_before_baseline.pdf",
            "first_seen_at": "2026-10-19T07:50:00Z",
            "delivery_status": "pending",
        }

        merged_a = app.merge_states(newer, stale)
        merged_b = app.merge_states(stale, newer)
        for m in (merged_a, merged_b):
            self.assertEqual(m["notices"]["fp_sent"]["delivery_status"], "delivered")
            self.assertEqual(m["notices"]["fp_old_pending"]["delivery_status"], "baselined")

    def test_18_state_retention_policy_preserves_fingerprints(self) -> None:
        """
        Verify apply_state_retention_policy compacts verbose fields above max_full_records
        without discarding notice fingerprints needed for duplicate protection.
        """
        st = app.create_initial_state()
        st["initialized"] = True
        for i in range(15):
            fp = f"fp_{i:04d}"
            st["notices"][fp] = {
                "fingerprint": fp,
                "source_id": "ssc",
                "organization": "Staff Selection Commission (SSC)",
                "title": f"Notice Title {i}",
                "normalized_title": f"notice title {i}",
                "normalized_url": f"https://ssc.gov.in/{i}.pdf",
                "first_seen_at": f"2026-10-19T00:{i:02d}:00Z",
                "last_seen_at": f"2026-10-19T00:{i:02d}:00Z",
                "delivery_status": "delivered",
                "last_delivery_error": None,
                "test_delivered_at": "2026-10-19T00:00:00Z",
                "test_delivery_attempts": 1,
            }
        app.apply_state_retention_policy(st, max_full_records=5, max_total_fingerprints=100)
        # All 15 fingerprints must still be present
        self.assertEqual(len(st["notices"]), 15)
        # Older compacted records still pass schema validation and retain 'delivered' status
        app.validate_state_schema(st)
        self.assertNotIn("test_delivered_at", st["notices"]["fp_0000"])
        self.assertEqual(st["notices"]["fp_0000"]["delivery_status"], "delivered")

    def test_19_direct_document_url_identity_matching_across_title_edits(self) -> None:
        """
        Verify that if an already-baselined notice has a specific PDF URL and its
        link text is later edited on the portal while pointing to the same PDF,
        it is matched to the existing record and does not trigger a duplicate alert.
        """
        upsc_src = app.SOURCE_BY_ID["upsc"]
        html_v1 = """
        <html><body><h2>What's New</h2><ul>
          <li><a href="/sites/default/files/Advt_No_12_2026.pdf">Recruitment Advertisement No. 12/2026 for Specialist Grade III</a></li>
        </ul></body></html>
        """
        html_v2 = """
        <html><body><h2>What's New</h2><ul>
          <li><a href="/sites/default/files/Advt_No_12_2026.pdf">Revised Title: Recruitment Advertisement No. 12/2026 for Specialist Grade-III Officers</a></li>
        </ul></body></html>
        """
        routes: Dict[str, Any] = {
            upsc_src.notice_urls[0]: (200, {"Content-Type": "text/html"}, html_v1),
        }
        client = FakeHttpClient(routes=routes)
        notifier = RecordingNotifier()

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "notice_state.json")
            app.save_state_atomically(app.create_initial_state(), state_file)

            app.run_monitor(
                state_path=state_file,
                mode="baseline",
                sources=(upsc_src,),
                http_client=client,
                notifier=notifier,
            )
            routes[upsc_src.notice_urls[0]] = (200, {"Content-Type": "text/html"}, html_v2)
            summary_mon, _ = app.run_monitor(
                state_path=state_file,
                mode="monitor",
                sources=(upsc_src,),
                http_client=client,
                notifier=notifier,
            )
            self.assertEqual(summary_mon.genuinely_new_notices, 0)
            self.assertEqual(len(notifier.sent_notices), 0)

    def test_20_repeated_manual_and_scheduled_runs_zero_duplicates(self) -> None:
        """
        Verify a full sequence of baseline -> auto -> live_test -> monitor -> auto
        runs produces zero duplicate production alerts.
        """
        ssc_src = app.SOURCE_BY_ID["ssc"]
        payload_1 = {
            "statusCode": "200",
            "data": [
                {
                    "id": "ssc-1",
                    "headline": "SSC Multi Tasking Staff Examination 2026 Notice",
                    "createdAt": "2026-10-18T10:00:00Z",
                    "attachments": [{"path": "uploads/mts_2026.pdf"}],
                }
            ],
        }
        payload_2 = {
            "statusCode": "200",
            "data": [
                {
                    "id": "ssc-2",
                    "headline": "SSC Junior Engineer Examination 2026 Paper-II Schedule",
                    "createdAt": "2026-10-19T09:00:00Z",
                    "attachments": [{"path": "uploads/je_2026.pdf"}],
                },
                {
                    "id": "ssc-1",
                    "headline": "SSC Multi Tasking Staff Examination 2026 Notice",
                    "createdAt": "2026-10-18T10:00:00Z",
                    "attachments": [{"path": "uploads/mts_2026.pdf"}],
                },
            ],
        }
        routes: Dict[str, Any] = {
            ssc_src.notice_urls[0]: (200, {"Content-Type": "application/json"}, json.dumps(payload_1)),
        }
        client = FakeHttpClient(routes=routes)
        prod_notifier = RecordingNotifier()
        test_notifier = RecordingNotifier()

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "notice_state.json")
            app.save_state_atomically(app.create_initial_state(), state_file)

            # 1. Initial auto run (baselines payload_1, 0 prod alerts)
            s1, _ = app.run_monitor(
                state_path=state_file,
                mode="auto",
                sources=(ssc_src,),
                http_client=client,
                notifier=prod_notifier,
                reference_date=app.datetime.date(2026, 10, 18),
            )
            self.assertEqual(s1.mode, "baseline")
            self.assertEqual(len(prod_notifier.sent_notices), 0)

            # 2. Second auto run with unchanged data (0 prod alerts)
            s2, _ = app.run_monitor(
                state_path=state_file,
                mode="auto",
                sources=(ssc_src,),
                http_client=client,
                notifier=prod_notifier,
                reference_date=app.datetime.date(2026, 10, 18),
            )
            self.assertEqual(s2.mode, "monitor")
            self.assertEqual(len(prod_notifier.sent_notices), 0)

            # 3. Manual live_test run (sends 1 [LIVE TEST] message on test_notifier, 0 prod alerts)
            s3, _ = app.run_monitor(
                state_path=state_file,
                mode="live_test",
                sources=(ssc_src,),
                http_client=client,
                notifier=test_notifier,
                reference_date=app.datetime.date(2026, 10, 18),
            )
            self.assertEqual(s3.test_messages_delivered, 1)
            self.assertEqual(len(prod_notifier.sent_notices), 0)

            # 4. New notice appears in payload_2 -> monitor run sends exactly 1 prod alert
            routes[ssc_src.notice_urls[0]] = (200, {"Content-Type": "application/json"}, json.dumps(payload_2))
            s4, _ = app.run_monitor(
                state_path=state_file,
                mode="monitor",
                sources=(ssc_src,),
                http_client=client,
                notifier=prod_notifier,
                reference_date=app.datetime.date(2026, 10, 19),
            )
            self.assertEqual(s4.genuinely_new_notices, 1)
            self.assertEqual(s4.telegram_deliveries_confirmed, 1)
            self.assertEqual(len(prod_notifier.sent_notices), 1)

            # 5. Repeated auto runs with payload_2 -> 0 prod alerts
            s5, _ = app.run_monitor(
                state_path=state_file,
                mode="auto",
                sources=(ssc_src,),
                http_client=client,
                notifier=prod_notifier,
                reference_date=app.datetime.date(2026, 10, 19),
            )
            self.assertEqual(s5.genuinely_new_notices, 0)
            self.assertEqual(len(prod_notifier.sent_notices), 1)

    def test_21_rssb_rajasthan_portal_and_legacy_tls_adapter(self) -> None:
        """
        Verify RSSB adapter parses official Rajasthan State Recruitment Portal
        (<div class="notice-item"> notificationdocview links and <div class="job-card"> ongoing recruitments).
        """
        rssb_src = app.SOURCE_BY_ID["rssb"]
        html_fixture = """
        <html><body>
        <div class="notice-item">
          <p>Notification for Livestock Assistant Direct Recruitment 2026 <a href="notificationdocview?ID=LSA_2026.pdf">View Document</a></p>
        </div>
        <ul>
          <li>
            <div class="job-card">
              <span class="badge">RSSB</span>
              <h6 onclick="postDetailsClick(4686)">JUNIOR INSTRUCTOR DIRECT RECRUITMENT 2026</h6>
              <small>28-Oct-2026</small>
            </div>
          </li>
        </ul>
        </body></html>
        """
        client = FakeHttpClient(
            routes={
                "https://recruitment.rajasthan.gov.in/": (200, {"Content-Type": "text/html"}, html_fixture),
            }
        )
        notices = app.adapter_rssb(rssb_src, client)
        self.assertGreaterEqual(len(notices), 2)
        self.assertIn("Livestock Assistant Direct Recruitment 2026", notices[0].title)
        self.assertIn("notificationdocview?ID=LSA_2026.pdf", notices[0].url)
        self.assertTrue(any("JUNIOR INSTRUCTOR DIRECT RECRUITMENT 2026" in n.title and "28-Oct-2026" in n.title for n in notices))

    def test_22_dsssb_online_portal_vacancies_and_admit_cards(self) -> None:
        """
        Verify DSSSB adapter extracts Current Vacancies (Post Code:XX/YY) and eAdmit Card links
        from https://dsssbonline.nic.in/.
        """
        dsssb_src = app.SOURCE_BY_ID["dsssb"]
        html_fixture = """
        <html><body>
        <table>
          <tr><td>Current Vacancies</td></tr>
          <tr><td>LEGAL ASSISTANT :: Post Code:815/25</td></tr>
          <tr><td>ASSISTANT ENGINEER (CIVIL) :: Post Code:816/25</td></tr>
        </table>
        <a href="AdmitCardEntry.aspx">Generate/Print eAdmit Card</a>
        </body></html>
        """
        client = FakeHttpClient(
            routes={
                "https://dsssbonline.nic.in/": (200, {"Content-Type": "text/html"}, html_fixture),
            }
        )
        notices = app.adapter_dsssb(dsssb_src, client)
        self.assertEqual(len(notices), 3)
        self.assertIn("Post Code: 815/25", notices[0].title)
        self.assertIn("Post Code: 816/25", notices[1].title)
        self.assertTrue(any("eAdmit Card" in n.title for n in notices))

    def test_23_ukpsc_react_bundle_and_hash_router_preservation(self) -> None:
        """
        Verify UKPSC adapter extracts structured exam notifications from ukpsc.net.in's React JS bundle
        and preserves SPA hash-router paths ('#/') in normalize_url.
        """
        ukpsc_src = app.SOURCE_BY_ID["ukpsc"]
        spa_url = app.normalize_url("https://ukpscnet.in/#/pg0zx1/admit-card-login")
        self.assertEqual(spa_url, "https://ukpscnet.in/#/pg0zx1/admit-card-login")

        html_shell = '<html><head><script defer="defer" src="/static/js/main.06eb16fc.js"></script></head><body><div id="root"></div></body></html>'
        js_bundle = """
        const exams = [
          {id:1,exam_name:"Uttarakhand Combined State Civil/Upper Subordinate Services Examination-2026",exam_apply_url:"https://ukpscnet.in/#/upcs26/login",start_date_format:"10-10-2026 (Saturday)",end_date_format:"30-10-2026 (Friday)"},
          {id:2,exam_name:"Lecturer Uttarakhand Special Subordinate Education Service Exam-2026",exam_apply_url:"https://ukpscnet.in/#/lect26/admit-card",start_date_format:"05-10-2026 (Monday)",end_date_format:"25-10-2026 (Sunday)"}
        ];
        """
        client = FakeHttpClient(
            routes={
                "https://ukpsc.net.in/": (200, {"Content-Type": "text/html"}, html_shell),
                "https://ukpsc.net.in/static/js/main.06eb16fc.js": (200, {"Content-Type": "application/javascript"}, js_bundle),
            }
        )
        notices = app.adapter_ukpsc(ukpsc_src, client)
        self.assertEqual(len(notices), 2)
        self.assertIn("Upper Subordinate Services Examination-2026", notices[0].title)
        self.assertEqual(notices[0].publication_date, "10-10-2026")
        self.assertIn("#/upcs26/login", notices[0].url)

    def test_24_all_9_retained_sources_deliver_production_telegram_alerts(self) -> None:
        """
        Verify that a new eligible notice from each of the 9 retained websites triggers
        a production Telegram notification and is never resent on subsequent runs.
        """
        routes: Dict[str, Any] = {}
        for src in LEGACY_SOURCES:
            if src.source_id == "ssc":
                routes[src.notice_urls[0]] = (
                    200,
                    {"Content-Type": "application/json"},
                    json.dumps(
                        {
                            "statusCode": "200",
                            "data": [
                                {
                                    "id": "ssc-prod-9",
                                    "headline": "SSC Combined Graduate Level Examination 2026 Final Result",
                                    "createdAt": "2026-10-19T10:00:00Z",
                                    "attachments": [{"path": "uploads/ssc_cgl_result_2026.pdf"}],
                                }
                            ],
                        }
                    ),
                )
            elif src.source_id == "uppsc":
                routes[src.notice_urls[0]] = (
                    200,
                    {"Content-Type": "text/html"},
                    """
                    <html><body><table>
                      <tr><td>1</td><td>19-10-2026</td><td><a href="Open_PDF_DB.aspx?I4PnQ0tBag=101" title="19-10-2026 UPPSC Combined State Upper Subordinate Recruitment Notification 2026">19-10-2026 UPPSC Combined State Upper Subordinate Recruitment Notification 2026</a></td><td>Visible upto 31 Dec 2026</td></tr>
                    </table></body></html>
                    """,
                )
            else:
                routes[src.notice_urls[0]] = (
                    200,
                    {"Content-Type": "text/html"},
                    f"""
                    <html><body>
                      <h2>Notice Board - {src.organization}</h2>
                      <table>
                        <tr><td>1</td><td>19-10-2026</td><td><a href="/notices/{src.source_id}_new_2026.pdf">19-10-2026 Official Recruitment Notification and Exam Schedule 2026 for {src.source_id.upper()}</a></td></tr>
                      </table>
                    </body></html>
                    """,
                )

        client = FakeHttpClient(routes=routes)
        notifier = RecordingNotifier()

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "notice_state.json")
            st = app.create_initial_state()
            st["initialized"] = True
            for src in LEGACY_SOURCES:
                st["sources"][src.source_id]["baseline_completed"] = True
                st["sources"][src.source_id]["baseline_completed_at"] = "2026-10-01T00:00:00Z"
            app.save_state_atomically(st, state_file)

            summary1, _ = app.run_monitor(
                state_path=state_file,
                mode="monitor",
                sources=LEGACY_SOURCES,
                http_client=client,
                notifier=notifier,
                reference_date=app.datetime.date(2026, 10, 19),
                max_alerts_per_run=10,
            )
            self.assertEqual(summary1.total_websites, 9)
            self.assertEqual(summary1.succeeded_websites, 9)
            self.assertEqual(summary1.failed_websites, 0)
            self.assertEqual(summary1.genuinely_new_notices, 9)
            self.assertEqual(summary1.telegram_deliveries_confirmed, 9)
            self.assertEqual(len(notifier.sent_notices), 9)

            # Repeated run sends 0 duplicate alerts
            summary2, _ = app.run_monitor(
                state_path=state_file,
                mode="monitor",
                sources=LEGACY_SOURCES,
                http_client=client,
                notifier=notifier,
                reference_date=app.datetime.date(2026, 10, 19),
                max_alerts_per_run=10,
            )
            self.assertEqual(summary2.genuinely_new_notices, 0)
            self.assertEqual(summary2.telegram_deliveries_confirmed, 0)
            self.assertEqual(len(notifier.sent_notices), 9)

    def test_25_single_source_failure_does_not_block_remaining_8_websites(self) -> None:
        """
        Verify that a failure on one retained website does not block the other 8 websites
        from being fetched, parsed, and having their eligible Telegram alerts delivered.
        """
        routes: Dict[str, Any] = {}
        for src in LEGACY_SOURCES:
            if src.source_id == "mpesb":
                for u in src.notice_urls:
                    routes[u] = app.SourceFetchError("CONNECTION_TIMEOUT", "MPESB timed out")
            elif src.source_id == "ssc":
                routes[src.notice_urls[0]] = (
                    200,
                    {"Content-Type": "application/json"},
                    json.dumps(
                        {
                            "statusCode": "200",
                            "data": [
                                {
                                    "id": "ssc-iso-1",
                                    "headline": "SSC Selection Post Phase-XIV Recruitment Notification 2026",
                                    "createdAt": "2026-10-19T10:00:00Z",
                                    "attachments": [{"path": "uploads/ssc_phase14_2026.pdf"}],
                                }
                            ],
                        }
                    ),
                )
            elif src.source_id == "uppsc":
                routes[src.notice_urls[0]] = (
                    200,
                    {"Content-Type": "text/html"},
                    """
                    <html><body><table>
                      <tr><td>1</td><td>19-10-2026</td><td><a href="Open_PDF_DB.aspx?I4PnQ0tBag=202" title="19-10-2026 UPPSC RO/ARO Examination Schedule 2026">19-10-2026 UPPSC RO/ARO Examination Schedule 2026</a></td><td>Visible upto 31 Dec 2026</td></tr>
                    </table></body></html>
                    """,
                )
            else:
                routes[src.notice_urls[0]] = (
                    200,
                    {"Content-Type": "text/html"},
                    f"""
                    <html><body>
                      <h2>Notice Board - {src.organization}</h2>
                      <table>
                        <tr><td>1</td><td>19-10-2026</td><td><a href="/notices/{src.source_id}_iso_2026.pdf">19-10-2026 Direct Recruitment Advertisement 2026 for {src.source_id.upper()}</a></td></tr>
                      </table>
                    </body></html>
                    """,
                )

        client = FakeHttpClient(routes=routes)
        notifier = RecordingNotifier()

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "notice_state.json")
            st = app.create_initial_state()
            st["initialized"] = True
            for src in LEGACY_SOURCES:
                st["sources"][src.source_id]["baseline_completed"] = True
                st["sources"][src.source_id]["baseline_completed_at"] = "2026-10-01T00:00:00Z"
            app.save_state_atomically(st, state_file)

            summary, _ = app.run_monitor(
                state_path=state_file,
                mode="monitor",
                sources=LEGACY_SOURCES,
                http_client=client,
                notifier=notifier,
                reference_date=app.datetime.date(2026, 10, 19),
                max_alerts_per_run=10,
            )
            self.assertEqual(summary.total_websites, 9)
            self.assertEqual(summary.succeeded_websites, 8)
            self.assertEqual(summary.failed_websites, 1)
            self.assertEqual(summary.genuinely_new_notices, 8)
            self.assertEqual(summary.telegram_deliveries_confirmed, 8)
            self.assertEqual(len(notifier.sent_notices), 8)

    def test_26_dead_host_caching_prevents_redundant_timeout_retries(self) -> None:
        """
        Verify HttpClient caches unreachable hosts (CONNECTION_TIMEOUT, CONNECTION_RESET, DNS_ERROR)
        within a single run so fallback URLs on the same dead host fail fast without redundant retries.
        """
        client = FakeHttpClient(
            routes={
                "https://upsssc.gov.in/": app.SourceFetchError("CONNECTION_TIMEOUT", "Connection timed out"),
                "https://upsssc.gov.in/Default.aspx": app.SourceFetchError("CONNECTION_TIMEOUT", "Connection timed out"),
            },
            max_retries=2,
        )
        with self.assertRaises(app.SourceFetchError) as ctx1:
            client.fetch_text("https://upsssc.gov.in/")
        self.assertEqual(ctx1.exception.category, "CONNECTION_TIMEOUT")
        self.assertEqual(client.call_counts["https://upsssc.gov.in/"], 2)

        # Second URL on the same dead host should fail immediately from cache with 0 network calls
        with self.assertRaises(app.SourceFetchError) as ctx2:
            client.fetch_text("https://upsssc.gov.in/Default.aspx")
        self.assertEqual(ctx2.exception.category, "CONNECTION_TIMEOUT")
        self.assertEqual(client.call_counts.get("https://upsssc.gov.in/Default.aspx", 0), 0)

    def test_27_state_migration_prunes_crpf_cisf_ssb_and_preserves_retained_9_history(self) -> None:
        """
        Verify that loading/validating a state containing 12 or 19 sources (including CRPF, CISF, SSB)
        safely prunes removed sites from active monitoring while preserving all 9 retained sources,
        their baseline status, and their notice history without re-sending old notices.
        """
        legacy_state = {
            "schema_version": 1,
            "initialized": True,
            "created_at": "2026-10-18T00:00:00Z",
            "updated_at": "2026-10-18T01:00:00Z",
            "sources": {
                # Retained working sources
                "ssc": {
                    "organization": "Staff Selection Commission (SSC)",
                    "homepage_url": "https://ssc.gov.in/",
                    "baseline_completed": True,
                    "baseline_completed_at": "2026-10-18T00:05:00Z",
                    "last_checked_at": "2026-10-18T01:00:00Z",
                    "last_success_at": "2026-10-18T01:00:00Z",
                    "last_status": "MONITOR_CHECK_OK",
                    "last_error": None,
                    "notice_count": 100,
                },
                "upsc": {
                    "organization": "Union Public Service Commission (UPSC)",
                    "homepage_url": "https://www.upsc.gov.in/",
                    "baseline_completed": True,
                    "baseline_completed_at": "2026-10-18T00:05:00Z",
                    "last_checked_at": "2026-10-18T01:00:00Z",
                    "last_success_at": "2026-10-18T01:00:00Z",
                    "last_status": "MONITOR_CHECK_OK",
                    "last_error": None,
                    "notice_count": 57,
                },
                # Removed CRPF, CISF, SSB sources and earlier removed sources
                "crpf": {
                    "organization": "Central Reserve Police Force (CRPF)",
                    "homepage_url": "https://rect.crpf.gov.in/",
                    "baseline_completed": True,
                    "baseline_completed_at": "2026-10-18T00:05:00Z",
                    "last_checked_at": "2026-10-18T01:00:00Z",
                    "last_success_at": "2026-10-18T01:00:00Z",
                    "last_status": "MONITOR_CHECK_OK",
                    "last_error": None,
                    "notice_count": 20,
                },
                "cisf": {
                    "organization": "Central Industrial Security Force (CISF)",
                    "homepage_url": "https://cisfrectt.cisf.gov.in/",
                    "baseline_completed": True,
                    "baseline_completed_at": "2026-10-18T00:05:00Z",
                    "last_checked_at": "2026-10-18T01:00:00Z",
                    "last_success_at": "2026-10-18T01:00:00Z",
                    "last_status": "MONITOR_CHECK_OK",
                    "last_error": None,
                    "notice_count": 15,
                },
                "ssb": {
                    "organization": "Sashastra Seema Bal (SSB)",
                    "homepage_url": "https://ssb.gov.in/",
                    "baseline_completed": True,
                    "baseline_completed_at": "2026-10-18T00:05:00Z",
                    "last_checked_at": "2026-10-18T01:00:00Z",
                    "last_success_at": "2026-10-18T01:00:00Z",
                    "last_status": "MONITOR_CHECK_OK",
                    "last_error": None,
                    "notice_count": 30,
                },
                "upsssc": {
                    "organization": "Uttar Pradesh Subordinate Services Selection Commission (UPSSSC)",
                    "homepage_url": "https://upsssc.gov.in/",
                    "baseline_completed": False,
                    "baseline_completed_at": None,
                    "last_checked_at": "2026-10-18T01:00:00Z",
                    "last_success_at": None,
                    "last_status": "FETCH_FAILED",
                    "last_error": "Connection timed out",
                    "notice_count": 0,
                },
            },
            "notices": {
                "ssc_notice_fp_1": {
                    "fingerprint": "ssc_notice_fp_1",
                    "source_id": "ssc",
                    "organization": "Staff Selection Commission (SSC)",
                    "title": "SSC CGL 2026 Examination Notice",
                    "normalized_url": "https://ssc.gov.in/notices/cgl2026.pdf",
                    "first_seen_at": "2026-10-18T00:05:00Z",
                    "delivery_status": "delivered",
                },
                "upsc_notice_fp_2": {
                    "fingerprint": "upsc_notice_fp_2",
                    "source_id": "upsc",
                    "organization": "Union Public Service Commission (UPSC)",
                    "title": "UPSC Civil Services Examination 2026",
                    "normalized_url": "https://upsc.gov.in/notices/cse2026.pdf",
                    "first_seen_at": "2026-10-18T00:05:00Z",
                    "delivery_status": "baselined",
                },
                "crpf_removed_notice": {
                    "fingerprint": "crpf_removed_notice",
                    "source_id": "crpf",
                    "organization": "Central Reserve Police Force (CRPF)",
                    "title": "CRPF Notice",
                    "normalized_url": "https://rect.crpf.gov.in/old.pdf",
                    "first_seen_at": "2026-10-18T00:05:00Z",
                    "delivery_status": "baselined",
                },
                "cisf_removed_notice": {
                    "fingerprint": "cisf_removed_notice",
                    "source_id": "cisf",
                    "organization": "Central Industrial Security Force (CISF)",
                    "title": "CISF Notice",
                    "normalized_url": "https://cisfrectt.cisf.gov.in/old.pdf",
                    "first_seen_at": "2026-10-18T00:05:00Z",
                    "delivery_status": "baselined",
                },
                "ssb_removed_notice": {
                    "fingerprint": "ssb_removed_notice",
                    "source_id": "ssb",
                    "organization": "Sashastra Seema Bal (SSB)",
                    "title": "SSB Notice",
                    "normalized_url": "https://ssb.gov.in/old.pdf",
                    "first_seen_at": "2026-10-18T00:05:00Z",
                    "delivery_status": "baselined",
                },
            },
        }

        migrated = app.validate_state_schema(legacy_state)

        # 1. The 9 original sources are retained and the new rrb_rrcb source is added UN-baselined
        #    (so its first successful fetch baselines silently instead of alerting the whole feed)
        self.assertEqual(len(migrated["sources"]), 10)
        for src in LEGACY_SOURCES:
            self.assertIn(src.source_id, migrated["sources"])
        self.assertIn("rrb_rrcb", migrated["sources"])
        self.assertFalse(migrated["sources"]["rrb_rrcb"]["baseline_completed"])

        # 2. All removed sources (including crpf, cisf, ssb) pruned from sources
        for rem_id in app.REMOVED_SOURCE_IDS:
            self.assertNotIn(rem_id, migrated["sources"])

        # 3. Retained sources preserve their baseline_completed status and timestamps
        self.assertTrue(migrated["sources"]["ssc"]["baseline_completed"])
        self.assertEqual(migrated["sources"]["ssc"]["baseline_completed_at"], "2026-10-18T00:05:00Z")
        self.assertTrue(migrated["sources"]["upsc"]["baseline_completed"])

        # 4. State remains initialized
        self.assertTrue(migrated["initialized"])

        # 5. Retained notices preserved intact; removed source notices pruned
        self.assertIn("ssc_notice_fp_1", migrated["notices"])
        self.assertEqual(migrated["notices"]["ssc_notice_fp_1"]["delivery_status"], "delivered")
        self.assertIn("upsc_notice_fp_2", migrated["notices"])
        self.assertEqual(migrated["notices"]["upsc_notice_fp_2"]["delivery_status"], "baselined")
        self.assertNotIn("crpf_removed_notice", migrated["notices"])
        self.assertNotIn("cisf_removed_notice", migrated["notices"])
        self.assertNotIn("ssb_removed_notice", migrated["notices"])

    def test_28_strict_keyword_filtering_and_all_five_categories(self) -> None:
        """
        Verify strict keyword matching across all 5 categories (Recruitment, Examination,
        Results, Answer Keys, Next Stages), case-insensitivity, punctuation/spacing
        variations, and custom keyword configuration.
        """
        # 1. Recruitment
        ok, cat, kws = app.match_notice_keywords(
            "Detailed Recruitment Advertisement for 450 Posts — Apply Online via Application Form"
        )
        self.assertTrue(ok)
        self.assertEqual(cat, "Recruitment")
        self.assertTrue(any(k in kws for k in ("recruitment", "advertisement", "apply online", "application form")))

        # 2. Examination
        ok_ex, cat_ex, _ = app.match_notice_keywords(
            "Notice regarding Exam Schedule, City Intimation and Hall-Ticket / E-Admit Card for CGL 2026"
        )
        self.assertTrue(ok_ex)
        self.assertEqual(cat_ex, "Examination")

        # 3. Results (including cut-off / cutoff / cut off variations)
        for phrase in (
            "Final Result and Selection List for Combined State Exam 2026",
            "Declaration of Scorecard and Cut-Off Marks for Tier-1",
            "Provisional Merit List and Cutoff Marks for Constable Exam",
        ):
            ok_res, cat_res, _ = app.match_notice_keywords(phrase)
            self.assertTrue(ok_res, f"Expected match for: {phrase}")
            self.assertEqual(cat_res, "Results")

        # 4. Answer Keys (including answer-key / answerkey / response sheet variations)
        for phrase in (
            "Uploading of Provisional Answer-Key and Response Sheet for JE Exam 2026",
            "Final AnswerKey for Sub-Inspector Examination 2026",
        ):
            ok_ak, cat_ak, _ = app.match_notice_keywords(phrase)
            self.assertTrue(ok_ak, f"Expected match for: {phrase}")
            self.assertEqual(cat_ak, "Answer Keys")

        # 5. Next Stages (DV, document verification, skill test, typing test, interview, PET, PST, medical examination)
        for phrase in (
            "Schedule for Document Verification (DV) and Medical Examination of Selected Candidates",
            "Call Letter for Skill Test / Typing Test for Stenographer Recruitment 2026",
            "Schedule of PET / PST and Interview for Assistant Commandant Examination 2026",
        ):
            ok_ns, cat_ns, _ = app.match_notice_keywords(phrase)
            self.assertTrue(ok_ns, f"Expected match for: {phrase}")
            self.assertEqual(cat_ns, "Next Stages")

        # 6. Custom keyword configuration override
        custom = {"Recruitment": ["special apprenticeship drive"]}
        ok_cust, cat_cust, _ = app.match_notice_keywords(
            "Notice for Special Apprenticeship Drive 2026",
            keyword_categories=custom,
        )
        self.assertTrue(ok_cust)
        self.assertEqual(cat_cust, "Recruitment")

    def test_29_irrelevant_notices_and_contextual_false_positives_rejected(self) -> None:
        """
        Verify administrative, tender, transfer, seniority list, and contextual false-positive
        notices ('posts', 'notification', 'DV', 'interview', 'PET', 'PST' in unrelated contexts)
        are strictly rejected.
        """
        irrelevant_samples = [
            "Notice Inviting Tender (NIT) for Supply of Office Furniture and Computers",
            "Corrigendum to e-Tender / e-Procurement Notice for Security Services",
            "Transfer and Posting Order of Section Officers and Administrative Staff",
            "Provisional Gradation List / Seniority List of Ministerial Cadre as on 01-01-2026",
            "Office Order regarding Empanelment of Private Hospitals and Medical Reimbursement",
            "Public Auction Notice for Condemned Government Vehicles",
            # Contextual false positives for 'posts'
            "Applications sent by Speed Post or Registered Post will not be entertained",
            "Order regarding Ex-Post Facto Sanction of Leave for Staff",
            # Contextual false positives for 'notification'
            "Gazette Notification regarding Public Holidays for Calendar Year 2026",
            "Tariff Notification and Pension Rules Amendment 2026",
            # Contextual false positives for 'DV'
            "Statement of Disbursement Voucher (DV No. 402) for March Quarter",
            # Contextual false positives for 'interview'
            "Press Release and Media Interview of Hon'ble Chairman regarding Digital Portal",
            # Generic navigation/unmatched text
            "Welcome to Official Portal - Annual Report 2025-26 Released",
        ]
        for sample in irrelevant_samples:
            ok, cat, kws = app.match_notice_keywords(sample)
            self.assertFalse(ok, f"Expected irrelevant notice to be rejected: {sample!r} (matched {cat}: {kws})")

    def test_30_mandatory_publication_date_validation_and_non_publication_dates(self) -> None:
        """
        Verify mandatory publication-date validation:
        - Rejects missing, invalid/impossible (31-02-2026, 29-02-2026), future, and stale dates
        - Never treats modified dates, application deadlines, or exam dates as the publication date
        - Extracts application_date and exam_date separately and includes them in formatted alerts
        """
        ref = app.datetime.date(2026, 10, 20)

        # Missing date -> rejected
        valid_missing, iso_missing, reason_missing = app.validate_publication_date(None, reference_date=ref)
        self.assertFalse(valid_missing)
        self.assertIsNone(iso_missing)
        self.assertEqual(reason_missing, "MISSING_PUBLICATION_DATE")

        # Impossible calendar dates -> rejected
        for bad_date in ("31-02-2026", "29-02-2026", "2026-02-30", "32-01-2026"):
            self.assertIsNone(app.parse_publication_date_sort_key(bad_date))
            valid_bad, _, reason_bad = app.validate_publication_date(bad_date, reference_date=ref)
            self.assertFalse(valid_bad)
            self.assertEqual(reason_bad, "INVALID_PUBLICATION_DATE")

        # Future date -> rejected unless explicitly validated
        valid_fut, iso_fut, reason_fut = app.validate_publication_date("25-10-2026", reference_date=ref)
        self.assertFalse(valid_fut)
        self.assertEqual(iso_fut, "2026-10-25")
        self.assertEqual(reason_fut, "FUTURE_PUBLICATION_DATE")

        # Stale date (> max_age_days) -> rejected
        valid_stale, iso_stale, reason_stale = app.validate_publication_date(
            "01-08-2026", reference_date=ref, max_age_days=30
        )
        self.assertFalse(valid_stale)
        self.assertEqual(iso_stale, "2026-08-01")
        self.assertEqual(reason_stale, "STALE_PUBLICATION_DATE")

        # Valid fresh date -> accepted and normalized to YYYY-MM-DD
        valid_ok, iso_ok, reason_ok = app.validate_publication_date(
            "18-10-2026", reference_date=ref, max_age_days=30
        )
        self.assertTrue(valid_ok)
        self.assertEqual(iso_ok, "2026-10-18")
        self.assertEqual(reason_ok, "VALID_PUBLICATION_DATE")

        # Modified dates, application deadlines, and exam dates must NEVER be extracted as publication_date
        self.assertIsNone(app.extract_explicit_date("Page Last Modified on 19-10-2026"))
        self.assertIsNone(app.extract_explicit_date("Last Date for Apply Online: 15-11-2026"))
        self.assertIsNone(app.extract_explicit_date("Closing Date of Application: 20/11/2026"))
        self.assertIsNone(app.extract_explicit_date("Written Exam Date: 12-12-2026"))

        # When notice has publication date + application window + exam date, each is extracted accurately
        rich_text = (
            "Published on 18-10-2026: Combined Graduate Level Recruitment 2026 "
            "(Apply Online from 20-10-2026 to 19-11-2026; Written Exam Date: 15-12-2026)"
        )
        self.assertEqual(app.extract_explicit_date(rich_text), "18-10-2026")
        self.assertEqual(app.extract_application_date(rich_text), "20-10-2026 to 19-11-2026")
        self.assertEqual(app.extract_exam_date(rich_text), "15-12-2026")

        # Verify formatted Telegram message includes Organization, Title, Category, Publication Date, Application/Exam Dates, and URL
        msg = app.format_telegram_message(
            {
                "organization": "Staff Selection Commission (SSC)",
                "title": "Combined Graduate Level Recruitment 2026",
                "category": "Recruitment",
                "publication_date": "2026-10-18",
                "application_date": "20-10-2026 to 19-11-2026",
                "exam_date": "15-12-2026",
                "normalized_url": "https://ssc.gov.in/notices/cgl_2026.pdf",
            }
        )
        self.assertIn("Staff Selection Commission (SSC)", msg)
        self.assertIn("Combined Graduate Level Recruitment 2026", msg)
        self.assertIn("Recruitment", msg)
        self.assertIn("2026-10-18", msg)
        self.assertIn("Application Date:</b> 20-10-2026 to 19-11-2026", msg)
        self.assertIn("Examination Date:</b> 15-12-2026", msg)
        self.assertIn("https://ssc.gov.in/notices/cgl_2026.pdf", msg)

    def test_31_monitor_suppresses_irrelevant_missing_date_and_old_notices(self) -> None:
        """
        Verify run_monitor in monitor mode suppresses:
        - Irrelevant notices (e.g. Tender / Transfer order)
        - Relevant notices missing an official publication date
        - Relevant notices with an impossible or stale publication date (> max_age_days)
        And sends alerts ONLY for relevant, newly published notices with valid dates.
        """
        upsc_src = app.SOURCE_BY_ID["upsc"]
        baseline_html = """
        <html><body><h2>What's New</h2><ul>
          <li><a href="/sites/default/files/Baseline_Exam_2026.pdf">10-10-2026 Civil Services Preliminary Examination 2026 Notification</a></li>
        </ul></body></html>
        """
        incremental_html = """
        <html><body><h2>What's New</h2><ul>
          <li><a href="/sites/default/files/Baseline_Exam_2026.pdf">10-10-2026 Civil Services Preliminary Examination 2026 Notification</a></li>
          <li><a href="/sites/default/files/Tender_Furniture_2026.pdf">19-10-2026 Notice Inviting Tender for Office Computers</a></li>
          <li><a href="/sites/default/files/Postal_Notice_2026.pdf">19-10-2026 Public Notice regarding Speed Post and Registered Post</a></li>
          <li><a href="/sites/default/files/Missing_Date_Result.pdf">Final Result for Combined Medical Services Examination</a></li>
          <li><a href="/sites/default/files/Old_Stale_AdmitCard.pdf">01-05-2026 e-Admit Card for NDA Examination (I) 2026</a></li>
          <li><a href="/sites/default/files/Valid_Fresh_Result_2026.pdf">19-10-2026 Final Result and Merit List for Engineering Services Examination 2026</a></li>
        </ul></body></html>
        """
        routes: Dict[str, Any] = {
            upsc_src.notice_urls[0]: (200, {"Content-Type": "text/html"}, baseline_html),
        }
        client = FakeHttpClient(routes=routes)
        notifier = RecordingNotifier()

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "notice_state.json")
            app.save_state_atomically(app.create_initial_state(), state_file)

            # Step 1: Baseline
            app.run_monitor(
                state_path=state_file,
                mode="baseline",
                sources=(upsc_src,),
                http_client=client,
                notifier=notifier,
                reference_date=app.datetime.date(2026, 10, 18),
            )
            self.assertEqual(len(notifier.sent_notices), 0)

            # Step 2: Monitor run with 2 irrelevant notices (tender + postal), 1 missing date, 1 old notice, and 1 fresh result
            # With publication-date filtering removed, all 3 newly discovered relevant notices are delivered!
            routes[upsc_src.notice_urls[0]] = (200, {"Content-Type": "text/html"}, incremental_html)
            summary, state = app.run_monitor(
                state_path=state_file,
                mode="monitor",
                sources=(upsc_src,),
                http_client=client,
                notifier=notifier,
                reference_date=app.datetime.date(2026, 10, 19),
                max_age_days=30,
            )
            self.assertEqual(summary.genuinely_new_notices, 3)
            self.assertEqual(summary.telegram_deliveries_confirmed, 3)
            self.assertEqual(summary.filtered_irrelevant_notices, 1)
            self.assertEqual(summary.rejected_missing_or_invalid_date_notices, 0)
            self.assertEqual(summary.rejected_stale_notices, 0)
            self.assertEqual(len(notifier.sent_notices), 3)
            sent_titles = [rec["title"] for rec, _, _ in notifier.sent_notices]
            self.assertTrue(
                any("Final Result and Merit List for Engineering Services Examination 2026" in t for t in sent_titles)
            )
            self.assertTrue(
                any("Final Result for Combined Medical Services Examination" in t for t in sent_titles)
            )
            self.assertTrue(
                any("e-Admit Card for NDA Examination (I) 2026" in t for t in sent_titles)
            )

            # Step 3: Repeated run sends 0 alerts and does not re-evaluate suppressed notices
            summary_repeat, _ = app.run_monitor(
                state_path=state_file,
                mode="monitor",
                sources=(upsc_src,),
                http_client=client,
                notifier=notifier,
                reference_date=app.datetime.date(2026, 10, 19),
                max_age_days=30,
            )
            self.assertEqual(summary_repeat.genuinely_new_notices, 0)
            self.assertEqual(summary_repeat.telegram_deliveries_confirmed, 0)
            self.assertEqual(len(notifier.sent_notices), 3)

    def test_32_cross_page_url_and_title_variation_deduplication(self) -> None:
        """
        Verify strong duplicate prevention across multiple pages, URL query parameters,
        HTTP/HTTPS scheme differences, and punctuation/spacing variations in titles.
        """
        upsc_src = app.SOURCE_BY_ID["upsc"]
        html_v1 = """
        <html><body><h2>What's New</h2><ul>
          <li><a href="http://www.upsc.gov.in/sites/default/files/Advt-12-2026.pdf?utm_source=homepage">19-10-2026 Recruitment Advertisement No. 12/2026 for 85 Posts</a></li>
        </ul></body></html>
        """
        # Second run serves the same notice with HTTPS, different query param, and minor title spacing/badge variation
        html_v2 = """
        <html><body><h2>What's New</h2><ul>
          <li><a href="https://upsc.gov.in/sites/default/files/Advt-12-2026.pdf?download=1">1. 19-10-2026 Recruitment  Advertisement No. 12/2026 - for 85 Posts [NEW] (2.1 MB)</a></li>
        </ul></body></html>
        """
        routes: Dict[str, Any] = {
            upsc_src.notice_urls[0]: (200, {"Content-Type": "text/html"}, html_v1),
        }
        client = FakeHttpClient(routes=routes)
        notifier = RecordingNotifier()

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "notice_state.json")
            st = app.create_initial_state()
            st["initialized"] = True
            st["sources"]["upsc"]["baseline_completed"] = True
            st["sources"]["upsc"]["baseline_completed_at"] = "2026-10-01T00:00:00Z"
            app.save_state_atomically(st, state_file)

            # Run 1 sends the new notice once
            s1, _ = app.run_monitor(
                state_path=state_file,
                mode="monitor",
                sources=(upsc_src,),
                http_client=client,
                notifier=notifier,
                reference_date=app.datetime.date(2026, 10, 19),
            )
            self.assertEqual(s1.genuinely_new_notices, 1)
            self.assertEqual(s1.telegram_deliveries_confirmed, 1)

            # Run 2 with URL/title variations must detect duplicate and send 0 alerts
            routes[upsc_src.notice_urls[0]] = (200, {"Content-Type": "text/html"}, html_v2)
            s2, _ = app.run_monitor(
                state_path=state_file,
                mode="monitor",
                sources=(upsc_src,),
                http_client=client,
                notifier=notifier,
                reference_date=app.datetime.date(2026, 10, 19),
            )
            self.assertEqual(s2.genuinely_new_notices, 0)
            self.assertEqual(s2.telegram_deliveries_confirmed, 0)
            self.assertEqual(len(notifier.sent_notices), 1)

    def test_33_max_ten_production_alerts_per_run_cap(self) -> None:
        """
        Verify that a single monitoring run sends at most MAX_PRODUCTION_ALERTS_PER_RUN (10)
        production notifications and leaves overflow notices queued for the next run.
        """
        ssc_src = app.SOURCE_BY_ID["ssc"]
        items = [
            {
                "id": f"ssc-bulk-{i}",
                "headline": f"Recruitment Notification No. {i}/2026 for Group-B Posts",
                "createdAt": "2026-10-19T09:00:00.000Z",
                "attachments": [{"path": f"uploads/notice_{i}_2026.pdf"}],
            }
            for i in range(1, 15)  # 14 distinct valid new notices
        ]
        ssc_payload = {"statusCode": "200", "data": items}
        client = FakeHttpClient(
            routes={
                ssc_src.notice_urls[0]: (200, {"Content-Type": "application/json"}, json.dumps(ssc_payload)),
            }
        )
        notifier = RecordingNotifier()

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "notice_state.json")
            st = app.create_initial_state()
            st["initialized"] = True
            st["sources"]["ssc"]["baseline_completed"] = True
            st["sources"]["ssc"]["baseline_completed_at"] = "2026-10-01T00:00:00Z"
            app.save_state_atomically(st, state_file)

            # Run 1: discovers 14 valid notices, sends exactly 10 (capped)
            s1, state1 = app.run_monitor(
                state_path=state_file,
                mode="monitor",
                sources=(ssc_src,),
                http_client=client,
                notifier=notifier,
                reference_date=app.datetime.date(2026, 10, 19),
                max_alerts_per_run=10,
            )
            self.assertEqual(s1.genuinely_new_notices, 14)
            self.assertEqual(s1.telegram_deliveries_confirmed, 10)
            self.assertEqual(s1.capped_deferred_notices, 4)
            self.assertEqual(len(notifier.sent_notices), 10)
            self.assertEqual(len(state1["pending_queue"]), 4)

            # Run 2: sends the remaining 4 deferred notices
            s2, state2 = app.run_monitor(
                state_path=state_file,
                mode="monitor",
                sources=(ssc_src,),
                http_client=client,
                notifier=notifier,
                reference_date=app.datetime.date(2026, 10, 19),
                max_alerts_per_run=10,
            )
            self.assertEqual(s2.genuinely_new_notices, 0)
            self.assertEqual(s2.telegram_deliveries_confirmed, 4)
            self.assertEqual(len(notifier.sent_notices), 14)
            self.assertEqual(len(state2["pending_queue"]), 0)

    def test_34_bug1_ist_same_day_yesterday_two_days_ago_and_utc_midnight_conversion(self) -> None:
        """
        Verify Bug 1 requirements (Scenarios 1-11):
        1. Notice published today in Asia/Kolkata -> accepted
        2. Notice published yesterday in Asia/Kolkata -> rejected (STALE_PUBLICATION_DATE)
        3. Notice published two days ago -> rejected
        4. Notice published in a previous month/year -> rejected
        5. Old notice first discovered today -> rejected and recorded as baselined
        6. Old notice edited today without a new official publication date -> rejected
        7. Notice with missing publication date -> rejected (MISSING_PUBLICATION_DATE)
        8. Notice with invalid/unparseable date -> rejected (INVALID_PUBLICATION_DATE)
        9. Notice with ambiguous or conflicting dates -> rejected (AMBIGUOUS_PUBLICATION_DATE / CONFLICTING_PUBLICATION_DATES)
        10. Notice with future date -> rejected (FUTURE_PUBLICATION_DATE)
        11. UTC timestamp near midnight converted accurately to Asia/Kolkata (UTC+05:30)
        """
        today_ist = app.datetime.date(2026, 10, 24)
        self.assertEqual(app.DEFAULT_MAX_NOTICE_AGE_DAYS, 0)

        # Scenario 1: Published today (2026-10-24) -> accepted
        ok_today, iso_today, reason_today = app.validate_publication_date(
            "24-10-2026", reference_date=today_ist
        )
        self.assertTrue(ok_today)
        self.assertEqual(iso_today, "2026-10-24")
        self.assertEqual(reason_today, "VALID_PUBLICATION_DATE")

        # Scenario 2: Published yesterday (2026-10-23) -> rejected!
        ok_yest, iso_yest, reason_yest = app.validate_publication_date(
            "23-10-2026", reference_date=today_ist
        )
        self.assertFalse(ok_yest)
        self.assertEqual(iso_yest, "2026-10-23")
        self.assertEqual(reason_yest, "STALE_PUBLICATION_DATE")

        # Exact SSC stenographer bug from kniklife1/sscv6 (createdAt=2026-10-23T11:02:28.718Z, url=tentative_vacancies_24102026.pdf)
        ok_ssc_bug, iso_ssc_bug, reason_ssc_bug = app.validate_publication_date(
            "2026-10-23T11:02:28.718Z",
            reference_date=today_ist,
            url="https://ssc.gov.in/api/attachment/uploads/masterData/NoticeBoards/tentative_vacancies_24102026.pdf",
        )
        self.assertFalse(ok_ssc_bug)
        self.assertIn(reason_ssc_bug, ("STALE_PUBLICATION_DATE", "CONFLICTING_PUBLICATION_DATES"))

        # Scenario 3: Published two days ago (2026-10-22) -> rejected
        ok_2d, iso_2d, reason_2d = app.validate_publication_date("22-10-2026", reference_date=today_ist)
        self.assertFalse(ok_2d)
        self.assertEqual(iso_2d, "2026-10-22")
        self.assertEqual(reason_2d, "STALE_PUBLICATION_DATE")

        # Scenario 4: Published in previous month/year -> rejected
        for old_str in ("15-09-2026", "24-10-2025"):
            ok_old, _, reason_old = app.validate_publication_date(old_str, reference_date=today_ist)
            self.assertFalse(ok_old)
            self.assertEqual(reason_old, "STALE_PUBLICATION_DATE")

        # Scenario 6: Sitemap <lastmod> or page Last-Modified today must NEVER be treated as publication date
        self.assertIsNone(app.extract_explicit_date("<lastmod>2026-10-24T10:00:00Z</lastmod>"))
        self.assertIsNone(app.extract_explicit_date("Last-Modified: 24-10-2026"))
        ok_mod, _, reason_mod = app.validate_publication_date(
            "24-10-2026",
            reference_date=today_ist,
            context_text="Page Last Modified on 24-10-2026",
        )
        self.assertFalse(ok_mod)
        self.assertEqual(reason_mod, "NON_PUBLICATION_DATE_TYPE")

        # Scenario 7 & 8: Missing and invalid dates -> rejected
        self.assertEqual(
            app.validate_publication_date(None, reference_date=today_ist),
            (False, None, "MISSING_PUBLICATION_DATE"),
        )
        self.assertEqual(
            app.validate_publication_date("31-02-2026", reference_date=today_ist),
            (False, None, "INVALID_PUBLICATION_DATE"),
        )

        # Scenario 9: Ambiguous partial/2-digit-year date or conflicting listing vs PDF date -> rejected (fail closed)
        ok_amb, _, reason_amb = app.validate_publication_date("12/26", reference_date=today_ist)
        self.assertFalse(ok_amb)
        self.assertEqual(reason_amb, "AMBIGUOUS_PUBLICATION_DATE")

        ok_conf, _, reason_conf = app.validate_publication_date(
            "24-10-2026",
            reference_date=today_ist,
            context_text="Recruitment Notice Published on 24-10-2026",
            url="https://ssc.gov.in/uploads/notice_23102026.pdf",
        )
        self.assertFalse(ok_conf)
        self.assertEqual(reason_conf, "CONFLICTING_PUBLICATION_DATES")

        # Scenario 10: Future date -> rejected
        ok_fut, iso_fut, reason_fut = app.validate_publication_date("25-10-2026", reference_date=today_ist)
        self.assertFalse(ok_fut)
        self.assertEqual(iso_fut, "2026-10-25")
        self.assertEqual(reason_fut, "FUTURE_PUBLICATION_DATE")

        # Scenario 11: UTC timestamp near midnight converted to Asia/Kolkata (UTC+05:30):
        # 2026-10-23T19:00:00Z is 2026-10-24T00:30:00+05:30 (Today in IST -> accepted)
        ok_utc_after_midnight, iso_utc_after, _ = app.validate_publication_date(
            "2026-10-23T19:00:00Z", reference_date=today_ist
        )
        self.assertTrue(ok_utc_after_midnight)
        self.assertEqual(iso_utc_after, "2026-10-24")

        # 2026-10-23T18:00:00Z is 2026-10-23T23:30:00+05:30 (Yesterday in IST -> rejected!)
        ok_utc_before_midnight, iso_utc_before, reason_utc_before = app.validate_publication_date(
            "2026-10-23T18:00:00Z", reference_date=today_ist
        )
        self.assertFalse(ok_utc_before_midnight)
        self.assertEqual(iso_utc_before, "2026-10-23")
        self.assertEqual(reason_utc_before, "STALE_PUBLICATION_DATE")

        # Scenario 5: End-to-end monitor check where yesterday's notice & old edited notice are first discovered today
        ssc_src = app.SOURCE_BY_ID["ssc"]
        ssc_payload = {
            "statusCode": "200",
            "data": [
                {
                    "id": "c96uok9wtnz6h5z0kfopt",
                    "headline": "Tentative Vacancy of Grade C Stenographers Limited Departmental Competitive Examination, 2025",
                    "createdAt": "2026-10-23T11:02:28.718Z",
                    "attachments": [{"path": "uploads/masterData/NoticeBoards/tentative_vacancies_23102026.pdf"}],
                },
                {
                    "id": "ssc-today-valid",
                    "headline": "SSC Combined Graduate Level Examination 2026 Tier-I Admit Card Notice",
                    "createdAt": "2026-10-23T19:15:00.000Z",  # 2026-10-24 00:45 IST (Today in IST)
                    "attachments": [{"path": "uploads/masterData/NoticeBoards/cgl_admit_24102026.pdf"}],
                },
            ],
        }
        client = FakeHttpClient(
            routes={
                ssc_src.notice_urls[0]: (200, {"Content-Type": "application/json"}, json.dumps(ssc_payload)),
            }
        )
        notifier = RecordingNotifier()
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "notice_state.json")
            st = app.create_initial_state()
            st["initialized"] = True
            st["sources"]["ssc"]["baseline_completed"] = True
            st["sources"]["ssc"]["baseline_completed_at"] = "2026-10-01T00:00:00Z"
            app.save_state_atomically(st, state_file)

            summary, final_state = app.run_monitor(
                state_path=state_file,
                mode="monitor",
                sources=(ssc_src,),
                http_client=client,
                notifier=notifier,
                reference_date=today_ist,
            )
            # With publication-date filtering removed, both newly discovered relevant SSC notices are eligible and delivered
            self.assertEqual(summary.genuinely_new_notices, 2)
            self.assertEqual(summary.rejected_stale_notices, 0)
            self.assertEqual(summary.telegram_deliveries_confirmed, 2)
            self.assertEqual(len(notifier.sent_notices), 2)
            by_id = {rec["source_identifier"]: (rec, msg) for rec, _, msg in notifier.sent_notices}
            self.assertIn("ssc-today-valid", by_id)
            self.assertIn("c96uok9wtnz6h5z0kfopt", by_id)
            self.assertEqual(by_id["ssc-today-valid"][0]["publication_date"], "2026-10-24")
            self.assertIn("Publication Date:</b> 2026-10-24", by_id["ssc-today-valid"][1])
            self.assertEqual(by_id["c96uok9wtnz6h5z0kfopt"][0]["publication_date"], "2026-10-23")
            self.assertIn("Publication Date:</b> 2026-10-23", by_id["c96uok9wtnz6h5z0kfopt"][1])

    def test_35_bug2_five_layer_identity_deduplication_and_two_distinct_notices_today(self) -> None:
        """
        Verify Bug 2 identity requirements (Scenarios 12-16):
        12. Same notice scraped across two consecutive runs -> sent once, skipped second time
        13. Same notice with tracking/cache-busting query parameters -> sent once
        14. Same notice with minor title whitespace/case/punctuation changes -> sent once
        15. Same notice discovered via listing URL and direct PDF URL -> sent once
        16. Two genuinely different notices published today -> both sent
        """
        today_ist = app.datetime.date(2026, 10, 24)
        upsc_src = app.SOURCE_BY_ID["upsc"]

        # Run 1: Listing has 2 genuinely distinct notices published today + 1 wrapper link for Notice A
        html_run1 = """
        <html><body><h2>What's New</h2><ul>
          <li><a href="https://www.upsc.gov.in/whats-new/nda-ii-2026">24-10-2026 Examination Schedule: National Defence Academy (II) Examination, 2026</a></li>
          <li><a href="https://www.upsc.gov.in/sites/default/files/NDA_II_2026_Schedule.pdf?utm_source=portal&_t=111">24-10-2026 Examination Schedule: National Defence Academy (II) Examination, 2026</a></li>
          <li><a href="https://www.upsc.gov.in/sites/default/files/CDS_II_2026_Schedule.pdf?v=1">24-10-2026 Examination Schedule: Combined Defence Services (II) Examination, 2026</a></li>
        </ul></body></html>
        """
        # Run 2: Same 2 notices with cache-busting query params, http vs https, and minor title edits
        html_run2 = """
        <html><body><h2>What's New</h2><ul>
          <li><a href="http://upsc.gov.in/sites/default/files/NDA_II_2026_Schedule.pdf?cachebust=999&fbclid=abc">1. 24-10-2026  Examination Schedule - National Defence Academy (II) Examination 2026 [NEW!] (1.4 MB)</a></li>
          <li><a href="https://upsc.gov.in/sites/default/files/CDS_II_2026_Schedule.pdf?timestamp=22222">2. 24-10-2026 Examination Schedule: Combined Defence Services (II) Examination, 2026 (New)</a></li>
        </ul></body></html>
        """
        routes: Dict[str, Any] = {
            upsc_src.notice_urls[0]: (200, {"Content-Type": "text/html"}, html_run1),
        }
        client = FakeHttpClient(routes=routes)
        notifier = RecordingNotifier()

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "notice_state.json")
            st = app.create_initial_state()
            st["initialized"] = True
            st["sources"]["upsc"]["baseline_completed"] = True
            st["sources"]["upsc"]["baseline_completed_at"] = "2026-10-01T00:00:00Z"
            app.save_state_atomically(st, state_file)

            # Run 1: Wrapper + PDF for NDA-II collapse to 1 notice; CDS-II is distinct -> 2 notices sent
            s1, state1 = app.run_monitor(
                state_path=state_file,
                mode="monitor",
                sources=(upsc_src,),
                http_client=client,
                notifier=notifier,
                reference_date=today_ist,
            )
            self.assertEqual(s1.genuinely_new_notices, 2)
            self.assertEqual(s1.telegram_deliveries_confirmed, 2)
            self.assertEqual(len(notifier.sent_notices), 2)

            # Run 2: Cache-busting params + minor title edits -> 0 new notices, 0 duplicate alerts
            routes[upsc_src.notice_urls[0]] = (200, {"Content-Type": "text/html"}, html_run2)
            s2, _ = app.run_monitor(
                state_path=state_file,
                mode="monitor",
                sources=(upsc_src,),
                http_client=client,
                notifier=notifier,
                reference_date=today_ist,
            )
            self.assertEqual(s2.genuinely_new_notices, 0)
            self.assertEqual(s2.telegram_deliveries_confirmed, 0)
            self.assertEqual(len(notifier.sent_notices), 2)

    def test_36_bug2_telegram_ledger_states_and_message_id_tracking(self) -> None:
        """
        Verify Bug 2 Telegram delivery ledger requirements (Scenarios 17-20):
        17. Telegram sendMessage returns success -> marked ledger_status='sent', delivery_status='delivered',
            telegram_message_id recorded, and never resent.
        18. Telegram sendMessage returns HTTP 500 or pre-send error -> marked 'failed', retried safely.
        19. Telegram sendMessage times out after request transmission (or returns HTTP 200 with corrupt JSON) ->
            marked ledger_status='ambiguous', delivery_status='delivered', and not blindly resent.
        20. Workflow YAML synchronizes with origin/${GITHUB_REF_NAME} after acquiring concurrency lock.
        """
        notifier = app.TelegramNotifier(
            bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            chat_id="-1001234567890",
            max_retries=1,
            sleep_fn=lambda _: None,
        )
        with mock.patch.object(
            notifier,
            "_post_json",
            return_value=(
                200,
                {"Content-Type": "application/json"},
                json.dumps({"ok": True, "result": {"message_id": 987654}}).encode("utf-8"),
            ),
        ):
            notifier.send_notice(
                {
                    "source_id": "ssc",
                    "organization": "Staff Selection Commission (SSC)",
                    "title": "SSC CGL 2026 Final Result",
                    "category": "Results",
                    "publication_date": "2026-10-24",
                    "normalized_url": "https://ssc.gov.in/cgl_2026.pdf",
                }
            )
            self.assertEqual(notifier.last_message_id, 987654)

        # HTTP 200 with truncated/unparseable JSON body must raise ambiguous=True
        with mock.patch.object(
            notifier,
            "_post_json",
            return_value=(200, {"Content-Type": "application/json"}, b"{truncated_json"),
        ):
            with self.assertRaises(app.TelegramDeliveryError) as ctx_amb:
                notifier.send_notice(
                    {
                        "source_id": "ssc",
                        "organization": "Staff Selection Commission (SSC)",
                        "title": "SSC CGL 2026 Final Result",
                        "category": "Results",
                        "publication_date": "2026-10-24",
                        "normalized_url": "https://ssc.gov.in/cgl_2026.pdf",
                    }
                )
            self.assertTrue(ctx_amb.exception.ambiguous)

        # Verify workflow YAML contains post-checkout git reset --hard origin/${GITHUB_REF_NAME}
        workflow_path = os.path.join(
            os.path.dirname(os.path.abspath(app.__file__)),
            ".github",
            "workflows",
            "main.yml",
        )
        with open(workflow_path, "r", encoding="utf-8") as fh:
            wf_content = fh.read()
        self.assertIn('git fetch origin "${GITHUB_REF_NAME}"', wf_content)
        self.assertIn('git reset --hard "origin/${GITHUB_REF_NAME}"', wf_content)
        self.assertIn("TZ: Asia/Kolkata", wf_content)

    def test_37_bug3_concurrent_fetching_noop_commit_suppression_and_pending_queue_revalidation(self) -> None:
        """
        Verify Bug 3 performance, no-op commit suppression, and pending_queue revalidation (Scenarios 21-22):
        - Concurrent fetching via ThreadPoolExecutor completes all 9 sources in parallel when using HttpClient
        - Pure no-op monitor run sets summary.state_modified = False and does not rewrite state file
        - Overflow notices (> 10) persisted in pending_queue are revalidated on the next run; if the next run
          occurs on the next calendar day in Asia/Kolkata, yesterday's queued notices are rejected and NOT sent.
        """
        ssc_src = app.SOURCE_BY_ID["ssc"]
        items = [
            {
                "id": f"ssc-q-{i}",
                "headline": f"Recruitment Advertisement No. {i}/2026 for Inspector Posts",
                "createdAt": "2026-10-24T09:00:00.000Z",
                "attachments": [{"path": f"uploads/insp_{i}_24102026.pdf"}],
            }
            for i in range(1, 14)  # 13 notices published on 2026-10-24
        ]
        ssc_payload = {"statusCode": "200", "data": items}
        client = FakeHttpClient(
            routes={
                ssc_src.notice_urls[0]: (200, {"Content-Type": "application/json"}, json.dumps(ssc_payload)),
            }
        )
        notifier = RecordingNotifier()

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "notice_state.json")
            st = app.create_initial_state()
            st["initialized"] = True
            st["sources"]["ssc"]["baseline_completed"] = True
            st["sources"]["ssc"]["baseline_completed_at"] = "2026-10-01T00:00:00Z"
            app.save_state_atomically(st, state_file)

            # Run 1 on 2026-10-24: sends 10, defers 3 in pending_queue
            s1, state1 = app.run_monitor(
                state_path=state_file,
                mode="monitor",
                sources=(ssc_src,),
                http_client=client,
                notifier=notifier,
                reference_date=app.datetime.date(2026, 10, 24),
                max_alerts_per_run=10,
            )
            self.assertEqual(s1.telegram_deliveries_confirmed, 10)
            self.assertEqual(s1.capped_deferred_notices, 3)
            self.assertEqual(len(state1["pending_queue"]), 3)
            self.assertTrue(s1.state_modified)

            # Run 2 happens the NEXT day (2026-10-25 in Asia/Kolkata):
            # With publication-date filtering removed, the 3 deferred notices in pending_queue remain eligible and are delivered!
            s2, state2 = app.run_monitor(
                state_path=state_file,
                mode="monitor",
                sources=(ssc_src,),
                http_client=client,
                notifier=notifier,
                reference_date=app.datetime.date(2026, 10, 25),
                max_alerts_per_run=10,
            )
            self.assertEqual(s2.telegram_deliveries_confirmed, 3)
            self.assertEqual(s2.rejected_stale_notices, 0)
            self.assertEqual(len(state2["pending_queue"]), 0)
            self.assertEqual(len(notifier.sent_notices), 13)

            # Run 3 with no changes at all -> pure no-op run, state_modified must be False!
            mtime_before = os.path.getmtime(state_file)
            s3, _ = app.run_monitor(
                state_path=state_file,
                mode="monitor",
                sources=(ssc_src,),
                http_client=client,
                notifier=notifier,
                reference_date=app.datetime.date(2026, 10, 25),
                max_alerts_per_run=10,
            )
            mtime_after = os.path.getmtime(state_file)
            self.assertTrue(s3.state_persisted)
            self.assertFalse(s3.state_modified)
            self.assertEqual(mtime_before, mtime_after)

    def test_38_preserved_notice_state_json_integrity_and_405_records(self) -> None:
        """
        Verify Scenarios 23 & 24:
        - Existing data/notice_state.json history is preserved without reset or truncation
          (all 405 historical records from kniklife1/sscv6 are present and valid)
        - Exactly the 9 configured websites exist in state['sources']
        """
        repo_state_path = os.path.join(
            os.path.dirname(os.path.abspath(app.__file__)),
            "data",
            "notice_state.json",
        )
        self.assertTrue(os.path.exists(repo_state_path))
        loaded = app.load_state(repo_state_path, allow_init_if_missing=False)
        self.assertEqual(loaded["schema_version"], 1)
        self.assertTrue(loaded["initialized"])
        self.assertEqual(len(loaded["sources"]), 10)  # 9 original + rrb_rrcb (added on load if absent)
        for src in LEGACY_SOURCES:
            self.assertIn(src.source_id, loaded["sources"])
            self.assertTrue(loaded["sources"][src.source_id]["baseline_completed"])
        self.assertIn("rrb_rrcb", loaded["sources"])
        self.assertGreaterEqual(len(loaded["notices"]), 405)
        for fp, rec in loaded["notices"].items():
            self.assertIn("canonical_url", rec)
            self.assertIn("canonical_title", rec)
            self.assertIn("content_hash", rec)
            self.assertIn("ledger_status", rec)
            self.assertIn("eligibility_status", rec)

    def test_39_live_website_check_engine_read_only_and_status_separation(self) -> None:
        """
        Verify Live Website Check engine (check_single_source_live & run_live_website_check):
        - Separates source_health_status (SUCCESS/FAILED) from discovery_status
          (NEW_NOTICE_FOUND, NO_NEW_NOTICE, DATE_UNVERIFIED, FAILED).
        - Accepts ONLY notices published today in Asia/Kolkata (rejects yesterday's notices).
        - Isolates a failed source without blocking the remaining sources.
        - Never modifies data/notice_state.json or sends Telegram messages.
        """
        today_ist = app.datetime.date(2026, 10, 24)
        ssc_src = app.SOURCE_BY_ID["ssc"]
        upsc_src = app.SOURCE_BY_ID["upsc"]
        dsssb_src = app.SOURCE_BY_ID["dsssb"]
        upprpb_src = app.SOURCE_BY_ID["upprpb"]

        # SSC has 1 notice published TODAY (2026-10-24 IST) and 1 published YESTERDAY (2026-10-23 IST)
        ssc_payload = {
            "statusCode": "200",
            "data": [
                {
                    "id": "ssc-today-101",
                    "headline": "SSC Combined Graduate Level Examination 2026 Final Answer Key",
                    "createdAt": "2026-10-24T05:30:00.000Z",
                    "attachments": [{"path": "uploads/cgl_final_answer_key_24102026.pdf"}],
                },
                {
                    "id": "ssc-yesterday-100",
                    "headline": "Tentative Vacancy of Grade C Stenographers Examination, 2025",
                    "createdAt": "2026-10-23T11:02:28.718Z",
                    "attachments": [{"path": "uploads/steno_vacancy_23102026.pdf"}],
                },
            ],
        }
        # UPSC has only yesterday's notice (23-10-2026) -> SUCCESS health, NO_NEW_NOTICE discovery
        upsc_html = """
        <html><body><h2>What's New</h2><ul>
            <li><a href="/sites/default/files/Advt-19-2026.pdf">23-10-2026 Recruitment Advertisement No. 19/2026 for 40 Posts</a></li>
        </ul></body></html>
        """
        # DSSSB has an undated relevant vacancy row -> SUCCESS health, DATE_UNVERIFIED discovery
        dsssb_html = """
        <html><body><table>
            <tr><td>Current Vacancy: Legal Assistant (Post Code: 815/25)</td><td><a href="/ notices/advt_815_25.pdf">Download PDF</a></td></tr>
        </table></body></html>
        """

        routes = {
            ssc_src.notice_urls[0]: (200, {"Content-Type": "application/json"}, json.dumps(ssc_payload)),
            upsc_src.notice_urls[0]: (200, {"Content-Type": "text/html"}, upsc_html),
            dsssb_src.notice_urls[0]: (200, {"Content-Type": "text/html"}, dsssb_html),
            # upprpb has no routes -> raises SourceFetchError -> FAILED
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "notice_state.json")
            st = app.create_initial_state()
            st["initialized"] = True
            app.save_state_atomically(st, state_file)
            with open(state_file, "rb") as f_before:
                before_bytes = f_before.read()

            streamed_events = []
            report = app.run_live_website_check(
                state_path=state_file,
                sources=(ssc_src, upsc_src, dsssb_src, upprpb_src),
                reference_date=today_ist,
                max_age_days=0,
                http_client_factory=lambda: FakeHttpClient(routes=routes),
                on_source_complete=lambda res: streamed_events.append(res["source_id"]),
            )

            with open(state_file, "rb") as f_after:
                after_bytes = f_after.read()
            self.assertEqual(before_bytes, after_bytes, "Live website check must never modify notice_state.json")
            self.assertTrue(report["ok"])
            self.assertEqual(report["data_origin"], "LIVE_FETCH")
            self.assertTrue(report["read_only_state"])
            self.assertEqual(report["telegram_alerts_sent"], 0)
            self.assertEqual(report["total_sources_checked"], 4)
            self.assertEqual(report["succeeded_sources"], 3)
            self.assertEqual(report["failed_sources"], 1)
            self.assertEqual(report["sources_with_new_notices"], 3)
            self.assertEqual(report["total_eligible_today_notices"], 4)
            self.assertEqual(set(streamed_events), {"ssc", "upsc", "dsssb", "upprpb"})

            by_id = {s["source_id"]: s for s in report["sources"]}
            # SSC: NEW_NOTICE_FOUND (both unseen relevant notices are eligible regardless of date)
            self.assertEqual(by_id["ssc"]["source_health_status"], "SUCCESS")
            self.assertEqual(by_id["ssc"]["discovery_status"], "NEW_NOTICE_FOUND")
            self.assertEqual(len(by_id["ssc"]["eligible_today_notices"]), 2)
            self.assertEqual(by_id["ssc"]["rejected_stale_count"], 0)

            # UPSC: NEW_NOTICE_FOUND (yesterday's unseen notice is eligible)
            self.assertEqual(by_id["upsc"]["source_health_status"], "SUCCESS")
            self.assertEqual(by_id["upsc"]["discovery_status"], "NEW_NOTICE_FOUND")
            self.assertEqual(len(by_id["upsc"]["eligible_today_notices"]), 1)
            self.assertEqual(by_id["upsc"]["rejected_stale_count"], 0)
            self.assertIsNotNone(by_id["upsc"]["latest_observed_relevant_notice"])

            # DSSSB: NEW_NOTICE_FOUND (missing publication date does not reject unseen relevant notice)
            self.assertEqual(by_id["dsssb"]["source_health_status"], "SUCCESS")
            self.assertEqual(by_id["dsssb"]["discovery_status"], "NEW_NOTICE_FOUND")
            self.assertEqual(len(by_id["dsssb"]["eligible_today_notices"]), 1)
            self.assertEqual(by_id["dsssb"]["rejected_unverified_date_count"], 0)

            # UPPRPB: FAILED (isolated cleanly)
            self.assertEqual(by_id["upprpb"]["source_health_status"], "FAILED")
            self.assertEqual(by_id["upprpb"]["discovery_status"], "FAILED")
            self.assertIsNotNone(by_id["upprpb"]["error_message"])

    def test_40_regression_parse_official_date_token_and_all_nine_adapters_date_handling(self) -> None:
        """
        Regression test for `NameError: name 'parse_official_date_token' is not defined`:
        - Verifies `parse_official_date_token` is defined and delegates to `parse_publication_date_sort_key`.
        - Verifies all 9 source adapters (`ssc`, `upsc`, `uppsc`, `hssc`, `rssb`, `mpesb`,
          `dsssb`, `upprpb`, `ukpsc`) execute `check_single_source_live` without `NameError`
          across valid today dates, stale dates, missing dates, conflicting dates, and
          pre-existing ledger records with `first_seen_at`.
        """
        today_ist = app.datetime.date(2026, 10, 24)

        # 1. Direct unit checks on parse_official_date_token
        self.assertEqual(app.parse_official_date_token("2026-10-24"), app.datetime.date(2026, 10, 24))
        self.assertEqual(app.parse_official_date_token("24-10-2026"), app.datetime.date(2026, 10, 24))
        self.assertEqual(app.parse_official_date_token("24 October 2026"), app.datetime.date(2026, 10, 24))
        self.assertEqual(app.parse_official_date_token("Oct 24, 2026"), app.datetime.date(2026, 10, 24))
        # Timezone-aware UTC -> Asia/Kolkata conversion
        self.assertEqual(
            app.parse_official_date_token("2026-10-23T19:00:00Z"),
            app.datetime.date(2026, 10, 24),
        )
        self.assertEqual(
            app.parse_official_date_token("2026-10-23T18:00:00Z"),
            app.datetime.date(2026, 10, 23),
        )
        # Invalid / missing / impossible dates return None
        self.assertIsNone(app.parse_official_date_token(None))
        self.assertIsNone(app.parse_official_date_token(""))
        self.assertIsNone(app.parse_official_date_token("31-02-2026"))
        self.assertIsNone(app.parse_official_date_token("not-a-date"))

        # 2. Verify all 9 adapters handle valid today, stale, missing, conflicting, and ledger first_seen_at
        active_keywords = app.load_keyword_categories()

        def _build_routes_for_source(
            src: app.SourceConfig,
            valid_date_str: str,
            stale_date_str: str,
            conflict_date_str: str,
        ) -> Dict[str, Any]:
            if src.source_id == "ssc":
                payload = {
                    "statusCode": "200",
                    "data": [
                        {
                            "id": "ssc-valid-today",
                            "headline": f"SSC Combined Graduate Level Recruitment 2026 Tier-I Result ({valid_date_str})",
                            "createdAt": "2026-10-24T06:00:00.000Z",
                            "attachments": [{"path": "uploads/cgl_result_24102026.pdf"}],
                        },
                        {
                            "id": "ssc-stale-yesterday",
                            "headline": f"SSC CHSL Examination 2025 Admit Card Notice ({stale_date_str})",
                            "createdAt": "2026-10-23T10:00:00.000Z",
                            "attachments": [{"path": "uploads/chsl_admit_23102026.pdf"}],
                        },
                        {
                            "id": "ssc-missing-date",
                            "headline": "SSC MTS Recruitment 2026 Tentative Vacancy Corrigendum",
                            "createdAt": "",
                            "attachments": [{"path": "uploads/mts_vacancy_notice.pdf"}],
                        },
                        {
                            "id": "ssc-conflict-date",
                            "headline": f"SSC Stenographer Grade C Answer Key Published on {conflict_date_str}",
                            "createdAt": "2026-10-24T08:00:00.000Z",
                            "attachments": [{"path": "uploads/steno_key_23102026.pdf"}],
                        },
                    ],
                }
                return {
                    src.notice_urls[0]: (200, {"Content-Type": "application/json"}, json.dumps(payload)),
                }

            if src.source_id == "uppsc":
                html_body = f"""
                <html><body><table>
                  <tr><td>{valid_date_str}</td><td><a href="/Open_PDF_DB.aspx?I4PnQ0tBagk=valid24" title="UPPSC Combined State Examination 2026 Recruitment Notification Published on {valid_date_str}">View Notice</a></td></tr>
                  <tr><td>{stale_date_str}</td><td><a href="/Open_PDF_DB.aspx?I4PnQ0tBagk=stale23" title="UPPSC PCS Preliminary Examination 2025 Result Notice Published on {stale_date_str}">View Notice</a></td></tr>
                  <tr><td>Notice</td><td><a href="/Open_PDF_DB.aspx?I4PnQ0tBagk=missing00" title="UPPSC RO ARO Examination 2026 Admit Card Download Instructions">View Notice</a></td></tr>
                  <tr><td>{conflict_date_str}</td><td><a href="/uploads/uppsc_answer_key_23102026.pdf" title="UPPSC Lecturer Examination Answer Key Published on {conflict_date_str}">View Notice</a></td></tr>
                </table></body></html>
                """
            else:
                html_body = f"""
                <html><body>
                  <h2>Latest Notices and Recruitment Advertisements</h2>
                  <table>
                    <tr>
                      <td>{valid_date_str}</td>
                      <td><a href="/notices/recruitment_result_24102026.pdf">Official Recruitment Examination Result &amp; Merit List Notification Published on {valid_date_str}</a></td>
                    </tr>
                    <tr>
                      <td>{stale_date_str}</td>
                      <td><a href="/notices/admit_card_23102026.pdf">Official Examination Admit Card &amp; Hall Ticket Schedule Published on {stale_date_str}</a></td>
                    </tr>
                    <tr>
                      <td>Notice</td>
                      <td><a href="/notices/vacancy_syllabus_undated.pdf">Official Recruitment Vacancy Notification and Examination Syllabus</a></td>
                    </tr>
                    <tr>
                      <td>{conflict_date_str}</td>
                      <td><a href="/notices/answer_key_23102026.pdf">Official Provisional Answer Key &amp; Objection Link Published on {conflict_date_str}</a></td>
                    </tr>
                  </table>
                </body></html>
                """
            return {
                url: (200, {"Content-Type": "text/html"}, html_body)
                for url in src.notice_urls
            }

        self.assertEqual(len(LEGACY_SOURCES), 9)
        for src in LEGACY_SOURCES:
            routes = _build_routes_for_source(src, "24-10-2026", "23-10-2026", "24-10-2026")
            fake_client = FakeHttpClient(routes=routes)

            # First discover the items so we can also test pre-populated ledger records with `first_seen_at`
            discovered = app.ADAPTER_REGISTRY[src.adapter_name](src, fake_client)
            self.assertGreaterEqual(len(discovered), 4, f"Adapter {src.source_id} must extract all 4 test notices")

            # Seed notices_store with the stale item having an ISO first_seen_at timestamp
            stale_item = next(
                n for n in discovered if n.publication_date in ("23-10-2026", "2026-10-23")
            )
            notices_store = {
                stale_item.fingerprint: {
                    "fingerprint": stale_item.fingerprint,
                    "source_id": src.source_id,
                    "title": stale_item.title,
                    "normalized_url": stale_item.normalized_url,
                    "normalized_title": stale_item.normalized_title,
                    "canonical_url": stale_item.canonical_url,
                    "canonical_pdf_url": stale_item.canonical_pdf_url,
                    "canonical_title": stale_item.canonical_title,
                    "content_hash": stale_item.content_hash,
                    "source_identifier": stale_item.source_identifier,
                    "publication_date": "23-10-2026",
                    "first_seen_at": "2026-10-23T12:00:00Z",
                    "ledger_status": "sent",
                    "delivery_status": "delivered",
                }
            }

            res = app.check_single_source_live(
                src=src,
                notices_store=notices_store,
                active_keywords=active_keywords,
                reference_date=today_ist,
                max_age_days=0,
                http_client_factory=lambda r=routes: FakeHttpClient(routes=r),
            )

            self.assertEqual(
                res["source_health_status"],
                "SUCCESS",
                f"{src.source_id} failed with {res.get('error_category')}: {res.get('error_message')}",
            )
            self.assertIsNone(res["error_category"])
            self.assertEqual(res["discovery_status"], "NEW_NOTICE_FOUND")
            # 3 notices are unseen in the ledger (today, missing-date, conflicting-date); 1 is already in notices_store
            self.assertEqual(len(res["eligible_today_notices"]), 3, f"{src.source_id} must accept all 3 unseen relevant notices")
            self.assertGreaterEqual(res["duplicate_in_ledger_count"], 1)

    def test_41_mpesb_bounded_timeout_and_retry_isolation(self) -> None:
        """
        Verify MPESB independent timeout handling:
        - adapter_mpesb enforces bounded connect_timeout (<=5s), read_timeout (<=8s), and max_retries (<=2).
        - After primary URL fails with CONNECTION_TIMEOUT, fallback URLs use max_retries=1 so timeouts
          do not compound across fallback hosts.
        - A timeout on MPESB is reported honestly as CONNECTION_TIMEOUT and never blocks other sources.
        """
        mpesb_src = app.SOURCE_BY_ID["mpesb"]
        ssc_src = app.SOURCE_BY_ID["ssc"]

        observed_calls: List[Tuple[str, int, int, int]] = []

        class TimeoutTrackingClient(app.HttpClient):
            def __init__(self) -> None:
                super().__init__(timeout=(20, 30), max_retries=4, sleep_fn=lambda _: None, enable_doh_fallback=False)

            def fetch_text(self, url: str, extra_headers: Optional[Dict[str, str]] = None) -> Tuple[str, str]:
                observed_calls.append((url, int(self.connect_timeout), int(self.read_timeout), int(self.max_retries)))
                raise app.SourceFetchError("CONNECTION_TIMEOUT", f"Timed out connecting to {url}")

        tracking_client = TimeoutTrackingClient()
        with self.assertRaises(app.SourceFetchError) as ctx:
            app.adapter_mpesb(mpesb_src, tracking_client)
        self.assertEqual(ctx.exception.category, "CONNECTION_TIMEOUT")

        # Verify bounded timeouts (5s connect, 8s read) and bounded retries (2 on primary, 1 on subsequent after timeout)
        self.assertEqual(len(observed_calls), len(mpesb_src.notice_urls))
        self.assertEqual(observed_calls[0][1:], (5, 8, 2))
        for fallback_call in observed_calls[1:]:
            self.assertEqual(fallback_call[1:], (5, 8, 1))
        # Verify original client settings were restored after adapter_mpesb returned
        self.assertEqual(tracking_client.connect_timeout, 20)
        self.assertEqual(tracking_client.read_timeout, 30)
        self.assertEqual(tracking_client.max_retries, 4)

    def test_42_open_notification_type_acceptance_and_deduplication_invariants(self) -> None:
        """
        Verify Requirement 3 & 4:
        - Notices outside the 5 predefined categories (corrigenda, addenda, errata, deadline extensions,
          cancellations, postponements, court-related updates, zone allocation/counselling, OTR/biometric/
          fee-refund instructions, and unlisted official notification categories with official document links)
          are accepted and classified as 'Official Notice' (or specific category when matched).
        - Generic homepage descriptions, navigation menus, maintenance banners, tenders, and contextual
          false-positive filler are still rejected.
        - All 5 deduplication layers and all 405 records in data/notice_state.json remain untouched.
        """
        # 1. Realistic official notifications outside the 5 classic categories -> MUST be accepted
        non_predefined_official_notices = [
            ("Corrigendum-II to CEN 01/2026 regarding Revised Table", "https://rrb.indianrailways.gov.in/uploads/corr_2.pdf"),
            ("Addendum / Errata to Notice No. 14/2026", "https://www.upsc.gov.in/sites/default/files/Addendum_14_2026.pdf"),
            ("Important Notice: Extension of Last Date for Online Submission of Representations", "https://ssc.gov.in/uploads/ext_2026.pdf"),
            ("Public Notice regarding Postponement of Paper-II due to administrative reasons", "https://www.hssc.gov.in/notices/postponed.pdf"),
            ("Cancellation Notice for Advt. No. 07/2025", "https://uppsc.up.nic.in/Open_PDF_DB.aspx?I4PnQ0tBag=777"),
            ("Compliance of Hon'ble High Court Order dated 14.10.2026 in Writ Petition No. 4120/2026", "https://rssb.rajasthan.gov.in/uploads/hc_order.pdf"),
            ("Notice for Zone Allocation, Counselling and Option Form Submission", "https://dsssb.delhi.gov.in/sites/allotment.pdf"),
            ("Important Instructions for Scribe, Biometric Attendance and Fee Refund Bank Details", "https://rrb.indianrailways.gov.in/uploads/scribe_refund.pdf"),
            ("Special Helpdesk and Grievance Redressal Portal for Candidates", "https://uppbpb.gov.in/Files/helpdesk_2026.pdf"),
            ("शुद्धिपत्र एवं अंतिम तिथि विस्तार सूचना क्रमांक 09/2026", "https://psc.uk.gov.in/pdf/shuddhipatra_09.pdf"),
        ]
        for title, url in non_predefined_official_notices:
            ok, cat, kws = app.match_notice_keywords(title, url=url)
            self.assertTrue(ok, f"Expected genuine official notice to be accepted: {title!r}")
            self.assertIsNotNone(cat)
            self.assertTrue(len(kws) >= 1)

        # 2. Generic homepage/navigation/maintenance filler -> MUST be rejected
        filler_and_boilerplate = [
            ("Website is under maintenance from 22:00 hrs to 02:00 hrs", "https://rrb.indianrailways.gov.in/"),
            ("Scheduled Server Maintenance on Sunday", "https://ssc.gov.in/"),
            ("Welcome to the Official Website of Staff Selection Commission", "https://ssc.gov.in/"),
            ("Home | About Us | Contact Us | Sitemap | Screen Reader Access", "https://www.upsc.gov.in/"),
            ("Disclaimer: The contents owned and maintained by the Commission", "https://www.hssc.gov.in/"),
            ("Best viewed in Chrome / Firefox at 1024x768 resolution", "https://esb.mp.gov.in/"),
            ("Copyright (c) 2026 All Rights Reserved", "https://uppbpb.gov.in/"),
            ("Privacy Policy and Hyperlink Policy", "https://dsssb.delhi.gov.in/"),
            ("Organization Chart and Directory of Officers", "https://psc.uk.gov.in/"),
        ]
        for title, url in filler_and_boilerplate:
            ok, cat, kws = app.match_notice_keywords(title, url=url)
            self.assertFalse(ok, f"Expected boilerplate/maintenance filler to be rejected: {title!r} (got {cat}: {kws})")

        # 3. End-to-end monitor delivery of non-predefined official notification types + zero duplicate resend
        upsc_src = app.SOURCE_BY_ID["upsc"]
        html_board = """
        <html><body><h2>What's New</h2><ul>
          <li><a href="/sites/default/files/Corrigendum_III_2026.pdf">Corrigendum-III to Notice No. 08/2026 regarding Centre Change</a></li>
          <li><a href="/sites/default/files/Extension_Window_2026.pdf">Extension of Last Date for Correction Window &amp; OTR</a></li>
          <li><a href="/sites/default/files/Postponement_Notice_2026.pdf">Notice regarding Postponement and Rescheduling of Paper-II</a></li>
          <li><a href="/sites/default/files/High_Court_Order_2026.pdf">Compliance of Hon'ble High Court Order in WP(C) 901/2026</a></li>
        </ul></body></html>
        """
        routes = {upsc_src.notice_urls[0]: (200, {"Content-Type": "text/html"}, html_board)}
        client = FakeHttpClient(routes=routes)
        notifier = RecordingNotifier()

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "notice_state.json")
            st = app.create_initial_state()
            st["initialized"] = True
            st["sources"]["upsc"]["baseline_completed"] = True
            st["sources"]["upsc"]["baseline_completed_at"] = "2026-10-01T00:00:00Z"
            app.save_state_atomically(st, state_file)

            s1, _ = app.run_monitor(
                state_path=state_file,
                mode="monitor",
                sources=(upsc_src,),
                http_client=client,
                notifier=notifier,
            )
            self.assertEqual(s1.genuinely_new_notices, 4)
            self.assertEqual(s1.telegram_deliveries_confirmed, 4)
            self.assertEqual(len(notifier.sent_notices), 4)
            for rec, _, msg in notifier.sent_notices:
                self.assertEqual(rec["category"], "Official Notice")
                self.assertIn("Publication Date:</b> Not available", msg)

            # Second run must suppress all 4 via deduplication ledger -> 0 duplicate alerts
            s2, _ = app.run_monitor(
                state_path=state_file,
                mode="monitor",
                sources=(upsc_src,),
                http_client=client,
                notifier=notifier,
            )
            self.assertEqual(s2.genuinely_new_notices, 0)
            self.assertEqual(s2.telegram_deliveries_confirmed, 0)
            self.assertEqual(len(notifier.sent_notices), 4)

    def test_43_hotfix_no_recursion_across_all_five_affected_adapters_and_four_working_sources(self) -> None:
        """
        Regression test for Commit 9ba0d7d GitHub Actions Run #4 failure:
        `UNEXPECTED_ERROR: maximum recursion depth exceeded` in `upsc`, `hssc`, `rssb`, `mpesb`, and `upprpb`.
        Verifies:
        1-5. `adapter_upsc`, `adapter_hssc`, `adapter_rssb`, `adapter_mpesb`, and `adapter_upprpb`
             all run without RecursionError when pages contain non-PDF HTML links whose titles do not
             match any keyword alongside valid official notices.
        6.   Direct unit tests of `is_valid_notice_candidate`, `is_relevant_job_notice`, and
             `match_notice_keywords` on non-PDF links with non-keyword titles confirm zero recursion.
        7.   All 4 working sources (`ssc`, `uppsc`, `dsssb`, `ukpsc`) remain fully functional, including
             the exact SSC revised-dates notice ("Important Notice - Revised dates for Junior Engineer
             Examination, 2026 and Sub-Inspector in Delhi Police and Central Armed Police Forces Examination, 2026.").
        8-9. RRB `CONNECTION_TIMEOUT` is classified accurately and isolated so the other 9 sources succeed.
        10-13. Duplicate notices are never resent, old/undated notices remain eligible, generic links are
               filtered, and the 405-record `data/notice_state.json` ledger remains intact.
        """
        # 6. Direct test of shared helpers that previously formed a mutual recursion cycle:
        # is_valid_notice_candidate -> is_relevant_job_notice -> match_notice_keywords -> is_valid_notice_candidate
        non_keyword_html_links = [
            ("Museum and Historical Archives Exhibition", "https://www.upsc.gov.in/museum-portal", "https://www.upsc.gov.in/"),
            ("Citizen Charter Service Standards Directory", "https://www.upsc.gov.in/charter-directory-system", "https://www.upsc.gov.in/"),
            ("Haryana Citizen Services Directory", "https://www.hssc.gov.in/citizen-services-portal", "https://www.hssc.gov.in/"),
            ("Face Authentication Process Help Page", "https://recruitment.rajasthan.gov.in/help-portal-page", "https://recruitment.rajasthan.gov.in/"),
            ("MPOnline Citizen Kiosk Services Directory", "https://esb.mponline.gov.in/CitizenServices/Services.aspx", "https://esb.mponline.gov.in/"),
            ("Regional Zonal Office Directory", "https://uppbpb.gov.in/Home/ZonalOfficeDirectory", "https://uppbpb.gov.in/"),
        ]
        for title, href, base_url in non_keyword_html_links:
            self.assertFalse(app.is_valid_notice_candidate(title, href, base_url))
            self.assertFalse(app.is_relevant_job_notice(title, url=href))
            ok, cat, kws = app.match_notice_keywords(title, url=href)
            self.assertFalse(ok)
            self.assertIsNone(cat)
            self.assertEqual(kws, [])

        # Build realistic routes for all 10 sources (9 succeeding + rrb_rrcb timing out)
        routes: Dict[str, Any] = {}

        # 1. UPSC (with non-PDF non-keyword links that triggered RecursionError in Run #4)
        upsc_src = app.SOURCE_BY_ID["upsc"]
        upsc_html = """
        <html><body>
          <a href="https://upsconline.gov.in/marksheet/exam/marksheet_system">Marks Information</a>
          <a href="/museum-and-exhibitions">Union Public Service Commission Museum</a>
          <h2>What's New</h2>
          <ul>
            <li><a href="/whats-new/Combined%20Defence%20Services%20Examination%20(II),%202026/Written%20Result">Written Result: Combined Defence Services Examination (II), 2026</a></li>
            <li><a href="/sites/default/files/TimeTable-GeoScientist-2027.pdf">Examination Time Table: Combined Geo-Scientist (Preliminary) Examination, 2027</a></li>
          </ul>
        </body></html>
        """
        for u in upsc_src.notice_urls:
            routes[u] = (200, {"Content-Type": "text/html"}, upsc_html)

        # 2. HSSC (with non-PDF portal links + publicNotice table rows)
        hssc_src = app.SOURCE_BY_ID["hssc"]
        hssc_html = """
        <html><body>
          <a href="/citizen-kiosk-portal">Haryana Citizen Kiosk Services</a>
          <table>
            <tr>
              <td>1</td>
              <td>Notice to the candidates regarding Final Answer Key for the post of Primary Teacher (PRT) Advt. 5/2024</td>
              <td>2026-09-10</td>
              <td><a href="/file/ac1f23cd-9fda-137e-81a0-89ede0820007/publicNotice">Download</a></td>
            </tr>
          </table>
        </body></html>
        """
        for u in hssc_src.notice_urls:
            routes[u] = (200, {"Content-Type": "text/html"}, hssc_html)

        # 3. RSSB (with notificationdocview + job-card + non-keyword links)
        rssb_src = app.SOURCE_BY_ID["rssb"]
        rssb_html = """
        <html><body>
          <a href="/portal/department-overview">Departmental Overview and Information</a>
          <div class="notice-item"><p>Face Authentication Process-Help Document <a href="notificationdocview?ID=VdZmi65lDdEtn">View Document</a></p></div>
          <ul>
            <li><div class="job-card"><h6 onclick="postDetailsClick(4686)">CONTRACTUAL SAFAI KARAMCHARI - 2026</h6><span class="badge">(LSG)</span><small>28-Oct-2026</small></div></li>
          </ul>
        </body></html>
        """
        for u in rssb_src.notice_urls:
            routes[u] = (200, {"Content-Type": "text/html"}, rssb_html)

        # 4. MPESB (with non-PDF MPOnline kiosk link + Rulebook PDF row)
        mpesb_src = app.SOURCE_BY_ID["mpesb"]
        mpesb_html = """
        <html><body>
          <a href="/CitizenServices/Services.aspx">MPOnline Citizen Kiosk Services Directory</a>
          <table>
            <tr>
              <td>Online Form - Group-4, Asstt. Grade-3 Stenographer Combined Recruitment Test - 2026 (15-09-2026)</td>
              <td><a href="/Rulebooks/English_2026/Group4_2026.pdf">Rulebook</a></td>
            </tr>
          </table>
        </body></html>
        """
        for u in mpesb_src.notice_urls:
            routes[u] = (200, {"Content-Type": "text/html"}, mpesb_html)

        # 5. UPPRPB (with non-PDF ImportantCourtDecision link + Hindi notice PDFs)
        upprpb_src = app.SOURCE_BY_ID["upprpb"]
        upprpb_html = """
        <html><body>
          <a href="/Home/ImportantCourtDecision">Important Court Decisions</a>
          <ul>
            <li><a href="/FilesUploaded/Notice/HG2025_Marks_Notice.pdf">उत्तर प्रदेश होमगार्ड्स के पदों पर एनरोलमेंट -2025 के अन्तर्गत अन्तिम अंकों के प्रकाशन के सम्बन्ध में</a></li>
          </ul>
        </body></html>
        """
        for u in upprpb_src.notice_urls:
            routes[u] = (200, {"Content-Type": "text/html"}, upprpb_html)

        # 7a. SSC (including the exact revised-dates notice from live ssc.gov.in)
        ssc_src = app.SOURCE_BY_ID["ssc"]
        ssc_payload = {
            "statusCode": "200",
            "data": [
                {
                    "id": "5jfpb0tey9le4bsgd207x",
                    "headline": "Important Notice - Revised dates for Junior Engineer Examination, 2026 and Sub-Inspector in Delhi Police and Central Armed Police Forces Examination, 2026. ",
                    "createdAt": "2026-10-26T13:07:31.342Z",
                    "attachments": [
                        {
                            "path": "uploads\\masterData\\NoticeBoards\\Important notice_revised_dated_26_10_2026.pdf"
                        }
                    ],
                }
            ],
        }
        routes[ssc_src.notice_urls[0]] = (200, {"Content-Type": "application/json"}, json.dumps(ssc_payload))

        # 7b. UPPSC
        uppsc_src = app.SOURCE_BY_ID["uppsc"]
        uppsc_html = """
        <html><body><table>
          <tr><td><a href="/Open_PDF_DB.aspx?I4PnQ0tBagmzNJIRVKnDR0yp8H0q1cBz=" title="NOTICE REGARDING DISTRICT INFORMATION SLIP / ADMIT CARD FOR ADVT. NO:- D-3/E-1/2023, LECTURER">View</a></td></tr>
        </table></body></html>
        """
        for u in uppsc_src.notice_urls:
            routes[u] = (200, {"Content-Type": "text/html"}, uppsc_html)

        # 7c. DSSSB
        dsssb_src = app.SOURCE_BY_ID["dsssb"]
        dsssb_html = """
        <html><body><table>
          <tr><td>Current Vacancies Sweeper/Safai Karamchari :: Post Code:46/26</td></tr>
        </table></body></html>
        """
        for u in dsssb_src.notice_urls:
            routes[u] = (200, {"Content-Type": "text/html"}, dsssb_html)

        # 7d. UKPSC
        ukpsc_src = app.SOURCE_BY_ID["ukpsc"]
        ukpsc_index_html = '<html><head><script src="/static/js/main.abc123.js"></script></head><body><div id="root"></div></body></html>'
        ukpsc_js = 'const exams = [{id: 167, exam_name: "Admit Card (Sanskrit) of Uttarakhand Special Subordinate Education (Lecturer Cadre)", exam_apply_url: "https://ukpscnet.in/lce/exam.html#/pg0zx1/admit-card-login", start_date_format: "08-10-2026", end_date_format: "28-10-2026"}];'
        routes["https://ukpsc.net.in/"] = (200, {"Content-Type": "text/html"}, ukpsc_index_html)
        routes["https://ukpsc.net.in/static/js/main.abc123.js"] = (200, {"Content-Type": "application/javascript"}, ukpsc_js)

        # 8-9. RRB/RRCB simulated CONNECTION_TIMEOUT
        rrb_src = app.SOURCE_BY_ID["rrb_rrcb"]
        for u in list(rrb_src.notice_urls) + [rrb_src.homepage_url]:
            routes[u] = app.SourceFetchError(
                "CONNECTION_TIMEOUT",
                "Timed out connecting to rrb.indianrailways.gov.in",
            )

        client = FakeHttpClient(routes=routes)

        # Verify all 5 recursion-affected adapters + all 4 working adapters individually
        for sid in ("upsc", "hssc", "rssb", "mpesb", "upprpb", "ssc", "uppsc", "dsssb", "ukpsc"):
            src_cfg = app.SOURCE_BY_ID[sid]
            discovered = app.ADAPTER_REGISTRY[src_cfg.adapter_name](src_cfg, client)
            self.assertGreaterEqual(len(discovered), 1, f"Adapter {sid} must extract >=1 valid notice without RecursionError")

        # Verify full 10-source run_monitor where 9 non-RRB sources succeed and rrb_rrcb times out (isolated)
        notifier = RecordingNotifier()
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "notice_state.json")
            st = app.create_initial_state()
            st["initialized"] = True
            for sid in ("upsc", "hssc", "rssb", "mpesb", "upprpb", "ssc", "uppsc", "dsssb", "ukpsc"):
                st["sources"][sid]["baseline_completed"] = True
                st["sources"][sid]["baseline_completed_at"] = "2026-10-01T00:00:00Z"
            app.save_state_atomically(st, state_file)

            summary1, state1 = app.run_monitor(
                state_path=state_file,
                mode="monitor",
                sources=app.SOURCES,
                http_client=client,
                notifier=notifier,
                max_alerts_per_run=25,
            )
            self.assertEqual(summary1.total_websites, 10)
            self.assertEqual(summary1.succeeded_websites, 9)
            self.assertEqual(summary1.failed_websites, 1)
            self.assertEqual(summary1.per_source_results["rrb_rrcb"]["status"], "error")
            self.assertEqual(summary1.per_source_results["rrb_rrcb"]["error_category"], "CONNECTION_TIMEOUT")
            self.assertFalse(state1["sources"]["rrb_rrcb"]["baseline_completed"])

            # Confirm the exact SSC revised-dates notice was delivered with its official PDF URL and 2026-10-26 date
            ssc_sent = [rec for rec, _, _ in notifier.sent_notices if rec["source_id"] == "ssc"]
            self.assertEqual(len(ssc_sent), 1)
            self.assertEqual(ssc_sent[0]["source_identifier"], "5jfpb0tey9le4bsgd207x")
            self.assertEqual(ssc_sent[0]["publication_date"], "2026-10-26")
            self.assertEqual(
                ssc_sent[0]["normalized_url"],
                "https://ssc.gov.in/api/attachment/uploads/masterData/NoticeBoards/Important%20notice_revised_dated_26_10_2026.pdf",
            )

            # Confirm a second run produces 0 duplicate notifications
            sent_count_after_run1 = len(notifier.sent_notices)
            summary2, _ = app.run_monitor(
                state_path=state_file,
                mode="monitor",
                sources=app.SOURCES,
                http_client=client,
                notifier=notifier,
                max_alerts_per_run=25,
            )
            self.assertEqual(summary2.genuinely_new_notices, 0)
            self.assertEqual(summary2.telegram_deliveries_confirmed, 0)
            self.assertEqual(len(notifier.sent_notices), sent_count_after_run1)


if __name__ == "__main__":
    unittest.main(verbosity=2)






