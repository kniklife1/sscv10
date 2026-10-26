#!/usr/bin/env python3
"""
RRB / RRCB integration tests.

IMPORTANT - what these tests are and are not:
  * Every response body below is a SYNTHETIC fixture shaped after the possible formats of the endpoint
    (JSON list, wrapped JSON, HTML table, JSON carrying an HTML fragment). No real capture of
    https://rrb.indianrailways.gov.in/getdata?loc= was available when these were written.
  * Passing them proves the adapter, date policy, state and delivery logic behave as specified. It does NOT
    prove the live endpoint is reachable from GitHub Actions or that its real format is parsed correctly.
    That needs the read-only probe workflow (.github/workflows/main.yml, mode=rrcb_probe) run on a real runner.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from typing import Any, Dict, List, Optional
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import main as app  # noqa: E402
from test_main import FakeHttpClient, RecordingNotifier  # noqa: E402

ENDPOINT = "https://rrb.indianrailways.gov.in/getdata?loc="
TODAY = app.datetime.date(2026, 10, 25)
RRB = app.SOURCE_BY_ID["rrb_rrcb"]
LEGACY_IDS = ["ssc", "upsc", "uppsc", "hssc", "rssb", "mpesb", "dsssb", "upprpb", "ukpsc"]
REPO_STATE = os.path.join(_HERE, "data", "notice_state.json")


def json_feed(extra: Optional[List[Dict[str, Any]]] = None) -> str:
    rows: List[Dict[str, Any]] = [
        {"id": 9101, "title": "CEN 04/2026 (NTPC): E-Call Letter for CBT-1 Examination", "link": "/uploads/cen04_2026_ecall.pdf", "date": "2026-10-23"},
        {"id": 9102, "title": "CEN 04/2026 (NTPC): Provisional Answer Key and Response Sheet", "link": "https://rrb.indianrailways.gov.in/uploads/cen04_2026_key.pdf", "date": "12-03-2024"},
        {"id": 9103, "title": "Tender for supply of stationery to the RRCB office", "link": "/uploads/tender_stationery.pdf", "date": "2026-10-24"},
        {"id": 9104, "title": "CEN 01/2025 Result of CBT-2 for Assistant Loco Pilot", "link": "https://rrbapply.gov.in/result", "date": ""},
    ]
    rows.extend(extra or [])
    return json.dumps(rows)


def make_state(path: str, baselined_ids: Optional[List[str]] = None) -> None:
    st = app.create_initial_state()
    st["initialized"] = True
    app.save_state_atomically(st, path)


def run(state_path: str, body: Any, notifier: Optional[Any] = None, max_alerts: int = 10,
        sources: Any = None, extra_routes: Optional[Dict[str, Any]] = None) -> Any:
    routes: Dict[str, Any] = {ENDPOINT: body if not isinstance(body, str) else (200, {"Content-Type": "application/json"}, body)}
    routes.update(extra_routes or {})
    client = FakeHttpClient(routes=routes, max_retries=1)
    summary, state = app.run_monitor(
        state_path=state_path, mode="monitor", sources=sources or (RRB,), http_client=client,
        notifier=notifier, reference_date=TODAY, max_age_days=0, max_alerts_per_run=max_alerts,
    )
    return summary, state, client


class RegistryTests(unittest.TestCase):
    def test_exactly_ten_sources_with_one_rrb_and_nine_untouched_originals(self) -> None:
        ids = [s.source_id for s in app.SOURCES]
        self.assertEqual(len(app.SOURCES), 10)
        self.assertEqual(ids[:9], LEGACY_IDS)
        self.assertEqual(ids.count("rrb_rrcb"), 1)
        self.assertEqual(len(app.ADAPTER_REGISTRY), 10)
        for src in app.SOURCES:
            self.assertIn(src.adapter_name, app.ADAPTER_REGISTRY)
            self.assertEqual(src.date_policy, "informational", src.source_id)

    def test_base_endpoint_is_configured_and_rrcb_value_is_gone(self) -> None:
        self.assertEqual(RRB.notice_urls, (ENDPOINT,))
        self.assertEqual(RRB.homepage_url, "https://rrb.indianrailways.gov.in/")
        self.assertEqual(app.RRB_RRCB_ENDPOINT, ENDPOINT)
        self.assertTrue(ENDPOINT.endswith("getdata?loc="))
        for url in RRB.notice_urls + (app.RRB_RRCB_ENDPOINT,):
            self.assertNotIn("RRCB", url.upper().replace("RRB_RRCB", ""))


class ParserTests(unittest.TestCase):
    def test_correct_endpoint_is_requested_exactly(self) -> None:
        client = FakeHttpClient(routes={ENDPOINT: (200, {"Content-Type": "application/json"}, json_feed())}, max_retries=1)
        notices = app.adapter_rrb_rrcb(RRB, client)
        self.assertEqual(list(client.call_counts), [ENDPOINT])
        self.assertEqual(len(notices), 4)

    def test_json_list_extracts_title_url_date_identifier(self) -> None:
        notices = app.parse_rrcb_response(RRB, json_feed())
        by_title = {n.title: n for n in notices}
        ecall = by_title["CEN 04/2026 (NTPC): E-Call Letter for CBT-1 Examination"]
        self.assertEqual(ecall.url, "https://rrb.indianrailways.gov.in/uploads/cen04_2026_ecall.pdf")
        self.assertEqual(ecall.publication_date, "2026-10-23")
        self.assertEqual(ecall.source_identifier, "rrcb-id:9101")
        key = by_title["CEN 04/2026 (NTPC): Provisional Answer Key and Response Sheet"]
        self.assertEqual(key.publication_date, "2024-03-12")  # old date is parsed, not dropped or "fixed"
        undated = by_title["CEN 01/2025 Result of CBT-2 for Assistant Loco Pilot"]
        self.assertIsNone(undated.publication_date)  # missing date is never invented
        self.assertEqual(undated.url, "https://rrbapply.gov.in/result")  # legitimate apply-portal link preserved

    def test_wrapped_json_and_json_with_html_fragment(self) -> None:
        wrapped = json.dumps({"status": "ok", "data": {"notices": json.loads(json_feed())}})
        self.assertEqual(len(app.parse_rrcb_response(RRB, wrapped)), 4)
        fragment = json.dumps({"html": '<table><tr><td>1</td><td>CEN 06/2026 Admit Card for Group D CBT</td><td>20-10-2026</td>'
                                        '<td><a href="/files/cen06_admit.pdf">Download</a></td></tr></table>'})
        got = app.parse_rrcb_response(RRB, fragment)
        self.assertEqual(len(got), 1)
        self.assertIn("CEN 06/2026 Admit Card", got[0].title)
        self.assertEqual(got[0].url, "https://rrb.indianrailways.gov.in/files/cen06_admit.pdf")

    def test_html_table_extracts_rows_and_validates_links(self) -> None:
        html_body = """
        <html><body><table>
          <tr><td>1</td><td>CEN 05/2026 Admit Card Download for Group D</td><td>20-10-2026</td><td><a href="/files/cen05_admit.pdf">View</a></td></tr>
          <tr><td>2</td><td>CEN 05/2026 E-Call Letter via TCS iON</td><td>19-10-2026</td><td><a href="https://rrb.digialm.com/EForms/callletter">Open</a></td></tr>
          <tr><td>3</td><td>Result published by a retired regional site</td><td>18-10-2026</td><td><a href="https://rrbahmedabad.gov.in/old/result.pdf">Open</a></td></tr>
          <tr><td>4</td><td>Unrelated external site notice for recruitment</td><td>18-10-2026</td><td><a href="https://evil.example.com/recruitment.pdf">Open</a></td></tr>
          <tr><td>5</td><td>Script link notice recruitment 2026</td><td>18-10-2026</td><td><a href="javascript:void(0)">Open</a></td></tr>
        </table></body></html>"""
        got = app.parse_rrcb_response(RRB, html_body)
        urls = sorted(n.url for n in got)
        self.assertEqual(urls, ["https://rrb.digialm.com/EForms/callletter", "https://rrb.indianrailways.gov.in/files/cen05_admit.pdf"])
        admit = next(n for n in got if n.url.endswith("cen05_admit.pdf"))
        self.assertEqual(admit.publication_date, "2026-10-20")

    def test_malformed_dates_become_none_or_preserved_safely(self) -> None:
        feed = json.dumps([
            {"id": 1, "title": "CEN 07/2026 Recruitment Notification for Technicians", "link": "/a.pdf", "date": "31-02-2026"},
            {"id": 2, "title": "CEN 07/2026 Corrigendum on Recruitment vacancy table", "link": "/b.pdf", "date": "not a date"},
            {"id": 3, "title": "CEN 07/2026 Tentative Exam Schedule for Technicians", "link": "/c.pdf", "date": "Oct 2026"},
        ])
        got = app.parse_rrcb_response(RRB, feed)
        self.assertEqual([n.publication_date for n in got], [None, None, "Oct 2026"])

    def test_challenge_empty_shell_and_unknown_shapes_fail_loudly_while_empty_json_is_valid(self) -> None:
        # Genuine empty JSON lists/collections return [] (valid empty result, not a crash)
        for valid_empty in ("[]", '{"data": []}', '{"records": []}', '{"notices": [], "count": 0}'):
            self.assertEqual(app.parse_rrcb_response(RRB, valid_empty), [], valid_empty)

        cases = [
            ("<html><title>Just a moment...</title><body>Checking your browser</body></html>", "ACCESS_DENIED_PAGE"),
            ("<html><body><h1>Access Denied</h1></body></html>", "ACCESS_DENIED_PAGE"),
            ("<html><body><p>Welcome</p></body></html>", "UNRECOGNIZED_STRUCTURE"),
            ('{"data": null}', "UNRECOGNIZED_STRUCTURE"),
            ("{ not json", "INVALID_JSON"),
            ("   ", "EMPTY_RESPONSE"),
        ]
        for body, category in cases:
            with self.assertRaises(app.SourceFetchError, msg=body) as ctx:
                app.parse_rrcb_response(RRB, body)
            self.assertEqual(ctx.exception.category, category, body)

    def test_redirect_to_unapproved_host_is_rejected(self) -> None:
        client = FakeHttpClient(routes={ENDPOINT: lambda url, n: (200, {}, json_feed().encode(), "https://evil.example.com/getdata")}, max_retries=1)
        with self.assertRaises(app.SourceFetchError) as ctx:
            app.adapter_rrb_rrcb(RRB, client)
        self.assertEqual(ctx.exception.category, "UNAPPROVED_REDIRECT_DOMAIN")

    def test_same_cen_different_documents_and_same_title_different_urls_stay_distinct(self) -> None:
        feed = json.dumps([
            {"title": "CEN 08/2026 Admit Card and E-Call Letter notice", "link": "/f/one.pdf"},
            {"title": "CEN 08/2026 Admit Card and E-Call Letter notice", "link": "/f/two.pdf"},
            {"title": "CEN 08/2026 Result of CBT-1 shortlisted candidates", "link": "/f/three.pdf"},
            {"title": "CEN 08/2026 Result of CBT-1 shortlisted candidates", "link": "/f/three.pdf"},
        ])
        got = app.parse_rrcb_response(RRB, feed)
        self.assertEqual(len(got), 3)
        self.assertEqual(len({n.source_identifier for n in got}), 3)
        self.assertFalse(any("CEN 08/2026" == n.source_identifier for n in got))


class RelayTests(unittest.TestCase):
    RELAY = "https://relay.example.org/fetch?u={url}"

    def _routes(self, direct: Any) -> Dict[str, Any]:
        relay_url = self.RELAY.replace("{url}", "https%3A%2F%2Frrb.indianrailways.gov.in%2Fgetdata%3Floc%3D")
        return {ENDPOINT: direct, relay_url: (200, {"Content-Type": "application/json"}, json_feed())}

    def test_relay_used_only_when_configured_and_direct_failed_with_network_error(self) -> None:
        timeout = app.SourceFetchError("CONNECTION_TIMEOUT", "timed out")
        with mock.patch.dict(os.environ, {"RRCB_RELAY_URL_TEMPLATE": self.RELAY}):
            got = app.adapter_rrb_rrcb(RRB, FakeHttpClient(routes=self._routes(timeout), max_retries=1))
        self.assertEqual(len(got), 4)
        with mock.patch.dict(os.environ, {"RRCB_RELAY_URL_TEMPLATE": ""}):
            with self.assertRaises(app.SourceFetchError) as ctx:
                app.adapter_rrb_rrcb(RRB, FakeHttpClient(routes=self._routes(timeout), max_retries=1))
        self.assertEqual(ctx.exception.category, "CONNECTION_TIMEOUT")

    def test_relay_not_used_for_permanent_http_errors_or_insecure_template(self) -> None:
        forbidden = app.SourceFetchError("HTTP_403_FORBIDDEN", "403")
        with mock.patch.dict(os.environ, {"RRCB_RELAY_URL_TEMPLATE": self.RELAY}):
            with self.assertRaises(app.SourceFetchError) as ctx:
                app.adapter_rrb_rrcb(RRB, FakeHttpClient(routes=self._routes(forbidden), max_retries=1))
        self.assertEqual(ctx.exception.category, "HTTP_403_FORBIDDEN")
        for bad in ("http://relay.example.org/?u={url}", "https://relay.example.org/no-placeholder"):
            with mock.patch.dict(os.environ, {"RRCB_RELAY_URL_TEMPLATE": bad}):
                self.assertIsNone(app._rrb_relay_url())

    def test_relay_content_cannot_smuggle_unapproved_links(self) -> None:
        evil = json.dumps([{"title": "CEN 09/2026 Recruitment notification for posts", "link": "https://evil.example.com/x.pdf"},
                           {"title": "CEN 09/2026 Result of CBT-1 shortlisted candidates", "link": "/ok.pdf"}])
        relay_url = self.RELAY.replace("{url}", "https%3A%2F%2Frrb.indianrailways.gov.in%2Fgetdata%3Floc%3D")
        routes = {ENDPOINT: app.SourceFetchError("CONNECTION_RESET", "reset"), relay_url: (200, {}, evil)}
        with mock.patch.dict(os.environ, {"RRCB_RELAY_URL_TEMPLATE": self.RELAY}):
            got = app.adapter_rrb_rrcb(RRB, FakeHttpClient(routes=routes, max_retries=1))
        self.assertEqual([n.url for n in got], ["https://rrb.indianrailways.gov.in/ok.pdf"])


class MonitorBehaviourTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="rrb-test-")
        self.state_path = os.path.join(self.tmp, "notice_state.json")
        make_state(self.state_path)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _state(self) -> Dict[str, Any]:
        return app.load_state(self.state_path, allow_init_if_missing=False)

    def test_first_run_baselines_whole_feed_without_alerts(self) -> None:
        notifier = RecordingNotifier()
        summary, _, _ = run(self.state_path, json_feed(), notifier)
        self.assertEqual(len(notifier.sent_notices), 0)
        self.assertEqual(summary.genuinely_new_notices, 0)
        self.assertEqual(summary.baselined_notices, 4)
        st = self._state()
        self.assertTrue(st["sources"]["rrb_rrcb"]["baseline_completed"])
        self.assertEqual(len([r for r in st["notices"].values() if r["source_id"] == "rrb_rrcb"]), 4)

    def test_old_missing_and_malformed_dates_do_not_suppress_new_keyword_matches(self) -> None:
        run(self.state_path, json_feed())  # baseline
        extra = [
            {"id": 9201, "title": "CEN 10/2026 Recruitment Notification for 500 Posts", "link": "/f/cen10.pdf", "date": "2019-01-05"},
            {"id": 9202, "title": "CEN 10/2026 Admit Card and E-Call Letter for CBT-1", "link": "/f/cen10_call.pdf"},
            {"id": 9203, "title": "CEN 10/2026 Provisional Answer Key and Response Sheet", "link": "/f/cen10_key.pdf", "date": "31-02-2026"},
            {"id": 9204, "title": "CEN 10/2026 Document Verification Schedule for Shortlisted Candidates", "link": "/f/cen10_dv.pdf", "date": "2999-01-01"},
            {"id": 9205, "title": "Tender for repair of the RRCB canteen building", "link": "/f/tender.pdf", "date": "2026-10-25"},
        ]
        notifier = RecordingNotifier()
        summary, _, _ = run(self.state_path, json_feed(extra), notifier)
        titles = sorted(r["title"] for r, _, _ in notifier.sent_notices)
        self.assertEqual(len(titles), 4, titles)
        self.assertFalse(any("Tender" in t for t in titles))  # non-matching notice filtered out
        self.assertEqual(summary.telegram_deliveries_confirmed, 4)
        self.assertEqual(summary.filtered_date_notices, 0)
        messages = {r["title"]: m for r, _, m in notifier.sent_notices}
        old = next(m for t, m in messages.items() if "500 Posts" in t)
        self.assertIn("Publication Date:</b> 2019-01-05", old)  # informational date is still shown
        undated = next(m for t, m in messages.items() if "E-Call Letter" in t)
        self.assertIn("Publication Date:</b> Not available", undated)
        garbage = next(m for t, m in messages.items() if "Answer Key" in t)
        self.assertNotIn("31-02-2026", garbage)
        self.assertIn("Publication Date:</b> Not available", garbage)
        st = self._state()
        tender = next(r for r in st["notices"].values() if "canteen" in r["title"])
        self.assertEqual(tender["delivery_status"], "baselined")
        self.assertTrue(str(tender["eligibility_status"]).startswith("rejected:IRRELEVANT_KEYWORD"))

    def test_duplicates_are_never_resent(self) -> None:
        run(self.state_path, json_feed())
        extra = [{"id": 9301, "title": "CEN 11/2026 Result of CBT-1 shortlisted candidates", "link": "/f/r.pdf", "date": "2020-02-02"}]
        notifier = RecordingNotifier()
        run(self.state_path, json_feed(extra), notifier)
        self.assertEqual(len(notifier.sent_notices), 1)
        for _ in range(2):
            again = RecordingNotifier()
            run(self.state_path, json_feed(extra), again)
            self.assertEqual(len(again.sent_notices), 0)
        # same notice re-listed under a changed title but the same record id is still the same notice
        renamed = [{"id": 9301, "title": "CEN 11/2026 Result of CBT-1 (revised) shortlisted candidates", "link": "/f/r.pdf", "date": "2020-02-02"}]
        again = RecordingNotifier()
        run(self.state_path, json_feed(renamed), again)
        self.assertEqual(len(again.sent_notices), 0)

    def test_failed_fetch_never_overwrites_good_snapshot(self) -> None:
        run(self.state_path, json_feed())
        before = copy.deepcopy(self._state())
        before_rrb = {fp: r for fp, r in before["notices"].items() if r["source_id"] == "rrb_rrcb"}
        for failure in (
            app.SourceFetchError("CONNECTION_TIMEOUT", "timed out"),
            (200, {}, "<html><title>Just a moment...</title></html>"),
            (200, {}, "<html><body><p>Unrecognized shell</p></body></html>"),
            (200, {}, "{ broken"),
        ):
            summary, _, _ = run(self.state_path, failure, RecordingNotifier())
            self.assertEqual(summary.failed_websites, 1)
            self.assertEqual(summary.per_source_results["rrb_rrcb"]["status"], "error")
            after = self._state()
            after_rrb = {fp: r for fp, r in after["notices"].items() if r["source_id"] == "rrb_rrcb"}
            self.assertEqual(set(after_rrb), set(before_rrb))
            self.assertTrue(after["sources"]["rrb_rrcb"]["baseline_completed"])
            self.assertEqual({fp: r["delivery_status"] for fp, r in after_rrb.items()},
                             {fp: r["delivery_status"] for fp, r in before_rrb.items()})

    def test_empty_first_fetch_does_not_complete_baseline_so_history_is_never_announced(self) -> None:
        summary, _, _ = run(self.state_path, (200, {}, "[]"), RecordingNotifier())
        self.assertEqual(summary.per_source_results["rrb_rrcb"]["status"], "ok")
        self.assertEqual(summary.per_source_results["rrb_rrcb"]["discovered"], 0)
        st = self._state()
        self.assertFalse(st["sources"]["rrb_rrcb"]["baseline_completed"])
        notifier = RecordingNotifier()
        run(self.state_path, json_feed(), notifier)  # first non-empty fetch baselines silently
        self.assertEqual(len(notifier.sent_notices), 0)
        self.assertTrue(self._state()["sources"]["rrb_rrcb"]["baseline_completed"])

    def test_failed_telegram_delivery_stays_queued_and_is_retried_despite_old_date(self) -> None:
        run(self.state_path, json_feed())
        title = "CEN 12/2026 Recruitment Notification for Technician Posts"
        extra = [{"id": 9401, "title": title, "link": "/f/t.pdf", "date": "2018-06-06"}]
        failing = RecordingNotifier(fail_notice_titles=[title])
        summary, state, _ = run(self.state_path, json_feed(extra), failing)
        self.assertEqual(summary.telegram_delivery_failures, 1)
        self.assertEqual(len(failing.sent_notices), 0)
        rec = next(r for r in self._state()["notices"].values() if r["title"] == title)
        self.assertEqual(rec["delivery_status"], "failed")  # NOT marked delivered
        self.assertIn(rec["fingerprint"], self._state()["pending_queue"])
        good = RecordingNotifier()
        run(self.state_path, json_feed(extra), good)
        self.assertEqual([r["title"] for r, _, _ in good.sent_notices], [title])
        rec = next(r for r in self._state()["notices"].values() if r["title"] == title)
        self.assertEqual((rec["delivery_status"], rec["ledger_status"]), ("delivered", "sent"))
        self.assertIsNotNone(rec["delivered_at"])

    def test_alert_cap_is_respected_and_overflow_survives_to_later_runs(self) -> None:
        run(self.state_path, json_feed())
        extra = [{"id": 9500 + i, "title": f"CEN 13/2026 Result of CBT-1 Zone {i} shortlisted candidates", "link": f"/f/z{i}.pdf", "date": "2017-01-01"}
                 for i in range(5)]
        first = RecordingNotifier()
        summary, _, _ = run(self.state_path, json_feed(extra), first, max_alerts=2)
        self.assertEqual(len(first.sent_notices), 2)
        self.assertEqual(summary.capped_deferred_notices, 3)
        self.assertEqual(len(self._state()["pending_queue"]), 3)
        second = RecordingNotifier()
        run(self.state_path, json_feed(extra), second, max_alerts=2)
        third = RecordingNotifier()
        run(self.state_path, json_feed(extra), third, max_alerts=2)
        sent = [r["title"] for n in (first, second, third) for r, _, _ in n.sent_notices]
        self.assertEqual(len(sent), 5)
        self.assertEqual(len(set(sent)), 5)  # nothing lost, nothing duplicated
        self.assertEqual(self._state()["pending_queue"], [])

    def test_all_sources_alert_on_new_keyword_matching_notices_regardless_of_publication_date(self) -> None:
        """Both SSC and RRB alert on newly discovered notices even when their publication dates are older than today."""
        ssc = app.SOURCE_BY_ID["ssc"]
        ssc_payload = lambda created: json.dumps({"statusCode": "200", "data": [{
            "id": "ssc-x", "headline": "SSC Combined Graduate Level Examination 2026 Final Answer Key",
            "createdAt": created, "attachments": [{"path": "uploads/cgl_key.pdf"}]}]})
        routes = {ssc.notice_urls[0]: (200, {"Content-Type": "application/json"}, ssc_payload("2026-10-20T05:30:00.000Z"))}
        run(self.state_path, json_feed(), None, sources=(ssc, RRB), extra_routes=routes)  # baseline both
        routes2 = {ssc.notice_urls[0]: (200, {"Content-Type": "application/json"}, json.dumps({"statusCode": "200", "data": [
            {"id": "ssc-x", "headline": "SSC Combined Graduate Level Examination 2026 Final Answer Key", "createdAt": "2026-10-20T05:30:00.000Z", "attachments": [{"path": "uploads/cgl_key.pdf"}]},
            {"id": "ssc-old", "headline": "SSC CHSL Examination 2025 Admit Card Notice", "createdAt": "2026-10-20T05:30:00.000Z", "attachments": [{"path": "uploads/chsl_admit.pdf"}]},
        ]}))}
        extra = [{"id": 9601, "title": "CEN 14/2026 Admit Card and E-Call Letter for CBT-1", "link": "/f/c14.pdf", "date": "2016-01-01"}]
        notifier = RecordingNotifier()
        run(self.state_path, json_feed(extra), notifier, sources=(ssc, RRB), extra_routes=routes2)
        sent_sources = sorted(r["source_id"] for r, _, _ in notifier.sent_notices)
        self.assertEqual(sent_sources, ["rrb_rrcb", "ssc"])

    @unittest.skipUnless(os.path.isfile(REPO_STATE), "data/notice_state.json not present")
    def test_existing_nine_source_baselines_and_ledger_are_not_disturbed(self) -> None:
        shutil.copyfile(REPO_STATE, self.state_path)
        before = app.load_state(self.state_path, allow_init_if_missing=False)
        legacy_before = {fp: copy.deepcopy(r) for fp, r in before["notices"].items()}
        baselines_before = {sid: before["sources"][sid]["baseline_completed"] for sid in LEGACY_IDS}
        notifier = RecordingNotifier()
        summary, _, _ = run(self.state_path, json_feed(), notifier, sources=app.SOURCES)  # only RRB has a route
        self.assertEqual(len(notifier.sent_notices), 0)  # RRB baselined silently; legacy sources failed (no route)
        after = self._state()
        for fp, rec in legacy_before.items():
            self.assertIn(fp, after["notices"])
            for key in ("delivery_status", "ledger_status", "publication_date", "title", "url", "source_id"):
                self.assertEqual(after["notices"][fp].get(key), rec.get(key), (fp, key))
        for sid in LEGACY_IDS:
            self.assertEqual(after["sources"][sid]["baseline_completed"], baselines_before[sid], sid)
        self.assertTrue(after["sources"]["rrb_rrcb"]["baseline_completed"])
        self.assertGreaterEqual(len(after["notices"]), len(legacy_before) + 4)

    def test_open_notification_types_for_rrb_corrigendum_extension_postponement_court_and_filler_exclusion(self) -> None:
        """
        Verify that RRB notices outside the 5 predefined categories (corrigenda, deadline extensions,
        postponements/cancellations, court compliance notices, zone allocation/fee refund instructions)
        are accepted and delivered with category 'Official Notice', while tenders and website maintenance
        banners are filtered out.
        """
        run(self.state_path, json_feed())  # baseline initial feed
        extra = [
            {"id": 9701, "title": "Corrigendum-3 to CEN 02/2026 regarding Revised Zone-wise Table", "link": "/f/corr3.pdf", "date": "2026-10-25"},
            {"id": 9702, "title": "Notice for Extension of Closing Date for Fee Refund Bank Details", "link": "/f/ext_bank.pdf", "date": ""},
            {"id": 9703, "title": "Important Notice: Postponement and Rescheduling of Stage-II due to administrative reasons", "link": "/f/postp.pdf"},
            {"id": 9704, "title": "Compliance of Hon'ble CAT / High Court Order in OA No. 310/2026", "link": "/f/court_oa.pdf"},
            {"id": 9705, "title": "Website is under maintenance from 23:00 hrs to 01:00 hrs", "link": "/f/maint.pdf"},
            {"id": 9706, "title": "e-Tender for Annual Maintenance Contract of Computers in RRB Office", "link": "/f/amc_tender.pdf"},
        ]
        notifier = RecordingNotifier()
        summary, _, _ = run(self.state_path, json_feed(extra), notifier)
        sent_titles = [r["title"] for r, _, _ in notifier.sent_notices]
        self.assertEqual(len(sent_titles), 4, sent_titles)
        self.assertEqual(summary.telegram_deliveries_confirmed, 4)
        self.assertTrue(any("Corrigendum-3" in t for t in sent_titles))
        self.assertTrue(any("Extension of Closing Date" in t for t in sent_titles))
        self.assertTrue(any("Postponement and Rescheduling" in t for t in sent_titles))
        self.assertTrue(any("High Court Order" in t for t in sent_titles))
        self.assertFalse(any("maintenance" in t.lower() for t in sent_titles))
        for rec, _, _ in notifier.sent_notices:
            self.assertEqual(rec["category"], "Official Notice")


class ReportingTests(unittest.TestCase):
    def test_live_check_marks_rrb_notices_eligible_by_ledger_not_by_date(self) -> None:
        report = app.run_live_website_check(
            state_path="/nonexistent.json", sources=(RRB,), reference_date=TODAY,
            http_client_factory=lambda: FakeHttpClient(routes={ENDPOINT: (200, {}, json_feed())}, max_retries=1),
        )
        res = report["sources"][0]
        self.assertEqual(res["source_health_status"], "SUCCESS")
        self.assertEqual(res["data_origin"], "LIVE_FETCH")
        eligible_titles = sorted(n["title"] for n in res["eligible_today_notices"])
        self.assertEqual(len(eligible_titles), 3)  # tender filtered; old/undated still eligible
        self.assertTrue(all(n["eligibility_reason"] == "NEW_TO_LEDGER_DATE_INFORMATIONAL" for n in res["eligible_today_notices"]))
        self.assertFalse(res["date_coverage_warning"])

    def test_github_report_does_not_flag_sources_for_missing_dates(self) -> None:
        summary = app.RunSummary(mode="monitor", total_websites=10, succeeded_websites=10)
        summary.per_source_results["rrb_rrcb"] = {"status": "ok", "discovered": 20, "discovered_with_publication_date": 0, "new": 0}
        summary.per_source_results["dsssb"] = {"status": "ok", "discovered": 7, "discovered_with_publication_date": 0, "new": 0}
        out = io.StringIO()
        app.emit_github_actions_report(summary, env={"GITHUB_ACTIONS": "true"}, out=out)
        self.assertNotIn("[dsssb]", out.getvalue())
        self.assertNotIn("[rrb_rrcb]", out.getvalue())

    def test_telegram_message_formats(self) -> None:
        base = {"organization": RRB.organization, "title": "CEN 15/2026 Result of CBT-1", "category": "Results",
                "url": "https://rrb.indianrailways.gov.in/f/r.pdf", "normalized_url": "https://rrb.indianrailways.gov.in/f/r.pdf"}
        self.assertIn("Publication Date:</b> Not available", app.format_telegram_message(dict(base, publication_date=None)))
        self.assertIn("Publication Date:</b> 2024-01-02", app.format_telegram_message(dict(base, publication_date="2024-01-02")))
        self.assertIn("Publication Date:</b> Oct 2026 (Tentative)", app.format_telegram_message(dict(base, publication_date="Oct 2026 (Tentative)")))


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from test_audit_regressions import _dump_yaml_obj, _load_yaml_doc
        path = os.path.join(_HERE, ".github", "workflows", "main.yml")
        if not os.path.isfile(path):
            raise unittest.SkipTest("main.yml not present")
        with open(path, encoding="utf-8") as fh:
            cls.raw = fh.read()
        cls.doc = _load_yaml_doc(cls.raw)
        cls._dump = staticmethod(_dump_yaml_obj)

    def test_single_workflow_file_with_three_mode_gated_jobs(self) -> None:
        wf_dir = os.path.join(_HERE, ".github", "workflows")
        self.assertEqual(sorted(os.listdir(wf_dir)), ["main.yml"])
        jobs = self.doc["jobs"]
        self.assertEqual(set(jobs), {"monitor-notices", "rrcb-probe", "ping"})
        self.assertEqual(jobs["monitor-notices"]["if"], "${{ inputs.mode != 'rrcb_probe' && inputs.mode != 'ping' }}")
        self.assertEqual(jobs["rrcb-probe"]["if"], "${{ inputs.mode == 'rrcb_probe' }}")
        self.assertEqual(jobs["ping"]["if"], "${{ inputs.mode == 'ping' }}")

    def test_scheduled_and_cron_dispatch_runs_still_select_only_the_monitor_job(self) -> None:
        """schedule events and a dispatch without inputs have inputs.mode == null/auto: only the monitor job may run."""
        def selected(mode: Optional[str]) -> List[str]:
            m = mode or ""
            return [name for name, cond in (
                ("monitor-notices", m != "rrcb_probe" and m != "ping"),
                ("rrcb-probe", m == "rrcb_probe"),
                ("ping", m == "ping"),
            ) if cond]
        for mode in (None, "", "auto", "baseline", "monitor", "live_test"):
            self.assertEqual(selected(mode), ["monitor-notices"], mode)
        self.assertEqual(selected("rrcb_probe"), ["rrcb-probe"])
        self.assertEqual(selected("ping"), ["ping"])

    def test_main_workflow_schedule_and_secrets_still_intact(self) -> None:
        triggers = self.doc.get("on", self.doc.get(True))
        self.assertEqual(triggers["schedule"], [{"cron": "*/10 * * * *"}])
        self.assertIn("workflow_dispatch", triggers)
        monitor_text = self._dump(self.doc["jobs"]["monitor-notices"])
        for needed in ("secrets.TELEGRAM_BOT_TOKEN", "secrets.TELEGRAM_CHAT_ID", "secrets.RRCB_RELAY_URL_TEMPLATE"):
            self.assertIn(needed, monitor_text)
        self.assertNotIn("rrb_rrcb", monitor_text)  # source handling stays in code, not in the monitor job
        self.assertEqual(self.doc["permissions"], {"contents": "write"})  # needed only by the monitor job

    def test_probe_job_is_read_only_and_secret_free(self) -> None:
        job = self.doc["jobs"]["rrcb-probe"]
        self.assertEqual(job["permissions"], {"contents": "read"})
        text = self._dump(job)
        self.assertNotIn("secrets.", text)
        self.assertNotIn("TELEGRAM", text)
        self.assertNotIn("git push", text)
        self.assertNotIn("git commit", text)
        self.assertIn("scripts/probe_rrcb.py", text)
        self.assertIn("inputs.relay_url_template", text)


# ---------------------------------------------------------------------------------------------
# Base endpoint https://rrb.indianrailways.gov.in/getdata?loc=   (empty `loc`)
# ---------------------------------------------------------------------------------------------
NO_RECORDS = "\u0915\u094b\u0908 \u0905\u092d\u093f\u0932\u0947\u0916 \u0909\u092a\u0932\u092c\u094d\u0927 \u0928\u0939\u0940\u0902 \u0939\u0948!"  # "No records available!"


class BaseEndpointRequestTests(unittest.TestCase):
    def test_adapter_requests_exactly_the_base_url_and_never_appends_rrcb(self) -> None:
        client = FakeHttpClient(routes={ENDPOINT: (200, {"Content-Type": "application/json"}, json_feed())}, max_retries=1)
        app.adapter_rrb_rrcb(RRB, client)
        self.assertEqual(list(client.call_counts), ["https://rrb.indianrailways.gov.in/getdata?loc="])
        self.assertFalse(any("RRCB" in url.upper() for url in client.call_counts))

    def test_real_http_client_sends_the_empty_loc_parameter_verbatim(self) -> None:
        """urllib/HttpClient must not drop or rewrite the empty `loc=` query (real loopback socket)."""
        import http.server
        import threading
        seen: List[str] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                seen.append(self.path)
                body = json_feed().encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            client = app.HttpClient(timeout=(2, 2), max_retries=1, enable_doh_fallback=False)
            status, headers, body, _ = client.request(f"http://127.0.0.1:{httpd.server_address[1]}/getdata?loc=")
        finally:
            httpd.shutdown()
            httpd.server_close()
        self.assertEqual(status, 200)
        self.assertEqual(seen, ["/getdata?loc="])
        self.assertEqual(len(app.parse_rrcb_response(RRB, body.decode("utf-8"))), 4)


class NoRecordsAndResponseSafetyTests(unittest.TestCase):
    def test_hindi_no_records_message_is_an_empty_result_not_a_notice_or_crash(self) -> None:
        for body in (NO_RECORDS, f"  \n{NO_RECORDS}\n ", f"<html><body><p>{NO_RECORDS}</p></body></html>",
                     json.dumps({"message": NO_RECORDS}), json.dumps(NO_RECORDS, ensure_ascii=True),
                     f'<div class="alert">{NO_RECORDS}</div>'):
            self.assertEqual(app.parse_rrcb_response(RRB, body), [], body)

    def test_no_records_never_completes_a_baseline_and_never_touches_the_ledger(self) -> None:
        tmp = tempfile.mkdtemp(prefix="rrb-norec-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "notice_state.json")
        make_state(path)
        summary, _, _ = run(path, (200, {"Content-Type": "text/html; charset=utf-8"}, f"<html><body>{NO_RECORDS}</body></html>"), RecordingNotifier())
        res = summary.per_source_results["rrb_rrcb"]
        self.assertEqual((res["status"], res["error_category"], res["discovered"]), ("ok", None, 0))
        self.assertEqual(res["test_outcome"], "NO_NOTICE_FOUND")
        st = app.load_state(path, allow_init_if_missing=False)
        self.assertFalse(st["sources"]["rrb_rrcb"]["baseline_completed"])
        self.assertEqual([r for r in st["notices"].values() if r["source_id"] == "rrb_rrcb"], [])

    def test_homepage_and_dynamic_endpoint_fallback_when_getdata_is_empty_shell(self) -> None:
        """If getdata?loc= returns an unrecognized HTML shell, adapter falls back to homepage / dynamic getdata endpoint."""
        dyn_url = "https://rrb.indianrailways.gov.in/getdata?loc=CENTRAL"
        homepage_html = f'<html><body><script>fetch("{dyn_url}")</script></body></html>'
        routes = {
            ENDPOINT: (200, {"Content-Type": "text/html"}, "<html><body><p>Welcome Shell</p></body></html>"),
            RRB.homepage_url: (200, {"Content-Type": "text/html"}, homepage_html),
            dyn_url: (200, {"Content-Type": "application/json"}, json_feed()),
        }
        client = FakeHttpClient(routes=routes, max_retries=1)
        got = app.adapter_rrb_rrcb(RRB, client)
        self.assertEqual(len(got), 4)
        self.assertIn(RRB.homepage_url, client.call_counts)
        self.assertIn(dyn_url, client.call_counts)

    def test_real_records_alongside_a_short_page_are_not_mistaken_for_no_records(self) -> None:
        body = json.dumps([{"id": 5, "title": "CEN 03/2026 Result of CBT-1 shortlisted candidates", "link": "/r.pdf"}])
        self.assertEqual(len(app.parse_rrcb_response(RRB, body)), 1)

    def test_hindi_text_in_a_record_title_survives_decoding_and_parsing(self) -> None:
        title = "\u092d\u0930\u094d\u0924\u0940 \u0905\u0927\u093f\u0938\u0942\u091a\u0928\u093e CEN 01/2026 Result of CBT-1"
        raw = json.dumps([{"id": 1, "title": title, "link": "/h.pdf"}], ensure_ascii=False).encode("utf-8")
        client = FakeHttpClient(routes={ENDPOINT: (200, {"Content-Type": "text/html; charset=ISO-8859-1"}, raw)}, max_retries=1)
        got = app.adapter_rrb_rrcb(RRB, client)  # a wrong declared charset must not mangle UTF-8 Devanagari
        self.assertEqual(got[0].title, title)

    def test_unexpected_content_types_are_rejected(self) -> None:
        for ctype in ("application/pdf", "application/octet-stream", "image/png", "application/zip", "video/mp4"):
            client = FakeHttpClient(routes={ENDPOINT: (200, {"Content-Type": ctype}, b"%PDF-1.7 ...")}, max_retries=1)
            with self.assertRaises(app.SourceFetchError, msg=ctype) as ctx:
                app.adapter_rrb_rrcb(RRB, client)
            self.assertEqual(ctx.exception.category, "UNEXPECTED_CONTENT_TYPE", ctype)
        for ctype in ("application/json", "text/html; charset=utf-8", "text/plain", ""):
            headers = {"Content-Type": ctype} if ctype else {}
            client = FakeHttpClient(routes={ENDPOINT: (200, headers, json_feed())}, max_retries=1)
            self.assertEqual(len(app.adapter_rrb_rrcb(RRB, client)), 4, ctype)

    def test_malformed_and_unknown_responses_fail_safely(self) -> None:
        for body, category in (("{ not json", "INVALID_JSON"), ("<html><body><p>Hello</p></body></html>", "UNRECOGNIZED_STRUCTURE"),
                               ("   ", "EMPTY_RESPONSE"), ('{"data": null}', "UNRECOGNIZED_STRUCTURE")):
            client = FakeHttpClient(routes={ENDPOINT: (200, {"Content-Type": "text/html"}, body)}, max_retries=1)
            with self.assertRaises(app.SourceFetchError, msg=body) as ctx:
                app.adapter_rrb_rrcb(RRB, client)
            self.assertEqual(ctx.exception.category, category, body)

    def test_http_errors_and_timeouts_are_reported_with_their_real_category(self) -> None:
        tmp = tempfile.mkdtemp(prefix="rrb-http-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "notice_state.json")
        make_state(path)
        for category in ("CONNECTION_TIMEOUT", "HTTP_503_SERVER_ERROR", "HTTP_404_NOT_FOUND", "HTTP_403_FORBIDDEN", "DNS_ERROR"):
            summary, _, _ = run(path, app.SourceFetchError(category, f"simulated {category}"), RecordingNotifier())
            res = summary.per_source_results["rrb_rrcb"]
            self.assertEqual((res["status"], res["error_category"]), ("error", category))
            self.assertEqual(summary.failed_websites, 1)

    def test_non_official_urls_are_rejected_on_the_base_endpoint(self) -> None:
        feed = json.dumps([
            {"id": 1, "title": "CEN 09/2026 Recruitment notification for posts", "link": "https://evil.example.com/a.pdf"},
            {"id": 2, "title": "CEN 09/2026 Result notice on a look-alike host", "link": "https://rrb.indianrailways.gov.in.evil.example/b.pdf"},
            {"id": 3, "title": "CEN 09/2026 Admit Card on a legacy regional domain", "link": "https://rrbbhopal.gov.in/c.pdf"},
            {"id": 4, "title": "CEN 09/2026 Answer Key on the official portal", "link": "/d.pdf"},
        ])
        got = app.parse_rrcb_response(RRB, feed)
        self.assertEqual([n.url for n in got], ["https://rrb.indianrailways.gov.in/d.pdf"])

    def test_unknown_publication_date_is_never_replaced_by_a_made_up_one(self) -> None:
        tmp = tempfile.mkdtemp(prefix="rrb-date-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "notice_state.json")
        make_state(path)
        run(path, json_feed())
        extra = [{"id": 9801, "title": "CEN 98/2026 Admit Card and E-Call Letter for CBT-1", "link": "/f/c98.pdf"}]
        notifier = RecordingNotifier()
        run(path, json_feed(extra), notifier)
        self.assertEqual(len(notifier.sent_notices), 1)
        rec = next(r for r in app.load_state(path, allow_init_if_missing=False)["notices"].values() if "CEN 98/2026" in r["title"])
        self.assertIsNone(rec["publication_date"])
        self.assertNotIn(TODAY.isoformat(), notifier.sent_notices[0][2])

    def test_state_file_is_byte_identical_when_nothing_new_appears(self) -> None:
        tmp = tempfile.mkdtemp(prefix="rrb-nochange-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "notice_state.json")
        make_state(path)
        run(path, json_feed())  # baseline
        with open(path, "rb") as fh_before:
            before = hashlib.sha256(fh_before.read()).hexdigest()
        for _ in range(3):
            notifier = RecordingNotifier()
            run(path, json_feed(), notifier)
            self.assertEqual(len(notifier.sent_notices), 0)
        with open(path, "rb") as fh_after:
            self.assertEqual(before, hashlib.sha256(fh_after.read()).hexdigest())

    def test_rrb_failure_does_not_stop_other_sources(self) -> None:
        tmp = tempfile.mkdtemp(prefix="rrb-isolate-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "notice_state.json")
        make_state(path)
        ssc = app.SOURCE_BY_ID["ssc"]
        ssc_body = json.dumps({"statusCode": "200", "data": [{"id": "s1", "headline": "SSC Combined Graduate Level Examination 2026 Final Answer Key",
                                                               "createdAt": "2026-10-25T05:30:00.000Z", "attachments": [{"path": "uploads/k.pdf"}]}]})
        routes = {ssc.notice_urls[0]: (200, {"Content-Type": "application/json"}, ssc_body),
                  ENDPOINT: app.SourceFetchError("CONNECTION_TIMEOUT", "Timed out connecting")}
        client = FakeHttpClient(routes=routes, max_retries=1)
        summary, _ = app.run_monitor(state_path=path, mode="monitor", sources=(ssc, RRB), http_client=client, notifier=RecordingNotifier(),
                                     reference_date=TODAY, max_age_days=0)
        self.assertEqual(summary.per_source_results["ssc"]["status"], "ok")
        self.assertEqual(summary.per_source_results["rrb_rrcb"]["error_category"], "CONNECTION_TIMEOUT")
        self.assertEqual((summary.succeeded_websites, summary.failed_websites), (1, 1))

    def test_split_connect_and_read_timeout_allows_slow_header_response(self) -> None:
        """
        Regression test for HttpClient._perform_single_request:
        When connect_timeout=1s and read_timeout=3s, a server that accepts TCP immediately
        and takes 1.3s to generate HTTP headers must SUCCEED under read_timeout (3s) rather
        than timing out under connect_timeout (1s).
        """
        import http.server
        import threading
        import time

        class SlowHeaderHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                time.sleep(1.3)
                body = json_feed().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), SlowHeaderHandler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            client = app.HttpClient(timeout=(1, 3), max_retries=1, enable_doh_fallback=False)
            status, _, body, _ = client.request(f"http://127.0.0.1:{httpd.server_address[1]}/getdata?loc=")
        finally:
            httpd.shutdown()
            httpd.server_close()
        self.assertEqual(status, 200)
        self.assertEqual(len(app.parse_rrcb_response(RRB, body.decode("utf-8"))), 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)

