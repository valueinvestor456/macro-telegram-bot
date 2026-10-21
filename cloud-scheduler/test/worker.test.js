import { test } from "node:test";
import assert from "node:assert/strict";
import worker, { MacroClock, dueAt } from "../worker.js";

const baseTime = Date.parse("2026-10-20T12:00:00Z"); // 19:00 Thai
const freshLedger = () => ({ version: 1, activated_on: "2026-10-15", days: {} });
const env = {
  ENABLED: "true", REPOSITORY: "owner/repo", WORKFLOW: "scheduled-send.yml",
  REF: "main", GH_DISPATCH_TOKEN: "test-token", CONTROL_TOKEN: "x".repeat(64),
};

function fixture(t) {
  let now = baseTime;
  const values = new Map();
  const ctx = { storage: {
    async get(key) { return structuredClone(values.get(key)); },
    async put(key, value) { values.set(key, structuredClone(value)); },
  }};
  const f = {
    ctx, ledger: freshLedger(), runs: [], calls: [], paused: false,
    ledgerError: false, postError: false,
    advance(ms) { now += ms; },
    clock: new MacroClock(ctx, env),
    tick() { return this.clock.fetch(new Request("https://clock/tick", {
      method: "POST", headers: { "X-Clock-Source": "cron" },
    })).then((r) => r.json()); },
  };
  t.mock.method(Date, "now", () => now);
  t.mock.method(globalThis, "fetch", async (url, options) => {
    f.calls.push({ path: new URL(url).pathname, method: options.method || "GET", body: options.body });
    if (url.includes("/actions/variables/"))
      return f.paused ? Response.json({ value: "false" }) : Response.json({}, { status: 404 });
    if (url.includes("/contents/")) {
      if (f.ledgerError) return Response.json({}, { status: 403 });
      return Response.json({ content: btoa(JSON.stringify(f.ledger)) });
    }
    if (url.includes("/runs?")) return Response.json({ workflow_runs: f.runs });
    if (url.endsWith("/dispatches")) {
      if (f.postError) throw new Error("test-token must not leak");
      return new Response(null, { status: 204 });
    }
    throw new Error("Unexpected endpoint");
  });
  f.posts = () => f.calls.filter((call) => call.method === "POST");
  return f;
}

test("calendar uses Thai time, opening round, half hours, holidays and cutoff", () => {
  assert.equal(dueAt(Date.parse("2026-10-20T02:44:59Z")).slot, null);
  assert.equal(dueAt(Date.parse("2026-10-20T02:45:00Z")).slot, "09:45");
  assert.equal(dueAt(Date.parse("2026-10-20T03:29:59Z")).slot, "10:00");
  assert.equal(dueAt(Date.parse("2026-10-20T03:30:00Z")).slot, "10:30");
  assert.equal(dueAt(Date.parse("2026-10-20T14:30:01Z")).slot, null);
  assert.equal(dueAt(Date.parse("2026-10-23T03:30:00Z")).reason, "bank_closed");
  assert.equal(dueAt(Date.parse("2026-10-24T03:30:00Z")).reason, "bank_closed");
  assert.throws(() => dueAt(Date.parse("2027-01-04T03:30:00Z")), /calendar/);
});

test("dispatches independently, then confirms Telegram acknowledgement without resending", async (t) => {
  const f = fixture(t);
  const first = await f.tick();
  assert.equal(first.status, "awaiting_confirmation");
  assert.equal(first.last_dispatch.source, "cron");
  assert.equal(first.last_confirmed, null);
  assert.deepEqual(JSON.parse(f.posts()[0].body), { ref: "main", inputs: { preview: false } });
  f.ledger.days["2026-10-20"] = {
    covered_through: "19:00",
    attempts: { "19:00": { status: "sent", message_id: 987, updated_at: "2026-10-20T19:00:30+07:00" } },
  };
  f.advance(60_000);
  const confirmed = await f.tick();
  assert.equal(confirmed.status, "confirmed");
  assert.equal(confirmed.last_confirmed.message_id, 987);
  assert.equal(confirmed.cron_ticks, 2);
  assert.equal(f.posts().length, 1);
  f.advance(30 * 60_000);
  assert.equal((await f.tick()).due_slot, "19:30");
  assert.equal(f.posts().length, 2);
});

test("already delivered by GitHub backup is not sent again", async (t) => {
  const f = fixture(t);
  f.ledger.days["2026-10-20"] = {
    covered_through: "19:00", attempts: {
      "16:15": { status: "sent", message_id: 1 }, "19:00": { status: "sent", message_id: 2 },
    },
  };
  assert.equal((await f.tick()).status, "confirmed");
  assert.equal(f.posts().length, 0);
});

test("concurrent ticks and object restart respect persisted dispatch cooldown", async (t) => {
  const f = fixture(t);
  await Promise.all([f.tick(), f.tick()]);
  f.clock = new MacroClock(f.ctx, env);
  await f.tick();
  assert.equal(f.posts().length, 1);
  f.advance(5 * 60_000);
  await f.tick();
  assert.equal(f.posts().length, 2);
});

test("ambiguous dispatch is throttled and errors do not expose credentials", async (t) => {
  const f = fixture(t);
  f.postError = true;
  const first = await f.tick();
  assert.equal(first.status, "error");
  assert.ok(!JSON.stringify(first).includes("test-token"));
  await f.tick();
  assert.equal(f.posts().length, 1);
  for (let attempt = 0; attempt < 3; attempt++) {
    f.advance(5 * 60_000);
    await f.tick();
  }
  assert.equal(f.posts().length, 3);
  assert.equal((await f.tick()).status, "needs_review");
});

test("active GitHub jobs do not receive overlapping dispatches", async (t) => {
  const f = fixture(t);
  f.runs = [{ id: 15, status: "queued" }];
  assert.equal((await f.tick()).status, "workflow_pending");
  assert.equal(f.posts().length, 0);
  f.runs = [];
  assert.equal((await f.tick()).status, "awaiting_confirmation");
  assert.equal(f.posts().length, 1);
});

test("pause switch and missing ledger fail closed", async (t) => {
  const f = fixture(t);
  f.paused = true;
  assert.equal((await f.tick()).status, "paused");
  f.paused = false;
  f.ledgerError = true;
  assert.equal((await f.tick()).status, "error");
  assert.equal(f.posts().length, 0);
});

test("uncertain or failed delivery is not automatically replayed", async (t) => {
  const f = fixture(t);
  for (const status of ["uncertain", "failed"]) {
    f.ledger.days["2026-10-20"] = { covered_through: "", attempts: { "19:00": { status } } };
    assert.equal((await f.tick()).status, "needs_review");
  }
  assert.equal(f.posts().length, 0);
  f.advance(30 * 60_000);
  assert.equal((await f.tick()).status, "awaiting_confirmation");
});

test("sender leases and backoff are respected", async (t) => {
  const f = fixture(t);
  f.ledger.days["2026-10-20"] = {
    covered_through: "", attempts: { "19:00": { status: "preparing", lease_until: baseTime / 1000 + 300 } },
  };
  assert.equal((await f.tick()).status, "sender_busy");
  f.ledger.days["2026-10-20"].attempts["19:00"] = { status: "retry", retry_at: baseTime / 1000 + 60 };
  assert.equal((await f.tick()).status, "sender_backoff");
  assert.equal(f.posts().length, 0);
});

test("stale sending can be reviewed by the Python worker, not blocked forever", async (t) => {
  const f = fixture(t);
  f.ledger.days["2026-10-20"] = {
    covered_through: "", attempts: { "18:30": { status: "sending", sending_at: baseTime / 1000 - 300 } },
  };
  assert.equal((await f.tick()).status, "awaiting_confirmation");
  assert.equal(f.posts().length, 1);
});

test("coverage without acknowledgement is reported as corrupt instead of success", async (t) => {
  const f = fixture(t);
  f.ledger.days["2026-10-20"] = { covered_through: "19:00", attempts: {} };
  assert.equal((await f.tick()).status, "error");
  assert.equal(f.posts().length, 0);
});

test("deployment check dispatches preview only even outside the sending window", async (t) => {
  const f = fixture(t);
  f.advance(-12 * 60 * 60_000); // 07:00 Thai
  const response = await f.clock.fetch(new Request("https://clock/check", { method: "POST" }));
  assert.equal((await response.json()).status, "preview_dispatched");
  assert.deepEqual(JSON.parse(f.posts()[0].body), { ref: "main", inputs: { preview: true } });
  assert.equal((await f.ctx.storage.get("status")).last_dispatch, undefined);
});

test("scheduler retries a stuck queue with bounded dispatch rather than blocking all later slots", async (t) => {
  const f = fixture(t);
  f.runs = [{ id: 15, status: "queued", created_at: new Date(baseTime - 11 * 60_000).toISOString() }];
  const state = await f.tick();
  assert.equal(state.status, "awaiting_confirmation");
  assert.equal(state.stalled_run_id, 15);
  assert.equal(f.posts().length, 1);
  await f.tick();
  assert.equal(f.posts().length, 1);
});

test("cooldown is preserved across half-hour boundaries", async (t) => {
  const f = fixture(t);
  f.advance(29 * 60_000);
  await f.tick();
  f.advance(60_000);
  assert.equal((await f.tick()).status, "awaiting_confirmation");
  assert.equal(f.posts().length, 1);
  f.advance(4 * 60_000);
  assert.equal((await f.tick()).last_dispatch.key, "2026-10-20/19:30");
  assert.equal(f.posts().length, 2);
});

test("holidays never contact GitHub or send", async (t) => {
  const f = fixture(t);
  f.advance(3 * 24 * 60 * 60_000); // 23 October bank holiday
  assert.equal((await f.tick()).status, "bank_closed");
  assert.equal(f.calls.length, 0);
});

test("public health cannot trigger delivery and private controls require authentication", async () => {
  let calls = 0;
  const binding = { getByName() { return { async fetch() { calls++; return Response.json({ status: "ok" }); } }; } };
  const config = { ...env, CLOCK: binding };
  assert.equal((await worker.fetch(new Request("https://worker/health"), config)).status, 200);
  assert.equal((await worker.fetch(new Request("https://worker/tick", { method: "POST" }), config)).status, 404);
  assert.equal((await worker.fetch(new Request("https://worker/status"), config)).status, 404);
  assert.equal(calls, 0);
  const response = await worker.fetch(new Request("https://worker/status", {
    headers: { Authorization: `Bearer ${env.CONTROL_TOKEN}` },
  }), config);
  assert.equal(response.status, 200);
  assert.equal(calls, 1);
});
