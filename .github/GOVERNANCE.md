# MainRAG repository governance

This document describes the public repository controls required before the
storage-v2 implementation epic can start. It does not grant authority to merge,
deploy, activate, delete data, tag, or release.

## Repository boundary

- The repository is public and the protected default branch is `main`.
- Public workflows use GitHub-hosted `ubuntu-24.04` runners only.
- Workflow permissions are least-privilege, jobs have explicit timeouts, and
  reusable actions are pinned to full commit SHAs.
- Pull-request metadata is treated as untrusted data. A
  `pull_request_target` workflow may read metadata and trusted base-revision
  code, but must never execute or check out pull-request head code.
- The private root `AGENTS.md` is deliberately ignored. It contains local
  worker instructions and must never be committed, quoted in an issue, or
  copied into public evidence.

## Work-item contract

Every implementation PR has exactly one primary open task, story, or bug. An
epic may be referenced only as a parent. The primary issue owns scope,
dependencies, acceptance criteria, verification, cleanup, stop conditions, and
closure mode. A PR uses `Closes #N` only when the issue explicitly permits
closure on merge; otherwise it uses `Refs #N`.

The `issue-contract` check validates the public structure. It does not establish
that acceptance criteria have been met, that evidence is truthful, or that a
deployment or release occurred.

## Single-maintainer responsibility model

| Role | Current public assignment | Boundary |
| --- | --- | --- |
| Repository owner/settings operator | Sole maintainer | Must be an administrator and may apply settings only with explicit owner authorization. |
| Worker | Sole maintainer | Implements issue-owned changes and records exact commands and results. |
| Verification controller | Sole maintainer | Performs a fresh end-control pass at the exact candidate head. This is self-verification, not independent review. |
| Merge authority | Separately authorized sole-maintainer action | Merge authority does not follow from implementation or verification and does not imply deployment or release authority. |
| Runtime operator | **Unassigned** | Production operations require separate issue authority and evidence. |
| Destructive-action authority | **Unassigned** | Destructive cleanup requires an explicit target, backup/rollback boundary, and separate confirmation. |
| Deployment/release authority | **Unassigned** | Deployment, activation, tag, RC, and release remain separately authorized actions. |

The repository has a single maintainer. Requiring a distinct approving identity
would create an unfulfillable merge gate, so the repository does not claim
independent review. The maintainer owns implementation and a separate, fresh
end-control pass over the exact candidate head. Public evidence must identify
that limitation rather than presenting self-verification as independent
acceptance.

## Desired `main` controls

- Required checks are strict and source-bound to the GitHub Actions app:
  `ci-required`, `workflow-policy`, and `issue-contract`.
- No approving review is required while only one qualified maintainer exists.
- A changed head invalidates prior verification evidence and requires the
  protected checks and maintainer end-control pass to run again.
- All review conversations must be resolved.
- Administrators are covered; force pushes and branch deletion are disabled.
- Default workflow token permissions are read-only and workflows cannot approve
  pull requests.
- GitHub Actions SHA pinning is required at repository level.

The checked-in `.github/scripts/repository-settings.sh` separates readback from
mutation. Its `apply` mode is not authorization: the operator still needs an
explicit owner instruction and must explicitly confirm the single-maintainer
model. Post-apply readback is mandatory.

## Required enforcement evidence

Use synthetic public-safe PR content and retain only public GitHub object IDs,
head SHAs, check sources, settings readback, and repository-file hashes. Observe:

1. missing or malformed issue/PR contract is blocked;
2. a failed required check is blocked;
3. an unresolved review conversation is blocked;
4. a changed head reruns all head-bound checks and invalidates older evidence;
5. a positive PR becomes mergeable only after all intended automated gates
   pass and the sole maintainer records the fresh end-control result.

None of these tests may deploy, activate, publish a release, use a self-hosted
runner, expose a secret, or execute untrusted PR content. Test branches and
temporary artifacts are removed only under explicit cleanup authority.

## Evidence states

Keep these states distinct: source present, configured, check reported,
required, review approved, mergeable, merged, deployed, activated, cleaned up,
and released. A green workflow or settings payload proves none of the later
states by itself.

For a baseline review, record the date, default-branch head, repository
visibility, branch-protection readback, workflow-permission readback, expected
check app ID, and SHA-256 hashes of the templates, governance scripts, and
workflows. Refresh volatile facts immediately before any settings mutation.
