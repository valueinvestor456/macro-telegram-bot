# Macro delivery operations

## What is running

[The independent Cloudflare clock](./cloud-scheduler/README.md) checks every
minute and dispatches [Scheduled Macro Summary Send](./.github/workflows/scheduled-send.yml)
through GitHub's API. That workflow runs [macro_scheduler.py](./macro_scheduler.py).
The original GitHub cron is a fallback, not the primary clock. Times and holidays
come from [macro_schedule.json](./macro_schedule.json), in `Asia/Bangkok`.

- There are 24 daily slots: 09:45, then every half hour from 10:00 through 21:00.
  Additional wakeups occur at minutes 7, 22, 37 and 52 during 09:00–21:59 Thai
  time on weekdays. The sender still checks the Thai bank holiday calendar.
- The retired 16:15 slot remains valid in historical ledger entries, but is
  not scheduled anymore. Keep the existing state branch when updating.
- The scheduler sends only the latest due slot. At 11:20, missing 09:45,
  10:00, 10:30 and 11:00 produces one fresh summary covering through 11:00.
- No send before 09:45, after 21:30, on bank holidays/weekends, or for a previous day.
- A state branch stores claims and acknowledged Telegram message IDs. State
  commits never change `main`. Concurrent writers must match the prior file SHA.
- The same slot is not automatically sent again after success. Missing or
  unreadable state stops delivery rather than risking duplicate messages.
- Data fetching has a 150-second deadline. Explicit temporary Telegram rejections
  use persisted backoff (including `retry_after`), with at most 3 attempts per slot.
- A preparation abandoned before sending can be reclaimed after 5 minutes.
- A network timeout during send, or a worker dying after recording `sending`,
  is **uncertain**. No automatic retry of that slot: Telegram's send API has no
  idempotency key, so exact-once delivery cannot be guaranteed across that gap.
- After the end-of-day cutoff, the next worker checks final-round coverage. A
  missed final round produces a failed run. This is not an independent uptime
  monitor: inspect Cloudflare's heartbeat and confirmed delivery too. A successful
  dispatch alone does not prove that GitHub started the sender or Telegram accepted it.

## Check and recover

1. Open Actions → **Scheduled Macro Summary Send**. Read its job summary, not
   just the green/red badge. `SENT` means Telegram acknowledged a message ID.
2. Run workflow with `preview=true` (default) to validate the state store and
   inspect work without sending or changing the ledger. Outside sending hours,
   preview validates the ledger and reports `preview_idle`, without failing for
   a previous day's missing final round. Normal runs still audit that missed round.
3. To process a pending due slot, run with `preview=false`. It still honors
   holidays, the time window and deduplication; it does not force a duplicate.
4. Inspect `macro_delivery.json` on branch `macro-delivery-state`. It contains
   timestamps, slot statuses and message IDs, never tokens or message content.
5. For `uncertain`, inspect the recipient's Telegram history first. If it arrived,
   record status `sent`, its message ID and advance `covered_through` to that slot.
   Only if confirmed absent, change status to `retry` with `retry_at: 0` and an
   attempt count below 3. Pause the scheduler before manual ledger changes and
   resume afterward. Do not clear the ledger or blindly replay an uncertain send.
6. For `failed`, fix the recorded source/authentication issue, then reset that
   slot to `retry` only when retrying it is appropriate. Newer slots remain eligible.

Use repository variable `MACRO_SCHEDULER_ENABLED=false` to pause automatic sends.
Preview remains available. Restore to `true` to resume. This variable is separate
from `TELEGRAM_POLLING_ENABLED`: this sender never calls `getUpdates`.

## Provisioning or replacing the state store

The initial `macro-delivery-state` branch contains `macro_delivery.json`:

```json
{"version":1,"activated_on":"YYYY-MM-DD","days":{}}
```

Use the actual Thai activation date. Provision once before enabling the workflow.
If replacing a previously used ledger, migrate acknowledged slots first. The
worker deliberately does not auto-create a missing ledger. GitHub Actions needs
`contents: write` on its built-in token; no new personal token is needed there.

Keep the branch and its history. Do not include it in automated branch cleanup.
The live file retains 35 days; Git history retains earlier audits. Restrict edits
to trusted operators. Enable GitHub Actions failure notifications for this repo.

## Always-on host (prepared, not installed automatically)

GitHub wakeups improve recovery, not exact timing. For independence from GitHub
cron, install the project on an always-on Linux host and use
[macro-scheduler.service](./deploy/macro-scheduler.service):

1. Create a dedicated `macrobot` user and install the project at
   `/opt/macro-telegram-bot`, with its virtualenv at `.venv` and requirements installed.
2. Store the Telegram credentials privately in `/etc/macro-telegram-bot.env`
   using [this example](./deploy/telegram.env.example). Allow only root and the
   service account to read it. Never put credentials in Git or chat.
3. Set `MACRO_SCHEDULER_ENABLED=false` on GitHub; wait for in-progress send jobs
   to finish before switching. Do not run the legacy `python main.py` scheduler,
   other senders, or `--once` alongside this worker. A command receiver, if needed,
   must use `--poll-only` and must remain the only `getUpdates` receiver.
4. Migrate that day's acknowledged slots before activating a different store, or
   switch before 09:45 on a new business day. Different stores cannot deduplicate
   each other.
5. Install the service under `/etc/systemd/system/`, reload systemd and enable it.
   SQLite is stored persistently under `/var/lib/macro-telegram-bot/`. The service
   restarts after crashes and checks for work every 30 seconds.
6. Verify `systemctl status macro-scheduler` and `journalctl -u macro-scheduler`.
   Back up the SQLite database using SQLite's backup API. Never silently replace
   a used/lost database with an empty one during the same day's send window.

Windows can use `python macro_scheduler.py --loop --store sqlite`; it requires
an awake PC and a restart mechanism. This session does not install an autostart
task without a host selection. SQLite closes connections after each transaction.

For a shared GitHub ledger on another host, use `--store github` and configure
`GITHUB_REPOSITORY` plus a narrowly scoped `GH_TOKEN` with Contents read/write.
This permits coordinated fallback workers but still depends on GitHub's API.

## Maintenance and rollback

- Keep [the shared Thai holiday calendar](./macro_schedule.json) current before each
  new year; the bundled calendar covers 2026. Both senders refuse unconfigured years.
  Push the Python configuration and redeploy Cloudflare after changes. Check
  credential expiry and GitHub access.
- Run `python -m unittest discover -s tests` before deploying. Tests exercise
  delayed wakeups, concurrent claims, restarts, corruption/unavailable state,
  retries, and the crash window around Telegram delivery.
- To roll back, first pause automatic sends, let the current worker finish and
  inspect its ledger. Keep the ledger even if rolling back application code.
  A legacy sender does not read it and can send duplicates.
- No delivery system can guarantee both zero loss and zero duplicates when the
  remote send succeeds but its acknowledgement is lost. This implementation
  favors avoiding duplicate sends and makes that uncertainty visible for review.
