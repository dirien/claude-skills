# Agent runtime notes

Verified in the Workprentice agent runtime. Nothing here is specific to a
tool name; it describes what the runtime does to processes.

## Only SKILL.md is delivered

The platform delivers the text of SKILL.md. `scripts/`, `references/`,
`archetype/` and `tests/` do not reach the agent's machine. The agent
fetches the skill folder from `pulumi/autoregistry` (`skills/pulumi-labs-export`
on `main`) into `$HOME/.local/pulumi-labs-export/src` and follows the fetched
SKILL.md. The copy in `pulumi/autoregistry` is therefore what runs; a change
merged there takes effect without re-uploading the skill. If the fetched
SKILL.md differs from the delivered one, follow the fetched one and report
that the uploaded copy is stale.

Fetch sequence (each line its own command):

```
git clone --depth 1 --no-checkout https://github.com/pulumi/autoregistry $HOME/.local/pulumi-labs-export/src
git -C $HOME/.local/pulumi-labs-export/src sparse-checkout set skills/pulumi-labs-export
git -C $HOME/.local/pulumi-labs-export/src checkout
```

Refresh: `git -C $HOME/.local/pulumi-labs-export/src pull --ff-only`.

## Credentials reach only directly invoked gh and git

GitHub credentials are injected into a `gh` or `git` only when that binary is
the command itself. A `gh` or `git` started as a child of `python3` or of a
shell gets none: `gh` exits 4 with "To get started with GitHub CLI, please
run: gh auth login", and `git` fails with an authentication error.

Consequences:

- `scripts/access_check.py` detects this and exits 2. Run its
  `--print-commands` lines directly instead, one command per call, no pipes
  and no `cd` (use `git -C <dir>`). PASS rule: every command exits 0, and the
  push probe output contains no "denied" or "403".
- Scripts that only run local git (`check.py`) or make no network calls
  (`onboarding.py`, `render.py`, `names.py`) need no credentials.
- `archetype_drift.py` reads a public repo over unauthenticated HTTPS with
  the standard library; it needs neither `gh` nor credentials, but is subject
  to the unauthenticated API rate limit (reported clearly when hit).
- `bootstrap.py` downloads public URLs with `urllib` and also needs none.
- Anything else that needs a private repo or the API as the app (issues, PRs,
  pushes) must be a directly invoked `gh` or `git` command.

## No partial clones

`git clone --filter=blob:none` fails on the later lazy blob fetch with
"Repository not found", because credentials are not injected for that
background fetch. Use `--depth 1 --no-checkout` followed by
`sparse-checkout set` and `checkout`, as above.

The export's `git subtree split` needs the opposite: a full, non-shallow,
unfiltered clone of `pulumi/autoregistry` (about 12 MB), made with a plain
`git clone https://github.com/pulumi/autoregistry <dir>`.
