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

## Required roles

| Role | Current public assignment | Boundary |
| --- | --- | --- |
| Repository owner/settings operator | **Unassigned — blocking** | Must be an administrator and may apply reviewed settings only with explicit owner authorization. |
| Worker | **Unassigned for storage v2 — blocking** | Implements issue-owned changes; cannot provide the independent required approval. |
| Independent reviewer/orchestrator | **Unassigned — blocking** | Must be a qualified, distinct GitHub identity with repository write access. |
| Merge authority | **Unassigned — blocking** | Merge authority does not imply deployment or release authority. |
| Runtime operator | **Unassigned** | Production operations require separate issue authority and evidence. |
| Destructive-action authority | **Unassigned** | Destructive cleanup requires an explicit target, backup/rollback boundary, and separate confirmation. |
| Deployment/release authority | **Unassigned** | Deployment, activation, tag, RC, and release remain separately authorized actions. |

As reviewed on 2026-08-13, the repository has one listed collaborator,
`@obtFusi`, with administrative access. Access is not an assignment of any role
above. Enabling a required independent approval before a second qualified
identity has write access would create an unfulfillable merge gate.
Storage-v2 implementation remains blocked until that role exists, the desired
settings are applied, and the enforcement tests below are observed.

## Desired `main` controls

- Required checks are strict and source-bound to the GitHub Actions app:
  `ci-required`, `workflow-policy`, and `issue-contract`.
- At least one approving review is required.
- Stale reviews are dismissed and the most recent push must be approved by an
  actor other than the pusher.
- All review conversations must be resolved.
- Administrators are covered; force pushes and branch deletion are disabled.
- Default workflow token permissions are read-only and workflows cannot approve
  pull requests.
- GitHub Actions SHA pinning is required at repository level.

The checked-in `.github/scripts/repository-settings.sh` separates readback from
mutation. Its `apply` mode is not authorization: the operator still needs an
explicit owner instruction, a provisioned independent reviewer, and a reviewed
diff. Post-apply readback is mandatory.

## Required enforcement evidence

Use synthetic public-safe PR content and retain only public GitHub object IDs,
head SHAs, check sources, settings readback, and repository-file hashes. Observe:

1. missing or malformed issue/PR contract is blocked;
2. a failed required check is blocked;
3. missing independent approval is blocked;
4. an unresolved review conversation is blocked;
5. a new worker push invalidates the reviewed state;
6. the worker cannot satisfy its own required approval;
7. a positive PR becomes mergeable only after all intended gates pass.

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
