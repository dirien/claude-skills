# Sync on every new version, and drift

## Trigger

`lib/providers/<p>/version.txt` on monorepo `main` is greater than the entry's
`lastSyncedVersion` in `carved-providers.json`. Versions stay `0.x.y`
(`export.py` refuses anything else). Merging other changes without a version
bump publishes nothing.

## Procedure

1. Fresh worktree of monorepo `origin/main` (clean; its HEAD is the source SHA).
   Run `verify.py --monorepo` if the change is large.
2. In a fresh clone of the mirror: `git switch -c sync/v<version>`.
3. `python3 scripts/export.py --monorepo <checkout> --provider <p> --out . --generate`
   (display name and excluded platforms are reused from `.pulumi-labs-export.json`).
   `git add -A && git commit -m "Sync pulumi/autoregistry@<sha> (v<version>)"`.
4. `python3 scripts/check.py --repo . --against-source <checkout>` (expect OK),
   `python3 scripts/secret_scan.py --repo .`, `python3 scripts/verify.py --repo .`,
   `python3 scripts/verify.py --assert-pushable --repo .`; push the branch; open a PR.
   The committed SDKs carry `0.0.1-dev` whatever the provider version, so a
   version bump changes `source_version` in `.pulumi-labs-export.json` but not
   the SDK manifests; the release workflow regenerates the SDKs with the tag.
5. Reviewer clearance on the exact head SHA, green CI, squash-merge.
6. Monorepo PR updating `lastSyncedCommit` and `lastSyncedVersion` (and
   nothing else) in `carved-providers.json`.
7. Tell Engin: "ready to tag v<version>". The agent never tags.

Engineering feedback on the mirror, a failing gate, a wrong doc: fix it in the
monorepo (PR, review), merge, then run the sync again. A version bump is not
needed for a doc fix if no release is wanted; the sync PR is simply deferred.
Never edit the mirror, never force-push it.

## Drift detection

```
python3 scripts/check.py --repo <mirror> --against-source <autoregistry checkout>
```

Reads `source_sha` from `.pulumi-labs-export.json`, re-runs the export transform
at that commit into a temp dir (`git archive`, local only; fetch the commit in
the checkout first if it is missing) and lists every path that differs, as
`DRIFT <path>: content differs | missing | unexpected | executable bit differs`.
`sdk/` and the schema file are excluded (`make` produces them); untracked
ignored files (`bin/`) are ignored. A hand edit in the mirror therefore shows up
as drift. Remedy: put the change in the monorepo, then sync. If the mirror is
merely older than the monorepo, that is not drift; it is the next sync.
