Build a complete, working application called **WWP Launch Radar** for monitoring the Early Access launch of Wild West Pioneers on Steam.

Implement the application, not just a plan. Make reasonable implementation decisions autonomously. Prioritize reliable collection, durable historical data, and a usable dashboard.

## 1. Purpose and scope

The application must:
- Display automatically refreshed player counts and Steam reviews.
- Continuously store source observations for later analysis.
- Produce historical aggregates and charts.
- Export data directly from the interface.
- Run locally on Windows without Docker.
- Also run as a Linux Docker container, deployable through Docker Compose and Portainer.

Exclude sales, revenue, wishlists, and financial Steamworks integrations.

Default configuration:
- Game: Wild West Pioneers.
- Steam App ID: `3222640`.
- Display timezone: `Europe/Warsaw`.
- CCU polling: every 60 seconds.
- Review polling: every 120 seconds.
- Launch timestamp: user-configurable; do not invent the launch hour.

Keep the architecture simple and suitable for one monitored game, while allowing the App ID to be changed.

## 2. Suggested architecture

Prefer:
- Python with FastAPI.
- SQLite for persistent storage.
- A lightweight web interface with locally bundled chart assets.
- One application instance and one collection scheduler.

Avoid unnecessary infrastructure such as Redis or a separate database server.

Collection must run independently of browser sessions. Closing the dashboard must not stop collection. Multiple open tabs must not create additional collectors.

Use a supported dependency set, lock dependencies, and verify current official API documentation during implementation.

## 3. Steam data collection

Use official Steam interfaces where available:

- Current players:
  `https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/?appid=3222640`
- Reviews:
  `https://store.steampowered.com/appreviews/3222640?json=1`

Fetch upstream data through the backend.

Implement:
- Request timeouts, bounded retries, exponential backoff, and rate-limit handling.
- Clear distinction between a successful zero result and a failed request.
- Per-source health, last successful fetch, and last error.
- Persistent collection checkpoints and restart recovery.
- Bounded request concurrency.

Do not fabricate data when Steam is unavailable.

### Current players

Store every successful observation with its UTC collection timestamp and player count.

Record collection failures separately. A failed request must never become a zero-player observation.

Historical player counts before monitoring started are unavailable from this endpoint. Label peaks as “highest observed since monitoring started,” not Steam all-time peaks.

Do not infer unique players, retention, or units sold from CCU.

### Reviews

On initial setup, import available review history using cursor pagination, with visible progress. Continue collecting CCU during this import.

Fetch all languages and all purchase types for review analysis. Preserve fields that allow Steam purchases and other acquisition types to be filtered separately.

Handle both newly created reviews and edits:
- Incrementally fetch recently created reviews.
- Separately reconcile recently updated reviews.
- Deduplicate by Steam recommendation ID.
- Preserve previous versions when relevant content or sentiment changes.
- Persist review creation time, update time, first-seen time, and observation time separately.
- Ensure polling catches up through all required pages after downtime; do not fetch only the first page.
- Do not treat a review missing from an incremental result as deleted.

Store relevant source fields including:
- Review ID and text.
- Positive/negative recommendation.
- Language.
- Playtime at review, when available.
- Steam purchase and received-for-free flags.
- Early Access flag.
- Developer response, when available.
- Source timestamps.

Store aggregate review-summary snapshots separately with their exact query/filter settings. Never silently mix summary counts from different review populations.

## 4. Persistence and aggregates

Persistence is a core requirement, not an optional feature.

Retain raw observations and review versions indefinitely by default. Never replace raw data with aggregates.

Use a versioned database schema, migrations, indexes, transactions, and appropriate SQLite concurrency settings.

Suggested entities:
- Monitored games and settings.
- Collection runs/errors and checkpoints.
- CCU observations.
- Review records and immutable versions.
- Review-summary snapshots.
- Aggregate buckets.
- Manual event annotations.
- Optional AI analysis runs.

Store timestamps in UTC and convert only for display. Exports must include UTC timestamps.

Produce rebuildable aggregates for:
- 5-minute, hourly, and daily CCU buckets: minimum, maximum, mean, sample count, and coverage.
- Newly created positive and negative reviews by hour/day.
- Review sentiment changes, tracked separately from new reviews.
- Review-summary trends over time.

Define aggregation semantics in the README. Label review counts as imported/observed data, and indicate incomplete imports.

Do not interpolate missing CCU intervals as actual observations. Charts must show gaps, and aggregate coverage must reflect missing samples.

Changing App ID must retain existing data and keep histories separated.

## 5. Dashboard

Create a polished, readable dashboard suitable for a second monitor. Default to a dark theme and Polish UI labels.

Top cards:
- Current CCU.
- Highest observed CCU and its timestamp.
- CCU change over approximately 15 and 60 minutes.
- Positive and negative review counts.
- Positive review percentage with sample size and explicit population/filter label.
- Number of newly created reviews in the last hour.
- Collection health and data freshness.

If there is no sufficiently close historical CCU sample for a comparison, show “unavailable.”

Charts:
- CCU over time.
- Positive and negative new reviews per time bucket.
- Review percentage over time from stored summary snapshots.

Time filters:
- Last hour.
- Last 6 hours.
- Last 24 hours.
- Since launch, when configured.
- Entire collection period.
- Custom date range.

Clearly distinguish event time from first discovery time. Imported historical reviews must not appear as a burst of newly posted reviews at import time.

Review feed:
- Positive/negative filter.
- Language filter.
- Steam purchase/other filter.
- Text search.
- Creation time and playtime at review.
- Original Steam review link.
- Indication of edited reviews and access to version history.

Manual annotations:
- Add, edit, and delete timestamped events such as launch, hotfix, stream, and marketing publication.
- Display annotations on charts.
- Persist and export them.

Show separate statuses for application health, Steam source availability, and historical-import completeness.

## 6. Export through the interface

Provide an obvious “Export data” action with date-range and game selection.

Support:
1. Raw CCU observations as CSV.
2. CCU aggregates as CSV.
3. Current review records as CSV and JSON.
4. Review version history as JSON.
5. Review-summary snapshots as CSV.
6. Review aggregates as CSV.
7. Manual annotations as CSV.
8. A complete analysis ZIP containing these datasets and metadata.
9. A consistent SQLite backup download.

ZIP metadata must include:
- App ID and game name.
- Export timestamp.
- Selected range and timezone conventions.
- Dataset/filter definitions.
- Schema/application version.
- Collection start, gaps, and import completeness.
- Definitions and limitations of metrics.

Specify which timestamp controls each date-filtered export, especially review creation versus version observation time.

Exports must work while collection continues. Use SQLite’s backup mechanism for database backups rather than naively copying an active database file.

Use Windows/Excel-friendly UTF-8 CSV encoding and proper multiline quoting. Protect spreadsheet exports against formula injection from user-generated review text; preserve original text in JSON.

Generate exports from stored data, not by fetching Steam again.

## 7. Optional AI review analysis

Implement as an optional, separately configurable feature. The application must remain fully functional without an AI API key.

When enabled:
- Process new or changed reviews approximately every 10 minutes.
- Group recurring issues such as crashes, saves, performance, tutorial, UI, and balance.
- Include positive themes as well.
- Show unique review counts, supporting review links, analysis timeframe, and a short Polish summary.
- Clearly label AI-generated conclusions.
- Persist analysis history, model identity, and supporting review IDs.
- Avoid reprocessing unchanged reviews unnecessarily.
- Provide a manual “Analyze now” action and bounded batch sizes.

Treat review text as untrusted input, never as instructions. Do not give the analysis model tools or operational permissions.

Compute numeric metrics in application code. AI must not invent statistics or turn unverified complaints into confirmed bugs.

Keep credentials on the backend and out of logs and exports.

## 8. Windows execution

Provide:
- `setup.bat` to create a virtual environment and install dependencies.
- `start.bat` to start the application and collector.
- `.env.example`.
- Clear README instructions for supported Python installation and startup.

Bind to localhost by default.

Use a predictable writable data directory that survives upgrades and restarts.

Explain that collection requires the process to remain running and the computer to remain awake. Include an optional Windows Task Scheduler startup example.

## 9. Docker and Portainer

Provide:
- Dockerfile.
- `.dockerignore`.
- Docker Compose configuration.
- A Portainer-compatible stack example.
- Health check.
- Persistent volume mounted at `/data`.
- Configurable published port.
- `restart: unless-stopped`.
- Graceful shutdown.

Ensure database, settings, checkpoints, and analysis history survive container recreation.

Explain how to build/tag the image and make it available to the Docker host used by Portainer. The stack must not depend on an image that has not been built or an inaccessible local build context.

Document migration between Windows and Docker using a consistent database backup.

Support optional password protection for LAN/server access. A deployment exposing the dashboard beyond localhost must require authentication or a documented authenticated reverse proxy. Protect exports and configuration routes too.

## 10. Verification and delivery

Add meaningful tests covering:
- Failed CCU requests never stored as zero.
- Review deduplication and version history.
- Pagination and recovery after downtime.
- Correct aggregate buckets, gaps, and timezone handling.
- Export validity and date-filter semantics.
- Persistence across restarts.
- A usable, consistent database backup.

Use deterministic fixtures for automated tests. Keep demo data explicitly separate from real data.

Perform a live API smoke test when network access permits. Report blocked or untested paths honestly.

Deliver the implemented repository, setup instructions, Windows startup scripts, Docker/Portainer configuration, and concise notes on validation and known limitations.

Start with reliable collection and persistence, then the dashboard and exports, then optional AI analysis. Do not stop after scaffolding.
