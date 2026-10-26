#!/usr/bin/env python3
"""
Regression tests added by the evidence-based project audit.

Every test here targets a defect that was REPRODUCED against the pre-audit code (see the audit report).
All tests are offline and deterministic: loopback HTTP servers on 127.0.0.1, temporary state copies,
fake HTTP clients. None of them contacts a real website, sends Telegram messages, or touches the
production data/notice_state.json (the latter is only read, and its SHA-256 is asserted unchanged).
"""

from __future__ import annotations

import dataclasses
import hashlib
import http.server
import io
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from typing import Any, Dict, List, Optional
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import main as app  # noqa: E402
from test_main import FakeHttpClient, RecordingNotifier  # noqa: E402

REPO_STATE = os.path.join(_HERE, "data", "notice_state.json")
TODAY = app.datetime.date(2026, 10, 25)


def _sha256(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


class _LoopbackServer:
    """Tiny controllable HTTP server on 127.0.0.1 (ephemeral port)."""

    def __init__(self, handler_factory: Any) -> None:
        self.stop = threading.Event()
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_factory(self.stop))
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.stop.set()
        self.httpd.shutdown()
        self.httpd.server_close()


def _slow_drip_handler(stop: threading.Event) -> Any:
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:  # silence
            pass

        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", "500")
            self.end_headers()
            try:
                for _ in range(500):  # one byte every 0.2s: never trips a 1s per-recv read timeout
                    if stop.is_set():
                        return
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.2)
            except OSError:
                pass

    return Handler


# ---------------------------------------------------------------------------------------------
# 1. Live Website Check must describe what really happened
# ---------------------------------------------------------------------------------------------
class LiveCheckHonestyTests(unittest.TestCase):
    def test_all_sources_failed_is_not_reported_ok_or_live_fetch(self) -> None:
        """Pre-audit: ok=True and data_origin=LIVE_FETCH even though 0/9 sources succeeded."""
        report = app.run_live_website_check(
            state_path="/nonexistent/state.json",
            reference_date=TODAY,
            max_age_days=0,
            http_client_factory=lambda: FakeHttpClient(routes={}),
        )
        self.assertEqual(report["succeeded_sources"], 0)
        self.assertEqual(report["failed_sources"], len(app.SOURCES))
        self.assertFalse(report["ok"])
        self.assertEqual(report["run_outcome"], "ALL_SOURCES_FAILED")
        self.assertEqual(report["data_origin"], "LIVE_FETCH_FAILED")
        self.assertEqual(report["sources_request_attempted"], len(app.SOURCES))
        self.assertEqual(report["sources_adapter_completed"], 0)
        for res in report["sources"]:
            self.assertTrue(res["request_attempted"], res["source_id"])
            self.assertFalse(res["adapter_completed"], res["source_id"])
            self.assertEqual(res["data_origin"], "LIVE_FETCH_FAILED", res["source_id"])
            self.assertEqual(res["failure_stage"], "FETCH_OR_PARSE", res["source_id"])
            self.assertEqual(res["error_category"], "HTTP_404_NOT_FOUND", res["source_id"])

    def test_missing_adapter_is_not_attempted_not_live_fetch(self) -> None:
        bad = app.SourceConfig(
            source_id="ghost",
            organization="Ghost Board",
            homepage_url="https://ghost.gov.in/",
            notice_urls=("https://ghost.gov.in/",),
            adapter_name="adapter_does_not_exist",
            notes="",
        )
        res = app.check_single_source_live(bad, {}, app.load_keyword_categories(), reference_date=TODAY)
        self.assertEqual(res["error_category"], "ADAPTER_NOT_IMPLEMENTED")
        self.assertEqual(res["data_origin"], "NOT_ATTEMPTED")
        self.assertFalse(res["request_attempted"])
        self.assertEqual(res["failure_stage"], "NOT_ATTEMPTED")

    def test_unknown_source_filter_is_not_ok(self) -> None:
        report = app.run_live_website_check(
            state_path="/nonexistent/state.json",
            source_ids_filter=["typo-source"],
            reference_date=TODAY,
            http_client_factory=lambda: FakeHttpClient(routes={}),
        )
        self.assertEqual(report["total_sources_checked"], 0)
        self.assertEqual(report["run_outcome"], "NO_SOURCES_SELECTED")
        self.assertFalse(report["ok"])
        self.assertEqual(report["data_origin"], "NOT_ATTEMPTED")

    def _ssc_routes(self) -> Dict[str, Any]:
        ssc = app.SOURCE_BY_ID["ssc"]
        payload = {
            "statusCode": "200",
            "data": [
                {
                    "id": "ssc-1",
                    "headline": "SSC Combined Graduate Level Examination 2026 Final Answer Key",
                    "createdAt": "2026-10-25T05:30:00.000Z",
                    "attachments": [{"path": "uploads/cgl_final_answer_key_25102026.pdf"}],
                }
            ],
        }
        return {ssc.notice_urls[0]: (200, {"Content-Type": "application/json"}, json.dumps(payload))}

    def test_partial_failure_outcome(self) -> None:
        report = app.run_live_website_check(
            state_path="/nonexistent/state.json",
            reference_date=TODAY,
            http_client_factory=lambda: FakeHttpClient(routes=self._ssc_routes()),
        )
        self.assertEqual(report["run_outcome"], "PARTIAL_FAILURE")
        self.assertTrue(report["ok"])
        self.assertEqual(report["data_origin"], "LIVE_FETCH")
        self.assertEqual(report["succeeded_sources"], 1)
        self.assertEqual(report["sources_adapter_completed"], 1)

    def test_post_fetch_processing_bug_is_not_reported_as_website_failure(self) -> None:
        """
        The reported `name 'parse_official_date_token' is not defined` crash happened AFTER a successful
        fetch+parse but was surfaced as a source failure with no stage information. A post-fetch bug must be
        classified as POST_PROCESSING_ERROR with adapter_completed=True, and must not affect other sources.
        """
        real_validate = app.validate_publication_date

        def exploding_validate(*args: Any, **kwargs: Any) -> Any:
            if "ssc.gov.in" in str(kwargs.get("url", "")) or "cgl_final" in str(kwargs.get("url", "")):
                raise NameError("name 'parse_official_date_token' is not defined")
            return real_validate(*args, **kwargs)

        routes = dict(self._ssc_routes())
        upsc = app.SOURCE_BY_ID["upsc"]
        routes[upsc.notice_urls[0]] = (
            200,
            {"Content-Type": "text/html"},
            "<html><body><h2>What's New</h2><ul><li><a href=\"/files/Advt-1.pdf\">25-10-2026 Recruitment Advertisement No. 1/2026 for 40 Posts</a></li></ul></body></html>",
        )
        with mock.patch.object(app, "validate_publication_date", exploding_validate):
            report = app.run_live_website_check(
                state_path="/nonexistent/state.json",
                sources=(app.SOURCE_BY_ID["ssc"], upsc),
                reference_date=TODAY,
                http_client_factory=lambda: FakeHttpClient(routes=routes),
            )
        by_id = {r["source_id"]: r for r in report["sources"]}
        ssc_res, upsc_res = by_id["ssc"], by_id["upsc"]
        self.assertEqual(ssc_res["source_health_status"], "FAILED")
        self.assertEqual(ssc_res["error_category"], "POST_PROCESSING_ERROR")
        self.assertEqual(ssc_res["failure_stage"], "POST_PROCESSING")
        self.assertTrue(ssc_res["adapter_completed"])
        self.assertEqual(ssc_res["data_origin"], "LIVE_FETCH")
        self.assertGreaterEqual(ssc_res["total_discovered"], 1)
        self.assertIn("parse_official_date_token", ssc_res["error_message"])
        self.assertEqual(upsc_res["source_health_status"], "SUCCESS")

    def test_ledger_counts_are_labelled_separately(self) -> None:
        if not os.path.isfile(REPO_STATE):
            self.skipTest("data/notice_state.json not present in this checkout")
        before = _sha256(REPO_STATE)
        report = app.run_live_website_check(
            state_path=REPO_STATE,
            reference_date=TODAY,
            http_client_factory=lambda: FakeHttpClient(routes={}),
        )
        state = app.load_state(REPO_STATE, allow_init_if_missing=False)
        self.assertEqual(report["ledger_entry_count"], len(state["notices"]))
        self.assertEqual(report["ledger_notice_count"], report["ledger_entry_count"])
        fingerprints = {str(r.get("fingerprint") or k) for k, r in state["notices"].items()}
        self.assertEqual(report["ledger_unique_fingerprint_count"], len(fingerprints))
        self.assertIn("ledger_sent_count", report)
        self.assertEqual(before, _sha256(REPO_STATE), "live check must never modify the production state file")

    def test_date_blind_source_is_eligible_without_date_rejection(self) -> None:
        """With publication-date filtering removed, a source returning relevant notices with no publication date is eligible."""
        dsssb = app.SOURCE_BY_ID["dsssb"]
        html = (
            "<html><body><table><tr><td>Current Vacancy: Legal Assistant (Post Code: 815/25)</td>"
            "<td><a href=\"/notices/advt_815_25.pdf\">Download PDF</a></td></tr></table></body></html>"
        )
        routes = {dsssb.notice_urls[0]: (200, {"Content-Type": "text/html"}, html)}
        res = app.check_single_source_live(
            dsssb, {}, app.load_keyword_categories(), reference_date=TODAY,
            http_client_factory=lambda: FakeHttpClient(routes=routes),
        )
        self.assertEqual(res["source_health_status"], "SUCCESS")
        self.assertGreaterEqual(res["relevant_notices_count"], 1)
        self.assertEqual(res["relevant_with_publication_date"], 0)
        self.assertEqual(res["discovered_with_publication_date"], 0)
        self.assertFalse(res["date_coverage_warning"])
        self.assertEqual(res["discovery_status"], "NEW_NOTICE_FOUND")
        self.assertEqual(len(res["eligible_today_notices"]), 1)

        ssc_res = app.check_single_source_live(
            app.SOURCE_BY_ID["ssc"], {}, app.load_keyword_categories(), reference_date=TODAY,
            http_client_factory=lambda: FakeHttpClient(routes=self._ssc_routes()),
        )
        self.assertFalse(ssc_res["date_coverage_warning"])
        self.assertGreaterEqual(ssc_res["relevant_with_publication_date"], 1)


# ---------------------------------------------------------------------------------------------
# 2. Total request time must be bounded (real sockets, loopback)
# ---------------------------------------------------------------------------------------------
class BoundedRequestTimeTests(unittest.TestCase):
    def test_slow_drip_response_is_cut_off_by_wall_clock_deadline(self) -> None:
        """Pre-audit: read_timeout=1s did not stop a body trickling one byte per 0.2s (ran for ~100s)."""
        server = _LoopbackServer(_slow_drip_handler)
        try:
            client = app.HttpClient(
                timeout=(2, 1), max_retries=1, enable_doh_fallback=False, request_deadline_seconds=1.5
            )
            started = time.monotonic()
            with self.assertRaises(app.SourceFetchError) as ctx:
                client.request(f"http://127.0.0.1:{server.port}/")
            elapsed = time.monotonic() - started
            self.assertEqual(ctx.exception.category, "REQUEST_DEADLINE_EXCEEDED")
            self.assertLess(elapsed, 4.0, f"request took {elapsed:.1f}s despite a 1.5s deadline")
        finally:
            server.close()

    def test_exhausted_source_budget_stops_before_any_network_attempt(self) -> None:
        client = app.HttpClient(timeout=(2, 2), max_retries=3, enable_doh_fallback=False)
        client.start_budget(0.001)
        time.sleep(0.02)
        with mock.patch.object(client, "_perform_single_request") as attempt:
            with self.assertRaises(app.SourceFetchError) as ctx:
                client.request("https://example.invalid/")
            attempt.assert_not_called()
        self.assertEqual(ctx.exception.category, "SOURCE_TIME_BUDGET_EXCEEDED")

    def test_budget_bounds_a_multi_url_adapter(self) -> None:
        """One unresponsive source with several notice URLs must not consume N x (URLs x retries) x deadline."""
        server = _LoopbackServer(_slow_drip_handler)
        try:
            urls = tuple(f"http://127.0.0.1:{server.port}/page{i}" for i in range(4))
            src = dataclasses.replace(app.SOURCE_BY_ID["mpesb"], notice_urls=urls)
            client = app.HttpClient(
                timeout=(2, 1), max_retries=2, sleep_fn=lambda _: None, enable_doh_fallback=False,
                request_deadline_seconds=1.0,
            )
            client.start_budget(2.0)
            started = time.monotonic()
            with self.assertRaises(app.SourceFetchError) as ctx:
                app.adapter_mpesb(src, client)
            elapsed = time.monotonic() - started
            self.assertIn(ctx.exception.category, ("REQUEST_DEADLINE_EXCEEDED", "SOURCE_TIME_BUDGET_EXCEEDED"))
            self.assertLess(elapsed, 3.6, f"adapter ran {elapsed:.1f}s with a 2s source budget")
        finally:
            server.close()

    def test_isolated_worker_client_inherits_deadline_and_budget(self) -> None:
        parent = app.HttpClient(timeout=(3, 4), max_retries=2, enable_doh_fallback=False, request_deadline_seconds=7.0)
        captured: Dict[str, Any] = {}

        def fake_adapter(src: Any, client: Any) -> List[Any]:
            captured["deadline"] = client.request_deadline_seconds
            captured["doh"] = client.enable_doh_fallback
            captured["remaining"] = client._remaining_budget()
            captured["is_parent"] = client is parent
            return []

        src = app.SOURCE_BY_ID["ssc"]
        with mock.patch.dict(app.ADAPTER_REGISTRY, {src.adapter_name: fake_adapter}):
            app._fetch_single_source(src, parent, True)
        self.assertFalse(captured["is_parent"])
        self.assertEqual(captured["deadline"], 7.0)
        self.assertFalse(captured["doh"])
        self.assertIsNotNone(captured["remaining"])
        self.assertGreater(captured["remaining"], 0)
        self.assertLessEqual(captured["remaining"], app.DEFAULT_SOURCE_BUDGET_SECONDS)


# ---------------------------------------------------------------------------------------------
# 3. DNS-over-HTTPS fallback must be thread-safe (no global socket.getaddrinfo patching)
# ---------------------------------------------------------------------------------------------
class DohFallbackThreadSafetyTests(unittest.TestCase):
    def _gated_handler(self, label: str, connected: threading.Event, release: threading.Event, seen: List[bool],
                       orig_gai: Any) -> Any:
        def factory(stop: threading.Event) -> Any:
            class Handler(http.server.BaseHTTPRequestHandler):
                def log_message(self, *args: Any) -> None:
                    pass

                def do_GET(self) -> None:
                    seen.append(socket.getaddrinfo is orig_gai)  # was the process-global resolver touched mid-request?
                    connected.set()
                    release.wait(10)
                    body = label.encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            return Handler

        return factory

    def test_pinned_fallback_connects_and_leaves_global_resolver_untouched(self) -> None:
        orig = socket.getaddrinfo
        seen: List[bool] = []
        connected, release = threading.Event(), threading.Event()
        release.set()
        server = _LoopbackServer(self._gated_handler("pinned-ok", connected, release, seen, orig))
        try:
            client = app.HttpClient(timeout=(2, 2), max_retries=1, enable_doh_fallback=True)
            client._resolve_via_doh = lambda host: ["127.0.0.1"]  # type: ignore[assignment]
            status, _, body, _ = client.request(f"http://pinned-host.test.invalid:{server.port}/")
            self.assertEqual(status, 200)
            self.assertEqual(body, b"pinned-ok")
            self.assertEqual(seen, [True], "socket.getaddrinfo must not be monkeypatched during the fallback")
            self.assertIs(socket.getaddrinfo, orig)
        finally:
            server.close()

    def test_two_concurrent_fallbacks_do_not_leak_or_cross_wires(self) -> None:
        """Pre-audit: overlapping fallbacks left a stale `_patched_gai` installed on socket.getaddrinfo for good."""
        orig = socket.getaddrinfo
        seen: List[bool] = []
        a_connected, a_release = threading.Event(), threading.Event()
        b_connected, b_release = threading.Event(), threading.Event()
        server_a = _LoopbackServer(self._gated_handler("body-A", a_connected, a_release, seen, orig))
        server_b = _LoopbackServer(self._gated_handler("body-B", b_connected, b_release, seen, orig))
        results: Dict[str, Any] = {}
        try:
            client = app.HttpClient(timeout=(2, 5), max_retries=1, enable_doh_fallback=True)
            client._resolve_via_doh = lambda host: ["127.0.0.1"]  # type: ignore[assignment]

            def run(name: str, port: int) -> None:
                try:
                    results[name] = client.request(f"http://{name}.test.invalid:{port}/")[2]
                except Exception as exc:  # pragma: no cover - failure path is asserted below
                    results[name] = exc

            ta = threading.Thread(target=run, args=("a", server_a.port))
            tb = threading.Thread(target=run, args=("b", server_b.port))
            ta.start()
            self.assertTrue(a_connected.wait(10))
            tb.start()
            self.assertTrue(b_connected.wait(10))
            a_release.set()
            ta.join(10)
            b_release.set()
            tb.join(10)
            self.assertEqual(results.get("a"), b"body-A")
            self.assertEqual(results.get("b"), b"body-B")
            self.assertTrue(all(seen), "global resolver was modified while a request was in flight")
            self.assertIs(socket.getaddrinfo, orig, "stale DNS wrapper leaked onto socket.getaddrinfo")
        finally:
            server_a.close()
            server_b.close()
            socket.getaddrinfo = orig


# ---------------------------------------------------------------------------------------------
# 4. GitHub Actions visibility of partial failures
# ---------------------------------------------------------------------------------------------
class GithubActionsReportTests(unittest.TestCase):
    def _summary(self) -> app.RunSummary:
        s = app.RunSummary(mode="monitor", total_websites=9, succeeded_websites=8, failed_websites=1)
        s.state_persisted = True
        s.per_source_results["mpesb"] = {
            "status": "error",
            "error_category": "CONNECTION_TIMEOUT",
            "error": "Timed out for https://esb.mp.gov.in/e_default.html token=SECRET-TOKEN-123\nsecond line",
        }
        s.per_source_results["dsssb"] = {
            "status": "ok", "discovered": 7, "discovered_with_publication_date": 0, "new": 0,
        }
        s.per_source_results["ssc"] = {
            "status": "ok", "discovered": 10, "discovered_with_publication_date": 10, "new": 0,
        }
        return s

    def test_warning_notice_and_step_summary_with_redaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            summary_file = os.path.join(tmp, "step_summary.md")
            out = io.StringIO()
            app.emit_github_actions_report(
                self._summary(),
                secrets=("SECRET-TOKEN-123", "-1001234"),
                env={"GITHUB_ACTIONS": "true", "GITHUB_STEP_SUMMARY": summary_file},
                out=out,
            )
            printed = out.getvalue()
            with open(summary_file, encoding="utf-8") as fh:
                md = fh.read()
        self.assertIn("::warning::[mpesb] website check failed - CONNECTION_TIMEOUT", printed)
        self.assertIn("::warning::1/9 websites failed", printed)
        self.assertNotIn("::notice::[dsssb]", printed)
        self.assertNotIn("::notice::[ssc]", printed)
        self.assertNotIn("SECRET-TOKEN-123", printed)
        self.assertNotIn("SECRET-TOKEN-123", md)
        self.assertIn("%0A", printed)  # newline escaped for the workflow-command protocol
        self.assertEqual(sum(1 for ln in printed.splitlines() if ln.startswith("::")), 2)
        self.assertIn("| mpesb | FAILED |", md)
        self.assertIn("| ssc | OK |", md)

    def test_noop_outside_github_actions(self) -> None:
        out = io.StringIO()
        app.emit_github_actions_report(self._summary(), env={}, out=out)
        self.assertEqual(out.getvalue(), "")

    def _run_main_with(self, summary: app.RunSummary, extra: List[str]) -> int:
        with tempfile.TemporaryDirectory() as tmp:
            state_file = os.path.join(tmp, "notice_state.json")
            st = app.create_initial_state()
            st["initialized"] = True
            app.save_state_atomically(st, state_file)
            with mock.patch.object(app, "run_monitor", return_value=(summary, {})), \
                 mock.patch.object(app, "print_run_summary"), \
                 mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}):
                return app.main(["--mode", "monitor", "--dry-run", "--state-file", state_file] + extra)

    def test_exit_code_semantics_for_partial_and_total_failure(self) -> None:
        partial = self._summary()
        self.assertEqual(self._run_main_with(partial, ["--allow-partial-source-failures"]), 0)
        self.assertEqual(self._run_main_with(self._summary(), []), 6)
        total = app.RunSummary(mode="monitor", total_websites=9, succeeded_websites=0, failed_websites=9)
        total.state_persisted = True
        self.assertEqual(self._run_main_with(total, ["--allow-partial-source-failures"]), 5)
        unpersisted = self._summary()
        unpersisted.state_persisted = False
        self.assertEqual(self._run_main_with(unpersisted, ["--allow-partial-source-failures"]), 2)


# ---------------------------------------------------------------------------------------------
# 5. Production-state safety (always operates on COPIES)
# ---------------------------------------------------------------------------------------------
@unittest.skipUnless(os.path.isfile(REPO_STATE), "data/notice_state.json not present in this checkout")
class StateSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="state-copy-")
        self.copy = os.path.join(self.tmp, "notice_state.json")
        shutil.copyfile(REPO_STATE, self.copy)
        self.prod_hash = _sha256(REPO_STATE)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.assertEqual(self.prod_hash, _sha256(REPO_STATE), "production state file was modified by a test")

    def test_dry_run_with_every_source_failing_never_writes_state(self) -> None:
        before = _sha256(self.copy)
        summary, _ = app.run_monitor(
            state_path=self.copy, mode="monitor", http_client=FakeHttpClient(routes={}), notifier=None,
            dry_run=True, reference_date=TODAY, max_age_days=0,
        )
        self.assertEqual(summary.succeeded_websites, 0)
        self.assertEqual(summary.failed_websites, len(app.SOURCES))
        self.assertTrue(all(r["status"] == "error" for r in summary.per_source_results.values()))
        self.assertEqual(before, _sha256(self.copy))

    def test_merge_state_is_lossless_and_idempotent_on_a_copy(self) -> None:
        original = app.load_state(self.copy, allow_init_if_missing=False)
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}):
            self.assertEqual(app.main(["--merge-state", REPO_STATE, "--state-file", self.copy]), 0)
        merged = app.load_state(self.copy, allow_init_if_missing=False)
        self.assertEqual(set(merged["notices"]), set(original["notices"]))
        self.assertEqual(len(merged["notices"]), len(original["notices"]))

    def test_merging_identical_state_does_not_rewrite_the_file(self) -> None:
        """Pre-audit: a no-op merge rewrote the file (new updated_at) so the workflow committed every 10 minutes."""
        before = _sha256(self.copy)
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}):
            self.assertEqual(app.main(["--merge-state", REPO_STATE, "--state-file", self.copy]), 0)
        self.assertEqual(before, _sha256(self.copy))

    def test_merging_state_with_a_new_notice_does_write(self) -> None:
        incoming = json.loads(json.dumps(app.load_state(self.copy, allow_init_if_missing=False)))
        any_rec = next(iter(incoming["notices"].values()))
        clone = dict(any_rec, fingerprint="f" * 64, title="Extra notice only present in the incoming state copy",
                     url="https://ssc.gov.in/extra-merge-test.pdf", normalized_url="https://ssc.gov.in/extra-merge-test.pdf",
                     source_identifier="merge-test-1", canonical_pdf_url="", content_hash="", canonical_title="extra notice merge test")
        incoming["notices"]["f" * 64] = clone
        incoming_path = os.path.join(self.tmp, "incoming.json")
        app.save_state_atomically(incoming, incoming_path)
        before = _sha256(self.copy)
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}):
            self.assertEqual(app.main(["--merge-state", incoming_path, "--state-file", self.copy]), 0)
        self.assertNotEqual(before, _sha256(self.copy))
        merged = app.load_state(self.copy, allow_init_if_missing=False)
        self.assertIn("f" * 64, merged["notices"])
        self.assertEqual(len(merged["notices"]), len(incoming["notices"]))

    def test_corrupt_incoming_state_is_rejected_and_base_untouched(self) -> None:
        bad = os.path.join(self.tmp, "bad.json")
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write("{ this is not valid json")
        before = _sha256(self.copy)
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}):
            self.assertEqual(app.main(["--merge-state", bad, "--state-file", self.copy]), 2)
        self.assertEqual(before, _sha256(self.copy))


# ---------------------------------------------------------------------------------------------
# 6. Static checks of the production workflow (syntax/config only; NOT a substitute for a real run)
# ---------------------------------------------------------------------------------------------
def _find_workflow() -> Optional[str]:
    for candidate in (
        os.path.join(_HERE, ".github", "workflows", "main.yml"),
        os.path.join(_HERE, "main.yml"),
    ):
        if os.path.isfile(candidate):
            return candidate
    return None


def _parse_workflow_fallback(text: str) -> Dict[str, Any]:
    """Minimal deterministic YAML parser for .github/workflows/main.yml when PyYAML is not installed."""
    cron_matches = re.findall(r'-\s*cron:\s*["\']?([^"\'\n]+)["\']?', text)
    opt_block = re.search(r"options:\s*\n((?:\s*-\s*[^\n]+\n)+)", text)
    options = [re.sub(r"^\s*-\s*", "", ln).strip() for ln in (opt_block.group(1).splitlines() if opt_block else []) if ln.strip()]
    default_mode_m = re.search(r'mode:\s*\n(?:[^\n]*\n){1,5}?\s*default:\s*["\']?([^"\'\n]+)["\']?', text)
    perm_m = re.search(r"^permissions:\s*\n\s+contents:\s*(\S+)", text, re.MULTILINE)
    cancel_m = re.search(r"cancel-in-progress:\s*(true|false)", text, re.IGNORECASE)

    def _extract_job_block(job_id: str) -> str:
        pat = re.compile(rf"^  {re.escape(job_id)}:\s*\n((?:    .*\n|\s*\n)*)", re.MULTILINE)
        m = pat.search(text)
        return m.group(1) if m else ""

    def _parse_steps(job_block: str) -> List[Dict[str, Any]]:
        steps: List[Dict[str, Any]] = []
        step_chunks = re.split(r"\n      - ", "\n" + job_block)
        for chunk in step_chunks[1:]:
            step: Dict[str, Any] = {}
            name_m = re.search(r"^name:\s*(.+)$", chunk, re.MULTILINE)
            if name_m:
                step["name"] = name_m.group(1).strip()
            if_m = re.search(r"^\s*if:\s*(.+)$", chunk, re.MULTILINE)
            if if_m:
                step["if"] = if_m.group(1).strip()
            uses_m = re.search(r"^\s*uses:\s*(.+)$", chunk, re.MULTILINE)
            if uses_m:
                step["uses"] = uses_m.group(1).strip()
            run_block = re.search(r"^\s*run:\s*\|\s*\n((?:          .*\n|\s*\n)+)", chunk, re.MULTILINE)
            if run_block:
                step["run"] = "\n".join(ln[10:] if ln.startswith("          ") else ln for ln in run_block.group(1).splitlines())
            else:
                run_inline = re.search(r"^\s*run:\s*(.+)$", chunk, re.MULTILINE)
                if run_inline:
                    step["run"] = run_inline.group(1).strip()
            steps.append(step)
        return steps

    mon_block = _extract_job_block("monitor-notices")
    probe_block = _extract_job_block("rrcb-probe")
    ping_block = _extract_job_block("ping")

    mon_to = re.search(r"timeout-minutes:\s*(\d+)", mon_block)
    mon_if = re.search(r"^\s*if:\s*(.+)$", mon_block, re.MULTILINE)
    probe_if = re.search(r"^\s*if:\s*(.+)$", probe_block, re.MULTILINE)
    ping_if = re.search(r"^\s*if:\s*(.+)$", ping_block, re.MULTILINE)
    probe_perm = re.search(r"permissions:\s*\n\s+contents:\s*(\S+)", probe_block)

    return {
        "on": {
            "schedule": [{"cron": c.strip()} for c in cron_matches],
            "workflow_dispatch": {
                "inputs": {
                    "mode": {
                        "default": default_mode_m.group(1).strip() if default_mode_m else "auto",
                        "options": options,
                    }
                }
            },
        },
        "permissions": {"contents": perm_m.group(1).strip()} if perm_m else {},
        "concurrency": {"cancel-in-progress": (cancel_m.group(1).lower() == "true") if cancel_m else True},
        "jobs": {
            "monitor-notices": {
                "if": mon_if.group(1).strip() if mon_if else "",
                "timeout-minutes": int(mon_to.group(1)) if mon_to else 10,
                "steps": _parse_steps(mon_block),
                "_raw": mon_block,
            },
            "rrcb-probe": {
                "if": probe_if.group(1).strip() if probe_if else "",
                "permissions": {"contents": probe_perm.group(1).strip()} if probe_perm else {},
                "steps": _parse_steps(probe_block),
                "_raw": probe_block,
            },
            "ping": {
                "if": ping_if.group(1).strip() if ping_if else "",
                "permissions": {} if "permissions: {}" in ping_block else {"contents": "unknown"},
                "steps": _parse_steps(ping_block),
                "_raw": ping_block,
            },
        },
    }


def _load_yaml_doc(text: str) -> Dict[str, Any]:
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text)
    except ImportError:
        return _parse_workflow_fallback(text)


def _dump_yaml_obj(obj: Any) -> str:
    try:
        import yaml  # type: ignore
        return yaml.safe_dump(obj)
    except ImportError:
        if isinstance(obj, dict) and "_raw" in obj:
            return str(obj["_raw"])
        return json.dumps(obj)


@unittest.skipIf(_find_workflow() is None, "workflow file not found")
class WorkflowStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.path = _find_workflow()
        with open(cls.path, encoding="utf-8") as fh:  # type: ignore[arg-type]
            cls.text = fh.read()
        cls.doc = _load_yaml_doc(cls.text)

    def _steps(self) -> List[Dict[str, Any]]:
        self.assertIsNotNone(self.doc)
        return self.doc["jobs"]["monitor-notices"]["steps"]

    def _step(self, name_fragment: str) -> Dict[str, Any]:
        for step in self._steps():
            if name_fragment.lower() in str(step.get("name", "")).lower():
                return step
        self.fail(f"step containing {name_fragment!r} not found")

    def test_triggers_permissions_and_concurrency(self) -> None:
        self.assertIsNotNone(self.doc)
        triggers = self.doc.get("on", self.doc.get(True))  # PyYAML parses bare `on` as True
        self.assertIn("workflow_dispatch", triggers)
        self.assertIn("schedule", triggers)
        modes = triggers["workflow_dispatch"]["inputs"]["mode"]
        self.assertEqual(modes["default"], "auto")
        self.assertEqual(set(modes["options"]), {"auto", "baseline", "monitor", "live_test", "rrcb_probe", "ping"})
        self.assertEqual(self.doc["permissions"], {"contents": "write"})
        conc = self.doc["concurrency"]
        self.assertIs(conc["cancel-in-progress"], False)
        job = self.doc["jobs"]["monitor-notices"]
        self.assertLessEqual(int(job["timeout-minutes"]), 10)

    def test_no_hardcoded_secrets(self) -> None:
        self.assertIsNone(re.search(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b", self.text))
        self.assertIn("${{ secrets.TELEGRAM_BOT_TOKEN }}", self.text)
        self.assertIn("${{ secrets.TELEGRAM_CHAT_ID }}", self.text)

    def test_pre_run_merge_failure_is_not_swallowed(self) -> None:
        """Pre-audit: `python main.py --merge-state ... || true` hid a corrupt/unmergeable state file."""
        monitor_script = self._step("Run government notice monitor")["run"]
        self.assertIsNone(re.search(r"--merge-state[^\n]*\|\|\s*true", monitor_script))
        self.assertIn("--merge-state", monitor_script)

    def test_state_is_persisted_even_when_monitor_step_is_cancelled_or_times_out(self) -> None:
        """Pre-audit: persist step had no `if:`, so a cancel/timeout after Telegram sends lost the sent-ledger."""
        persist = self._step("Persist and push state")
        condition = str(persist.get("if", ""))
        self.assertIn("always()", condition)
        self.assertIn("steps.monitor.outcome != 'skipped'", condition)
        # fatal state-corruption / missing-credential exits must still NOT be committed
        self.assertIn("monitor_exit_code != '2'", condition)
        self.assertIn("monitor_exit_code != '3'", condition)

    def test_final_status_step_is_truthful(self) -> None:
        final = self._step("Evaluate final workflow status")
        self.assertIn("always()", str(final.get("if", "")))
        script = final["run"]
        self.assertNotIn("all websites checked", script.lower())
        self.assertIn("did not complete", script.lower())  # empty exit code (cancel/timeout) is handled explicitly

    def test_monitor_step_invokes_expected_cli(self) -> None:
        script = self._step("Run government notice monitor")["run"]
        self.assertIn('--mode "${RUN_MODE}"', script)
        self.assertIn("--state-file data/notice_state.json", script)
        parser_help = app.parse_args(["--mode", "monitor", "--state-file", "x.json", "--allow-partial-source-failures"])
        self.assertEqual(parser_help.mode, "monitor")
        self.assertTrue(parser_help.allow_partial_source_failures)


@unittest.skipIf(_find_workflow() is None, "workflow file not found")
class PingJobTests(unittest.TestCase):
    """The cron-job.org test target (mode=ping) must be provably side-effect free."""

    def test_ping_job_has_no_secrets_checkout_or_write_access_and_runs_only_for_ping(self) -> None:
        with open(_find_workflow(), encoding="utf-8") as fh:  # type: ignore[arg-type]
            doc = _load_yaml_doc(fh.read())
        job = doc["jobs"]["ping"]
        self.assertEqual(job["if"], "${{ inputs.mode == 'ping' }}")
        self.assertEqual(job["permissions"], {})
        dumped = _dump_yaml_obj(job)
        self.assertNotIn("secrets.", dumped)
        self.assertNotIn("notice_state", dumped)
        self.assertNotIn("git push", dumped)
        for step in job["steps"]:
            self.assertNotIn("uses", step, "ping job must not use any action (checkout etc.)")


# ---------------------------------------------------------------------------------------------
# 7. Telegram configuration errors must not consume notices
# ---------------------------------------------------------------------------------------------
class _SwitchableNotifier(RecordingNotifier):
    """Rejects every send with the given error until `error` is set to None (simulating fixed credentials)."""

    def __init__(self, error: Optional[app.TelegramDeliveryError]) -> None:
        super().__init__()
        self.error = error

    def send_notice(self, notice_record: Dict[str, Any], is_live_test: bool = False) -> str:
        if self.error is not None:
            raise self.error
        return super().send_notice(notice_record, is_live_test=is_live_test)


class TelegramConfigErrorTests(unittest.TestCase):
    FEED_URL = "https://rrb.indianrailways.gov.in/getdata?loc="
    TITLE = "CEN 77/2026 Result of CBT-1 shortlisted candidates"

    def test_classifier(self) -> None:
        config = [(400, "Bad Request: chat not found"), (401, "Unauthorized"), (404, "Not Found"),
                  (403, "Forbidden: bot was blocked by the user"), (403, "Forbidden: bot is not a member of the channel chat"),
                  (400, "Bad Request: not enough rights to send text messages to the chat")]
        message_specific = [(400, "Bad Request: can't parse entities: Unsupported start tag"), (400, "Bad Request: message is too long")]
        for status, text in config:
            self.assertTrue(app.is_telegram_config_error(status, text), (status, text))
        for status, text in message_specific:
            self.assertFalse(app.is_telegram_config_error(status, text), (status, text))

    def test_notifier_tags_http_rejections(self) -> None:
        notifier = app.TelegramNotifier(bot_token="123456789:AAH_TestTokenValue1234567890", chat_id="-100123",
                                        max_retries=1, sleep_fn=lambda _: None)
        cases = [(400, b'{"ok":false,"description":"Bad Request: chat not found"}', True),
                 (401, b'{"ok":false,"description":"Unauthorized"}', True),
                 (400, b'{"ok":false,"description":"Bad Request: can\'t parse entities"}', False)]
        for status, body, expected in cases:
            with mock.patch.object(notifier, "_post_json", return_value=(status, {}, body)):
                with self.assertRaises(app.TelegramDeliveryError) as ctx:
                    notifier._send_html_message("<b>x</b>")
            self.assertTrue(ctx.exception.permanent)
            self.assertEqual(ctx.exception.config_error, expected, (status, body))

    def _setup(self) -> Any:
        tmp = tempfile.mkdtemp(prefix="tg-cfg-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "notice_state.json")
        st = app.create_initial_state()
        st["initialized"] = True
        app.save_state_atomically(st, path)
        return path

    def _run(self, path: str, notifier: Any, with_new: bool) -> Any:
        rows = [{"id": 1, "title": "CEN 01/2026 Admit Card for CBT-1 examination", "link": "/a.pdf"}]
        if with_new:
            rows.append({"id": 2, "title": self.TITLE, "link": "/b.pdf", "date": "2020-01-01"})
        client = FakeHttpClient(routes={self.FEED_URL: (200, {"Content-Type": "application/json"}, json.dumps(rows))}, max_retries=1)
        return app.run_monitor(state_path=path, mode="monitor", sources=(app.SOURCE_BY_ID["rrb_rrcb"],), http_client=client,
                               notifier=notifier, reference_date=TODAY, max_age_days=0)

    def _record(self, path: str) -> Dict[str, Any]:
        st = app.load_state(path, allow_init_if_missing=False)
        return next(r for r in st["notices"].values() if r["title"] == self.TITLE)

    def test_notice_survives_credential_outage_and_is_delivered_after_fix(self) -> None:
        """Pre-audit: the first 'chat not found' marked the notice baselined forever; fixing credentials never delivered it."""
        path = self._setup()
        self._run(path, RecordingNotifier(), with_new=False)  # silent baseline
        broken = _SwitchableNotifier(app.TelegramDeliveryError("Permanent Telegram API error (HTTP 400): Bad Request: chat not found",
                                                                permanent=True, config_error=True))
        for _ in range(app.MAX_NOTICE_DELIVERY_ATTEMPTS + 2):  # more failed runs than the old retry limit
            summary, _ = self._run(path, broken, with_new=True)
            self.assertEqual(summary.telegram_delivery_failures, 1)
            rec = self._record(path)
            self.assertEqual((rec["delivery_status"], rec["ledger_status"]), ("failed", "failed"))
            self.assertEqual(int(rec["delivery_attempts"]), 0)
            self.assertIn(rec["fingerprint"], app.load_state(path, allow_init_if_missing=False)["pending_queue"])
        broken.error = None  # credentials fixed
        summary, _ = self._run(path, broken, with_new=True)
        self.assertEqual(summary.telegram_deliveries_confirmed, 1)
        self.assertEqual([r["title"] for r, _, _ in broken.sent_notices], [self.TITLE])
        rec = self._record(path)
        self.assertEqual((rec["delivery_status"], rec["ledger_status"]), ("delivered", "sent"))
        again = RecordingNotifier()
        self._run(path, again, with_new=True)
        self.assertEqual(len(again.sent_notices), 0)  # delivered exactly once

    def test_message_specific_permanent_error_still_stops_retries(self) -> None:
        """A message Telegram can never accept (bad HTML) must still be suppressed, not retried forever."""
        path = self._setup()
        self._run(path, RecordingNotifier(), with_new=False)
        bad = _SwitchableNotifier(app.TelegramDeliveryError("Permanent Telegram API error (HTTP 400): can't parse entities",
                                                             permanent=True, config_error=False))
        self._run(path, bad, with_new=True)
        rec = self._record(path)
        self.assertEqual((rec["delivery_status"], rec["ledger_status"]), ("baselined", "baselined"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
