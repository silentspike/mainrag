## Work item

<!-- Use exactly one primary engineering issue. Use `Closes #N` only when the
issue permits closure on merge to `main`; otherwise use `Refs #N`. -->

Refs #

## Summary

<!-- State the observable outcome and why it is needed. -->

## Scope and non-claims

<!-- Name the issue-owned semantic paths and what this PR deliberately does not
change or prove. Deployment, activation, cleanup, RC, and release are separate. -->

## Implementation

<!-- Trace producer -> transport/API -> persistence -> recovery/concurrency ->
consumer -> observable effect. Name reused abstractions and direct callers. -->

## Verification

<!-- List exact commands, exit status, focused tests, final aggregate gate, and
whether filtered tests executed at least one test. Preserve FAIL/BLOCKED/SKIP. -->

| Surface | Command or evidence | Result |
| --- | --- | --- |
| Static/focused |  | NOT RUN |
| Aggregate |  | NOT RUN |
| External/live |  | NOT REQUIRED |

## Privacy and security

<!-- Confirm that no credentials, identities, private paths/content, hostnames,
addresses, raw private logs, or reusable private request recipes are included. -->

## Risk and rollback

<!-- Describe failure modes, safe disable/revert behavior, cleanup, and any
rollback/recovery surface that remains untested or unavailable. -->

## Evidence

<!-- Bind evidence to the exact head commit, tree/schema/package/fixture identity
as applicable. A manifest cannot name its own future commit. -->

## Landing boundary

- [ ] The PR targets the branch and closure mode declared by the issue.
- [ ] The diff contains only issue-owned changes.
- [ ] Focused checks passed and matched the intended tests.
- [ ] The final aggregate gate passed or is explicitly delegated to protected CI.
- [ ] Real external claims have real redacted evidence; untested surfaces are named.
- [ ] Temporary resources owned by this work are cleaned up or have an owner and cleanup point.
- [ ] No secret, identity, private path/content, hostname, address, or raw sensitive evidence is included.
- [ ] No merge, deployment, activation, destructive cleanup, tag, RC, or release is implied beyond explicit authority.
