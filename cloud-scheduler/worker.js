import schedule from "../macro_schedule.json" with { type: "json" };

const MINUTE = 60_000;
const DISPATCH_BACKOFF = 5 * MINUTE;
const MAX_DISPATCHES = 3;
const LEDGER_SLOTS = new Set([
  ...schedule.send_times,
  ...schedule.schedule_changes.flatMap((policy) => policy.send_times),
  ...schedule.retired_slots,
]);
const ACTIVE_RUNS = new Set(["queued", "in_progress", "waiting", "pending", "requested"]);
const json = (value, status = 200) => Response.json(value, {
  status, headers: { "Cache-Control": "no-store" },
});
class ClockError extends Error {}

const minutes = (slot) => Number(slot.slice(0, 2)) * 60 + Number(slot.slice(3, 5));
function policyForDay(day) {
  let policy = schedule;
  for (const change of schedule.schedule_changes)
    if (day >= change.effective_from) policy = change;
  return policy;
}
function slotOrder(day, slot) {
  if (!slot) return -1;
  const value = minutes(slot);
  return value + (value < minutes(policyForDay(day).day_rollover) ? 1440 : 0);
}

export function dueAt(timestamp) {
  // Fixed UTC+7: Thailand has no daylight-saving transition.
  const local = new Date(timestamp + 7 * 60 * MINUTE);
  const calendarDay = local.toISOString().slice(0, 10);
  const previous = new Date(local.getTime() - 24 * 60 * MINUTE).toISOString().slice(0, 10);
  const clock = local.getUTCHours() * 60 + local.getUTCMinutes();
  const day = clock < minutes(policyForDay(previous).day_rollover) ? previous : calendarDay;
  const tradingDate = new Date(`${day}T00:00:00Z`);
  if (!schedule.calendar_years.includes(tradingDate.getUTCFullYear()))
    throw new ClockError("Holiday calendar needs updating");
  if ([0, 6].includes(tradingDate.getUTCDay()) || schedule.bank_holidays.includes(day))
    return { day, slot: null, reason: "bank_closed" };
  const elapsed = clock + local.getUTCSeconds() / 60 + local.getUTCMilliseconds() / MINUTE +
    (day === previous ? 1440 : 0);
  const policy = policyForDay(day);
  for (const window of policy.windows) {
    const start = slotOrder(day, window.start);
    const end = slotOrder(day, window.end);
    if (start <= elapsed && elapsed <= end) {
      const slot = policy.send_times.filter((candidate) =>
        start <= slotOrder(day, candidate) && slotOrder(day, candidate) <= elapsed).at(-1);
      if (slot) return { day, slot, reason: null };
    }
  }
  return { day, slot: null, reason: "outside_window" };
}

export function ledgerDay(payload, day) {
  if (payload?.version !== 1 || !payload.days || typeof payload.days !== "object" ||
      typeof payload.activated_on !== "string")
    throw new ClockError("Invalid delivery ledger");
  const entry = payload.days[day];
  if (entry === undefined) return { covered_through: "", attempts: {} };
  if (!entry || !entry.attempts || typeof entry.attempts !== "object" ||
      (entry.covered_through !== "" && !LEDGER_SLOTS.has(entry.covered_through)))
    throw new ClockError("Invalid delivery ledger day");
  for (const [slot, attempt] of Object.entries(entry.attempts)) {
    if (!LEDGER_SLOTS.has(slot) || !attempt ||
        !["preparing", "sending", "sent", "retry", "failed", "uncertain"].includes(attempt.status))
      throw new ClockError("Invalid delivery ledger attempt");
  }
  const covered = entry.attempts[entry.covered_through];
  if (entry.covered_through && (covered?.status !== "sent" || !Number.isSafeInteger(covered.message_id)))
    throw new ClockError("Coverage lacks Telegram acknowledgement");
  return entry;
}

async function github(env, path, options = {}) {
  const response = await fetch(`https://api.github.com/repos/${env.REPOSITORY}/${path}`, {
    ...options,
    headers: {
      "Authorization": `Bearer ${env.GH_DISPATCH_TOKEN}`,
      "Accept": "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "macro-summary-clock",
      "Content-Type": "application/json",
      "Cache-Control": "no-cache",
    },
    // Workers supports manual/follow, not the Fetch standard's error mode.
    // Reject 3xx below without forwarding credentials to a redirect destination.
    redirect: "manual",
    signal: AbortSignal.timeout(10_000),
  });
  if (response.status === 404 && options.allowMissing) return null;
  if (!response.ok) throw new ClockError(`GitHub API HTTP ${response.status}`);
  return response.status === 204 ? null : response.json();
}

export class MacroClock {
  constructor(ctx, env) {
    this.ctx = ctx;
    this.env = env;
    this.pending = Promise.resolve();
  }

  async fetch(request) {
    const path = new URL(request.url).pathname;
    if (request.method === "GET" && path === "/status")
      return json(await this.ctx.storage.get("status") || { status: "not_started" });
    if (request.method !== "POST" || !["/tick", "/check"].includes(path)) return json({ error: "Not found" }, 404);
    const source = request.headers.get("X-Clock-Source") === "cron" ? "cron" : "manual";
    // Serialise async network operations within the singleton.
    const result = this.pending.then(() => path === "/check" ? this.check() : this.tick(source));
    this.pending = result.catch(() => {});
    return json(await result);
  }

  async check() {
    // Authenticated deployment diagnostic: preview never sends or changes the ledger.
    const check = { at: new Date().toISOString() };
    try {
      if (!this.env.GH_DISPATCH_TOKEN) throw new ClockError("GitHub secret missing");
      const result = await github(this.env, `actions/workflows/${this.env.WORKFLOW}/dispatches`, {
        method: "POST", body: JSON.stringify({ ref: this.env.REF, inputs: { preview: true } }),
      });
      check.status = "preview_dispatched";
      check.run_id = result?.workflow_run_id || null;
    } catch (error) {
      check.status = "error";
      check.error = error instanceof ClockError ? error.message : "Clock dependency unavailable";
    }
    const state = await this.ctx.storage.get("status") || {};
    state.last_check = check;
    await this.ctx.storage.put("status", state);
    return check;
  }

  async tick(source) {
    const now = Date.now();
    const state = await this.ctx.storage.get("status") || {};
    state.last_tick_at = new Date(now).toISOString();
    state.last_source = source;
    state.stalled_run_id = null;
    if (source === "cron") {
      state.last_cron_at = state.last_tick_at;
      state.cron_ticks = (state.cron_ticks || 0) + 1;
    }
    const finish = async (status, error = null) => {
      state.status = status;
      state.error = error;
      await this.ctx.storage.put("status", state);
      return state;
    };
    try {
      if (this.env.ENABLED !== "true") return await finish("paused");
      if (!this.env.GH_DISPATCH_TOKEN) throw new ClockError("GitHub secret missing");
      const due = dueAt(now);
      if (state.day !== due.day) state.covered_through = "";
      state.day = due.day;
      state.due_slot = due.slot;
      state.schedule_effective_from = policyForDay(due.day).effective_from || null;
      state.scheduled_slots = policyForDay(due.day).send_times.length;
      if (!due.slot) return await finish(due.reason);
      const enabled = await github(this.env, "actions/variables/MACRO_SCHEDULER_ENABLED", { allowMissing: true });
      if (enabled === null) {
        // A single-variable 404 can also mask missing Variables permission.
        // Confirm that this credential can read repository variables before
        // treating the pause switch as unset.
        const variables = await github(this.env, "actions/variables?per_page=1");
        if (!Array.isArray(variables?.variables)) throw new ClockError("Invalid repository variable list");
      }
      if (enabled?.value === "false") return await finish("paused");
      const file = await github(this.env, "contents/macro_delivery.json?ref=macro-delivery-state");
      let payload;
      try { payload = JSON.parse(atob(file.content.replace(/\s/g, ""))); }
      catch { throw new ClockError("Invalid delivery ledger encoding"); }
      const day = ledgerDay(payload, due.day);
      state.covered_through = day.covered_through;
      const acknowledged = Object.entries(day.attempts)
        .filter(([, attempt]) => attempt.status === "sent" && Number.isSafeInteger(attempt.message_id))
        .sort(([a], [b]) => slotOrder(due.day, a) - slotOrder(due.day, b)).at(-1);
      state.last_confirmed = acknowledged ? {
        day: due.day, slot: acknowledged[0], message_id: acknowledged[1].message_id,
        at: acknowledged[1].updated_at,
      } : null;
      if (slotOrder(due.day, day.covered_through) >= slotOrder(due.day, due.slot))
        return await finish("confirmed");
      const prior = day.attempts[due.slot];
      if (["uncertain", "failed"].includes(prior?.status)) return await finish("needs_review");
      if (prior?.retry_at * 1000 > now) return await finish("sender_backoff");
      if (Object.values(day.attempts).some((entry) =>
        (entry.status === "preparing" && entry.lease_until * 1000 > now) ||
        (entry.status === "sending" && entry.sending_at * 1000 + 2 * MINUTE > now)))
        return await finish("sender_busy");

      const key = `${due.day}/${due.slot}`;
      const priorDispatch = state.last_dispatch?.key === key ? state.last_dispatch : null;
      if (state.last_dispatch && now - state.last_dispatch.at < DISPATCH_BACKOFF)
        return await finish("awaiting_confirmation");
      if ((priorDispatch?.attempts || 0) >= MAX_DISPATCHES) return await finish("needs_review", "Dispatch attempts exhausted");

      const runs = await github(this.env, `actions/workflows/${this.env.WORKFLOW}/runs?per_page=30&branch=${encodeURIComponent(this.env.REF)}`);
      if (!Array.isArray(runs.workflow_runs)) throw new ClockError("Invalid workflow run list");
      const activeRuns = runs.workflow_runs.filter((run) => ACTIVE_RUNS.has(run.status));
      state.stalled_run_id = null;
      for (const active of activeRuns) {
        const started = Date.parse(active.run_started_at || active.created_at);
        if (!Number.isFinite(started)) throw new ClockError("Active workflow run has no valid timestamp");
        if (now - started <= 10 * MINUTE) {
          state.active_run_id = active.id;
          return await finish("workflow_pending");
        }
        state.stalled_run_id ||= active.id;
      }
      // A new dispatch can replace a stuck pending concurrency entry. Never
      // cancel an in-flight sender; its ledger and workflow concurrency stay authoritative.
      state.active_run_id = null;
      state.last_dispatch = { key, at: now, attempts: (priorDispatch?.attempts || 0) + 1, source };
      // Persist before dispatch. An ambiguous response cannot cause a retry storm.
      await finish("dispatching");
      const dispatched = await github(this.env, `actions/workflows/${this.env.WORKFLOW}/dispatches`, {
        method: "POST",
        body: JSON.stringify({ ref: this.env.REF, inputs: { preview: false } }),
      });
      state.last_dispatch.run_id = dispatched?.workflow_run_id || null;
      state.dispatches = [...(state.dispatches || []), { ...state.last_dispatch }].slice(-48);
      return await finish("awaiting_confirmation");
    } catch (error) {
      // Never return request headers, credentials, upstream bodies or raw errors.
      return await finish("error", error instanceof ClockError ? error.message : "Clock dependency unavailable");
    }
  }
}

async function authorized(request, env) {
  if (!env.CONTROL_TOKEN || env.CONTROL_TOKEN.length < 32) return false;
  const provided = request.headers.get("Authorization")?.replace(/^Bearer /, "") || "";
  const digest = async (value) => new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value)));
  const [a, b] = await Promise.all([digest(provided), digest(env.CONTROL_TOKEN)]);
  let difference = 0;
  for (let i = 0; i < a.length; i++) difference |= a[i] ^ b[i];
  return difference === 0;
}

export default {
  async scheduled(controller, env) {
    // Use actual invocation time, never a stale scheduledTime to replay old slots.
    const response = await env.CLOCK.getByName("macro").fetch(new Request("https://clock/tick", {
      method: "POST", headers: { "X-Clock-Source": "cron" },
    }));
    const state = await response.json();
    if (["error", "needs_review"].includes(state.status) || state.stalled_run_id)
      throw new Error(`Macro clock needs attention: ${state.status}`);
  },
  async fetch(request, env) {
    const path = new URL(request.url).pathname;
    if (request.method === "GET" && path === "/health")
      return json({ ok: true, service: "macro-summary-clock" });
    if (!((request.method === "GET" && path === "/status") ||
          (request.method === "POST" && ["/tick", "/check"].includes(path))) ||
        !await authorized(request, env))
      return json({ error: "Not found" }, 404);
    return env.CLOCK.getByName("macro").fetch(new Request(`https://clock${path}`, {
      method: request.method, headers: { "X-Clock-Source": "manual" },
    }));
  },
};
