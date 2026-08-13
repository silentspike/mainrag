"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const validator = require("./validate-pr-contract.cjs");

const validPr = `
## Work item
Refs #69
## Summary
Summary
## Scope and non-claims
Scope
## Implementation
Implementation
## Verification
Verification
## Privacy and security
Privacy
## Risk and rollback
Risk
## Evidence
Evidence
## Landing boundary
Boundary
`;

const validTask = `
## Context and dependencies
Context
## Why
Why
## Goal
Goal
## Current baseline
Baseline
## Scope
### In scope
In
### Out of scope
Out
### Non-goals and non-claims
None
## Implementation boundary and reusable paths
Boundary
## Acceptance criteria
Criteria
## Verification strategy
Verify
## Privacy, evidence, and cleanup
Privacy
## Landing, closure, and approval boundary
Landing
## Stop conditions
Stop
`;

const validStory = validTask
  .replace("## Context and dependencies", "## Parent and dependencies")
  .replace("## Goal\nGoal", "## User outcome\nOutcome\n## Desired outcome\nDesired");

function mockCore() {
  const summary = {
    addHeading() { return this; },
    addRaw() { return this; },
    async write() {},
  };
  return { info() {}, summary };
}

function mockContext(body, actor = "worker") {
  return {
    actor,
    repo: { owner: "silentspike", repo: "mainrag" },
    payload: { pull_request: { body, labels: [] } },
  };
}

test("extracts one primary reference and an optional parent", () => {
  assert.deepEqual(validator.extractReferences("Refs #69\nPart of #53"), [
    { mode: "refs", number: 69 },
    { mode: "parent", number: 53 },
  ]);
  assert.deepEqual(validator.selectPrimaryReference(validator.extractReferences("Closes #54\nPart of #53")), {
    mode: "closes",
    number: 54,
  });
});

test("rejects missing and multiple primary work items", () => {
  assert.throws(() => validator.selectPrimaryReference([]), /exactly one primary/u);
  assert.throws(
    () => validator.selectPrimaryReference(validator.extractReferences("Refs #54\nCloses #55")),
    /multiple primary references/u,
  );
  assert.throws(
    () => validator.selectPrimaryReference(validator.extractReferences("Refs #69\nCloses #69")),
    /multiple primary references/u,
  );
});

test("validates complete PR and engineering issue contracts", () => {
  assert.deepEqual(validator.validatePullRequestBody(validPr), { mode: "refs", number: 69 });
  assert.doesNotThrow(() => validator.validateIssueBody(validTask, ["enhancement"]));
  assert.doesNotThrow(() => validator.validateIssueBody(validStory, ["enhancement"]));
});

test("rejects incomplete PR and issue contracts", () => {
  assert.throws(() => validator.validatePullRequestBody("Refs #69"), /Missing headings/u);
  assert.throws(() => validator.validateIssueBody("## Why\nOnly one field", []), /Missing headings/u);
});

test("recognizes issue-owned Refs closure mode", () => {
  assert.equal(validator.issueRequiresRefs("- Closure mode: PRs use `Refs #...`."), true);
  assert.equal(validator.issueRequiresRefs("- Closure mode: `Closes #...` is allowed."), false);
});

test("allows only the explicit Dependabot exception", () => {
  const pullRequest = { labels: [{ name: "no-issue-required" }] };
  assert.equal(validator.hasNoIssueException(pullRequest, "dependabot[bot]"), true);
  assert.equal(validator.hasNoIssueException(pullRequest, "untrusted-user"), false);
  assert.equal(validator.hasNoIssueException({ labels: [] }, "dependabot[bot]"), false);
});

test("treats hostile markdown as inert data", () => {
  const hostile = validPr.replace("Summary\n", "Summary\n${{ secrets.SHOULD_NOT_BE_READ }}; $(touch /tmp/nope)\n");
  assert.deepEqual(validator.validatePullRequestBody(hostile), { mode: "refs", number: 69 });
});

test("fails closed when the linked issue cannot be read", async () => {
  const github = { rest: { issues: { get: async () => { throw new Error("Not Found"); } } } };
  await assert.rejects(
    () => validator({ github, context: mockContext(validPr), core: mockCore() }),
    /Not Found/u,
  );
});

test("rejects a closing keyword when the issue requires Refs", async () => {
  const github = {
    rest: {
      issues: {
        get: async () => ({
          data: {
            body: `${validTask}\n- Closure mode: PRs use \`Refs #...\`.`,
            labels: [],
            state: "open",
          },
        }),
      },
    },
  };
  await assert.rejects(
    () => validator({
      github,
      context: mockContext(validPr.replace("Refs #69", "Closes #69")),
      core: mockCore(),
    }),
    /requires `Refs #69`/u,
  );
});

test("accepts a complete open issue with the declared closure mode", async () => {
  const github = {
    rest: {
      issues: {
        get: async () => ({
          data: {
            body: `${validTask}\n- Closure mode: PRs use \`Refs #...\`.`,
            labels: [],
            state: "open",
          },
        }),
      },
    },
  };
  await assert.doesNotReject(
    () => validator({ github, context: mockContext(validPr), core: mockCore() }),
  );
});
