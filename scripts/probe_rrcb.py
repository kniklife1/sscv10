#!/usr/bin/env python3
"""
Read-only probe for https://rrb.indianrailways.gov.in/getdata?loc= (base endpoint, empty loc), meant to run on a real GitHub Actions
runner (see .github/workflows/main.yml, mode=rrcb_probe). It sends NO Telegram messages, uses NO secrets and never reads or
writes data/notice_state.json. Its job is to establish, from the environment that matters:

  1. DNS + TCP reachability of the host (which IPs, how long, did the handshake complete),
  2. what the endpoint really returns (status, headers, size, first bytes) for several request variants,
  3. whether the project's real adapter can parse that real response (record count, sample titles, date coverage),
  4. optionally, whether a relay URL template YOU supply works (nothing is guessed or invented).

All raw bodies are saved to probe_out/ so the true response format can be inspected and turned into a fixture.
"""
from __future__ import annotations

import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HOST = "rrb.indianrailways.gov.in"
ENDPOINT = f"https://{HOST}/getdata?loc="
OLD_ENDPOINT = f"https://{HOST}/getdata?loc=RRCB"  # kept only to compare against the base endpoint
OUT = "probe_out"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/129.0.0.0 Safari/537.36")
SAFE_HEADERS = ("content-type", "content-length", "server", "location", "cache-control", "date", "content-encoding",
                "x-frame-options", "cf-ray", "x-cache", "via", "retry-after")


def section(title: str) -> None:
    print(f"\n===== {title} =====", flush=True)


def dns_and_tcp() -> None:
    section("DNS + TCP reachability")
    try:
        t0 = time.monotonic()
        infos = socket.getaddrinfo(HOST, 443, proto=socket.IPPROTO_TCP)
        addrs = sorted({i[4][0] for i in infos})
        print(f"DNS ok in {time.monotonic() - t0:.2f}s -> {addrs}")
    except OSError as exc:
        print(f"DNS FAILED: {exc!r}")
        return
    for addr in addrs:
        t0 = time.monotonic()
        try:
            with socket.create_connection((addr, 443), timeout=10) as sock:
                print(f"TCP {addr}:443 connected in {time.monotonic() - t0:.2f}s")
                ctx = ssl.create_default_context()
                with ctx.wrap_socket(sock, server_hostname=HOST) as tls:
                    print(f"  TLS ok: {tls.version()} cert-subject={dict(x[0] for x in tls.getpeercert().get('subject', ()))}")
        except Exception as exc:  # noqa: BLE001 - diagnostic output only
            print(f"TCP/TLS {addr}:443 FAILED after {time.monotonic() - t0:.2f}s: {exc!r}")


def fetch_variant(name: str, url: str, headers: dict) -> None:
    section(f"HTTP variant: {name}")
    print(f"GET {url}")
    req = urllib.request.Request(url, headers=headers, method="GET")
    t0 = time.monotonic()
    status, resp_headers, body, final = None, {}, b"", url
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            status, final = resp.status, resp.geturl()
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            body = resp.read(2_000_000)
    except urllib.error.HTTPError as exc:
        status, final = exc.code, exc.geturl()
        resp_headers = {k.lower(): v for k, v in exc.headers.items()}
        body = exc.read(200_000)
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED after {time.monotonic() - t0:.2f}s: {exc!r}")
        return
    print(f"status={status} elapsed={time.monotonic() - t0:.2f}s final_url={final} bytes={len(body)}")
    for key in SAFE_HEADERS:
        if key in resp_headers:
            print(f"  {key}: {resp_headers[key]}")
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, f"body_{name}.bin")
    with open(path, "wb") as fh:
        fh.write(body)
    print(f"saved raw body -> {path}")
    text = body.decode("utf-8", errors="replace")
    try:
        sys.path.insert(0, os.getcwd())
        import main as _app
        print(f"portal 'no records available' message detected: {_app._rrb_is_no_records_message(text.strip())}")
    except Exception as exc:  # noqa: BLE001
        print(f"(no-records detection unavailable: {exc!r})")
    preview = text[:1500].replace("\r", "")
    print("--- first 1500 chars ---\n" + preview + "\n--- end preview ---")


def parse_with_project_adapter() -> None:
    section("Project adapter on the REAL response (read-only)")
    sys.path.insert(0, os.getcwd())
    try:
        import main as app
    except Exception as exc:  # noqa: BLE001
        print(f"cannot import main.py: {exc!r}")
        return
    src = app.SOURCE_BY_ID["rrb_rrcb"]
    client = app.HttpClient()
    client.start_budget(app.DEFAULT_SOURCE_BUDGET_SECONDS)
    t0 = time.monotonic()
    try:
        notices = app.adapter_rrb_rrcb(src, client)
    except app.SourceFetchError as exc:
        print(f"ADAPTER FAILED after {time.monotonic() - t0:.2f}s: category={exc.category} detail={str(exc)[:400]}")
        return
    dated = sum(1 for n in notices if n.publication_date)
    print(f"adapter OK in {time.monotonic() - t0:.2f}s: {len(notices)} notices, {dated} with a parseable date")
    cats = app.load_keyword_categories()
    relevant = [n for n in notices if app.match_notice_keywords(n.title, n.title, categories=cats)[0]]
    print(f"keyword-relevant: {len(relevant)}")
    for n in notices[:12]:
        print(f"  - [{n.publication_date or 'no date'}] {n.title[:110]} -> {n.url[:100]}")


def main() -> int:
    print(f"python={sys.version.split()[0]} runner_os={os.environ.get('RUNNER_OS', 'n/a')} "
          f"runner_name={os.environ.get('RUNNER_NAME', 'n/a')}")
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=8) as r:
            print("runner egress IP:", r.read().decode().strip())
    except Exception as exc:  # noqa: BLE001
        print("runner egress IP lookup failed:", repr(exc))
    dns_and_tcp()
    fetch_variant("A_browser_ua", ENDPOINT, {"User-Agent": BROWSER_UA, "Accept": "application/json, text/html, */*;q=0.8"})
    fetch_variant("B_xhr_referer", ENDPOINT, {"User-Agent": BROWSER_UA, "Accept": "application/json, text/html, */*;q=0.8",
                                              "Referer": f"https://{HOST}/", "X-Requested-With": "XMLHttpRequest"})
    fetch_variant("E_old_loc_RRCB_comparison", OLD_ENDPOINT, {"User-Agent": BROWSER_UA, "Accept": "application/json, text/html, */*;q=0.8"})
    fetch_variant("C_homepage", f"https://{HOST}/", {"User-Agent": BROWSER_UA, "Accept": "text/html,*/*;q=0.8"})
    template = os.environ.get("PROBE_RELAY_URL_TEMPLATE", "").strip()
    if template.startswith("https://") and "{url}" in template:
        fetch_variant("D_user_supplied_relay", template.replace("{url}", urllib.parse.quote(ENDPOINT, safe="")),
                      {"User-Agent": BROWSER_UA, "Accept": "application/json, text/html, */*;q=0.8"})
    elif template:
        print("\nPROBE_RELAY_URL_TEMPLATE ignored: it must start with https:// and contain {url}")
    parse_with_project_adapter()
    print("\nProbe finished. Download the 'rrcb-probe-output' artifact for the raw response bodies.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
