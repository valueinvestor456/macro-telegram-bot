# Independent Macro Summary clock

Cloudflare runs `macro-summary-clock` every minute, independently of GitHub's
scheduled-event queue. During the Thai bank-business-day window it checks the
existing delivery ledger and dispatches `scheduled-send.yml` with `preview=false`
when a slot is due. GitHub Actions still collects market data and sends Telegram.
The GitHub cron entries remain a fallback; both clocks use the same sender and
compare-and-swap ledger, so they do not independently send duplicate messages.

The clock runs without the user's PC being on. A workflow dispatch is **not**
proof of delivery: subsequent ticks confirm the Telegram message ID in the ledger.
GitHub runner/API availability, market-data APIs and Telegram remain dependencies.

## Schedule and state

- Shared configuration: [macro_schedule.json](../macro_schedule.json).
- 09:45, then every half hour 10:00–21:00, Asia/Bangkok; 24 slots on bank business days.
- Catch up only the latest due slot, until 21:30. Never replay past days overnight.
- Calendar is currently verified for 2026. Both clocks fail closed for unconfigured
  years; update the dates and `calendar_years`, push Python and redeploy this Worker.
- The singleton Durable Object stores heartbeats, dispatch cooldowns and the last
  acknowledged delivery. The GitHub `macro-delivery-state` branch remains the sole
  authority for sent messages. No ledger migration or deletion is needed.
- Before dispatching, respect the repository pause variable, sender leases/backoff
  and active workflow jobs. At most three dispatch attempts per slot, five minutes
  apart, including ambiguous API failures. Newer slots remain eligible.
- Failed/uncertain Telegram sends require review; they are not blindly replayed.
- Stale `sending` leases can reach the Python sender for recovery classification.

## Deployment

Worker account: `fa4a3dd24799e1e5c632c30b069cb74d`, the existing bot Cloudflare account.
This is separate from the website's domain account and changes no website routes.

1. Run `node --test cloud-scheduler/test/*.test.js` and
   `python -m unittest discover -s tests` from the repository root.
2. Push the shared configuration/Python changes to `main`.
3. Deploy initially paused with Wrangler:
   `wrangler deploy --config cloud-scheduler/wrangler.jsonc --var ENABLED:false`.
4. Provision `GH_DISPATCH_TOKEN` and `CONTROL_TOKEN` as Cloudflare encrypted secrets.
   GitHub access needs Actions write, Contents read and Variables read for this
   repository. Telegram credentials remain in GitHub and are not copied here.
   [provision-secrets.ps1](./provision-secrets.ps1) supports this workstation's
   `MACRO_DISPATCH_TOKEN` or explicitly selected `-UseExistingGhLogin`, and a
   domain-separated local control key. The existing CLI credential retains its
   original scopes; the helper does not create a narrower token. Prefer a
   repository-scoped credential when provisioning a new one. In either case,
   credentials travel via stdin, never source files or command-line arguments.
5. Deploy the checked-in config (`ENABLED=true`) with
   `wrangler deploy --config cloud-scheduler/wrangler.jsonc`.
6. Use the private `check` operation to dispatch a **preview-only** workflow and
   verify that run succeeds. Check automatic cron heartbeats separately. Cron
   changes may take up to 15 minutes to propagate.
7. During the next business slot, verify `last_dispatch.source=cron` and then
   `last_confirmed` with the expected slot, timestamp and Telegram message ID.

The repository check workflow runs unit tests; it does not deploy Cloudflare.
Redeploy the Worker whenever its code or the shared calendar changes.

## Operating and diagnosing

[control.ps1](./control.ps1) derives the local control credential without printing
it. Supply the Worker URL returned by deployment:

```powershell
./cloud-scheduler/control.ps1 -BaseUrl https://macro-summary-clock.valueinvestor182.workers.dev -Operation status
./cloud-scheduler/control.ps1 -BaseUrl https://macro-summary-clock.valueinvestor182.workers.dev -Operation check
```

- `GET /health` is public **liveness only**, not delivery confirmation.
- `GET /status` is authenticated and read-only. Compare `last_cron_at` with current
  time (normal lag about one minute) and `last_confirmed` with the expected slot.
- `POST /check` is authenticated: preview dispatch, no message or ledger changes,
  usable outside sending hours. It does not consume the real dispatch cooldown.
- `POST /tick` is authenticated: processes the current due slot, respecting the
  same calendar, window and deduplication. It cannot force an out-of-window send.
- `confirmed` means the ledger has a Telegram acknowledgement through the due slot.
- `workflow_pending` / `awaiting_confirmation` is not a successful send.
- `error`, `needs_review` or a `stalled_run_id` also fail the cron invocation so
  they appear in Cloudflare's invocation logs. A job over ten minutes old permits
  bounded replacement dispatches instead of blocking every later slot. The clock
  never cancels an in-flight sender; GitHub concurrency still serialises execution.
- `bank_closed`, `outside_window` and `paused` are expected no-send outcomes.

Set repository variable `MACRO_SCHEDULER_ENABLED=false` to pause both normal
clocks; previews remain available. To stop only the Cloudflare clock, deploy
`ENABLED=false`. Revoking the provisioned GitHub credential stops dispatching;
rotate the Cloudflare secret when changing/revoking that credential.

Never clear the delivery ledger to retry a missed or uncertain round. Follow
[the sender recovery instructions](../SCHEDULER_OPERATIONS.md) instead.
