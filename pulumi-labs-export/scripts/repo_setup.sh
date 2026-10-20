#!/usr/bin/env bash
# repo_setup.sh - create and configure pulumi-labs mirror repositories.
#
# create, protect and verify are HUMANS ONLY. The Autoregistry Publisher (or any agent) must NEVER run this
# script's create, protect or verify: they need an org admin's own `gh` login, and they refuse to run without an
# interactive terminal so that an accidental agent run fails closed.
# An agent uses `plan` instead, only for the one repository of the provider it is carving, and only when SKILL.md
# ("Repository setup") allows it. `plan` changes nothing and calls nothing: it writes the request bodies and prints
# read-only `gh api` checks. The agent runs each check as its own direct `gh` command. A check prints a `RUN <command>`
# line only for a setting that is missing or wrong, so a re-run after a partial failure does only what is left.
# See references/repo-setup.md for what each step does and why.
#
# Usage:
#   scripts/repo_setup.sh create  <slug>...   create the repos and apply every setting except the branch ruleset
#   scripts/repo_setup.sh protect <slug>...   apply the default-branch ruleset (run after the Publisher's first push)
#   scripts/repo_setup.sh verify  <slug>... [--imported]   check everything, exit 1 on any failure
#   scripts/repo_setup.sh plan create|protect|verify <slug> [--imported]   the agent path: print the checks, change nothing
# Options (before the slugs): --org ORG (default pulumi-labs; plan accepts only pulumi-labs),
#   --dry-run (print the API calls, change nothing),
#   --out DIR (plan only: where the request bodies go; default $HOME/.local/pulumi-labs-export/repo-setup/pulumi-<slug>).
#
# <slug> is the provider name, for example `modal`; the repo is ORG/pulumi-<slug>.
# Needs: jq and bash 4+. create, protect and verify also need gh, logged in as an org admin with repo and admin:org scopes.

# shellcheck disable=SC2016 # the single-quoted $r, $w, $n ... below are jq variables in the plan's programs
set -euo pipefail

ORG="pulumi-labs"
DRY=0
OUT=""
ENVS=(publish-npm publish-pypi publish-nuget)
TAG_PATTERN='v*.*.*'
TAG_RULESET='Release tags: people only'
BRANCH_RULESET='Default branch: PR only'
FAILS=0

usage() { awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; }
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

# Request bodies. The human path and `plan` send exactly these, so the two cannot drift apart.
describe() { printf 'Pulumi provider for %s (experimental, generated mirror of pulumi/autoregistry; do not edit by hand)' "$1"; }
body_create() { # body_create SLUG: public, empty (no README, license or .gitignore)
  jq -n --arg name "pulumi-$1" --arg d "$(describe "$1")" \
    '{name:$name, description:$d, visibility:"public", auto_init:false, has_issues:true, has_wiki:false, has_projects:false}'
}
# General: issues on (engineering files feedback here), no wiki/projects, squash-only, delete merged branches.
body_settings() {
  jq -n '{has_issues:true, has_wiki:false, has_projects:false, has_discussions:false,
          allow_squash_merge:true, allow_merge_commit:false, allow_rebase_merge:false, allow_auto_merge:false,
          allow_update_branch:true, delete_branch_on_merge:true,
          squash_merge_commit_title:"PR_TITLE", squash_merge_commit_message:"PR_BODY",
          security_and_analysis:{secret_scanning:{status:"enabled"}, secret_scanning_push_protection:{status:"enabled"}}}'
}
# Actions: enabled, read-only default token (release.yml declares what it needs), no Actions-created approvals.
body_actions() { jq -n '{enabled:true, allowed_actions:"all"}'; }
body_workflow() { jq -n '{default_workflow_permissions:"read", can_approve_pull_request_reviews:false}'; }
body_fork_pr() { jq -n '{approval_policy:"all_external_contributors"}'; }
# Deployment environments used by release.yml: only tag refs may deploy.
body_environment() { jq -n '{deployment_branch_policy:{protected_branches:false, custom_branch_policies:true}}'; }
body_tag_policy() { jq -n --arg p "$TAG_PATTERN" '{name:$p, type:"tag"}'; }
# Tag ruleset: only people with maintain/admin may create, move or delete v* tags. Agents never tag.
body_tag_ruleset() {
  jq -n --arg n "$TAG_RULESET" --arg p "refs/tags/v*" '{name:$n, target:"tag", enforcement:"active",
      conditions:{ref_name:{include:[$p], exclude:[]}},
      rules:[{type:"creation"},{type:"update"},{type:"deletion"},{type:"non_fast_forward"}],
      bypass_actors:[{actor_id:5, actor_type:"RepositoryRole", bypass_mode:"always"},
                     {actor_id:2, actor_type:"RepositoryRole", bypass_mode:"always"}]}'
}
# The app merges sync PRs: 0 approvals (Reviewer clearance is a PR comment, not a GitHub approval), required
# check 'test'. Admins may bypass only through a pull request; nobody can force-push or delete the branch.
body_branch_ruleset() {
  jq -n --arg n "$BRANCH_RULESET" '{name:$n, target:"branch", enforcement:"active",
      conditions:{ref_name:{include:["~DEFAULT_BRANCH"], exclude:[]}},
      rules:[{type:"deletion"},{type:"non_fast_forward"},
             {type:"pull_request", parameters:{required_approving_review_count:0, dismiss_stale_reviews_on_push:false,
                require_code_owner_review:false, require_last_push_approval:false,
                required_review_thread_resolution:false, allowed_merge_methods:["squash"]}},
             {type:"required_status_checks", parameters:{strict_required_status_checks_policy:false,
                required_status_checks:[{context:"test"}]}}],
      bypass_actors:[{actor_id:5, actor_type:"RepositoryRole", bypass_mode:"pull_request"}]}'
}

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
        gh repo create "$repo" --public --description "$(describe "$slug")"
      fi
    fi
    apply_settings "$repo"
    say "done: $repo. Next: install/verify the workprentice app covers it, then run: verify $slug"
  done
}

apply_settings() { # apply_settings ORG/REPO
  local repo="$1" env
  body_settings | api PATCH "repos/$repo" --input - --silent
  body_actions | api PUT "repos/$repo/actions/permissions" --input - --silent
  body_workflow | api PUT "repos/$repo/actions/permissions/workflow" --input - --silent
  body_fork_pr | api PUT "repos/$repo/actions/permissions/fork-pr-contributor-approval" --input - --silent \
    || note "$repo: could not set fork-PR approval policy; set 'Require approval for all external contributors' in Settings > Actions"
  # Dependabot: alerts on (visibility), automated security PRs off (the monorepo is the source of truth).
  api PUT "repos/$repo/vulnerability-alerts" --silent
  api DELETE "repos/$repo/automated-security-fixes" --silent || true
  api PUT "repos/$repo/private-vulnerability-reporting" --silent \
    || note "$repo: could not enable private vulnerability reporting"
  # Deployment environments used by release.yml. Created here so they exist and only tag refs may deploy.
  for env in "${ENVS[@]}"; do
    body_environment | api PUT "repos/$repo/environments/$env" --input - --silent
    body_tag_policy | api POST "repos/$repo/environments/$env/deployment-branch-policies" --input - --silent 2>/dev/null || true
  done
  if [ "$DRY" = 1 ] || ! gh api "repos/$repo/rulesets" --jq '.[].name' | grep -qx "$TAG_RULESET"; then
    body_tag_ruleset | api POST "repos/$repo/rulesets" --input - --silent
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
    if [ "$DRY" = 0 ] && gh api "repos/$repo/rulesets" --jq '.[].name' | grep -qx "$BRANCH_RULESET"; then
      note "$repo already has the default-branch ruleset"
      continue
    fi
    confirm "Protect the default branch of $repo (PR required, squash only, check 'test', no force-push)?"
    body_branch_ruleset | api POST "repos/$repo/rulesets" --input - --silent
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
    grep -qx "$TAG_RULESET" <<<"$names" && pass "tag ruleset" || fail "tag ruleset missing"
    if [ "$imported" = 0 ]; then
      if gh api "repos/$repo/commits?per_page=1" >/dev/null 2>&1; then
        fail "repository is not empty; the Publisher's initial import needs an empty repo (no README)"
      else
        pass "repository is empty (ready for the initial import)"
      fi
    else
      [ "$(jq -r .default_branch <<<"$json")" = main ] && pass "default branch main" || fail "default branch is not main"
      grep -qx "$BRANCH_RULESET" <<<"$names" && pass "default-branch ruleset" || fail "default-branch ruleset missing; run protect"
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

# ---------------------------------------------------------------------------------------------------------------
# plan: the agent path. Credentials reach only a `gh` the agent invokes directly, never one started by this script,
# so `plan` calls nothing. Each printed check is a GET whose --jq program compares the live setting with the request
# body and prints `ok`, or `fix` plus the exact write (`RUN`/`TRY`) that sets it. The comparison runs in gh's jq at
# check time, so the plan text never depends on stale state.

# shq ARG: ARG quoted for a POSIX shell (unquoted when it is plainly safe).
shq() {
  if [[ "$1" =~ ^[A-Za-z0-9_./:=@%+,-]+$ ]]; then printf '%s' "$1"; else printf "'%s'" "${1//\'/\'\\\'\'}"; fi
}
# jlit TEXT: TEXT as a jq string literal.
jlit() { jq -n --arg s "$1" '$s'; }
# oneline TEXT: TEXT on one line with runs of blanks squeezed (jq programs are written over several lines below).
oneline() {
  local s="${1//$'\n'/ }"
  while [[ "$s" == *"  "* ]]; do s="${s//  / }"; done
  s="${s# }"; printf '%s' "${s% }"
}
# fill TEMPLATE KEY VALUE...: replace every @KEY@ in TEMPLATE with VALUE.
fill() {
  local t="$1"; shift
  while [ $# -ge 2 ]; do t="${t//@$1@/"$2"}"; shift 2; done
  printf '%s' "$t"
}
# write CMD: the gh api write that the plan prints after RUN or TRY.
write() { # write METHOD PATH [BODYFILE]
  local s
  s="gh api -X $1 $(shq "$2")"
  if [ -n "${3:-}" ]; then s+=" --input $(shq "$OUT/$3")"; fi
  printf '%s --silent' "$s"
}
emit_check() { # emit_check PATH JQ-PROGRAM
  say "gh api $(shq "$1") --jq $(shq "$(oneline "$2")")"
}
emit_raw() { say "gh api $(shq "$1") --silent"; }

# Programs. In fix mode a wrong setting prints "fix ..." and the write; in verify mode it prints FAIL (or NOTE).
P_FIELDS_FIX='. as $r | @WANT@ as $w
  | [$w | to_entries[] | select($r[.key] != .value) | .key + " is " + ($r[.key] | tojson) + ", want " + (.value | tojson)] as $bad
  | if ($bad | length) == 0 then @OK@ else ($bad[] | @FIX@ + .), @WRITE@ end'
P_FIELDS_VERIFY='. as $r | @WANT@ as $w | $w | to_entries[]
  | if $r[.key] == .value then @PASS@ + .key + " = " + (.value | tojson) else @BAD@ + .key + " is " + ($r[.key] | tojson) + ", want " + (.value | tojson) end'

P_REPO_FIX='. as $r | @WANT@ as $w
  | if $r.archived then @STOPARCHIVED@
    elif $r.visibility != "public" then @STOPVIS@ + $r.visibility + ". Never change visibility; report it."
    elif $r.security_and_analysis == null then @STOPADMIN@
    else ([$w | del(.security_and_analysis) | to_entries[] | select($r[.key] != .value)
            | .key + " is " + ($r[.key] | tojson) + ", want " + (.value | tojson)]
          + [$w.security_and_analysis | to_entries[] | ($r.security_and_analysis[.key].status // "disabled") as $h
            | select($h != .value.status) | .key + " is " + $h + ", want " + .value.status]) as $bad
      | if ($bad | length) == 0 then "ok repository settings" else ($bad[] | "fix repository: " + .), @WRITE@ end
    end'
P_REPO_VERIFY='. as $r | @WANT@ as $w
  | (if $r.visibility == "public" then "PASS visibility public" else "FAIL visibility is " + $r.visibility + ", want public" end),
    (if $r.archived then "FAIL repository is archived" else empty end),
    (if $r.security_and_analysis == null then "FAIL security settings are not visible: this identity is not an admin of the repository" else empty end),
    ($w | del(.security_and_analysis) | to_entries[]
      | if $r[.key] == .value then "PASS " + .key + " = " + (.value | tojson) else "FAIL " + .key + " is " + ($r[.key] | tojson) + ", want " + (.value | tojson) end),
    ($w.security_and_analysis | to_entries[] | ($r.security_and_analysis[.key].status // "disabled") as $h
      | if $h == .value.status then "PASS " + .key + " " + $h else "FAIL " + .key + " is " + $h + ", want " + .value.status end)
    @MAIN@'

P_ENVS='([.environments[] | {key: .name, value: .deployment_branch_policy}] | from_entries) as $e
  | @ENVS@[] as $n
  | if ($e | has($n) | not) then @MISSING@
    elif ($e[$n].custom_branch_policies != true or $e[$n].protected_branches != false)
    then @WRONG@
    else @GOOD@ end'

P_POLICIES='[.branch_policies[] | {name, type: (.type // "branch")}] as $p
  | ([$p[] | select(.name == @PAT@ and .type == "tag")] | length) as $t
  | if $t > 0 and ($p | length) == $t then @GOOD@
    elif ($p | length) == 0 then @MISSING@
    else @OTHER@ + ($p | tojson) + @OTHERSUF@ end'

P_RULESETS='[.[] | select(.name == @NAME@ and .source_type == "Repository")] as $m
  | if ($m | length) == 0 then @MISSING@
    elif ($m | length) > 1 then @MANY@
    else @CHECK@ + ($m[0].id | tostring) + " --jq " + (@DETAIL@ | @sh) end'

# Compares a ruleset with its request body: target, enforcement, ref conditions, rule types, the parameters the body
# sets (status checks by context name) and the bypass actors.
P_RULESET_DETAIL='def n: {target, enforcement, include: ((.conditions.ref_name.include // []) | sort),
      exclude: ((.conditions.ref_name.exclude // []) | sort), rules: ([.rules[]?.type] | sort)};
  def c: if type == "array" then map(if type == "object" then .context else . end) | sort else . end;
  def b: [.bypass_actors[]? | {actor_id, actor_type, bypass_mode}] | sort_by(.actor_id);
  . as $r | @WANT@ as $w
  | [ (($w | n) as $x | ($r | n) as $y | $x | keys[] | select($x[.] != $y[.])
        | . + " is " + ($y[.] | tojson) + ", want " + ($x[.] | tojson)),
      ($w.rules[] | . as $wr | ($r.rules[]? | select(.type == $wr.type) | .parameters // {}) as $hp
        | ($wr.parameters // {}) | to_entries[] | select((.value | c) != ($hp[.key] | c))
        | "rule " + $wr.type + " " + .key + " is " + ($hp[.key] | c | tojson) + ", want " + (.value | c | tojson)),
      (if $r.bypass_actors == null then "bypass actors are not visible to this identity"
       elif ($r | b) != ($w | b) then "bypass actors are " + ($r | b | tojson) + ", want " + ($w | b | tojson)
       else empty end) ] as $bad
  | if ($bad | length) == 0 then @GOOD@ else @BADLINES@ end'

plan_header() { # plan_header SUB
  local sub="$1" next
  say "# repo_setup.sh plan $sub $REPO"
  say "# Request bodies: $OUT (rewritten by every plan run; never edit them)."
  say "# Run each line below that does not start with '#' as its own command, in order: a direct gh command (never"
  say "# through a shell or a script), no pipes, no redirects, no cd. These lines only read."
  if [ "$sub" = verify ]; then
    say "# They print PASS, FAIL and NOTE lines. A 'CHECK <cmd>' line is a read-only follow-up check: run <cmd> now and"
    say "# treat its output the same way."
    say "# A '# on HTTP <code>:' comment says what that status means for the command right below it. Any other failure of"
    say "# a line counts as a FAIL."
    say "# The repository is ready when every line ran and none printed FAIL. Report every FAIL and NOTE line."
    return
  fi
  next="bash $(shq "$0") plan verify $SLUG"
  [ "$sub" = protect ] && next+=" --imported"
  say "# Each prints lines that mean:"
  say "#   ok ...       nothing to do"
  say "#   fix ...      what is missing or wrong; the RUN or TRY line after it sets it"
  say "#   RUN <cmd>    run <cmd> now, as its own command, before the next line. If it fails: STOP."
  say "#   TRY <cmd>    run <cmd> now, as its own command. If it fails, report it as a note and go on. Run each TRY"
  say "#                command at most once."
  say "#   CHECK <cmd>  a read-only follow-up check: run <cmd> now and treat its output the same way."
  say "#   STOP ...     stop the repository setup and report the line. Never work around it."
  say "# A '# on HTTP <code>:' comment says what that status means for the command right below it. Any other failure"
  say "# (an unexpected status, 401, 403, 5xx): STOP and report the command and the status."
  say "# Repeat the whole pass until a pass prints no RUN line, at most four passes (a RUN line in the fourth: STOP)."
  say "# Then run: $next"
}

plan_repo() { # plan_repo MODE (fix|verify) IMPORTED
  local mode="$1" imported="$2" want main=""
  want="$(body_settings | jq -c .)"
  say "#"
  say "# Repository"
  if [ "$mode" = fix ]; then
    say "# on HTTP 404: $REPO does not exist yet: RUN $(write POST "orgs/$ORG/repos" create.json)"
    say "#   then run this line again. If that create call fails: STOP and use the issue path."
    emit_check "repos/$REPO" "$(fill "$P_REPO_FIX" WANT "$want" \
      STOPARCHIVED "$(jlit "STOP $REPO is archived. Never unarchive it; report it.")" \
      STOPVIS "$(jlit "STOP $REPO is not public: it is ")" \
      STOPADMIN "$(jlit "STOP the security settings of $REPO are not visible: this identity is not an admin of it. Use the issue path.")" \
      WRITE "$(jlit "RUN $(write PATCH "repos/$REPO" settings.json)")")"
    say "# on HTTP 409 (Git Repository is empty): ok, the repository is empty."
    emit_check "repos/$REPO/commits?per_page=1" \
      "$(jlit "STOP $REPO already has commits; the initial import needs an empty repository. Stop and escalate.")"
  else
    [ "$imported" = 1 ] && main=', (if $r.default_branch == "main" then "PASS default branch main" else "FAIL default branch is " + $r.default_branch + ", want main" end)'
    say "# on HTTP 404: FAIL $REPO does not exist or is not visible."
    emit_check "repos/$REPO" "$(fill "$P_REPO_VERIFY" WANT "$want" MAIN "$main")"
    if [ "$imported" = 1 ]; then
      say "# on HTTP 409 (Git Repository is empty): FAIL the repository is empty."
      emit_check "repos/$REPO/commits?per_page=1" '"PASS repository has commits"'
    else
      say "# on HTTP 409 (Git Repository is empty): PASS the repository is empty (ready for the initial import)."
      emit_check "repos/$REPO/commits?per_page=1" \
        '"FAIL repository is not empty; the initial import needs an empty repository (no README)"'
    fi
  fi
}

plan_fields() { # plan_fields MODE PATH LABEL BODY-FUNCTION WRITE-KIND(RUN|TRY) WRITE-METHOD [BODYFILE]
  local mode="$1" path="$2" label="$3" body="$4" kind="$5" method="$6" file="${7:-}" want
  want="$("$body" | jq -c .)"
  if [ "$mode" = fix ]; then
    emit_check "$path" "$(fill "$P_FIELDS_FIX" WANT "$want" OK "$(jlit "ok $label")" FIX "$(jlit "fix $label: ")" \
      WRITE "$(jlit "$kind $(write "$method" "$path" "$file")")")"
  else
    local bad=FAIL
    [ "$kind" = TRY ] && bad=NOTE
    emit_check "$path" "$(fill "$P_FIELDS_VERIFY" WANT "$want" PASS "$(jlit "PASS $label: ")" BAD "$(jlit "$bad $label: ")")"
  fi
}
body_pvr_on() { jq -n '{enabled:true}'; }
body_asf_off() { jq -n '{enabled:false}'; }

plan_security() { # plan_security MODE
  local mode="$1" r="repos/$REPO"
  say "#"
  say "# Actions"
  if [ "$mode" = fix ]; then
    say "# on HTTP 403 or 404: this identity is not an admin of $REPO (this read needs the Administration permission):"
    say "#   STOP and use the issue path."
  else
    say "# on HTTP 403 or 404: FAIL this identity cannot read the Actions settings (it is not an admin of $REPO)."
  fi
  plan_fields "$mode" "$r/actions/permissions" "actions" body_actions RUN PUT actions.json
  plan_fields "$mode" "$r/actions/permissions/workflow" "workflow token" body_workflow RUN PUT workflow.json
  if [ "$mode" = fix ]; then
    say "# on HTTP 404 or 422: the setting is not available here: report it as a note (a human sets 'Require approval"
    say "#   for all external contributors' in Settings > Actions)."
  else
    say "# on HTTP 404 or 422: NOTE the fork-PR approval policy cannot be read here; a human checks Settings > Actions."
  fi
  plan_fields "$mode" "$r/actions/permissions/fork-pr-contributor-approval" "fork PR approval" body_fork_pr TRY PUT fork-pr.json
  say "#"
  say "# Dependabot and vulnerability reporting"
  if [ "$mode" = fix ]; then
    say "# Prints nothing and exits 0 when Dependabot alerts are on (HTTP 204): ok."
    say "# on HTTP 404: they are off: RUN $(write PUT "$r/vulnerability-alerts")"
  else
    say "# Prints nothing and exits 0 when Dependabot alerts are on (HTTP 204): PASS dependabot alerts."
    say "# on HTTP 404: FAIL dependabot alerts are off."
  fi
  emit_raw "$r/vulnerability-alerts"
  if [ "$mode" = fix ]; then
    say "# on HTTP 404: ok (Dependabot security updates are off)."
  else
    say "# on HTTP 404: PASS dependabot security updates are off."
  fi
  plan_fields "$mode" "$r/automated-security-fixes" "dependabot security updates" body_asf_off TRY DELETE
  plan_fields "$mode" "$r/private-vulnerability-reporting" "private vulnerability reporting" body_pvr_on TRY PUT
}

plan_environments() { # plan_environments MODE
  local mode="$1" r="repos/$REPO" envs env pre suf
  envs="$(printf '%s\n' "${ENVS[@]}" | jq -R . | jq -sc .)"
  say "#"
  say "# Deployment environments (release.yml): deployable only from tags $TAG_PATTERN"
  pre="$(write PUT "$r/environments/@ENV@" environment.json)"
  suf="${pre#*@ENV@}"; pre="${pre%%@ENV@*}"
  if [ "$mode" = fix ]; then
    emit_check "$r/environments" "$(fill "$P_ENVS" ENVS "$envs" \
      MISSING "\"fix environment \" + \$n + \" is missing\", $(jlit "RUN $pre") + \$n + $(jlit "$suf")" \
      WRONG "\"fix environment \" + \$n + \": deployment branch policy is \" + (\$e[\$n] | tojson) + \", want custom policies only\", $(jlit "RUN $pre") + \$n + $(jlit "$suf")" \
      GOOD '"ok environment " + $n')"
  else
    emit_check "$r/environments" "$(fill "$P_ENVS" ENVS "$envs" \
      MISSING '"FAIL environment " + $n + " is missing"' \
      WRONG '"FAIL environment " + $n + ": deployment branch policy is " + ($e[$n] | tojson) + ", want custom policies only"' \
      GOOD '"PASS environment " + $n')"
  fi
  for env in "${ENVS[@]}"; do
    if [ "$mode" = fix ]; then
      say "# on HTTP 404: environment $env does not exist yet. Run the RUN line the environments check printed, then this line again."
      emit_check "$r/environments/$env/deployment-branch-policies" "$(fill "$P_POLICIES" PAT "$(jlit "$TAG_PATTERN")" \
        GOOD "$(jlit "ok environment $env deploys only from tags $TAG_PATTERN")" \
        MISSING "$(jlit "fix environment $env has no deployment policy"), $(jlit "RUN $(write POST "$r/environments/$env/deployment-branch-policies" tag-policy.json)")" \
        OTHER "$(jlit "STOP environment $env has other deployment policies: ")" \
        OTHERSUF "$(jlit ". A human must review them.")")"
    else
      say "# on HTTP 404: FAIL environment $env is missing."
      emit_check "$r/environments/$env/deployment-branch-policies" "$(fill "$P_POLICIES" PAT "$(jlit "$TAG_PATTERN")" \
        GOOD "$(jlit "PASS environment $env deploys only from tags $TAG_PATTERN")" \
        MISSING "$(jlit "FAIL environment $env has no deployment policy (any branch may deploy)")" \
        OTHER "$(jlit "FAIL environment $env has other deployment policies: ")" \
        OTHERSUF "$(jlit "")")"
    fi
  done
}

plan_ruleset() { # plan_ruleset MODE NAME BODY-FUNCTION BODYFILE
  local mode="$1" name="$2" body="$3" file="$4" r="repos/$REPO" want detail pre suf
  want="$("$body" | jq -c .)"
  say "#"
  say "# Ruleset '$name'"
  pre="$(write PUT "$r/rulesets/@ID@" "$file")"
  suf="${pre#*@ID@}"; pre="${pre%%@ID@*}"
  if [ "$mode" = fix ]; then
    detail="$(fill "$P_RULESET_DETAIL" WANT "$want" GOOD "$(jlit "ok ruleset $name")" \
      BADLINES "(\$bad[] | $(jlit "fix ruleset $name: ") + .), $(jlit "RUN $pre") + (\$r.id | tostring) + $(jlit "$suf")")"
    emit_check "$r/rulesets" "$(fill "$P_RULESETS" NAME "$(jlit "$name")" \
      MISSING "$(jlit "fix ruleset $name is missing"), $(jlit "RUN $(write POST "$r/rulesets" "$file")")" \
      MANY "$(jlit "STOP more than one ruleset of $REPO is named '$name'. A human must remove the extra one.")" \
      CHECK "$(jlit "CHECK gh api $r/rulesets/")" DETAIL "$(jlit "$(oneline "$detail")")")"
  else
    detail="$(fill "$P_RULESET_DETAIL" WANT "$want" GOOD "$(jlit "PASS ruleset $name")" \
      BADLINES "(\$bad[] | $(jlit "FAIL ruleset $name: ") + .)")"
    emit_check "$r/rulesets" "$(fill "$P_RULESETS" NAME "$(jlit "$name")" \
      MISSING "$(jlit "FAIL ruleset $name is missing")" \
      MANY "$(jlit "FAIL more than one ruleset is named '$name'")" \
      CHECK "$(jlit "CHECK gh api $r/rulesets/")" DETAIL "$(jlit "$(oneline "$detail")")")"
  fi
}

plan_app() {
  say "#"
  say "# The workprentice GitHub App on $ORG"
  say "# on HTTP 403 or 404: NOTE this identity cannot list the org's app installations (that needs the organization"
  say "#   Administration read permission). A human checks Org settings > GitHub Apps > workprentice."
  emit_check "orgs/$ORG/installations" "[.installations[] | select(.app_slug | test(\"workprentice\"))]
    | if length == 0 then $(jlit "FAIL no workprentice app installation on $ORG")
      else (.[] | \"NOTE \" + .app_slug + \": repository_selection=\" + .repository_selection
        + (if .repository_selection == \"selected\" then $(jlit "; check that $REPO is one of the selected repositories (Org settings > GitHub Apps > workprentice > Configure)") else \"\" end))
      end"
  say "# Nothing here proves the app can push .github/workflows; the first import push is the test (Workflows permission)."
}

cmd_plan() {
  [ $# -ge 1 ] || die "plan needs create, protect or verify, then one slug"
  local sub="$1" imported=0 slugs=() a; shift
  case "$sub" in create|protect|verify) ;; *) die "plan needs create, protect or verify, not '$sub'" ;; esac
  for a in "$@"; do
    if [ "$a" = "--imported" ]; then imported=1; else slugs+=("$a"); fi
  done
  [ "${#slugs[@]}" = 1 ] || die "plan works on exactly one slug, the provider of the current card"
  SLUG="${slugs[0]}"
  slug_ok "$SLUG" || die "bad slug '$SLUG' (lowercase letters, digits, single hyphens)"
  [ "$ORG" = pulumi-labs ] || die "plan works only on pulumi-labs, not '$ORG'"
  [ "$imported" = 0 ] || [ "$sub" = verify ] || die "--imported goes with plan verify only"
  REPO="$ORG/pulumi-$SLUG"
  if [ -z "$OUT" ]; then
    [ -n "${HOME:-}" ] || die "HOME is not set; pass --out DIR"
    OUT="$HOME/.local/pulumi-labs-export/repo-setup/pulumi-$SLUG"
  fi
  mkdir -p "$OUT" || die "cannot create $OUT"
  OUT="$(cd "$OUT" && pwd)"
  body_create "$SLUG" >"$OUT/create.json"
  body_settings >"$OUT/settings.json"
  body_actions >"$OUT/actions.json"
  body_workflow >"$OUT/workflow.json"
  body_fork_pr >"$OUT/fork-pr.json"
  body_environment >"$OUT/environment.json"
  body_tag_policy >"$OUT/tag-policy.json"
  body_tag_ruleset >"$OUT/ruleset-tags.json"
  body_branch_ruleset >"$OUT/ruleset-branch.json"
  plan_header "$sub"
  case "$sub" in
    create)
      plan_repo fix 0
      plan_security fix
      plan_environments fix
      plan_ruleset fix "$TAG_RULESET" body_tag_ruleset ruleset-tags.json ;;
    protect)
      say "#"
      say "# Repository"
      say "# on HTTP 409 (Git Repository is empty): STOP. Protect only after the initial import and a green test run."
      emit_check "repos/$REPO/commits?per_page=1" '"ok repository has commits, head " + .[0].sha'
      plan_ruleset fix "$BRANCH_RULESET" body_branch_ruleset ruleset-branch.json ;;
    verify)
      plan_repo verify "$imported"
      plan_security verify
      plan_environments verify
      plan_ruleset verify "$TAG_RULESET" body_tag_ruleset ruleset-tags.json
      if [ "$imported" = 1 ]; then
        plan_ruleset verify "$BRANCH_RULESET" body_branch_ruleset ruleset-branch.json
        say "#"
        say "# on HTTP 404: FAIL release.yml missing."
        emit_check "repos/$REPO/contents/.github/workflows/release.yml" '"PASS release.yml present"'
      fi
      plan_app ;;
  esac
}

# Parse global options, then the subcommand.
args=()
while [ $# -gt 0 ]; do
  case "$1" in
    --org) [ $# -ge 2 ] || die "--org needs a value"; ORG="$2"; shift 2 ;;
    --out) [ $# -ge 2 ] || die "--out needs a value"; OUT="$2"; shift 2 ;;
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
  plan)
    # An agent's shell often lacks jq; scripts/bootstrap.py installs it into this bin.
    command -v jq >/dev/null || PATH="$HOME/.local/pulumi-labs-export/bin:$PATH"
    command -v jq >/dev/null || die "jq not found (scripts/bootstrap.py installs it into ~/.local/pulumi-labs-export/bin)"
    cmd_plan "${rest[@]}" ;;
  *) usage; exit 2 ;;
esac
