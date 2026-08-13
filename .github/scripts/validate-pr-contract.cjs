"use strict";

const PR_HEADINGS = [
  "Work item",
  "Summary",
  "Scope and non-claims",
  "Implementation",
  "Verification",
  "Privacy and security",
  "Risk and rollback",
  "Evidence",
  "Landing boundary",
];

const TASK_HEADINGS = [
  "Context and dependencies",
  "Why",
  "Goal",
  "Current baseline",
  "In scope",
  "Out of scope",
  "Non-goals and non-claims",
  "Implementation boundary and reusable paths",
  "Acceptance criteria",
  "Verification strategy",
  "Privacy, evidence, and cleanup",
  "Landing, closure, and approval boundary",
  "Stop conditions",
];

const STORY_HEADINGS = [
  "Parent and dependencies",
  "User outcome",
  "Why",
  "Current baseline",
  "Desired outcome",
  "In scope",
  "Out of scope",
  "Non-goals and non-claims",
  "Implementation boundary and reusable paths",
  "Acceptance criteria",
  "Verification strategy",
  "Privacy, evidence, and cleanup",
  "Landing, closure, and approval boundary",
  "Stop conditions",
];

const BUG_HEADINGS = [
  "Current behavior",
  "Expected behavior",
  "Reproduction",
  "Environment and baseline",
  "Impact and safety boundary",
  "In scope",
  "Out of scope",
  "Non-goals and non-claims",
  "Affected path and reusable components",
  "Acceptance criteria",
  "Verification strategy",
  "Redacted evidence and cleanup",
  "Landing and rollback",
  "Stop conditions",
];

function normalizedHeadings(body) {
  const headings = new Set();
  for (const line of (body || "").split(/\r?\n/u)) {
    const match = line.match(/^#{2,3}\s+(.+?)\s*$/u);
    if (match) headings.add(match[1].trim().toLowerCase());
  }
  return headings;
}

function missingHeadings(body, required) {
  const actual = normalizedHeadings(body);
  return required.filter((heading) => !actual.has(heading.toLowerCase()));
}

function extractReferences(body) {
  const references = [];
  const pattern = /\b(closes?|fixes?|resolves?|refs?|part\s+of)\s+#(\d+)\b/giu;
  let match;
  while ((match = pattern.exec(body || "")) !== null) {
    const keyword = match[1].toLowerCase().replace(/\s+/gu, " ");
    const mode = keyword === "part of" ? "parent" : keyword.startsWith("ref") ? "refs" : "closes";
    references.push({ mode, number: Number.parseInt(match[2], 10) });
  }
  return references;
}

function selectPrimaryReference(references) {
  const primary = references.filter((reference) => reference.mode !== "parent");
  if (primary.length !== 1) {
    const issueNumbers = [...new Set(primary.map((reference) => reference.number))];
    throw new Error(
      primary.length === 0
        ? "PR body must contain exactly one primary `Closes #N` or `Refs #N` work item."
        : `PR body contains multiple primary references: ${issueNumbers.map((number) => `#${number}`).join(", ")}.`,
    );
  }
  return primary[0];
}

function issueRequiresRefs(body) {
  return /^[-*]?\s*Closure mode:[^\n]*\bRefs\b/imu.test(body || "");
}

function validateIssueBody(body, labels) {
  const labelNames = new Set((labels || []).map((label) => (typeof label === "string" ? label : label.name)));
  if (labelNames.has("epic")) {
    throw new Error("An epic cannot be the primary implementation work item; reference a child story, task, or bug.");
  }

  const headings = normalizedHeadings(body);
  const required = labelNames.has("bug")
    ? BUG_HEADINGS
    : headings.has("user outcome") || headings.has("desired outcome")
      ? STORY_HEADINGS
      : TASK_HEADINGS;
  const missing = missingHeadings(body, required);
  if (missing.length > 0) {
    throw new Error(`Issue contract is incomplete. Missing headings: ${[...new Set(missing)].join(", ")}.`);
  }
}

function validatePullRequestBody(body) {
  const missing = missingHeadings(body, PR_HEADINGS);
  if (missing.length > 0) {
    throw new Error(`PR contract is incomplete. Missing headings: ${missing.join(", ")}.`);
  }
  return selectPrimaryReference(extractReferences(body));
}

function hasNoIssueException(pullRequest, actor) {
  const labels = new Set((pullRequest.labels || []).map((label) => label.name));
  return actor === "dependabot[bot]" && labels.has("no-issue-required");
}

async function validate({ github, context, core }) {
  const pullRequest = context.payload.pull_request;
  if (!pullRequest) throw new Error("pull_request payload is required");

  if (hasNoIssueException(pullRequest, context.actor)) {
    core.info("Approved mechanical Dependabot exception: no-issue-required.");
    return;
  }

  const primary = validatePullRequestBody(pullRequest.body || "");
  const { data: issue } = await github.rest.issues.get({
    owner: context.repo.owner,
    repo: context.repo.repo,
    issue_number: primary.number,
  });

  if (issue.pull_request) throw new Error(`#${primary.number} is a pull request, not an engineering issue.`);
  if (issue.state !== "open") throw new Error(`#${primary.number} is not open.`);
  validateIssueBody(issue.body || "", issue.labels || []);

  if (issueRequiresRefs(issue.body || "") && primary.mode !== "refs") {
    throw new Error(`#${primary.number} requires \`Refs #${primary.number}\`; merge must not close it automatically.`);
  }

  await core.summary
    .addHeading("Issue contract", 2)
    .addRaw(`Primary work item: #${primary.number}\n\n`)
    .addRaw(`Closure mode used by PR: ${primary.mode}\n`)
    .write();
  core.info(`Validated PR contract and issue #${primary.number}.`);
}

module.exports = validate;
module.exports.extractReferences = extractReferences;
module.exports.hasNoIssueException = hasNoIssueException;
module.exports.issueRequiresRefs = issueRequiresRefs;
module.exports.missingHeadings = missingHeadings;
module.exports.normalizedHeadings = normalizedHeadings;
module.exports.selectPrimaryReference = selectPrimaryReference;
module.exports.validateIssueBody = validateIssueBody;
module.exports.validatePullRequestBody = validatePullRequestBody;
