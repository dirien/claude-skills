#!/usr/bin/env bash
# repo_setup.sh - create and configure pulumi-labs mirror repositories.
#
# HUMANS ONLY. The Autoregistry Publisher (or any agent) must NEVER run this
# script: agents do not create, configure or protect repositories, change
# visibility, or touch app installations. It needs an org admin's `gh` login,
# and it refuses to run without an interactive terminal to make an accidental
# agent run fail closed. See references/repo-setup.md for what each step does
# and why.
#
# Usage:
#   scripts/repo_setup.sh create  <slug>...   create the repos and apply every setting except the branch ruleset
#   scripts/repo_setup.sh protect <slug>...   apply the default-branch ruleset (run after the Publisher's first push)
#   scripts/repo_setup.sh verify  <slug>... [--imported]   check everything, exit 1 on any failure
# Options (before the slugs): --org ORG (default pulumi-labs), --dry-run (print the API calls, change nothing).
#
# <slug> is the provider name, for example `modal`; the repo is ORG/pulumi-<slug>.
# Needs: gh (logged in as an org admin with repo and admin:org scopes), jq, bash 4+.
set -euo pipefail

ORG="pulumi-labs"
DRY=0
ENVS=(publish-npm publish-pypi publish-nuget)
TAG_PATTERN='v*.*.*'
FAILS=0

usage() { sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; }
die() { echo "repo_setup: $*" >&2; exit 2; }
say() { printf '%s\n' "$*"; }

# api METHOD PATH [gh api args...]: run (or print) one GitHub API call; JSON body on stdin when --input - is given.
api() {
  local method="$1" path="$2"; shift 2
  if [ "$DRY" = 1 ]; then
    say "DRY-RUN gh api -X $method $path $*"
    if [[ " $* " == *" --input - "* ]]; then cat >&2; fi
    return 0
  fi
  gh api -X "$method" "$path" "$@"
}

require_human() {
  [ -z "${CI:-}" ] || die "refusing to run in CI. This script is for humans."
  if [ "$DRY" = 0 ] && ! { exec 3</dev/tty; } 2>/dev/null; then
    die "no interactive terminal. This script is for humans and never runs unattended."
  fi
}

confirm() { # confirm PROMPT
  [ "$DRY" = 1 ] && return 0
  local ans
  read -r -p "$1 [type yes] " ans <&3
  [ "$ans" = yes ] || die "aborted"
}

slug_ok() { [[ "$1" =~ ^[a-z0-9]+(-[a-z0-9]+)*$ ]]; }

pass() { say "PASS  $1"; }
fail() { say "FAIL  $1"; FAILS=$((FAILS + 1)); }
note() { say "NOTE  $1"; }

repo_exists() { gh api "repos/$1" >/dev/null 2>&1; }

cmd_create() {
  local slug repo
  for slug in "$@"; do
    slug_ok "$slug" || die "bad slug '$slug' (lowercase letters, digits, single hyphens)"
  done
  say "Will create PUBLIC, EMPTY repositories (no README, no license, no .gitignore):"
  for slug in "$@"; do say "  $ORG/pulumi-$slug"; done
  say "Reminder: the Registry name must already be decided and the card approved. Names cannot be undone."
  confirm "Create these under $ORG?"
  for slug in "$@"; do
    repo="$ORG/pulumi-$slug"
    if [ "$DRY" = 0 ] && repo_exists "$repo"; then
      note "$repo exists; applying settings only (not touching its contents)"
    else
      if [ "$DRY" = 1 ]; then
        say "DRY-RUN gh repo create $repo --public --description ..."
      else
        gh repo create "$repo" --public \
          --description "Pulumi provider for $slug (experimental, generated mirror of pulumi/autoregistry; do not edit by hand)"
      fi
    fi
    apply_settings "$repo"
    say "done: $repo. Next: install/verify the workprentice app covers it, then run: verify $slug"
  done
}

apply_settings() { # apply_settings ORG/REPO
  local repo="$1" env
  # General: issues on (engineering files feedback here), no wiki/projects, squash-only, delete merged branches.
  jq -n '{has_issues:true, has_wiki:false, has_projects:false, has_discussions:false,
          allow_squash_merge:true, allow_merge_commit:false, allow_rebase_merge:false, allow_auto_merge:false,
          allow_update_branch:true, delete_branch_on_merge:true,
          squash_merge_commit_title:"PR_TITLE", squash_merge_commit_message:"PR_BODY",
          security_and_analysis:{secret_scanning:{status:"enabled"}, secret_scanning_push_protection:{status:"enabled"}}}' \
    | api PATCH "repos/$repo" --input - --silent
  # Actions: enabled, read-only default token (release.yml declares what it needs), no Actions-created approvals.
  jq -n '{enabled:true, allowed_actions:"all"}' | api PUT "repos/$repo/actions/permissions" --input - --silent
  jq -n '{default_workflow_permissions:"read", can_approve_pull_request_reviews:false}' \
    | api PUT "repos/$repo/actions/permissions/workflow" --input - --silent
  jq -n '{approval_policy:"all_external_contributors"}' \
    | api PUT "repos/$repo/actions/permissions/fork-pr-contributor-approval" --input - --silent \
    || note "$repo: could not set fork-PR approval policy; set 'Require approval for all external contributors' in Settings > Actions"
  # Dependabot: alerts on (visibility), automated security PRs off (the monorepo is the source of truth).
  api PUT "repos/$repo/vulnerability-alerts" --silent
  api DELETE "repos/$repo/automated-security-fixes" --silent || true
  api PUT "repos/$repo/private-vulnerability-reporting" --silent \
    || note "$repo: could not enable private vulnerability reporting"
  # Deployment environments used by release.yml. Created here so they exist and only tag refs may deploy.
  for env in "${ENVS[@]}"; do
    jq -n '{deployment_branch_policy:{protected_branches:false, custom_branch_policies:true}}' \
      | api PUT "repos/$repo/environments/$env" --input - --silent
    jq -n --arg p "$TAG_PATTERN" '{name:$p, type:"tag"}' \
      | api POST "repos/$repo/environments/$env/deployment-branch-policies" --input - --silent 2>/dev/null || true
  done
  # Tag ruleset: only people with maintain/admin may create, move or delete v* tags. Agents never tag.
  if [ "$DRY" = 1 ] || ! gh api "repos/$repo/rulesets" --jq '.[].name' | grep -qx 'Release tags: people only'; then
    jq -n --arg p "refs/tags/v*" '{name:"Release tags: people only", target:"tag", enforcement:"active",
        conditions:{ref_name:{include:[$p], exclude:[]}},
        rules:[{type:"creation"},{type:"update"},{type:"deletion"},{type:"non_fast_forward"}],
        bypass_actors:[{actor_id:5, actor_type:"RepositoryRole", bypass_mode:"always"},
                       {actor_id:2, actor_type:"RepositoryRole", bypass_mode:"always"}]}' \
      | api POST "repos/$repo/rulesets" --input - --silent
  fi
}

cmd_protect() {
  local slug repo
  for slug in "$@"; do slug_ok "$slug" || die "bad slug '$slug'"; done
  for slug in "$@"; do
    repo="$ORG/pulumi-$slug"
    if [ "$DRY" = 0 ]; then
      gh api "repos/$repo/commits?per_page=1" >/dev/null 2>&1 \
        || die "$repo has no commits yet. Run protect only after the Publisher's initial import and a green test run."
    fi
    if [ "$DRY" = 0 ] && gh api "repos/$repo/rulesets" --jq '.[].name' | grep -qx 'Default branch: PR only'; then
      note "$repo already has the default-branch ruleset"
      continue
    fi
    confirm "Protect the default branch of $repo (PR required, squash only, check 'test', no force-push)?"
    # The app merges sync PRs: 0 approvals (Reviewer clearance is a PR comment, not a GitHub approval), required
    # check 'test'. Admins may bypass only through a pull request; nobody can force-push or delete the branch.
    jq -n '{name:"Default branch: PR only", target:"branch", enforcement:"active",
        conditions:{ref_name:{include:["~DEFAULT_BRANCH"], exclude:[]}},
        rules:[{type:"deletion"},{type:"non_fast_forward"},
               {type:"pull_request", parameters:{required_approving_review_count:0, dismiss_stale_reviews_on_push:false,
                  require_code_owner_review:false, require_last_push_approval:false,
                  required_review_thread_resolution:false, allowed_merge_methods:["squash"]}},
               {type:"required_status_checks", parameters:{strict_required_status_checks_policy:false,
                  required_status_checks:[{context:"test"}]}}],
        bypass_actors:[{actor_id:5, actor_type:"RepositoryRole", bypass_mode:"pull_request"}]}' \
      | api POST "repos/$repo/rulesets" --input - --silent
    say "done: $repo default branch protected"
  done
}

cmd_verify() {
  local imported=0 slugs=() a
  for a in "$@"; do
    if [ "$a" = "--imported" ]; then imported=1; else slugs+=("$a"); fi
  done
  [ "${#slugs[@]}" -gt 0 ] || die "verify needs at least one slug"
  local slug repo json env names
  for slug in "${slugs[@]}"; do
    slug_ok "$slug" || die "bad slug '$slug'"
    repo="$ORG/pulumi-$slug"
    say "== $repo"
    if ! json="$(gh api "repos/$repo" 2>/dev/null)"; then fail "repository does not exist or is not visible"; continue; fi
    [ "$(jq -r .visibility <<<"$json")" = public ] && pass "visibility public" || fail "visibility is $(jq -r .visibility <<<"$json"), want public"
    [ "$(jq -r .has_issues <<<"$json")" = true ] && pass "issues enabled" || fail "issues disabled"
    [ "$(jq -r .allow_squash_merge <<<"$json")" = true ] && [ "$(jq -r .allow_merge_commit <<<"$json")" = false ] \
      && pass "squash merge only" || fail "merge methods: want squash only"
    [ "$(jq -r .delete_branch_on_merge <<<"$json")" = true ] && pass "delete branch on merge" || fail "delete_branch_on_merge off"
    [ "$(jq -r '.security_and_analysis.secret_scanning.status // "off"' <<<"$json")" = enabled ] \
      && pass "secret scanning" || fail "secret scanning not enabled"
    [ "$(jq -r '.security_and_analysis.secret_scanning_push_protection.status // "off"' <<<"$json")" = enabled ] \
      && pass "push protection" || fail "push protection not enabled"
    gh api "repos/$repo/actions/permissions" --jq .enabled 2>/dev/null | grep -qx true \
      && pass "Actions enabled" || fail "Actions not enabled"
    [ "$(gh api "repos/$repo/actions/permissions/workflow" --jq .default_workflow_permissions 2>/dev/null)" = read ] \
      && pass "default workflow token is read-only" || fail "default workflow token is not read-only"
    for env in "${ENVS[@]}"; do
      gh api "repos/$repo/environments/$env" >/dev/null 2>&1 && pass "environment $env" || fail "environment $env missing"
    done
    names="$(gh api "repos/$repo/rulesets" --jq '.[].name' 2>/dev/null || true)"
    grep -qx 'Release tags: people only' <<<"$names" && pass "tag ruleset" || fail "tag ruleset missing"
    if [ "$imported" = 0 ]; then
      if gh api "repos/$repo/commits?per_page=1" >/dev/null 2>&1; then
        fail "repository is not empty; the Publisher's initial import needs an empty repo (no README)"
      else
        pass "repository is empty (ready for the initial import)"
      fi
    else
      [ "$(jq -r .default_branch <<<"$json")" = main ] && pass "default branch main" || fail "default branch is not main"
      grep -qx 'Default branch: PR only' <<<"$names" && pass "default-branch ruleset" || fail "default-branch ruleset missing; run protect"
      gh api "repos/$repo/contents/.github/workflows/release.yml" >/dev/null 2>&1 \
        && pass "release.yml present" || fail "release.yml missing"
    fi
  done
  say "== app installation"
  if gh api "orgs/$ORG/installations" --jq '.installations[] | select(.app_slug | test("workprentice")) | "\(.app_slug): repository_selection=\(.repository_selection)"' 2>/dev/null; then
    note "if repository_selection is 'selected', add each repo to the installation (Org settings > GitHub Apps > workprentice > Configure)"
  else
    note "cannot read org installations (needs admin:org). Check Org settings > GitHub Apps > workprentice: all repos, or each repo added;"
    note "permissions: Contents, Pull requests, Issues, Checks, Workflows (read and write). Then let the Publisher run access_check.py."
  fi
  note "manual: nothing here proves the app can push .github/workflows; the Publisher's first import push is the test (needs 'Workflows' permission)."
  [ "$FAILS" = 0 ] || { say "$FAILS check(s) failed"; exit 1; }
  say "all checks passed"
}

# Parse global options, then the subcommand.
args=()
while [ $# -gt 0 ]; do
  case "$1" in
    --org) [ $# -ge 2 ] || die "--org needs a value"; ORG="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) args+=("$1"); shift ;;
  esac
done
[ "${#args[@]}" -ge 1 ] || { usage; exit 2; }
sub="${args[0]}"
rest=("${args[@]:1}")
case "$sub" in
  create|protect|verify)
    [ "${#rest[@]}" -ge 1 ] || die "$sub needs at least one slug"
    command -v gh >/dev/null || die "gh not found"
    command -v jq >/dev/null || die "jq not found"
    require_human
    "cmd_$sub" "${rest[@]}" ;;
  *) usage; exit 2 ;;
esac
