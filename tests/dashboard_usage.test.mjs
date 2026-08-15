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

function providerUsageLabel() {
  const context = loadDashboardContext();
  assert.equal(typeof context.providerUsageLabel, "function");
  return context.providerUsageLabel;
}

function renderedUsage(item) {
  const usageRoot = new FakeNode("div");
  const document = {
    querySelector(selector) {
      return selector === "#usage-list" ? usageRoot : new FakeNode(selector);
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
  const context = loadDashboardContext(document);
  context.renderUsage([item]);
  const flatten = (node) => String(node.textContent || "") + node.children.map(flatten).join("");
  return flatten(usageRoot);
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
