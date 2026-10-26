# govt-notice-telegram-alerts (v5 — 10 Configured Portals)

Production Python 3.12 and GitHub Actions monitor for **10 official Indian government recruitment, examination, and public-service commission websites (9 state/central boards plus the RRB/RRCB central feed)**. Detects genuinely new official notices and sends one Telegram message per new notice while maintaining a Git-tracked JSON state file (`data/notice_state.json`).

---

## 1. Monitored Official Websites (10 Configured Targets: 9 original + RRB/RRCB)

> **Status wording:** the "Live Runner Verification Status" column below is historical documentation. It was **not re-verified** by the 2026-10-25 audit (the audit environment had no outbound network). Use `python main.py --live-check-json` from a real runner and read `run_outcome`, `data_origin` and each source's `request_attempted` / `adapter_completed` fields for the actual state (section 7).

| # | Source ID | Organization | Primary URL | Official Notice / API Endpoints | Adapter | Live Runner Verification Status |
|---|---|---|---|---|---|---|
| 1 | `ssc` | Staff Selection Commission (SSC) | `https://ssc.gov.in/` | `https://ssc.gov.in/api/general-website/portal/notice-boards` | `adapter_ssc` | Previously reported live-verified (unconfirmed) — Queries SSC JSON API with attachment path resolution (**100 notices in baseline**) |
| 2 | `upsc` | Union Public Service Commission (UPSC) | `https://www.upsc.gov.in/` | `/whats-new`, `/examinations/active-exams`, `/recruitment/...` | `adapter_upsc` | Previously reported live-verified (unconfirmed) — Parses UPSC What's New & Active Exam feeds (**57 notices in baseline**) |
| 3 | `uppsc` | Uttar Pradesh Public Service Commission (UPPSC) | `https://uppsc.up.nic.in/` | `/CandidatePages/Notifications.aspx`, `/` | `adapter_uppsc` | Previously reported live-verified (unconfirmed) — Parses UPPSC Notifications & `Open_PDF_DB.aspx` rows (**50 notices in baseline**) |
| 4 | `hssc` | Haryana Staff Selection Commission (HSSC) | `https://www.hssc.gov.in/` | `/PublicNotices`, `/Advertisements`, `/Result` | `adapter_hssc` | Previously reported live-verified (unconfirmed) — Parses HSSC chronological notice tables with row-context extraction (**41 notices in baseline**) |
| 5 | `rssb` | Rajasthan Staff Selection Board (RSSB) | `https://rssb.rajasthan.gov.in/` | `https://recruitment.rajasthan.gov.in/`, `/news`, `/advertisements` | `adapter_rssb` | Previously reported live-verified (unconfirmed) — Official Rajasthan State Recruitment Portal + legacy TLS renegotiation (**7 notices in baseline**) |
| 6 | `mpesb` | Madhya Pradesh Employees Selection Board (MPESB) | `https://esb.mp.gov.in/` | `/e_default.html`, `https://esb.mponline.gov.in/` | `adapter_mpesb` | Adapter present (live reachability unconfirmed; see section 6) — Parses MPESB Latest Updates and MPOnline vacancy notices |
| 7 | `dsssb` | Delhi Subordinate Services Selection Board (DSSSB) | `https://dsssb.delhi.gov.in/` | `https://dsssbonline.nic.in/`, `/High-Light`, `/latest-vacancies` | `adapter_dsssb` | Previously reported live-verified (unconfirmed) — Official OARS portal (`dsssbonline.nic.in`) active Post Code vacancies & eAdmit Cards (**9 notices in baseline**) |
| 8 | `upprpb` | Uttar Pradesh Police Recruitment and Promotion Board (UPPRPB) | `https://uppbpb.gov.in/` | `/Home/Notice`, `/Home/DirectRecruitment`, `/` | `adapter_upprpb` | Previously reported live-verified (unconfirmed) — Parses UPPRPB notice board and official exam links (**38 notices in baseline**) |
| 9 | `ukpsc` | Uttarakhand Public Service Commission (UKPSC) | `https://psc.uk.gov.in/` | `https://ukpsc.net.in/`, `/public/announcement`, `/candidate-corner/recruitment` | `adapter_ukpsc` | Previously reported live-verified (unconfirmed) — Official UKPSC examination portal (`ukpsc.net.in`) React bundle parser with SPA `#/` preservation (**8 notices in baseline**) |
| 10 | `rrb_rrcb` | Railway Recruitment Boards (RRB portal feed) | `https://rrb.indianrailways.gov.in/` | `https://rrb.indianrailways.gov.in/getdata?loc=` (base endpoint, empty `loc`) | `adapter_rrb_rrcb` | **NOT live-verified.** Parser/state/delivery logic is covered by synthetic-fixture tests only; run the *RRCB endpoint probe* workflow on a real runner first (section 8). Publication date is informational for this source. |

---

## 2. Excluded & Pruned Portals

- **Excluded in 9-Site Configuration (`crpf`, `cisf`, `ssb`):** Central Reserve Police Force (`https://rect.crpf.gov.in/`), Central Industrial Security Force (`https://cisfrectt.cisf.gov.in/`), and Sashastra Seema Bal (`https://ssb.gov.in/`) have been removed from active monitoring while preserving all retained websites' state history and duplicate detection.
- **Inaccessible Portals Pruned Earlier (`upsssc`, `bpsc`, `bssc`, `hppsc`, `bsf`, `itbp`, `assam_rifles`):**

| Source ID | Organization | Primary URL | Root-Cause Analysis |
|---|---|---|---|
| `upsssc` | UPSSSC | `https://upsssc.gov.in/` | UP State Data Center (`164.100.181.233`) drops TCP SYN packets on ports 80/443 from non-Indian cloud IPs (Connection timeout). |
| `bpsc` | BPSC | `https://bpsc.bihar.gov.in/` | NIC Bihar perimeter firewall (`164.100.251.14` & `164.100.130.21`) sends immediate TCP `RST` (`errno 104`) to non-Indian cloud IPs (Connection reset). |
| `bssc` | BSSC | `https://bssc.bihar.gov.in/` | NIC Bihar firewall (`164.100.137.211`) sends immediate TCP `RST` (`errno 104`) to non-Indian cloud IPs (Connection reset). |
| `hppsc` | HPPSC | `https://hppsc.hp.gov.in/` | Authoritative `.hp.gov.in` DNS servers return `SERVFAIL` on bare domain queries from public resolvers and port 443 drops packets from cloud IPs. |
| `bsf` | BSF | `https://rectt.bsf.gov.in/` | NIC WAF returns `403 Forbidden` / connection timeout on cloud IPs; `bsf.mha.gov.in` is NXDOMAIN. |
| `itbp` | ITBP | `https://recruitment.itbpolice.nic.in/` | NIC servers (`164.100.158.135` & `164.100.58.234`) drop TCP SYN packets on port 443 from cloud IPs (Connection timeout). |
| `assam_rifles` | Assam Rifles | `https://www.assamrifles.gov.in/` | Server (`164.100.149.188`) drops TCP SYN packets on ports 80/443 from non-Indian cloud IPs (Connection timeout). |

---

## 3. Open-Category Official Notice Acceptance, Informational Publication-Date Handling & Duplicate Prevention

### 3.1 All Official Notification Types Accepted (5 Primary Categories + Open `"Official Notice"` Fallback)
Every genuine official notification published on the 10 monitored portals is eligible for discovery, deduplication, and Telegram delivery—never restricted solely to a closed keyword whitelist:

- **Recruitment:** `new vacancy`, `recruitment`, `notification`, `advertisement`, `apply online`, `application form`, `posts`
- **Examination:** `exam date`, `examination date`, `exam schedule`, `admit card`, `hall ticket`, `city intimation`
- **Results:** `result`, `scorecard`, `merit list`, `cut off`, `cutoff`, `selection list`
- **Answer Keys:** `answer key`, `answerkey`, `provisional answer key`, `final answer key`, `response sheet`
- **Next Stages:** `DV`, `document verification`, `skill test`, `typing test`, `interview`, `PET`, `PST`, `medical examination`
- **Official Notice (Open Fallback Category):** Any genuine official notice that does not match one of the 5 specific categories above—including **corrigenda, addenda, errata, amendments, revised notices, important notices, press notes, deadline extensions, cancellations, postponements, rescheduling notices, court/tribunal updates, counselling/zone allocation/panel notices, biometric/OTR/scribe instructions, fee-refund notices, or any newly introduced official notification type** on a monitored notice board or official document link—is accepted and tagged with category `Official Notice`.

**Contextual False-Positive & Boilerplate Protection:**
- Automatically rejects tenders (`NIT`, `e-Tender`, `RFP`, `RFQ`, `GeM bid`), auctions, vendor/hospital empanelments, staff transfer/posting orders, seniority/gradation lists, pension adalats, holiday notifications, and annual reports.
- Rejects generic website navigation/chrome, utility links (`About Us`, `Contact Us`, `Sitemap`, `Screen Reader Access`), and portal maintenance/downtime banners (`Website is under maintenance`, `Scheduled maintenance downtime`).
- Validates ambiguous words (`posts`, `notification`, `DV`, `interview`, `PET`, `PST`) so unrelated phrases (such as *"sent by Speed Post / Registered Post"*, *"Ex-Post Facto"*, *"Holiday / Tariff Notification"*, *"Disbursement Voucher (DV No.)"*, or *"Media Interview"*) never trigger job alerts.

### 3.2 Informational Publication-Date Handling (All 10 Sources)
Every production Telegram alert includes the **Organization**, **Notice Title**, **Category**, **Publication Date**, **Official Notice URL**, and **Application / Examination Dates** when available:
- **No Publication-Date Eligibility Filtering (`date_policy = "informational"`):** All 10 configured sources use an informational publication-date policy. Notices are **never** skipped, suppressed, or dropped from the pending queue because their publication date is older than today, missing, or unparseable.
- **Normalize or Preserve:** When a publication date can be reliably parsed (including timezone-aware UTC → `Asia/Kolkata` ISO-8601 timestamps), it is normalized to `YYYY-MM-DD`. When a date string contains date digits that cannot be unambiguously normalized (e.g. `"Oct 2026"` or `"15/10/26"`), the cleaned raw string is preserved. When no publication date is available or a 4-digit-year calendar date is impossible (e.g. `"31-02-2026"`), `publication_date` is stored as `None` and formatted in Telegram alerts as `📅 <b>Publication Date:</b> Not available`. Today's date or fetch time is never fabricated as a fallback.

### 3.3 5-Layer Notice Identity & Persistent Delivery Ledger
- **5-Layer Identity Matching:** Deduplicates notices across (1) SHA-256 fingerprint, (2) official notice/advertisement/document ID (`source_identifier`), (3) canonical direct PDF/document URL (`canonical_pdf_url`), (4) content hash (`content_hash`), and (5) canonical title (`canonical_title`) with `www.` normalization, tracking/cache-busting query removal (`utm_*`, `_t`, `v`, `ver`, `timestamp`, `cachebust`), and table serial-number/badge stripping.
- **Persistent Delivery Ledger:** Tracks `ledger_status` (`baselined`, `pending`, `claimed`, `sent`, `failed`, `ambiguous`), `telegram_message_id`, `delivered_at`, `first_seen_at`, `publication_date`, and `eligibility_status`, persisting state atomically immediately after each confirmed Telegram send.
- **Ambiguous Timeout Protection:** If a request times out after HTTP POST transmission (or returns HTTP 200 with a truncated body), the notice is recorded as `ledger_status = "ambiguous"` (`delivery_status = "delivered"`) and is never blindly resent.
- **Concurrency Queue Stale-SHA Prevention:** `.github/workflows/main.yml` runs `git fetch origin "${GITHUB_REF_NAME}" && git reset --hard "origin/${GITHUB_REF_NAME}"` after acquiring the concurrency lock so overlapping `schedule` and `cron-job.org` `workflow_dispatch` runs always load the freshest committed `data/notice_state.json`.

### 3.4 Fast Concurrent Scraping, No-Op Commit Suppression & Pending Queue
- **Concurrent Source Fetching:** Scrapes all 10 official sources in parallel via `ThreadPoolExecutor` with split `(10, 20)`s `(connect_timeout, read_timeout)` socket timeouts while processing results in deterministic source order.
- **No-Op Commit Suppression:** Skips rewriting `data/notice_state.json` when no meaningful state changed, eliminating unnecessary Git commits and push conflicts on quiet 10-minute runs.
- **Per-Run Cap & Persistent Pending Queue:** Enforces a maximum of **10 production notifications per run** (`MAX_PRODUCTION_ALERTS_PER_RUN = 10`), persisting overflow notices in `pending_queue` for delivery on subsequent runs without dropping older or dateless notices.

---

## 4. GitHub Actions Workflow & Configuration

1. In your GitHub repository, navigate to **Settings → Secrets and variables → Actions → Repository secrets**.
2. Add your secrets:
   - `TELEGRAM_BOT_TOKEN`: Your Telegram Bot API token from `@BotFather`.
   - `TELEGRAM_CHAT_ID`: Your Telegram group, supergroup, or channel ID (e.g. `-100xxxxxxxxxx`).
3. Optional environment variables / CLI flags:
   - `MAX_ALERTS_PER_RUN` / `--max-alerts-per-run`: Maximum production notifications sent per run (default: `10`).
   - `--dry-run`: Run scraping, deduplication, and keyword validation without sending Telegram messages or modifying `data/notice_state.json`.
   - `NOTICE_KEYWORDS_JSON` / `--keywords-file`: Custom JSON mapping of category names to keyword arrays.
   - `NOTICE_EXTRA_KEYWORDS`: Comma-separated extra recruitment keywords.
4. Under the **Actions** tab, the workflow runs on schedule every 10 minutes (`*/10 * * * *`) or via manual `workflow_dispatch` (`auto`, `monitor`, `baseline`, `live_test`, `rrcb_probe`, `ping`).

---

## 5. cron-job.org Trigger (GitHub `workflow_dispatch`)

**Execution model (from the repository itself):** this repo contains no custom HTTP endpoint, so cron-job.org must call GitHub's REST API "Create a workflow dispatch event" endpoint directly. `.github/workflows/main.yml` exposes `workflow_dispatch`, and its `schedule` trigger (`*/10 * * * *`) remains as a second, independent trigger.

| Setting | Value |
|---|---|
| URL | `https://api.github.com/repos/<OWNER>/<REPO>/actions/workflows/main.yml/dispatches` (the workflow file name is accepted as the workflow id) |
| Method | `POST` |
| Header | `Authorization: Bearer <FINE_GRAINED_TOKEN>` |
| Header | `Accept: application/vnd.github+json` |
| Header | `X-GitHub-Api-Version: 2022-11-28` |
| Header | `Content-Type: application/json` |
| Body | `{"ref":"main"}` — **do not send `inputs`.** The workflow then defaults to `mode=auto` (monitor once state is initialized). Never schedule `baseline` or `live_test` from cron: `live_test` sends test messages to your real chat. |
| Schedule | every 10 minutes at minutes `3,13,23,33,43,53` (offset from GitHub's own `:00` schedule slots) |
| Success criterion | HTTP `204 No Content` with an empty body |
| Failure notifications | enable on-failure notifications in cron-job.org |

**Token:** create a *fine-grained* personal access token limited to this one repository with repository permission **Actions: Read and write** (GitHub documents `actions:write` for this endpoint; classic tokens would need the broad `repo` scope, so avoid them). Put it only in the cron-job.org header field. Never commit it, paste it in logs, or store it in this repository. Rotate it if it is ever exposed.

**What `204` means:** the dispatch was *accepted and queued*. It does **not** mean the monitor ran, any website was reachable, or state was saved. Confirm completion in **Actions → Government Notice Telegram Monitor** (event `workflow_dispatch`, conclusion green, and read the run's job summary/annotations).

Typical error responses: `401` bad/expired token; `403` token lacks Actions write (or SSO authorization is pending); `404` wrong owner/repo/workflow name *or* a token that cannot see the repository; `422` the ref does not exist, the workflow lacks `workflow_dispatch`, or an invalid input was sent. cron-job.org's request timeout is 30 s, far more than GitHub needs to answer.

**Overlap safety:** the workflow's `concurrency` group serialises runs and keeps at most one pending run, so a `schedule` run and a cron-job.org run can never execute in parallel or race on `data/notice_state.json`.

**Safe test procedure (no alerts, no state change):** `main.yml` contains a side-effect-free `ping` mode (no secrets, no checkout, no write access).
1. Create a *test* cron-job.org job identical to the table above, but with body `{"ref":"main","inputs":{"mode":"ping"}}`, and use cron-job.org's "Test run".
2. Expect `204`, then a green run of the `Confirm dispatch was received` job in the Actions tab. The monitor job is skipped.
3. Only then create/enable the production job (body `{"ref":"main"}`, no inputs) and delete or disable the test job.

---

## 6. Operational Notes & Diagnostics

- **Publication dates are informational across all 10 sources:** Sources that omit publication dates on their notice boards (such as `dsssb`, `rssb`, or partial rows on `upsc`, `uppsc`, `hssc`, `upprpb`) still generate alerts whenever a genuinely new keyword-matching notice appears. The Telegram message shows `Publication Date: Not available` when the source does not provide a date.
- **A green run does not mean every website was reachable.** With `--allow-partial-source-failures` the job exits 0 if at least one website succeeded. Failed websites are listed as `::warning::` annotations and in the job summary.
- **Telegram configuration errors keep notices queued.** If Telegram rejects messages because of the bot/chat setup (wrong token, `chat not found`, bot blocked or without rights), the run goes red (exit 4) but the notice stays queued and is delivered after you fix the secrets. Errors specific to one message (for example unparseable HTML) still stop retries for that notice.
- **Telegram is sent before state is committed.** State is written atomically after each confirmed send, and the workflow also persists it after a cancel/timeout, but if the final `git push` fails three times the next run can repeat alerts.
- The live-check CLI (`--live-check-json`) exits 0 whenever a report is produced; read `ok` / `run_outcome` in the JSON.

---

## 7. Live Website Check Status Fields

| Field | Meaning |
|---|---|
| `ok` | `true` only if at least one website was really fetched, parsed and evaluated |
| `run_outcome` | `ALL_SOURCES_OK`, `PARTIAL_FAILURE`, `ALL_SOURCES_FAILED` or `NO_SOURCES_SELECTED` |
| `data_origin` (report and per source) | `LIVE_FETCH` (adapter returned data), `LIVE_FETCH_FAILED` (request attempted, adapter did not complete) or `NOT_ATTEMPTED` |
| `request_attempted` / `adapter_completed` | whether a request really started / whether the adapter returned parsed notices |
| `failure_stage` | `FETCH_OR_PARSE`, `POST_PROCESSING` (fetched fine, later logic failed), `WORKER_CRASH` or `NOT_ATTEMPTED` |
| `discovered_with_publication_date`, `relevant_with_publication_date`, `date_coverage_warning` | publication-date coverage per source |
| `ledger_entry_count`, `ledger_unique_fingerprint_count`, `ledger_sent_count` | separately labelled ledger counts (entries in `notices`, distinct fingerprints, records with status `sent`/`ambiguous`) |

HTTP requests are bounded by a per-attempt wall-clock deadline (`DEFAULT_REQUEST_DEADLINE_SECONDS`, 30 s) and a per-source budget (`DEFAULT_SOURCE_BUDGET_SECONDS`, 90 s); error categories `REQUEST_DEADLINE_EXCEEDED` and `SOURCE_TIME_BUDGET_EXCEEDED` report them.

---

## 8. RRB Portal Source (`rrb_rrcb`)

- **Official portal & endpoints:** `https://rrb.indianrailways.gov.in/` and `https://rrb.indianrailways.gov.in/getdata?loc=`. `adapter_rrb_rrcb` tries the `getdata?loc=` feed with browser/XHR headers and falls back to fetching the unified portal homepage (`https://rrb.indianrailways.gov.in/`) and any dynamically discovered `getdata?loc=...` endpoints embedded in the homepage scripts/links.
- **Split connect/read timeouts:** `HttpClient` applies `connect_timeout` (`10s`) strictly to TCP + TLS establishment and switches the socket to `read_timeout` (`20s`) before waiting for HTTP response headers/body, preventing premature `CONNECTION_TIMEOUT` failures when `getdata` takes >6s to generate its response after TLS handshake.
- **Genuine empty response vs. failure:** Binary content types are rejected (`UNEXPECTED_CONTENT_TYPE`); text is decoded as UTF-8 first (Devanagari-safe). Genuine empty responses (`[]`, `{"data": []}`, `{"records": []}`, the Hindi `"कोई अभिलेख उपलब्ध नहीं है!"` message, or a recognized empty HTML notice board) return an empty notice list (`status="ok"`, `discovered=0`, `test_outcome="NO_NOTICE_FOUND"`). Blank/whitespace bodies, invalid JSON, WAF/challenge pages, unrendered SPA shells without fallback notices, or unrecognized schemas raise `SourceFetchError` (`EMPTY_RESPONSE`, `INVALID_JSON`, `ACCESS_DENIED_PAGE`, `UNRECOGNIZED_STRUCTURE`).
- **Links:** only `rrb.indianrailways.gov.in`, `indianrailways.gov.in`, `rrbapply.gov.in` and TCS iON (`*.digialm.com`); look-alike hosts, retired regional RRB domains and anything else are dropped; a redirect away from the portal host is an error.
- **First non-empty run baselines safely:** if `rrb_rrcb` has not completed its baseline yet and returns an empty list (`0` notices), `baseline_completed` remains `false` so the first non-empty response baselines existing notices without flooding Telegram.
- **Identity:** each notice has its own identifier (the feed's record id, else a hash of its document URL). The CEN number is deliberately not used, because one CEN has many notices.
- **RRB endpoint probe (read-only):** Actions -> *Government Notice Telegram Monitor* -> Run workflow -> `mode = rrcb_probe`. Only the probe job runs: no secrets, no Telegram, no commits, no state change.
- **Optional relay (off by default):** if direct access from GitHub is blocked at the TCP/IP layer by NIC/CRIS firewall rules, set the repository secret `RRCB_RELAY_URL_TEMPLATE` to an `https://` URL containing `{url}`. It is used only after a network-type failure of the direct request (timeout, reset, DNS, 5xx), never for 403/404.
