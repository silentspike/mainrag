#!/usr/bin/env bash
set -euo pipefail

readonly MODE="${1:-check}"
readonly REPOSITORY="${REPOSITORY:-silentspike/mainrag}"
readonly BRANCH="${BRANCH:-main}"
readonly REQUIRED_CONTEXTS=(ci-required workflow-policy issue-contract)

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

require_tools() {
  command -v gh >/dev/null || die 'gh is required'
  command -v jq >/dev/null || die 'jq is required'
  command -v sha256sum >/dev/null || die 'sha256sum is required'
  [[ "$REPOSITORY" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die 'invalid REPOSITORY'
  [[ "$BRANCH" =~ ^[A-Za-z0-9._/-]+$ ]] || die 'invalid BRANCH'
}

github_actions_app_id() {
  local response app_ids
  response="$(gh api \
    -H 'Accept: application/vnd.github+json' \
    "repos/${REPOSITORY}/commits/${BRANCH}/check-runs?per_page=100")"
  app_ids="$(jq -r \
    '[.check_runs[] | select(.app.slug == "github-actions") | .app.id] | unique | .[]' \
    <<<"$response")"
  [[ -n "$app_ids" ]] || die 'no GitHub Actions check source observed on the current branch head'
  [[ "$(wc -l <<<"$app_ids")" -eq 1 ]] || die 'multiple GitHub Actions app IDs observed'
  printf '%s\n' "$app_ids"
}

readback() {
  local metadata protection actions workflow app_id
  metadata="$(gh api -H 'Accept: application/vnd.github+json' "repos/${REPOSITORY}")"
  protection="$(gh api -H 'Accept: application/vnd.github+json' \
    "repos/${REPOSITORY}/branches/${BRANCH}/protection")"
  actions="$(gh api -H 'Accept: application/vnd.github+json' \
    "repos/${REPOSITORY}/actions/permissions")"
  workflow="$(gh api -H 'Accept: application/vnd.github+json' \
    "repos/${REPOSITORY}/actions/permissions/workflow")"
  app_id="$(github_actions_app_id)"

  jq -n \
    --arg reviewed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --arg repository "$REPOSITORY" \
    --arg branch "$BRANCH" \
    --arg app_id "$app_id" \
    --argjson metadata "$metadata" \
    --argjson protection "$protection" \
    --argjson actions "$actions" \
    --argjson workflow "$workflow" \
    '{
      reviewed_at: $reviewed_at,
      repository: $repository,
      visibility: $metadata.visibility,
      default_branch: $metadata.default_branch,
      branch: $branch,
      branch_protection: {
        strict: $protection.required_status_checks.strict,
        checks: [$protection.required_status_checks.checks[] | {context, app_id}],
        pull_request_review_configuration_present: ($protection.required_pull_request_reviews != null),
        required_approvals: $protection.required_pull_request_reviews.required_approving_review_count,
        dismiss_stale_reviews: $protection.required_pull_request_reviews.dismiss_stale_reviews,
        require_last_push_approval: $protection.required_pull_request_reviews.require_last_push_approval,
        conversation_resolution: $protection.required_conversation_resolution.enabled,
        enforce_admins: $protection.enforce_admins.enabled,
        force_pushes: $protection.allow_force_pushes.enabled,
        deletions: $protection.allow_deletions.enabled
      },
      actions: {
        enabled: $actions.enabled,
        allowed_actions: $actions.allowed_actions,
        sha_pinning_required: $actions.sha_pinning_required,
        default_workflow_permissions: $workflow.default_workflow_permissions,
        can_approve_pull_request_reviews: $workflow.can_approve_pull_request_reviews
      },
      expected_github_actions_app_id: ($app_id | tonumber)
    }'

  find .github/ISSUE_TEMPLATE .github/workflows .github/scripts \
    -maxdepth 1 -type f -print0 \
    | sort -z \
    | xargs -0 sha256sum
  sha256sum .github/PULL_REQUEST_TEMPLATE.md .github/GOVERNANCE.md .gitignore
}

protection_payload() {
  local app_id="${1:-}" checks_json
  [[ "$app_id" =~ ^[1-9][0-9]*$ ]] || die 'positive GitHub Actions app ID required'
  checks_json="$(printf '%s\n' "${REQUIRED_CONTEXTS[@]}" \
    | jq -R --argjson app_id "$app_id" '{context: ., app_id: $app_id}' \
    | jq -s '.')"
  jq -n --argjson checks "$checks_json" \
    '{
      required_status_checks: {strict: true, checks: $checks},
      enforce_admins: true,
      required_pull_request_reviews: {
        dismiss_stale_reviews: true,
        require_code_owner_reviews: false,
        required_approving_review_count: 0,
        require_last_push_approval: false
      },
      restrictions: null,
      required_conversation_resolution: true,
      allow_force_pushes: false,
      allow_deletions: false
    }'
}

apply_settings() {
  local operator authenticated_actor operator_permission app_id payload
  [[ "${CONFIRM_REPOSITORY:-}" == "$REPOSITORY" ]] || \
    die 'set CONFIRM_REPOSITORY to the exact repository'
  [[ "${CONFIRM_OWNER_SETTINGS_APPLY:-}" == 'yes' ]] || \
    die 'explicit owner settings authorization is required'
  [[ "${CONFIRM_SINGLE_MAINTAINER_MODEL:-}" == 'yes' ]] || \
    die 'confirm the documented single-maintainer responsibility model'
  operator="${SETTINGS_OPERATOR:-}"
  [[ "$operator" =~ ^[A-Za-z0-9-]+$ ]] || die 'SETTINGS_OPERATOR is required'
  authenticated_actor="$(gh api user --jq '.login')"
  [[ "$authenticated_actor" == "$operator" ]] || \
    die 'authenticated actor does not match SETTINGS_OPERATOR'
  operator_permission="$(gh api \
    "repos/${REPOSITORY}/collaborators/${operator}/permission" \
    --jq '.permission')"
  [[ "$operator_permission" == 'admin' ]] || die 'settings operator needs admin permission'

  [[ "$(gh api "repos/${REPOSITORY}" --jq '.visibility')" == 'public' ]] || \
    die 'repository visibility changed; stop for review'
  [[ "$(gh api "repos/${REPOSITORY}" --jq '.default_branch')" == "$BRANCH" ]] || \
    die 'default branch changed; stop for review'

  app_id="$(github_actions_app_id)"
  payload="$(protection_payload "$app_id")"

  gh api --method PUT \
    -H 'Accept: application/vnd.github+json' \
    "repos/${REPOSITORY}/branches/${BRANCH}/protection" \
    --input - <<<"$payload" >/dev/null
  gh api --method PUT \
    -H 'Accept: application/vnd.github+json' \
    "repos/${REPOSITORY}/actions/permissions" \
    -F enabled=true \
    -f allowed_actions=all \
    -F sha_pinning_required=true >/dev/null
  gh api --method PUT \
    -H 'Accept: application/vnd.github+json' \
    "repos/${REPOSITORY}/actions/permissions/workflow" \
    -f default_workflow_permissions=read \
    -F can_approve_pull_request_reviews=false >/dev/null
  gh label create no-issue-required \
    --repo "$REPOSITORY" \
    --color D4C5F9 \
    --description 'Reviewed mechanical Dependabot update; primary engineering issue not required' \
    --force >/dev/null

  readback
}

# Sourcing exposes the pure payload builder for offline tests, not live actions.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  require_tools
  case "$MODE" in
    check) readback ;;
    apply) apply_settings ;;
    *) die 'usage: repository-settings.sh [check|apply]' ;;
  esac
fi
