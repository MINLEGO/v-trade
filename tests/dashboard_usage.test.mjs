import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const dashboardSource = readFileSync(
  "src/vtrade/dashboard/static/dashboard.js",
  "utf8",
);
const dashboardDefinitions = dashboardSource.slice(
  0,
  dashboardSource.lastIndexOf("initialiseFromLocation();"),
);

class FakeNode {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.textContent = "";
  }

  append(...children) {
    this.children.push(...children.filter(Boolean));
  }

  replaceChildren(...children) {
    this.children = children;
  }
}

function loadDashboardContext(document = undefined) {
  const context = { Intl, Number };
  if (document !== undefined) context.document = document;
  vm.runInNewContext(dashboardDefinitions, context);
  return context;
}

function fakeDocument() {
  return {
    querySelector(selector) {
      return new FakeNode(selector);
    },
    createElement(tag) {
      return new FakeNode(tag);
    },
    createDocumentFragment() {
      return new FakeNode("fragment");
    },
    createTextNode(value) {
      const node = new FakeNode("text");
      node.textContent = String(value);
      return node;
    },
  };
}

function flatten(node) {
  return String(node.textContent || "") + node.children.map(flatten).join("");
}

function providerUsageLabel() {
  const context = loadDashboardContext();
  assert.equal(typeof context.providerUsageLabel, "function");
  return context.providerUsageLabel;
}

function renderedUsage(item) {
  const usageRoot = new FakeNode("div");
  const document = {
    ...fakeDocument(),
    querySelector(selector) {
      return selector === "#usage-list" ? usageRoot : new FakeNode(selector);
    },
  };
  const context = loadDashboardContext(document);
  context.renderUsage([item]);
  return flatten(usageRoot);
}

function renderedTimelineEntry(event) {
  const context = loadDashboardContext(fakeDocument());
  return flatten(context.timelineEntry(event));
}

function renderedCycleContext(detail) {
  const context = loadDashboardContext(fakeDocument());
  return flatten(context.cycleContext(detail));
}

test("provider usage label uses canonical SQL counters", () => {
  assert.equal(
    providerUsageLabel()({
      request_count: 2,
      prompt_tokens: 100,
      completion_tokens: 20,
      reasoning_tokens: 5,
      cached_tokens: 3,
    }),
    "125 tokens / 2 requests",
  );
});

test("renderUsage displays canonical counters", () => {
  assert.match(
    renderedUsage({
      provider: "openrouter",
      request_count: 2,
      prompt_tokens: 100,
      completion_tokens: 20,
      reasoning_tokens: 5,
      cached_tokens: 3,
    }),
    /125 tokens \/ 2 requests/,
  );
});

test("provider usage label defaults missing counters to zero", () => {
  assert.equal(
    providerUsageLabel()({
      request_count: null,
      prompt_tokens: undefined,
      completion_tokens: null,
      reasoning_tokens: undefined,
    }),
    "0 tokens / 0 requests",
  );
});

test("provider usage label supports legacy total and request aliases", () => {
  const label = providerUsageLabel();
  assert.equal(label({ total_tokens: 42, requests: 3 }), "42 tokens / 3 requests");
  assert.equal(label({ tokens: 9, requests: 1 }), "9 tokens / 1 requests");
});

test("control requests use the authenticated audited operator contract", async () => {
  const calls = [];
  const context = loadDashboardContext();
  context.fetch = async (endpoint, options) => {
    calls.push({ endpoint, options });
    return { ok: true, json: async () => ({ status: "ok" }) };
  };

  await context.controlRequest("/admin/agents/agent-1/pause");
  await context.controlRequest("/admin/agents/agent-1/resume");

  assert.equal(calls[0].options.method, "POST");
  assert.equal(calls[0].options.credentials, "same-origin");
  assert.equal(calls[0].options.headers.Accept, "application/json");
  assert.equal(calls[0].options.headers["X-Operator-Id"], "dashboard");
  assert.match(calls[0].options.headers["Idempotency-Key"], /^dashboard-.+/);
  assert.notEqual(
    calls[0].options.headers["Idempotency-Key"],
    calls[1].options.headers["Idempotency-Key"],
  );
  assert.deepEqual(calls.map((call) => call.endpoint), [
    "/admin/agents/agent-1/pause",
    "/admin/agents/agent-1/resume",
  ]);
});

test("auditText marks a retained value unavailable after retention purge", () => {
  const context = loadDashboardContext();
  const audit = context.auditText("sensitive payload", "Payload unavailable after retention cleanup.", true);
  assert.equal(audit.content, "Payload unavailable after retention cleanup.");
  assert.equal(audit.unavailable, true);
});

test("timeline entries hide purged model payloads", () => {
  const rendered = renderedTimelineEntry({
    kind: "reasoning",
    title: "Model turn 0",
    at: "2026-07-30T12:00:00Z",
    reasoning: "sensitive reasoning",
    input: { secret: "sensitive input" },
    response: { secret: "sensitive response" },
    retention_purged: true,
  });

  assert.doesNotMatch(rendered, /sensitive (reasoning|input|response)/);
  assert.match(rendered, /Payload unavailable after retention cleanup\./);
});

test("cycle context hides purged prompt payloads", () => {
  const rendered = renderedCycleContext({
    metadata: {
      rendered_cycle_prompt: "sensitive prompt",
      prompt_context: { secret: "sensitive context" },
      prompt_retention_purged: true,
    },
    plan_revisions: [],
    belief_revisions: [],
  });

  assert.doesNotMatch(rendered, /sensitive (prompt|context)/);
  assert.match(rendered, /Payload unavailable after retention cleanup\./);
});
