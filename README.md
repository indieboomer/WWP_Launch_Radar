# WWP Launch Radar

Local monitor for the Steam Early Access launch of **Wild West Pioneers** (App ID `3222640`).
It polls the concurrent player count (CCU) and Steam reviews, stores every observation in SQLite, builds
historical aggregates, and serves a dark, Polish-language dashboard with charts, a review feed, event
annotations and one-click exports. Optional AI analysis groups recurring review themes.

Sales, revenue, wishlists and financial Steamworks data are out of scope. The original requirements are
in [`docs/SPEC.md`](docs/SPEC.md).

- **One process:** FastAPI + background collector threads. Collection does not depend on the browser.
  Closing the dashboard does not stop it, and extra tabs do not start extra collectors. An OS-level lock
  file lets only one collector write to a data directory.
- **Storage:** SQLite (WAL mode) with versioned migrations. Raw data is kept forever, and aggregates can be rebuilt from it.
- **No external services** except Steam (and Anthropic, if you enable the AI feature). Chart assets
  (uPlot 1.6.32) are bundled in `wwp_radar/static/vendor`.

---

## 1. Quick start on Windows

Requirements: Windows 10/11, **Python 3.10–3.13** from <https://www.python.org/downloads/windows/>
(tick *“Add python.exe to PATH”* and *“py launcher”*). Tested with Python 3.11.

```bat
setup.bat      :: creates .venv, installs the locked dependencies, creates .env from .env.example
start.bat      :: starts dashboard + collector
```

Open <http://127.0.0.1:8765>. The server binds to **localhost only** by default.

- **Data directory:** `%LOCALAPPDATA%\WWPLaunchRadar` (`radar.sqlite3`, session secret, temp exports).
  It is outside the repository, so it survives upgrades and re-cloning. Override it with `WWP_DATA_DIR`.
- **Collection only runs while `start.bat` is running and the computer is awake.** Sleep, hibernation or
  closing the window create gaps. Gaps show up in the charts, in aggregate coverage and in export
  metadata. Nothing is backfilled for CCU, because Steam has no historical CCU endpoint. After downtime,
  review collection catches up on its own.
  For launch day, turn off sleep in *Settings → System → Power*.
- Launch time: set `WWP_LAUNCH_AT` to a date **and** time, e.g. `2026-10-15 18:00` (read in `WWP_DISPLAY_TZ`)
  or `2026-10-15T18:00:00+02:00`, or use
  **Ustawienia** in the dashboard. It is intentionally empty by default.
- Demo: `demo.bat` writes synthetic data to a **separate** `demo.sqlite3` and starts the dashboard with a
  purple *DANE DEMONSTRACYJNE* banner and no collector. Real data is never touched.

### Optional: start automatically at logon (Task Scheduler)

PowerShell, as your normal user:

```powershell
$dir = "C:\path\to\WWP_Launch_Radar"
$action   = New-ScheduledTaskAction -Execute "$dir\start.bat" -WorkingDirectory $dir
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "WWP Launch Radar" -Action $action -Trigger $trigger -Settings $settings
# remove: Unregister-ScheduledTask -TaskName "WWP Launch Radar" -Confirm:$false
```

### Other commands

```bat
.venv\Scripts\python -m wwp_radar backup D:\radar-backup.sqlite3   :: consistent online backup
.venv\Scripts\pip install -r requirements-dev.txt && .venv\Scripts\python -m pytest   :: tests
```

---

## 2. Configuration

All settings are environment variables (or `.env`). See [`.env.example`](.env.example). The most important ones:

| Variable | Default | Meaning |
|---|---|---|
| `WWP_APP_ID` / `WWP_GAME_NAME` | `3222640` / Wild West Pioneers | Initial game (can be switched in the UI) |
| `WWP_LAUNCH_AT` | empty | Launch date and time (local display time, or with UTC offset). Applied once; later changes go through *Ustawienia* |
| `WWP_DISPLAY_TZ` | `Europe/Warsaw` | Display timezone (storage is always UTC) |
| `WWP_CCU_INTERVAL` / `WWP_REVIEW_INTERVAL` | 60 / 120 s | Poll intervals |
| `WWP_HOST` / `WWP_PORT` | `127.0.0.1` / `8765` | Bind address |
| `WWP_DASHBOARD_PASSWORD` | empty | Enables login. **Required** for any non-localhost bind |
| `WWP_TRUST_PROXY_AUTH` | false | Allow a non-localhost bind without a password when an authenticating reverse proxy protects the app |
| `WWP_DATA_DIR` | `%LOCALAPPDATA%\WWPLaunchRadar`, `/data` in Docker | Database location |
| `WWP_AI_ENABLED`, `ANTHROPIC_API_KEY`, `WWP_AI_MODEL` | off, –, `claude-opus-5` | Optional AI analysis |

**Changing the App ID** (in *Ustawienia*) creates or selects a separate game record. All data is keyed
by game, so earlier histories, checkpoints and annotations stay intact and separate. Exports have a game selector.

---

## 3. Data collection

### Current players (CCU)
`GET https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/?appid=…`, every 60 s,
aligned to a fixed schedule (no drift, and no burst of catch-up calls after sleep).

- A successful response (`result: 1`) is stored in `ccu_observations` with the UTC collection time. A real `0` is stored as `0`.
- Every attempt is logged in `collection_runs` with status `ok`, `error` or `unavailable`. **A failed request is never stored as an observation.**
- `unavailable`: for apps without player data (for example before release), Steam answers **HTTP 404 with
  `{"response":{"result":42}}`**. Verified on 2026-09-25 for App 3222640. This counts as "no data", not zero players.
- Peaks are labelled *“najwyższe zaobserwowane od rozpoczęcia monitoringu”* (highest observed since monitoring started). They are not Steam all-time peaks.
  CCU is not used to infer unique players, retention or units sold.

### Reviews
`GET https://store.steampowered.com/appreviews/<appid>?json=1` with
`language=all&purchase_type=all&review_type=all&filter_offtopic_activity=0&num_per_page=100` and cursor pagination.

Two resumable **walkers** keep the review records complete:

| Walker | Steam `filter` | Purpose |
|---|---|---|
| `recent` | `recent` (newest created first) | The first run is the **historical import**, which walks to the end of the list. Later runs fetch new reviews until they pass the previous high-water mark minus 1 h overlap, following **as many pages as needed** after downtime. |
| `updated` | `updated` (newest updated first) | Starts after the import, from the import start time. It reconciles edits to older reviews that `recent` would not see again. |

- The cursor is checkpointed after **every page** (`checkpoints` table). A restart or crash resumes from the
  saved cursor. The watermark only moves forward once a walk completes, so no pages are skipped. If a cursor fails 3 times
  (it may have expired), the walk restarts from the top. Deduplication makes that safe.
- Import progress (`reviews_seen / expected_total` from Steam's `query_summary`) appears as a status pill. CCU keeps collecting in its own thread during the import.
- Reviews are deduplicated by `recommendationid`. Each record keeps `timestamp_created`, `timestamp_updated`,
  `first_seen_at` (discovery by the monitor) and `last_seen_at` separately.
- **Versions:** a change in text, recommendation, language, playtime at review, purchase/free/EA flags or
  developer response adds an immutable row to `review_versions` (with the raw Steam JSON and
  `observed_at`). Volatile counters such as votes and playtime_forever are updated in place.
- A review missing from an incremental result is **never treated as deleted**.
- **Summary snapshots** (`review_summary_snapshots`): every review poll stores Steam's `query_summary` for two
  explicitly defined populations, each with its exact query parameters. They are never mixed:
  - `all_all`: all languages, all acquisition types (Steam's default off-topic filter)
  - `all_steam`: all languages, Steam purchases only

  Stored review records also carry `steam_purchase` / `received_for_free`, so the feed and exports can
  split Steam purchases from other acquisition types (keys, gifts, free).

### Reliability
Timeouts (20 s), up to 4 retries with exponential backoff and jitter, `Retry-After` handling for 429/503,
at most 2 concurrent Steam requests, and at least 1 s between requests. Per-source health (last success, last
error, consecutive failures) appears in the dashboard (*Źródła danych*, the data sources panel) and in `/api/status`.

---

## 4. Storage, aggregates and their semantics

Timestamps are stored as UTC Unix epoch seconds. Local time is used only for display, for daily buckets and for the `*_local`
export columns. SQLite runs with WAL, `synchronous=NORMAL`, `busy_timeout`, foreign keys and indexes.
Writes happen inside short `BEGIN IMMEDIATE` transactions. The schema is versioned via `PRAGMA user_version`
(`wwp_radar/db.py`). An app older than the database refuses to start instead of damaging it.

Aggregates are rebuilt from raw data at startup and refreshed every 60 s. You can also rebuild them from *Ustawienia → Przebuduj agregaty*:

| Aggregate | Definition |
|---|---|
| **CCU 5m / 1h** | Buckets aligned to UTC multiples. Europe/Warsaw offsets are whole hours, so 1h buckets equal local clock hours. |
| **CCU 1d** | Calendar days in `WWP_DISPLAY_TZ`. DST days last 23 h or 25 h. |
| CCU fields | `min`, `max`, `mean` of successful observations, `samples`, `expected_samples` = monitored seconds in the bucket ÷ poll interval, `coverage` = samples ÷ expected (max 1). Buckets with **no samples still exist**, with NULL values and coverage 0. **Nothing is interpolated.** Charts break the line at gaps (more than 2.5 intervals without a sample). |
| **New reviews 1h / 1d** | Counted by **Steam creation time** (`timestamp_created`), classified by the review's **current** recommendation. Also split into Steam purchases only. Imported history lands at its real creation time, not as a burst at import time. |
| **Sentiment changes** | Counted separately, at the edit time (`timestamp_updated`) of a version whose recommendation differs from the previous *observed* version. |
| **Summary trend 1h / 1d** | The last summary snapshot in each bucket, per population. `positive_pct = positive / (positive + negative)`. |

Review counts in the dashboard are labelled as observed/imported data. Status pills and export metadata show when the import is incomplete.

---

## 5. Dashboard

Cards: current CCU (with freshness), highest observed CCU and its time, CCU change over ~15 and ~60 min
(*niedostępne* (unavailable) unless a sample exists within ±3 min / ±5 min of the reference time), positive/negative counts,
positive % with sample size and an explicit population selector, reviews created in the last hour, and collection health.
Three separate statuses: **application/collector**, **Steam source availability** and **historical import completeness**.

Charts (uPlot, bundled locally): CCU over time (raw ≤ 12 h, then 5-min/hourly/daily mean + max), new positive and negative
reviews per bucket with sentiment-change markers, and positive % from stored summary snapshots. Annotations appear as dashed lines.
Time filters: 1 h, 6 h, 24 h, *Od premiery* (since launch, when configured), *Cały okres* (whole period), and a custom range entered in the display timezone.

Review feed: filters for positive/negative, language, Steam purchase vs. other, edited only, and date range, plus text search.
Each review shows its creation time *and* its discovery time, playtime at review, flags, a link to the original Steam review and the developer response.
Edited reviews open their version history.

---

## 6. Exports (*Eksportuj dane*)

All exports come from stored data. Steam is never queried again. They work while collection continues. Ranges are half-open `[from, to)` in UTC,
and the dialog accepts times in the display timezone.

| # | Dataset | Format | Date filter applies to |
|---|---|---|---|
| 1 | Raw CCU observations | CSV | observation time |
| 2 | CCU aggregates (5m/1h/1d) | CSV | bucket start |
| 3 | Current review records | CSV, JSON | **review creation time** (`timestamp_created`) |
| 4 | Review version history (raw Steam JSON included) | JSON | **version observation time** (`observed_at`) |
| 5 | Review-summary snapshots (with population + query params) | CSV | observation time |
| 6 | Review aggregates | CSV | bucket start |
| 7 | Annotations | CSV | event time |
| 8 | **Analysis ZIP**: all of the above + `metadata.json` | ZIP | per dataset, as above. One consistent read snapshot |
| 9 | **SQLite backup** | .sqlite3 | whole database, via the SQLite online backup API |

CSV files are UTF-8 with BOM, CRLF line endings, and RFC 4180 quoting (multi-line review text is safe). Pick
the semicolon separator for Polish Excel. Text cells starting with `= + - @ Tab CR` get a leading `'` to block
spreadsheet formula injection. JSON keeps the original text. `metadata.json` holds the App ID, game, export time,
range and timezone conventions, dataset/filter definitions, app and schema version, monitoring start, CCU gaps,
import completeness, metric definitions and limitations. Credentials never appear in exports or logs.

---

## 7. Optional AI review analysis

Off by default. The app runs fully without an API key. To enable it, set `WWP_AI_ENABLED=true` and `ANTHROPIC_API_KEY=…`
(and optionally `WWP_AI_MODEL`, default `claude-opus-5`).

- Every 10 min (or when you click *Analizuj teraz*, "analyze now"), at most 3 batches of up to `WWP_AI_BATCH_SIZE` (120) reviews
  whose **current content** has not yet been analysed are sent in one structured-output request (`messages.parse`) with **no tools**.
- The model assigns reviews to a **fixed taxonomy** (crashes, saves, performance, other bugs, tutorial, UI,
  balance, content, price, localization, plus positive themes) and writes short Polish summaries.
- **The application computes all numbers.** Review IDs that are not in the batch are dropped. The dashboard shows unique review counts per
  theme, links to supporting reviews, the analysed time window and the model name, all under a *generowane przez AI* (AI-generated) label.
  The prompt describes findings as player reports, not confirmed bugs.
- Review text goes in as JSON data inside delimiters, and the system prompt says to never follow instructions found in it.
- Runs, model, token usage and supporting review IDs are stored (`ai_runs`, `ai_review_assignments`).
  Unchanged reviews are not reprocessed. Edited reviews are queued again. Failed or refused runs leave reviews pending.
- The API key stays in the backend. It is redacted from stored error messages and never exported.

---

## 8. Docker and Portainer

The image is `python:3.12-slim` and runs as a non-root user. Data lives in a volume at `/data`. There is a `HEALTHCHECK` on `/healthz`,
`restart: unless-stopped`, and graceful shutdown on SIGTERM (`stop_grace_period: 30s`).
Inside the container the app listens on `0.0.0.0`, so **it refuses to start without `WWP_DASHBOARD_PASSWORD`**
unless `WWP_TRUST_PROXY_AUTH=true` is set, which is only for when an authenticating reverse proxy
(e.g. Authelia, oauth2-proxy, Traefik/Caddy basic auth) protects it. The password protects the dashboard, the API,
exports and settings. Only `/healthz` and the login page are public. For access beyond your LAN, add
HTTPS through a reverse proxy.

### Compose (on the Docker host)
```bash
git clone <repo> && cd WWP_Launch_Radar
export WWP_DASHBOARD_PASSWORD='change-me'
docker compose up -d --build            # builds and tags wwp-launch-radar:1.0.0
# LAN access: WWP_PUBLISH_ADDR=0.0.0.0 docker compose up -d
```

### Portainer
`deploy/portainer-stack.yml` references the **pre-built** image `wwp-launch-radar:1.0.0` and needs no build
context. First make the image available to the Docker host that Portainer manages, using one of these:

1. **Build on that host:** `docker build -t wwp-launch-radar:1.0.0 .` (in a checkout on the host).
2. **Copy the image:** on the build machine run
   `docker build -t wwp-launch-radar:1.0.0 . && docker save wwp-launch-radar:1.0.0 -o wwp-launch-radar-1.0.0.tar`,
   then on the host run `docker load -i wwp-launch-radar-1.0.0.tar` (or use Portainer → Images → Import).
   Build for the host's architecture, e.g. `docker buildx build --platform linux/amd64 -t wwp-launch-radar:1.0.0 --load .`
3. **Registry:** tag as `registry.example.com/wwp-launch-radar:1.0.0`, push, and set `WWP_IMAGE` in the stack.

Then go to Portainer → *Stacks* → *Add stack* → paste the file. Set `WWP_DASHBOARD_PASSWORD` (and optionally
`WWP_LAUNCH_AT`, `WWP_PUBLISH_PORT`, `ANTHROPIC_API_KEY`) under *Environment variables* and deploy. The named volume
`wwp-data` holds the database, settings, checkpoints and AI history, and survives container recreation and image upgrades.

### Moving between Windows and Docker
Always move a **consistent backup**, never a live `radar.sqlite3` (its `-wal` file may hold committed data).

- **Windows → Docker:** download *Eksport → Kopia zapasowa bazy SQLite* (or run `python -m wwp_radar backup file.sqlite3`),
  stop the container, and copy it in as `radar.sqlite3`:
  ```bash
  docker compose stop
  docker run --rm -v wwp-launch-radar_wwp-data:/data -v "$PWD":/in alpine \
    sh -c 'rm -f /data/radar.sqlite3-wal /data/radar.sqlite3-shm && cp /in/backup.sqlite3 /data/radar.sqlite3 && chown 10001:10001 /data/radar.sqlite3'
  docker compose start
  ```
- **Docker → Windows:** download the backup from the dashboard, stop `start.bat`, delete any `radar.sqlite3-wal/-shm`
  in `%LOCALAPPDATA%\WWPLaunchRadar`, save the backup there as `radar.sqlite3`, and start again.

Only run one collector for a game at a time. Otherwise both instances record duplicate, interleaved data.

---

## 9. Project layout

```
wwp_radar/
  config.py      settings (.env / environment), exposure guard
  db.py          SQLite connection, migrations, online backup
  steam.py       Steam HTTP client (timeouts, retries, backoff, rate limits, concurrency)
  store.py       data access, review dedup/versioning, health, checkpoints, annotations
  collector.py   collector threads, lock file, resumable review walkers, summaries
  aggregates.py  rebuildable CCU/review/summary aggregates
  exports.py     CSV/JSON/ZIP exports + metadata
  ai.py          optional Claude analysis
  app.py         FastAPI app, API, auth middleware
  static/        dashboard (HTML/CSS/JS, bundled uPlot)
tests/           deterministic tests with a fake Steam backend (httpx.MockTransport)
deploy/          Portainer stack
```

Dependencies are pinned in `requirements.txt` (direct deps in `requirements.in`). Test tools are in `requirements-dev.txt`.

---

## 10. Validation and known limitations

**Automated tests** (`pytest`, 33 tests, deterministic, no network) cover: failed, network-error and HTTP-404
CCU responses never stored as zero, while a real `0` is stored; review deduplication, immutable versions and sentiment-change
flags; import resume from a checkpointed cursor after restart; multi-page catch-up after downtime;
cursor retention on transient errors; reconciliation of edits to old reviews; no deletion on missing reviews;
CCU bucket gaps and coverage, Warsaw hourly alignment and 23/25 h DST days; review aggregates by creation time
with no import burst; CSV BOM, quoting, formula injection, delimiters; date-filter semantics (creation vs. observation
time); ZIP contents and metadata; persistence and idempotent migrations across restarts; consistent backup under
concurrent writes; App ID switching keeping data separate; API endpoints; auth enforcement; AI validation, redaction and
no reprocessing (with a stub client).

**Live smoke test (2026-09-25):** the collector ran against real Steam. For App 3222640 (not yet released) CCU comes back
as *unavailable* (HTTP 404, `result: 42`), so nothing is stored. The review import completed with 0 reviews, and summaries were
stored. Real cursor pagination and summaries were checked on a released game (Hades, 1145360): 3 pages, 300 reviews,
17 languages.

**Not verified here:** building and running the Docker image (Docker Desktop was not running on the development machine;
both compose files pass `docker compose config`), and a live AI call (no API key was used; tests use a stub).

Limitations:
- CCU before monitoring started is not available from Steam. CCU is not tracked while the process is stopped or the PC sleeps.
- A sentiment change made before the monitor first saw a review cannot be known.
- Steam's review API may omit some reviews (e.g. removed or hidden ones), and its `query_summary` counts can differ
  from the number of retrievable records. Both are stored and labelled separately.
- `filter=updated` reconciliation relies on Steam updating `timestamp_updated` on edits. Deleted reviews are not detected.
- One monitored game at a time (history for several games is kept). The app runs as a single process (one uvicorn worker).
