#!/usr/bin/env python3
"""Pre-push verification gate for pulumi-labs provider repos.

Three modes (details in references/verify.md):

    python3 scripts/verify.py --repo ../pulumi-modal            # the gate (default repo: cwd)
    python3 scripts/verify.py --assert-pushable --repo ../pulumi-modal
    python3 scripts/verify.py --monorepo <autoregistry checkout> --provider modal
    python3 scripts/verify.py --monorepo <autoregistry checkout> --provider modal --profile examples

`--profile examples` is the CI examples gate (examples_gate.py, references/verify.md): it compiles every example and
template against the locally generated SDKs and prints stable failure keys `<provider>:<check>:<path>`.

The gate builds a clean local clone of the repo's HEAD in a scratch directory
(so the checkout stays untouched and the proof is for the exact commit) and
runs, capturing full logs: toolchain presence, make provider/lint/test/
check_copyright, schema regeneration + git diff, make sdks (errors in codegen
or `go mod tidy` fail), Go SDK build/vet, Go module-path checks, a consumer
smoke test, every Go example and template against the LOCAL Go SDK, all four
non-Go SDK builds (a "skipping" message is a failure), the default-provider and
credential checks, and check.py. Missing tools FAIL, they never skip.

A Terraform-bridged repo (archetype "bridged", recorded in .pulumi-labs-export.json,
references/bridged.md) runs the same gate with tfgen-based steps: the schema and the bridge
metadata are regenerated and diffed, `go mod tidy` must leave provider/ and its shim untouched,
the Go module is provider/, and the built provider binary must serve the committed schema.

The report goes to $HOME/.local/pulumi-labs-export/reports/<repo>-<sha>.{json,md}
(outside the repo). Exit 0 only if every step passes. `--assert-pushable`
exits 0 only if a passing report exists for the current HEAD and the worktree
is clean; run it immediately before any push.
"""
import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bootstrap  # noqa: E402
import check  # noqa: E402
import examples_gate  # noqa: E402
import gomod  # noqa: E402
import identity  # noqa: E402
import names  # noqa: E402
import render  # noqa: E402
import secret_scan  # noqa: E402

SKILL_DIR = os.path.dirname(HERE)
GATE_VERSION = 2
TAIL_LINES = 25
TAIL_CHARS = 4000
CMD_TIMEOUT = 3600

# Steps every passing carved-repo report must contain (so a partial or forged
# report cannot satisfy --assert-pushable). Per-example detail lives in items.
REQUIRED_STEPS = [
    "identity", "toolchain", "make_provider", "make_lint", "make_test", "make_check_copyright",
    "make_schema", "schema_diff", "make_sdks", "sdk_version", "go_sdk_build", "go_sdk_vet", "go_module_paths",
    "go_consumer_smoke", "go_examples", "build_nodejs_sdk", "build_python_sdk", "build_dotnet_sdk",
    "build_java_sdk", "dp_env_defaults", "dp_no_required_config", "dp_no_env_tags",
    "dp_no_explicit_provider", "dp_no_credential_require", "check_py", "worktree_clean_after",
]

# Bridged repos: the same gate, with tfgen-based schema/metadata freshness, go.mod tidiness of provider/ and its shim,
# and the proof that the built binary serves the committed schema (it embeds it).
REQUIRED_STEPS_BRIDGED = REQUIRED_STEPS + ["metadata_diff", "go_mod_tidy", "binary_schema", "dp_provider_info_defaults"]


def required_steps(archetype):
    """Return the step ids a passing report must contain for an archetype ("native" when unknown)."""
    return REQUIRED_STEPS_BRIDGED if archetype == "bridged" else REQUIRED_STEPS


# Lines in `make sdks` output (pulumi gen-sdk codegen, go mod tidy) that mean failure.
CODEGEN_ERROR_RES = [re.compile(p) for p in (
    r"(?i)\berror:",
    r"(?i)^\s*error\b",
    r"\bpanic:",
    r"(?i)\bfatal( error)?:",
    r"Traceback \(most recent call last\)",
    r"(?i)\bfailed to\b",
    r"(?i)^go: .*\b(cannot find|no required module|missing go\.sum|unknown revision|updates to go\.mod needed|verifying module|invalid version)",
    r"(?i)^go: .*: (reading|parsing|invalid)",
    r"(?i)module .* found .*, but does not contain package",
    r"(?i)ambiguous import",
    r"(?i)build constraints exclude all Go files",
)]
WARNING_RE = re.compile(r"(?i)\bwarn(ing)?\b")
# Pulumi CLI self-update notices are noise, not codegen warnings.
NOTICE_RE = re.compile(r"(?i)very old version of Pulumi|upgrade from ['\"]?\d|pulumi\.com/docs/install|new version of Pulumi")
# The archetype Make targets echo "skipping <lang> SDK build (...)" at the start of a line. Match only that:
# setuptools prints "warning: build_py: byte-compiling is disabled, skipping." when PYTHONDONTWRITEBYTECODE is
# set (as it is in CI), and that is not a skipped build.
SKIPPING_RE = re.compile(r"(?im)^\s*skipping\b")
CRED_RE = re.compile(r"token|key|secret|password|credential", re.I)
ENVVAR_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


# ------------------------------------------------------------------ helpers

def utcnow():
    """Current UTC time as an ISO-8601 string."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def git(repo, *args):
    """Run local git in repo; return (rc, stdout text)."""
    try:
        p = subprocess.run(["git", "-C", repo] + list(args), capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as e:
        return 127, str(e)
    return p.returncode, p.stdout


def tail_of(text, lines=TAIL_LINES, chars=TAIL_CHARS):
    """Return the last `lines` lines of text, capped to `chars` characters."""
    t = "\n".join(text.rstrip().splitlines()[-lines:])
    return t[-chars:]


def git_state(repo):
    """Return {'head', 'tree', 'branch', 'porcelain'} for repo, or raise RuntimeError."""
    rc, head = git(repo, "rev-parse", "HEAD")
    if rc != 0:
        raise RuntimeError("%s is not a git repository with a commit (git rev-parse HEAD failed)" % repo)
    _, tree = git(repo, "rev-parse", "HEAD^{tree}")
    _, branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    _, por = git(repo, "status", "--porcelain")
    return {"head": head.strip(), "tree": tree.strip(), "branch": branch.strip(),
            "porcelain": [l for l in por.splitlines() if l.strip()]}


def skill_state():
    """Return the skill clone's SHA/dirty flag if the skill sits in a git checkout, else None."""
    rc, top = git(SKILL_DIR, "rev-parse", "--show-toplevel")
    if rc != 0:
        return None
    rc, sha = git(SKILL_DIR, "rev-parse", "HEAD")
    if rc != 0:
        return None
    _, por = git(SKILL_DIR, "status", "--porcelain", "--", ".")
    return {"path": SKILL_DIR, "git_toplevel": top.strip(), "sha": sha.strip(), "dirty": bool(por.strip())}


def report_dir_default():
    """Default directory for reports, outside any repo."""
    return os.path.join(bootstrap.default_prefix(), "reports")


def build_env(prefix):
    """Return the environment for build commands: toolchain on PATH, JAVA_HOME, DOTNET_ROOT, opt-outs."""
    env = dict(os.environ)
    env["PATH"] = os.path.join(prefix, "bin") + os.pathsep + env.get("PATH", "")
    try:
        specs = bootstrap.tool_specs(bootstrap.detect_arch(), bootstrap.read_pulumi_version())
        env.update(bootstrap.toolchain_env(prefix, specs))
    except bootstrap.BootstrapError:
        pass
    env.setdefault("DOTNET_CLI_TELEMETRY_OPTOUT", "1")
    env.setdefault("DOTNET_NOLOGO", "1")
    env["PULUMI_SKIP_UPDATE_CHECK"] = "true"
    env["PULUMI_AUTOMATION_API_SKIP_VERSION_CHECK"] = "true"
    return env


def prepare_sandbox_for(lang, prefix, env):
    """Refresh the sandbox fixes a language build needs; return env updates (best effort, never raises).

    dotnet: the NuGet migrations marker. java: gradle.properties from the current proxy env.
    """
    try:
        if lang == "dotnet":
            return bootstrap.ensure_nuget_marker(prefix, env)
        if lang == "java":
            bootstrap.write_gradle_properties(prefix, env)
    except (OSError, bootstrap.BootstrapError) as e:
        sys.stderr.write("sandbox fix for %s skipped: %s\n" % (lang, getattr(e, "strerror", None) or e))
    return {}


def has_c_compiler(env):
    """True if a C compiler is on PATH (needed by `go test -race`)."""
    path = env.get("PATH", "")
    return any(shutil.which(c, path=path) for c in ("gcc", "cc", "clang"))


# ------------------------------------------------------------ the gate state

NUGET_MUTEX_RE = re.compile(r"NuGet-Migrations|Mutex\.CreateMutexCore")
GRADLE_TIMEOUT_RE = re.compile(r"(Connect|Read) timed out|Could not (GET|HEAD|resolve)[^\n]*(timed out|Connect)|"
                               r"java\.net\.(SocketTimeout|ConnectException)|Connection timed out", re.I)
NUGET_HINT = ("hint: dotnet failed on the NuGet-Migrations named mutex; run `python3 scripts/bootstrap.py` "
              "(creates ${XDG_DATA_HOME:-$HOME/.local/share}/NuGet/Migrations/1, or sets XDG_DATA_HOME=<prefix>/xdg-data) "
              "and eval its exports")
GRADLE_HINT = ("hint: Gradle timed out connecting; its JVM ignores HTTP(S)_PROXY. Run `python3 scripts/bootstrap.py` "
               "to write $GRADLE_USER_HOME/gradle.properties from the proxy env")
XML_ENTITY_RE = re.compile(r"MSB4025[^\n]*(EntityName|reference|entity|'<')", re.I)
XML_HINT = ("hint: the generated .csproj is not valid XML: a provider metadata string (WithDescription, WithDisplayName, "
            "WithKeywords, ...) contains a bare `&` or `<` that the .NET generator copies unescaped. Reword it in the "
            "builder chain (for example `and` instead of `&`), regenerate, and rerun")


def failure_hints(text):
    """Return hint lines for known sandbox failure signatures in command output."""
    hints = []
    if NUGET_MUTEX_RE.search(text or ""):
        hints.append(NUGET_HINT)
    if GRADLE_TIMEOUT_RE.search(text or ""):
        hints.append(GRADLE_HINT)
    if XML_ENTITY_RE.search(text or ""):
        hints.append(XML_HINT)
    return hints


class Gate:
    """Collects step results and runs logged commands."""

    def __init__(self, logdir, env):
        self.logdir = logdir
        self.env = env
        self.steps = []
        os.makedirs(logdir, exist_ok=True)

    def add(self, sid, title, status, detail="", log=None, seconds=0.0, warnings=None, items=None, tail=""):
        """Record a finished step and return it."""
        step = {"id": sid, "title": title, "status": status, "seconds": round(seconds, 1),
                "detail": detail, "log": log, "log_tail": tail,
                "warnings": list(warnings or [])[:50], "items": list(items or [])}
        self.steps.append(step)
        sys.stderr.write("[%s] %s%s\n" % (status, sid, (": " + detail.splitlines()[0]) if detail else ""))
        return step

    def log_path(self, sid):
        """Log file path for a step id."""
        return os.path.join(self.logdir, "%02d-%s.log" % (len(self.steps) + 1, re.sub(r"[^\w.-]", "_", sid)))

    def run(self, argv, cwd, logpath, env=None, timeout=CMD_TIMEOUT):
        """Run argv, tee combined output into logpath; return (rc, text). rc 124 on timeout, 127 if not runnable."""
        t0 = time.time()
        try:
            p = subprocess.run(argv, cwd=cwd, env=env or self.env, capture_output=True, text=True,
                               errors="replace", timeout=timeout)
            rc, text = p.returncode, (p.stdout or "") + (p.stderr or "")
        except subprocess.TimeoutExpired as e:
            rc, text = 124, "TIMEOUT after %ds\n%s" % (timeout, (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or ""))
        except (OSError, ValueError) as e:
            rc, text = 127, "cannot run %s: %s" % (argv[0], e)
        with open(logpath, "a", encoding="utf-8") as f:
            f.write("$ %s   (cwd %s)\n%s\n[exit %d, %.1fs]\n" % (" ".join(argv), cwd, text, rc, time.time() - t0))
        return rc, text

    def cmd(self, sid, title, argv, cwd, fail_on_skip=False, error_res=None, collect_warnings=False,
            env=None, pass_detail=""):
        """Run one command as a step. FAIL on non-zero exit, 'skipping' (if asked) or an error-pattern line."""
        t0 = time.time()
        lp = self.log_path(sid)
        rc, text = self.run(argv, cwd, lp, env)
        reasons = []
        if rc != 0:
            reasons.append("exit %d from `%s`" % (rc, " ".join(argv)))
        lines = text.splitlines()
        if fail_on_skip:
            reasons += ["output says skipping: %s" % l.strip()[:160] for l in lines if SKIPPING_RE.search(l)][:3]
        if error_res:
            hits = [l.strip() for l in lines if any(r.search(l) for r in error_res)]
            reasons += ["error line: %s" % h[:200] for h in hits[:5]]
            if len(hits) > 5:
                reasons.append("... %d more error lines (see log)" % (len(hits) - 5))
        warns = [l.strip()[:200] for l in lines if WARNING_RE.search(l) and not NOTICE_RE.search(l)] if collect_warnings else []
        status = "FAIL" if reasons else "PASS"
        if reasons:
            reasons += failure_hints(text)
        self.add(sid, title, status, "\n".join(reasons) if reasons else pass_detail, lp, time.time() - t0,
                 warns, tail=tail_of(text))
        return status, text

    @property
    def ok(self):
        """True if no step failed."""
        return all(s["status"] == "PASS" for s in self.steps)


# -------------------------------------------------- static default-provider checks

def check_config_env_defaults(schema):
    """Return items [{name,status,detail}] for credential-like config variables needing defaultInfo.environment.

    A variable is credential-like if it is `secret: true` or its name matches token|key|secret|password|credential.
    """
    items = []
    variables = ((schema or {}).get("config") or {}).get("variables") or {}
    for name in sorted(variables):
        v = variables[name] or {}
        if not (v.get("secret") is True or CRED_RE.search(name)):
            continue
        env = ((v.get("defaultInfo") or {}).get("environment")) or []
        if isinstance(env, list) and env and all(isinstance(e, str) and e for e in env):
            items.append({"name": name, "status": "PASS", "detail": "env: " + ", ".join(env)})
        else:
            items.append({"name": name, "status": "FAIL",
                          "detail": "credential-like config variable has no defaultInfo.environment "
                                    "(add a.SetDefault(&c.Field, nil, \"ENV_VAR\") in the config's Annotate)"})
    if not items:
        items.append({"name": "(none)", "status": "PASS", "detail": "no credential-like config variables"})
    return items


def check_no_required_config(schema):
    """Return items for config.defaults and provider.requiredInputs, which must both be empty."""
    items = []
    cfg_defaults = ((schema or {}).get("config") or {}).get("defaults") or []
    req = ((schema or {}).get("provider") or {}).get("requiredInputs") or []
    for label, vals in (("config.defaults", cfg_defaults), ("provider.requiredInputs", req)):
        if vals:
            items.append({"name": label, "status": "FAIL",
                          "detail": "must be empty so the default provider works with no config; found: " + ", ".join(map(str, vals))})
        else:
            items.append({"name": label, "status": "PASS", "detail": "empty"})
    return items


ENV_TAG_RE = re.compile(r"`[^`\n]*\benv:\"[^`\n]*`")
GO_SKIP_DIRS = {"shim", "sdk", "vendor", "node_modules", ".git"}


def find_env_tags(go_text):
    """Return [(lineno, line)] for struct tags carrying both pulumi:"..." and env:"..." (infer ignores env)."""
    hits = []
    for i, line in enumerate(go_text.splitlines(), 1):
        m = ENV_TAG_RE.search(line)
        if m and 'pulumi:"' in m.group(0):
            hits.append((i, line.strip()[:200]))
    return hits


def scan_env_tags(code_root):
    """Scan non-test Go files under code_root; return ['rel:line: text']."""
    out = []
    for dp, dns, fns in os.walk(code_root):
        dns[:] = [d for d in dns if d not in GO_SKIP_DIRS]
        for fn in sorted(fns):
            if not fn.endswith(".go") or fn.endswith("_test.go"):
                continue
            full = os.path.join(dp, fn)
            with open(full, encoding="utf-8", errors="replace") as f:
                for ln, text in find_env_tags(f.read()):
                    out.append("%s:%d: %s" % (os.path.relpath(full, code_root), ln, text))
    return out


_NEW_PROVIDER = re.compile(r"\bnew\s+(?:[\w$]+\.)*Provider\s*\(")
EXPLICIT_PROVIDER_RES = {
    "js": [_NEW_PROVIDER],
    "py": [re.compile(r"\b\w+\.Provider\s*\(")],
    "go": [re.compile(r"\.NewProvider\s*\(\s*ctx\b")],
    "cs": [_NEW_PROVIDER],
    "java": [_NEW_PROVIDER],
    "yaml": [re.compile(r"pulumi:providers:")],
}
LANG_BY_EXT = {".ts": "js", ".tsx": "js", ".js": "js", ".mjs": "js", ".cjs": "js", ".py": "py",
               ".go": "go", ".cs": "cs", ".java": "java", ".yaml": "yaml", ".yml": "yaml"}
EXAMPLE_SKIP_DIRS = gomod.SKIP_DIRS | {"sdk", "dist", "build", ".gradle", "target"}
# Any config read: require*/get*/try* (TS, Python snake_case, Go, C#, Java) with a string key.
CONFIG_READ_RE = re.compile(r"\.(?:[Rr]equire|[Gg]et|[Tt]ry)\w*\s*\(\s*(?:ctx\s*,\s*)?[\"']([^\"']+)[\"']")


def norm_key(key):
    """Normalise a config key for comparison: drop any `ns:` prefix, case, `_` and `-` (tokenId == token_id)."""
    return re.sub(r"[^a-z0-9]", "", key.split(":")[-1].lower())


def provider_credential_names(schema):
    """Return the normalised names of the provider's own credential config variables.

    Those are the schema's config.variables that are `secret: true` or credential-named.
    """
    variables = ((schema or {}).get("config") or {}).get("variables") or {}
    return {norm_key(n) for n, v in variables.items() if (v or {}).get("secret") is True or CRED_RE.search(n)}


def _is_comment(line):
    s = line.strip()
    return s.startswith(("#", "//", "/*", "*"))


def find_explicit_provider(ext, text):
    """Return [(lineno, line)] where an example/template file instantiates an explicit provider."""
    lang = LANG_BY_EXT.get(ext.lower())
    if not lang:
        return []
    hits = []
    for i, line in enumerate(text.splitlines(), 1):
        if _is_comment(line):
            continue
        if any(r.search(line) for r in EXPLICIT_PROVIDER_RES[lang]):
            hits.append((i, line.strip()[:200]))
    return hits


def find_credential_requires(ext, text, cred_names):
    """Return [(lineno, key, line)] where a program reads a config key naming one of the provider's own
    credential variables (cred_names, see provider_credential_names). App secrets such as a token stored in
    a Secret resource are not flagged."""
    if not cred_names or ext.lower() not in LANG_BY_EXT or LANG_BY_EXT[ext.lower()] == "yaml":
        return []
    hits = []
    for i, line in enumerate(text.splitlines(), 1):
        if _is_comment(line):
            continue
        for m in CONFIG_READ_RE.finditer(line):
            if norm_key(m.group(1)) in cred_names:
                hits.append((i, m.group(1), line.strip()[:200]))
    return hits


def iter_example_files(root):
    """Yield (relpath, ext, text) for source files under root/examples and root/templates."""
    for top in ("examples", "templates"):
        for dp, dns, fns in os.walk(os.path.join(root, top)):
            dns[:] = [d for d in dns if d not in EXAMPLE_SKIP_DIRS]
            for fn in sorted(fns):
                ext = os.path.splitext(fn)[1]
                if ext.lower() not in LANG_BY_EXT:
                    continue
                full = os.path.join(dp, fn)
                try:
                    if os.path.getsize(full) > 2 * 1024 * 1024:
                        continue
                    with open(full, encoding="utf-8", errors="replace") as f:
                        yield os.path.relpath(full, root), ext, f.read()
                except OSError:
                    continue


def scan_examples(root, cred_names=()):
    """Return (explicit_provider_hits, credential_read_hits) as lists of strings."""
    prov, cred = [], []
    for rel, ext, text in iter_example_files(root):
        for ln, line in find_explicit_provider(ext, text):
            prov.append("%s:%d: %s" % (rel, ln, line))
        for ln, key, line in find_credential_requires(ext, text, cred_names):
            cred.append("%s:%d: reads config key %r, a credential of the provider itself: %s" % (rel, ln, key, line))
    return prov, cred


def _top_level_entries(block):
    """Yield (key, sub-block text, offset in block) for the `"key": {...}` entries directly inside a Go map literal block."""
    pos = 1
    rx = re.compile(r'"([^"\n]+)":\s*\{')
    while True:
        m = rx.search(block, pos)
        if not m:
            return
        sub = identity.go_block(block, m.end() - 1)
        if sub is None:
            return
        yield m.group(1), sub, m.start()
        pos = m.end() - 1 + len(sub)


def check_provider_info_defaults(code_root):
    """Items for the credential-like entries of a bridged ProviderInfo.Config map (resources.go).

    An entry that declares Default must name its environment variables (Default.EnvVars, upper snake case) and must not
    carry a literal Value: a credential default baked into the provider is never right. An entry with no Default is not a
    failure here; the schema (dp_env_defaults) is what proves the environment fallback exists.
    """
    items = []
    for fn in sorted(os.listdir(code_root)) if os.path.isdir(code_root) else []:
        if not fn.endswith(".go") or fn.endswith("_test.go"):
            continue
        with open(os.path.join(code_root, fn), encoding="utf-8", errors="replace") as f:
            text = f.read()
        m = re.search(r"\bConfig:\s*map\[string\]\*tfbridge\.SchemaInfo\{", text)
        if not m:
            continue
        block = identity.go_block(text, m.end() - 1) or ""
        for key, sub, off in _top_level_entries(block):
            if not CRED_RE.search(key):
                continue
            line = text.count("\n", 0, m.end() - 1 + off) + 1
            name = "%s:%d %s" % (fn, line, key)
            if not re.search(r"\bDefault:", sub):
                items.append({"name": name, "status": "PASS", "detail": "no Default declared (the schema check proves the env fallback)"})
                continue
            envs = re.search(r"\bEnvVars:\s*\[\]string\{([^}]*)\}", sub)
            vars_ = re.findall(r'"([^"]*)"', envs.group(1)) if envs else []
            if re.search(r"\bValue:", sub):
                items.append({"name": name, "status": "FAIL", "detail": "credential-like config has a literal Default.Value; remove it"})
            elif not vars_ or not all(ENVVAR_NAME_RE.match(v) for v in vars_):
                items.append({"name": name, "status": "FAIL",
                              "detail": "credential-like config declares Default without upper-case EnvVars (got %r); set Default: &tfbridge.DefaultInfo{EnvVars: []string{\"X_API_KEY\"}}" % vars_})
            else:
                items.append({"name": name, "status": "PASS", "detail": "Default.EnvVars: " + ", ".join(vars_)})
    if not items:
        items.append({"name": "(none)", "status": "PASS", "detail": "no credential-like entries in ProviderInfo.Config"})
    return items


def _info_path(name):
    """Stable path of a dp_provider_info_defaults item named `file.go:LINE key`: provider/file.go#key."""
    m = re.match(r"^(\S+?):\d+ (.+)$", name)
    return "provider/%s#%s" % (m.group(1), m.group(2)) if m else name


def default_provider_steps(gate, schema, code_root, examples_root, bridged=False):
    """Record the five static default-provider / credential steps.

    Every FAIL item carries a `path` (no line numbers, so it is stable): the failure key of the examples profile.
    """
    def step(sid, title, items, path_fn=lambda name: name):
        for i in items:
            i["path"] = path_fn(i["name"])
        bad = [i for i in items if i["status"] == "FAIL"]
        gate.add(sid, title, "FAIL" if bad else "PASS",
                 "\n".join("%s: %s" % (i["name"], i["detail"]) for i in bad[:20]) + (("\n... %d more" % (len(bad) - 20)) if len(bad) > 20 else ""),
                 items=items)

    if schema is None:
        for sid, title in (("dp_env_defaults", "credential config variables have env defaults"),
                           ("dp_no_required_config", "no required provider config")):
            gate.add(sid, title, "FAIL", "no schema available to inspect", items=[{"name": "schema", "path": "schema", "status": "FAIL",
                                                                                     "detail": "no schema available to inspect"}])
    else:
        step("dp_env_defaults", "credential config variables have defaultInfo.environment", check_config_env_defaults(schema),
             lambda name: "config/" + name)
        step("dp_no_required_config", "config.defaults and provider.requiredInputs are empty", check_no_required_config(schema))
    if bridged:
        step("dp_provider_info_defaults", "ProviderInfo.Config credential defaults name their env vars and carry no literal value",
             check_provider_info_defaults(code_root), _info_path)
    tags = scan_env_tags(code_root) if os.path.isdir(code_root) else []
    step("dp_no_env_tags", "no env:\"...\" struct tags on pulumi fields (infer ignores them)",
         [{"name": t.split(":", 2)[0] + ":" + t.split(":", 2)[1], "status": "FAIL", "detail": t} for t in tags]
         or [{"name": "provider code", "status": "PASS", "detail": "no env struct tags"}],
         lambda name: "provider/" + name.rsplit(":", 1)[0])
    prov, cred = scan_examples(examples_root, provider_credential_names(schema))
    step("dp_no_explicit_provider", "examples and templates use the default provider (no explicit Provider)",
         [{"name": h.split(":", 1)[0], "status": "FAIL", "detail": h} for h in prov]
         or [{"name": "examples+templates", "status": "PASS", "detail": "no explicit provider instantiation"}])
    step("dp_no_credential_require", "examples and templates do not read the provider's own credential config keys",
         [{"name": h.split(":", 1)[0], "status": "FAIL", "detail": h} for h in cred]
         or [{"name": "examples+templates", "status": "PASS", "detail": "no read of the provider's credential config keys"}])


# ------------------------------------------------------------ Go SDK helpers

def first_exported_symbol(sdk_dir):
    """Return ('func'|'type', name, pkgname) of an exported symbol in the SDK's root package, or None."""
    pkg, func, typ = None, None, None
    for fn in sorted(os.listdir(sdk_dir)):
        if not fn.endswith(".go") or fn.endswith("_test.go"):
            continue
        with open(os.path.join(sdk_dir, fn), encoding="utf-8", errors="replace") as f:
            text = f.read()
        m = re.search(r"^package\s+(\w+)", text, re.M)
        if m and not pkg:
            pkg = m.group(1)
        if not func:
            m = re.search(r"^func\s+([A-Z]\w*)\s*\(", text, re.M)
            func = m.group(1) if m else None
        if not typ:
            m = re.search(r"^type\s+([A-Z]\w*)\s", text, re.M)
            typ = m.group(1) if m else None
    if func:
        return "func", func, pkg
    if typ:
        return "type", typ, pkg
    return None


def consumer_main_go(import_path, symbol):
    """Return the main.go text of the consumer smoke test, or None if there is no symbol."""
    if not symbol:
        return None
    kind, name, _ = symbol
    ref = "var _ = sdk.%s" % name if kind == "func" else "var _ *sdk.%s" % name
    return 'package main\n\nimport sdk "%s"\n\n%s\n\nfunc main() {}\n' % (import_path, ref)


def go_sdk_dir_for(root, schema):
    """Locate the generated Go SDK dir (relative to root) from the schema's importBasePath."""
    base = (((schema or {}).get("language") or {}).get("go") or {}).get("importBasePath") or ""
    if "/sdk/go/" in base:
        return "sdk/go/" + base.split("/sdk/go/", 1)[1]
    d = os.path.join(root, "sdk", "go")
    kids = [k for k in sorted(os.listdir(d))] if os.path.isdir(d) else []
    return "sdk/go/" + kids[0] if len(kids) == 1 else "sdk/go"


def consumer_smoke(gate, sid, sdk_dir, sdk_module, tmp):
    """Prove `import "<sdk_module>"` compiles in a fresh module outside the repo via require+replace."""
    t0 = time.time()
    lp = gate.log_path(sid)
    work = tempfile.mkdtemp(prefix="consumer-", dir=tmp)
    sym = first_exported_symbol(sdk_dir) if os.path.isdir(sdk_dir) else None
    main_go = consumer_main_go(sdk_module, sym)
    if main_go is None:
        gate.add(sid, "consumer smoke test (import path compiles)", "FAIL",
                 "no exported symbol found in %s to reference" % sdk_dir, seconds=time.time() - t0)
        return
    with open(os.path.join(work, "main.go"), "w") as f:
        f.write(main_go)
    steps = [["go", "mod", "init", "example.com/consumer"],
             ["go", "mod", "edit", "-require=%s@v0.0.0" % sdk_module, "-replace=%s=%s" % (sdk_module, os.path.abspath(sdk_dir))],
             ["go", "mod", "tidy"], ["go", "build", "./..."]]
    out = ""
    for argv in steps:
        rc, text = gate.run(argv, work, lp)
        out += text
        if rc != 0:
            gate.add(sid, "consumer smoke test (import path compiles)", "FAIL",
                     "`%s` failed (exit %d); importing %s did not compile" % (" ".join(argv), rc, sdk_module),
                     lp, time.time() - t0, tail=tail_of(text))
            return
    gate.add(sid, "consumer smoke test (import path compiles)", "PASS",
             "import \"%s\" compiles in a fresh module (require+replace to local SDK)" % sdk_module, lp, time.time() - t0)


def go_example_dirs(root):
    """Return relative dirs under examples/ and templates/ that are Go programs (go.mod, or .go files)."""
    dirs = set(gomod.go_example_modules(root))
    for top in ("examples", "templates"):
        for dp, dns, fns in os.walk(os.path.join(root, top)):
            dns[:] = [d for d in dns if d not in EXAMPLE_SKIP_DIRS]
            rel = os.path.relpath(dp, root)
            if any(f.endswith(".go") for f in fns) and not any(rel == d or rel.startswith(d + os.sep) for d in dirs):
                dirs.add(rel)
    return sorted(dirs)


PLACEHOLDER_RE = re.compile(r"\$\{[A-Za-z_]+\}")


def go_examples(gate, root, sdk_dir, sdk_module, tmp, sid="go_examples"):
    """Compile every Go example/template against the local SDK in a scratch copy (go.mod replace, never in the repo)."""
    t0 = time.time()
    lp = gate.log_path(sid)
    items = []
    for rel in go_example_dirs(root):
        dst = os.path.join(tmp, "ex-" + re.sub(r"[^\w.-]", "_", rel))
        shutil.copytree(os.path.join(root, rel), dst, ignore=shutil.ignore_patterns(*EXAMPLE_SKIP_DIRS))
        for dp, _, fns in os.walk(dst):  # Pulumi templates carry ${PROJECT}-style placeholders
            for fn in fns:
                if fn == "go.mod" or fn.endswith(".go"):
                    p = os.path.join(dp, fn)
                    with open(p, encoding="utf-8", errors="replace") as f:
                        txt = f.read()
                    new = PLACEHOLDER_RE.sub("example", txt)
                    if new != txt:
                        with open(p, "w") as f:
                            f.write(new)
        cmds = []
        if not os.path.exists(os.path.join(dst, "go.mod")):
            cmds.append(["go", "mod", "init", "example.com/" + re.sub(r"[^\w]", "-", os.path.basename(rel))])
        cmds += [["go", "mod", "edit", "-replace=%s=%s" % (sdk_module, os.path.abspath(sdk_dir))],
                 ["go", "mod", "tidy"], ["go", "build", "-o", os.path.join(tmp, "out") + os.sep, "./..."]]
        status, detail = "PASS", "compiles against local SDK"
        for argv in cmds:
            rc, text = gate.run(argv, dst, lp)
            if rc != 0:
                status, detail = "FAIL", "`%s` failed (exit %d): %s" % (" ".join(argv[:3]), rc, examples_gate.summarize(text) or tail_of(text, 6, 600))
                break
        items.append({"name": rel, "status": status, "detail": detail})
    bad = [i for i in items if i["status"] == "FAIL"]
    gate.add(sid, "Go examples and templates compile against the local Go SDK",
             "FAIL" if bad else "PASS",
             "\n".join("%s: %s" % (i["name"], i["detail"]) for i in bad) if bad else
             ("%d Go example/template dir(s) compiled" % len(items) if items else "no Go examples or templates present"),
             lp, time.time() - t0, items=items)


def go_module_paths(gate, clone, slug_names, bridged=False):
    """`go list -m` must equal the repo module and every `go list ./...` package must sit under it.

    Bridged: the module is provider/ (github.com/pulumi-labs/pulumi-<p>/provider); the shim is a separate module.
    """
    t0 = time.time()
    sid = "go_module_paths"
    lp = gate.log_path(sid)
    want = slug_names["provider_module"] if bridged else slug_names["go_module"]
    if bridged:
        clone = os.path.join(clone, "provider")
    rc, text = gate.run(["go", "list", "-m"], clone, lp)
    got = text.strip().splitlines()[-1].strip() if rc == 0 and text.strip() else ""
    reasons = []
    if rc != 0 or got != want:
        reasons.append("go list -m = %r, want exactly %r" % (got or "(exit %d)" % rc, want))
    rc2, text2 = gate.run(["go", "list", "./..."], clone, lp)
    if rc2 != 0:
        reasons.append("go list ./... failed (exit %d)" % rc2)
    else:
        pkgs = [l.strip() for l in text2.splitlines() if l.strip() and not l.startswith("go: ")]
        bad = [p for p in pkgs if not (p == want or p.startswith(want + "/"))]
        if bad:
            reasons.append("packages outside %s: %s" % (want, ", ".join(bad[:5])))
    gate.add(sid, "Go module path is %s and all packages sit under it" % want, "FAIL" if reasons else "PASS",
             "\n".join(reasons), lp, time.time() - t0, tail=tail_of(text + text2))


# -------------------------------------------------------------- toolchain step

TOOL_PROBES = [("go", ["go", "version"]), ("pulumi", ["pulumi", "version"]), ("make", ["make", "--version"]),
               ("jq", ["jq", "--version"]), ("node", ["node", "--version"]), ("npm", ["npm", "--version"]),
               ("python3", ["python3", "--version"]), ("dotnet", ["dotnet", "--version"]),
               ("java", ["java", "-version"]), ("gradle", ["gradle", "--version"])]


def toolchain_step(gate, tmp, prefix, tools=None):
    """FAIL for any missing/unrunnable tool; return {tool: version line}.

    tools limits the probes to a subset of TOOL_PROBES names (the examples profile only needs what its languages use);
    the python venv probe runs when "python3" is in the subset or no subset is given.
    """
    t0 = time.time()
    lp = gate.log_path("toolchain")
    versions, items = {}, []
    for tool, argv in TOOL_PROBES:
        if tools is not None and tool not in tools:
            continue
        exe = shutil.which(argv[0], path=gate.env.get("PATH", ""))
        if not exe:
            items.append({"name": tool, "status": "FAIL",
                          "detail": "not found on PATH; run `eval \"$(python3 scripts/bootstrap.py)\"` (prefix %s)" % prefix})
            continue
        line, err = bootstrap.run_version([exe] + argv[1:], gate.env, {"gradle": "Gradle"}.get(tool))
        if err:
            items.append({"name": tool, "status": "FAIL", "detail": "%s: %s" % (exe, err)})
            continue
        versions[tool] = line
        items.append({"name": tool, "status": "PASS", "detail": "%s (%s)" % (line, exe)})
    if tools is None or "python3" in tools:
        venv = os.path.join(tmp, "venv-probe")
        rc, text = gate.run(["python3", "-m", "venv", venv], tmp, lp)
        if rc != 0 or not os.path.exists(os.path.join(venv, "bin", "pip")):
            items.append({"name": "python3 venv+pip", "status": "FAIL",
                          "detail": "python3 -m venv did not produce a venv with pip (ensurepip missing): " + tail_of(text, 3, 300)})
        else:
            items.append({"name": "python3 venv+pip", "status": "PASS", "detail": "ok"})
    bad = [i for i in items if i["status"] == "FAIL"]
    with open(lp, "a") as f:
        f.write("\n".join("%s %s %s" % (i["status"], i["name"], i["detail"]) for i in items) + "\n")
    gate.add("toolchain", "toolchain present (go pulumi make jq node npm python3-venv dotnet java gradle)",
             "FAIL" if bad else "PASS", "\n".join("%s: %s" % (i["name"], i["detail"]) for i in bad), lp,
             time.time() - t0, items=items)
    return versions


# --------------------------------------------------------------------- report

def write_report(report, report_dir, base):
    """Write <base>.json and <base>.md in report_dir; return their paths."""
    os.makedirs(report_dir, exist_ok=True)
    jp, mp = os.path.join(report_dir, base + ".json"), os.path.join(report_dir, base + ".md")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, sort_keys=True)
        f.write("\n")
    with open(mp, "w", encoding="utf-8") as f:
        f.write(render_markdown(report))
    return jp, mp


def render_markdown(r):
    """Render the report dict as Markdown."""
    L = ["# pulumi-labs-export verify: %s" % r["overall"], "",
         "- mode: %s" % r["mode"], "- repo: %s" % r["repo"], "- HEAD: %s" % r.get("head_sha"),
         "- tree: %s" % r.get("tree_sha"), "- branch: %s" % r.get("branch"),
         "- started: %s, finished: %s" % (r["started"], r["finished"]),
         "- `git status --porcelain`: %s" % ("clean" if not r.get("porcelain") else "DIRTY (%d)" % len(r["porcelain"]))]
    if r.get("skill"):
        L.append("- skill clone: %s%s" % (r["skill"]["sha"], " (dirty)" if r["skill"]["dirty"] else ""))
    L += ["", "## Tools", ""] + ["- %s: %s" % (k, v) for k, v in sorted(r.get("tools", {}).items())]
    L += ["", "## Steps", "", "| status | step | seconds | detail |", "|---|---|---|---|"]
    for s in r["steps"]:
        first = (s["detail"].splitlines() or [""])[0].replace("|", "\\|")[:140]
        L.append("| %s | %s | %s | %s |" % (s["status"], s["id"], s["seconds"], first))
    ex = r.get("examples")
    if ex is not None:
        L += ["", "## Failure keys (%d enforced, %d report-only, %d skipped checks)" % (
            len(ex["keys"]), len(ex["report_only_keys"]), len(ex["skipped_checks"])), ""]
        L += ["- `%s`" % k for k in ex["keys"]] or ["(none)"]
        if ex["report_only_keys"]:
            L += ["", "Report-only (listed, not enforced):"] + ["- `%s`" % k for k in ex["report_only_keys"]]
        if ex["skipped_checks"]:
            L += ["", "Skipped (an earlier step failed): " + ", ".join(ex["skipped_checks"])]
    for s in r["steps"]:
        if s["status"] != "PASS" or s["warnings"]:
            L += ["", "### %s (%s)" % (s["id"], s["status"]), ""]
            if s["detail"]:
                L += ["```", s["detail"], "```"]
            if s["items"]:
                L += ["Items:"] + ["- %s %s: %s" % (i["status"], i["name"], i["detail"].splitlines()[0] if i["detail"] else "") for i in s["items"]]
            if s["warnings"]:
                L += ["Warnings (%d):" % len(s["warnings"])] + ["- " + w for w in s["warnings"][:20]]
            if s["log_tail"] and s["status"] != "PASS":
                L += ["Log tail (%s):" % s["log"], "```", s["log_tail"], "```"]
    return "\n".join(L) + "\n"


def finish_report(gate, mode, repo, state, versions, started, report_dir, base, extra=None):
    """Assemble, write and return (report, json_path, md_path)."""
    report = {"gate_version": GATE_VERSION, "mode": mode, "repo": repo, "repo_name": os.path.basename(repo.rstrip("/")),
              "head_sha": state.get("head"), "tree_sha": state.get("tree"), "branch": state.get("branch"),
              "porcelain": state.get("porcelain", []), "skill": skill_state(), "tools": versions,
              "host": {"machine": os.uname().machine, "python": sys.version.split()[0]},
              "started": started, "finished": utcnow(), "steps": gate.steps,
              "overall": "PASS" if gate.ok and gate.steps else "FAIL"}
    report.update(extra or {})
    jp, mp = write_report(report, report_dir, base)
    return report, jp, mp


def _schema_without_version(text):
    """Parse schema JSON text (leading noise tolerated) and drop the version field; return the dict."""
    d = json.loads(text[text.index("{"):])
    d.pop("version", None)
    return d


def binary_schema_step(gate, clone, n):
    """The built provider binary must serve the committed schema (modulo version): it embeds it with //go:embed."""
    t0 = time.time()
    sid = "binary_schema"
    binary = os.path.join(clone, "bin", n["binary"])
    lp = gate.log_path(sid)
    rc, text = gate.run(["pulumi", "package", "get-schema", binary], clone, lp)
    if rc != 0:
        gate.add(sid, "provider binary serves the committed schema", "FAIL", "`pulumi package get-schema %s` failed (exit %d)" % (binary, rc),
                 lp, time.time() - t0, tail=tail_of(text))
        return
    try:
        served = _schema_without_version(text)
        with open(os.path.join(clone, n["schema_path"]), encoding="utf-8") as f:
            committed = _schema_without_version(f.read())
    except (ValueError, OSError) as e:
        gate.add(sid, "provider binary serves the committed schema", "FAIL", "cannot compare schemas: %s" % e, lp, time.time() - t0)
        return
    same = served == committed
    detail = ""
    if not same:
        keys = sorted(k for k in set(served) | set(committed) if served.get(k) != committed.get(k))
        detail = ("the binary serves a schema that differs from the committed one in: %s. The binary must embed the committed schema "
                  "(//go:embed schema.json in the rendered cmd main.go); run `make schema` and commit" % ", ".join(keys))
    gate.add(sid, "provider binary serves the committed schema (modulo version)", "PASS" if same else "FAIL", detail, lp, time.time() - t0)


def go_mod_tidy_step(gate, clone):
    """`make tidy` (go mod tidy in provider/ and provider/shim/) must leave go.mod and go.sum untouched."""
    t0 = time.time()
    sid = "go_mod_tidy"
    lp = gate.log_path(sid)
    rc, text = gate.run(["make", "tidy"], clone, lp)
    if rc != 0:
        gate.add(sid, "go.mod/go.sum of provider/ and provider/shim/ are tidy", "FAIL", "`make tidy` failed (exit %d)" % rc, lp,
                 time.time() - t0, tail=tail_of(text))
        return
    rc2, out = git(clone, "status", "--porcelain", "--", "provider")
    changed = [l for l in out.splitlines() if l.strip()]
    gate.add(sid, "go.mod/go.sum of provider/ and provider/shim/ are tidy", "FAIL" if changed else "PASS",
             ("`make tidy` changed: %s. Fix it in the monorepo (tools/replace-tidy enforces the same there), then re-sync" % ", ".join(changed[:6]))
             if changed else "", lp, time.time() - t0)


# ------------------------------------------------------------------ repo mode

def run_repo(repo, prefix, report_dir, keep=False):
    """Run the full gate on a carved repo; return (exit code, report paths)."""
    repo = os.path.abspath(repo)
    started = utcnow()
    state = git_state(repo)
    base = "%s-%s" % (os.path.basename(repo), state["head"])
    env = build_env(prefix)
    work = tempfile.mkdtemp(prefix="verify-", dir=os.environ.get("VERIFY_TMPDIR") or None)
    gate = Gate(os.path.join(report_dir, base + ".logs"), env)
    shutil.rmtree(gate.logdir, ignore_errors=True)
    os.makedirs(gate.logdir)
    versions = {}
    try:
        # 1. identity
        slug_names = None
        archetype = "native"
        problems = []
        if state["porcelain"]:
            problems.append("worktree is dirty (%d paths): commit or discard before the gate runs" % len(state["porcelain"]))
        opt = os.path.join(repo, render.OPTIONS_FILE)
        try:
            with open(opt, encoding="utf-8") as f:
                rec = json.load(f)
            slug = rec["provider"]
            archetype = render.archetype_of(rec)
            slug_names = names.derive(slug)
        except (OSError, ValueError, KeyError, names.SlugError) as e:
            problems.append("cannot read provider from %s: %s" % (render.OPTIONS_FILE, e))
        gate.add("identity", "HEAD %s, tree %s, clean worktree" % (state["head"][:12], state["tree"][:12]),
                 "FAIL" if problems else "PASS", "\n".join(problems))
        versions = toolchain_step(gate, work, prefix)
        if slug_names is None:
            return _end(gate, "repo", repo, state, versions, started, report_dir, base)
        n = slug_names
        bridged = archetype == "bridged"
        extra = {"archetype": archetype} if bridged else None
        clone = os.path.join(work, "build")
        lp = gate.log_path("clone")
        rc, text = gate.run(["git", "clone", "-q", "--no-checkout", "--no-hardlinks", repo, clone], work, lp)
        if rc == 0:
            rc, text = gate.run(["git", "-C", clone, "checkout", "-q", "--detach", state["head"]], work, lp)
        if rc != 0:
            gate.add("make_provider", "make provider", "FAIL", "cannot make a clean local clone of HEAD: " + tail_of(text, 5, 500))
            return _end(gate, "repo", repo, state, versions, started, report_dir, base, extra)

        gate.cmd("make_provider", "make provider", ["make", "provider"], clone)
        gate.cmd("make_lint", "make lint", ["make", "lint"], clone)
        if bridged or has_c_compiler(env):
            gate.cmd("make_test", "make test", ["make", "test"], clone)
        else:
            gate.cmd("make_test", "make test (no -race: no C compiler)",
                                ["go", "test", "-count=1", "./provider/pkg/..."], clone)
            gate.steps[-1]["warnings"].append("`make test` runs `go test -race`, which needs cgo and a C compiler; none on PATH. "
                                              "Ran `go test -count=1 ./provider/pkg/...` without the race detector. CI still runs -race.")
        gate.cmd("make_check_copyright", "make check_copyright", ["make", "check_copyright"], clone)
        gate.cmd("make_schema", "make schema", ["make", "schema"], clone)
        t0 = time.time()
        lp = gate.log_path("schema_diff")
        rc, text = gate.run(["git", "-C", clone, "diff", "--exit-code", "--", n["schema_path"]], work, lp)
        rc2, _ = gate.run(["git", "-C", clone, "ls-files", "--error-unmatch", n["schema_path"]], work, lp)
        gate.add("schema_diff", "regenerated schema equals committed schema (git diff --exit-code)",
                 "FAIL" if rc != 0 or rc2 != 0 else "PASS",
                 "" if rc == 0 and rc2 == 0 else ("schema.json is not tracked" if rc2 != 0 else "schema.json is out of date: run `make schema` and commit"),
                 lp, time.time() - t0, tail=tail_of(text))
        if bridged:
            # tfgen also writes bridge-metadata.json (and Pulumi.yaml) next to the schema; both are committed.
            t0 = time.time()
            lp = gate.log_path("metadata_diff")
            meta_paths = [n["bridge_metadata_path"], n["cmd_dir"] + "/Pulumi.yaml"]
            rc, text = gate.run(["git", "-C", clone, "diff", "--exit-code", "--"] + meta_paths, work, lp)
            rc2, _ = gate.run(["git", "-C", clone, "ls-files", "--error-unmatch", n["bridge_metadata_path"]], work, lp)
            gate.add("metadata_diff", "regenerated bridge metadata equals committed (git diff --exit-code)",
                     "FAIL" if rc != 0 or rc2 != 0 else "PASS",
                     "" if rc == 0 and rc2 == 0 else ("bridge-metadata.json is not tracked" if rc2 != 0 else
                                                      "bridge-metadata.json is out of date: run `make schema` and commit"),
                     lp, time.time() - t0, tail=tail_of(text))

        # Always the dev version: it is what export.py --generate committed, and the release workflow re-stamps it.
        argv = ["make", "sdks", "VERSION=" + check.SDK_DEV_VERSION]
        gate.cmd("make_sdks", " ".join(argv), argv, clone, error_res=CODEGEN_ERROR_RES, collect_warnings=True)
        t0 = time.time()
        stamp = check.sdk_version_failures(clone)
        gate.add("sdk_version", "regenerated SDKs carry %s (nodejs package.json, dotnet version.txt)" % check.SDK_DEV_VERSION,
                 "FAIL" if stamp else "PASS", "\n".join(stamp), seconds=time.time() - t0)
        _, por = git(clone, "status", "--porcelain", "--", "sdk")
        if por.strip():
            gate.steps[-1]["warnings"].append("regenerated sdk/ differs from the committed sdk/ (%d paths); "
                                              "commit the regenerated SDK if the schema changed" % len(por.splitlines()))
        sdk_dir = os.path.join(clone, n["go_sdk_dir"])
        gate.cmd("go_sdk_build", "go build ./... in %s" % n["go_sdk_dir"], ["go", "build", "./..."], sdk_dir)
        gate.cmd("go_sdk_vet", "go vet ./... in %s" % n["go_sdk_dir"], ["go", "vet", "./..."], sdk_dir)
        go_module_paths(gate, clone, n, bridged)
        if bridged:
            go_mod_tidy_step(gate, clone)
            binary_schema_step(gate, clone, n)
        consumer_smoke(gate, "go_consumer_smoke", sdk_dir, n["go_import_base_path"], work)
        go_examples(gate, clone, sdk_dir, n["go_import_base_path"], work)
        for lang in ("nodejs", "python", "dotnet", "java"):
            gate.env.update(prepare_sandbox_for(lang, prefix, gate.env))
            gate.cmd("build_%s_sdk" % lang, "make build_%s_sdk" % lang, ["make", "build_%s_sdk" % lang], clone,
                     fail_on_skip=True, collect_warnings=True)
        schema = None
        try:
            with open(os.path.join(clone, n["schema_path"]), encoding="utf-8") as f:
                schema = json.load(f)
        except (OSError, ValueError):
            pass
        default_provider_steps(gate, schema, os.path.join(clone, "provider"), clone, bridged)
        gate.cmd("check_py", "check.py --repo", [sys.executable, os.path.join(HERE, "check.py"), "--repo", repo], work)
        after = git_state(repo)
        moved = after["head"] != state["head"] or after["porcelain"]
        gate.add("worktree_clean_after", "checkout unchanged by the gate (HEAD same, porcelain empty)",
                 "FAIL" if moved else "PASS", "the checkout changed during the gate" if moved else "")
        state["porcelain"] = after["porcelain"]
        return _end(gate, "repo", repo, state, versions, started, report_dir, base, extra)
    finally:
        if not keep:
            shutil.rmtree(work, ignore_errors=True)


def _end(gate, mode, repo, state, versions, started, report_dir, base, extra=None):
    report, jp, mp = finish_report(gate, mode, repo, state, versions, started, report_dir, base, extra)
    print("verify %s: %s" % (mode, report["overall"]))
    for s in report["steps"]:
        print("  %-4s %s%s" % (s["status"], s["id"], ("  - " + s["detail"].splitlines()[0][:150]) if s["detail"] else ""))
        for i in s["items"]:
            if i["status"] == "FAIL":
                print("         FAIL %s: %s" % (i["name"], (i["detail"].splitlines() or [""])[0][:150]))
    print("report: %s\nreport: %s" % (jp, mp))
    return (0 if report["overall"] == "PASS" else 1), (jp, mp)


# ----------------------------------------------------------- assert-pushable

def assert_pushable(repo, report_dir):
    """Return (ok, message): a passing report must exist for the current HEAD and the worktree must be clean."""
    repo = os.path.abspath(repo)
    try:
        state = git_state(repo)
    except RuntimeError as e:
        return False, str(e)
    if state["porcelain"]:
        return False, "NOT PUSHABLE: worktree is dirty (%d paths); run the gate on a clean commit" % len(state["porcelain"])
    path = os.path.join(report_dir, "%s-%s.json" % (os.path.basename(repo), state["head"]))
    try:
        with open(path, encoding="utf-8") as f:
            r = json.load(f)
    except OSError:
        return False, "NOT PUSHABLE: no report for HEAD %s (expected %s); run `python3 scripts/verify.py --repo %s`" % (state["head"], path, repo)
    except ValueError as e:
        return False, "NOT PUSHABLE: report %s is not valid JSON: %s" % (path, e)
    if r.get("mode") != "repo":
        return False, "NOT PUSHABLE: %s is a %r report, not a gate report for a carved repo" % (path, r.get("mode"))
    if r.get("head_sha") != state["head"] or r.get("tree_sha") != state["tree"]:
        return False, "NOT PUSHABLE: report does not match HEAD/tree (report %s/%s, now %s/%s)" % (
            r.get("head_sha"), r.get("tree_sha"), state["head"], state["tree"])
    if r.get("porcelain"):
        return False, "NOT PUSHABLE: the gate ran on a dirty worktree"
    steps = {s.get("id"): s for s in r.get("steps", [])}
    try:
        with open(os.path.join(repo, render.OPTIONS_FILE), encoding="utf-8") as f:
            archetype = render.archetype_of(json.load(f))
    except (OSError, ValueError):
        archetype = "native"
    if r.get("archetype", "native") != archetype:
        return False, "NOT PUSHABLE: the report is for the %s archetype but the repo records %s" % (r.get("archetype", "native"), archetype)
    missing = [i for i in required_steps(archetype) if i not in steps]
    if missing:
        return False, "NOT PUSHABLE: report lacks required step(s): %s" % ", ".join(missing)
    failed = [i for i, s in steps.items() if s.get("status") != "PASS"]
    if failed or r.get("overall") != "PASS":
        return False, "NOT PUSHABLE: failing step(s): %s" % (", ".join(failed) or "overall != PASS")
    ok, msg = secret_scan_clean(repo, state, report_dir)
    if not ok:
        return False, msg
    return True, ("PUSHABLE: gate and secret scan passed for HEAD %s (tree %s), worktree clean. Reports: %s, %s"
                  % (state["head"], state["tree"], path, secret_scan.report_path(repo, state["head"], report_dir)))


def secret_scan_clean(repo, state, report_dir):
    """Return (ok, message): a clean secret_scan.py report must exist for the current HEAD (history and tree, default rules)."""
    path = secret_scan.report_path(repo, state["head"], report_dir)
    hint = "run `python3 scripts/secret_scan.py --repo %s`" % repo
    try:
        with open(path, encoding="utf-8") as f:
            r = json.load(f)
    except OSError:
        return False, "NOT PUSHABLE: no secret-scan report for HEAD %s (expected %s); %s" % (state["head"], path, hint)
    except ValueError as e:
        return False, "NOT PUSHABLE: secret-scan report %s is not valid JSON: %s" % (path, e)
    if r.get("mode") != "secret-scan" or r.get("head_sha") != state["head"] or r.get("tree_sha") != state["tree"]:
        return False, "NOT PUSHABLE: secret-scan report %s does not match HEAD/tree; %s" % (path, hint)
    if r.get("porcelain"):
        return False, "NOT PUSHABLE: the secret scan ran on a dirty worktree; commit, then %s" % hint
    found = len(r.get("history_findings") or []) + len(r.get("tree_findings") or [])
    if found or r.get("overall") != "PASS":
        return False, ("NOT PUSHABLE: the secret scan has %d finding(s) (see %s). Fix the cause in the monorepo; an allowlist entry "
                       "needs a reviewed monorepo PR (references/verify.md)" % (found, path))
    return True, "secret scan clean"


# --------------------------------------------------------------- monorepo mode

EXPECTED_META = (("WithGoImportPath", "go_import_base_path", "language.go.importBasePath"),
                 ("WithRepository", "repo_url", "repository"),
                 ("WithPublisher", "publisher", "publisher"),
                 ("WithPluginDownloadURL", "plugin_download_url", "pluginDownloadURL"))


def metadata_warnings(schema, n):
    """Return WARN strings for schema metadata that does not yet carry the pulumi-labs values."""
    got = {"language.go.importBasePath": (((schema.get("language") or {}).get("go") or {}).get("importBasePath")),
           "repository": schema.get("repository"), "publisher": schema.get("publisher"),
           "pluginDownloadURL": schema.get("pluginDownloadURL")}
    out = []
    for _, key, label in EXPECTED_META:
        if got[label] != n[key]:
            out.append("provider metadata %s is %r, the carve must set %r (the monorepo contract keeps the pulumi/ values until the namespace commit)"
                       % (label, got[label], n[key]))
    for lang in check.VERSIONED_LANGUAGES:
        if (((schema.get("language") or {}).get(lang) or {}).get("respectSchemaVersion")) is not True:
            out.append("schema language.%s.respectSchemaVersion is not true: `gen-sdk --version` would be ignored and the .NET SDK would not build. "
                       "A custom WithLanguageMap replaces the framework defaults and must carry them (references/export.md, step b)" % lang)
    return out


def tfgen_env(env):
    """Environment for a tfgen run: PULUMI_CONVERT=0 as in the archetype Makefile (decision 5 in references/bridged.md).

    With the converter on, tfgen converts the upstream HCL examples and can panic in the PCL binder on a provider's examples,
    which would fail the schema step for a reason that has nothing to do with readiness (doppler, langfuse, prefect, qdrant-cloud).
    """
    return dict(env, PULUMI_CONVERT="0")


def stamp_bridged_schema(schema, n):
    """Apply the identity-switch schema edits a monorepo bridged provider still lacks to the gate's scratch schema.

    Return the list of fields set. A monorepo provider has not had its identity-switch edit yet, so its ProviderInfo may lack
    `RespectSchemaVersion: true` (`gen-sdk --version` is then ignored and the .NET SDK cannot build for want of version.txt)
    and `Golang.ImportBasePath` (the Go SDK has no module path, so `go mod init` fails). Both are applied to the scratch copy so
    the SDK steps still prove everything else; `metadata_warnings` runs on the unstamped schema and reports each as a WARN
    (identity.py lists the same fields for the identity-switch PR). A present importBasePath is never overwritten.
    """
    changed = []
    langs = schema.setdefault("language", {})
    for lang in check.VERSIONED_LANGUAGES:
        info = langs.setdefault(lang, {})
        if info.get("respectSchemaVersion") is not True:
            info["respectSchemaVersion"] = True
            changed.append("language.%s.respectSchemaVersion" % lang)
    go = langs["go"]
    if not go.get("importBasePath"):
        go["importBasePath"] = n["go_import_base_path"]
        changed.append("language.go.importBasePath")
    return changed


def github_org_warnings(code_root):
    """Return a WARN list when the bridged provider's tfbridge.ProviderInfo sets no GitHubOrg.

    GitHubOrg names the upstream Terraform provider's organisation; with it tfgen brings the upstream docs into the schema.
    It is recommended, not required (decision 5 in references/bridged.md).
    """
    try:
        names_ = sorted(fn for fn in os.listdir(code_root) if fn.endswith(".go") and not fn.endswith("_test.go"))
    except OSError:
        return []
    for fn in names_:
        with open(os.path.join(code_root, fn), encoding="utf-8", errors="replace") as f:
            text = f.read()
        m = re.search(r"tfbridge\.ProviderInfo\{", text)
        if m:
            info = identity.go_block(text, m.end() - 1) or ""
            if re.search(r"\bGitHubOrg:", info):
                return []
            return ["%s:%d ProviderInfo has no GitHubOrg: set it to the upstream Terraform provider's organisation so tfgen brings the "
                    "upstream docs into the schema (recommended, not required)" % (fn, text.count("\n", 0, m.start()) + 1)]
    return []


def monorepo_schema(gate, provider, mod, work, n, build_binary=True):
    """Build the provider and produce its schema under work/gen; return (schema, schema_file, bridged, bridged_meta).

    Native: build the binary and `pulumi package get-schema` it. Bridged (a cmd/pulumi-tfgen-<p> exists): run tfgen,
    take its schema, record the pulumi-labs metadata WARNs and stamp the identity-switch edits into the scratch copy;
    the binary is then only built when build_binary (the examples profile does not need it). schema is None when a
    step failed.
    """
    binary = os.path.join(work, "bin", "pulumi-resource-" + provider)
    bridged = os.path.isdir(os.path.join(mod, "cmd", "pulumi-tfgen-" + provider))
    if build_binary or not bridged:
        gate.cmd("provider_binary", "build provider binary", ["go", "build", "-o", binary, "./cmd/pulumi-resource-" + provider], mod)
    schema_file = os.path.join(work, "gen", "provider", "cmd", "pulumi-resource-" + provider, "schema.json")
    os.makedirs(os.path.dirname(schema_file))
    schema = None
    bridged_meta = []
    if bridged:
        # A bridged provider's schema comes from its tfgen command, not from the binary (which is not
        # built with the generated schema embedded in the monorepo).
        gm = gomod.parse_gomod(open(os.path.join(mod, "go.mod"), encoding="utf-8").read())["module"]
        tfgen_out = os.path.join(work, "tfgen")
        os.makedirs(tfgen_out)
        st, text = gate.cmd("get_schema", "pulumi-tfgen-%s schema" % provider,
                            ["go", "run", "-ldflags", "-X %s/pkg/version.Version=%s" % (gm, check.SDK_DEV_VERSION),
                             "./cmd/pulumi-tfgen-" + provider, "schema", "--out", tfgen_out], mod,
                            env=tfgen_env(gate.env))
        if st == "PASS":
            try:
                with open(os.path.join(tfgen_out, "schema.json"), encoding="utf-8") as f:
                    schema = json.load(f)
                schema.pop("version", None)
                bridged_meta = metadata_warnings(schema, n)
                stamp_bridged_schema(schema, n)
                with open(schema_file, "w") as f:
                    json.dump(schema, f, indent=2)
                    f.write("\n")
            except (OSError, ValueError) as e:
                gate.steps[-1]["status"], gate.steps[-1]["detail"] = "FAIL", "tfgen did not write a valid schema.json: %s" % e
    else:
        st, text = gate.cmd("get_schema", "pulumi package get-schema", ["pulumi", "package", "get-schema", binary], work)
        if st == "PASS":
            try:
                schema = json.loads(text[text.index("{"):])
                schema.pop("version", None)
                with open(schema_file, "w") as f:
                    json.dump(schema, f, indent=2)
                    f.write("\n")
            except ValueError as e:
                gate.steps[-1]["status"], gate.steps[-1]["detail"] = "FAIL", "schema output is not JSON: %s" % e
    return schema, schema_file, bridged, bridged_meta


def run_monorepo(mono, provider, prefix, report_dir, keep=False):
    """Readiness run for lib/providers/<provider> in an autoregistry checkout; return (exit code, paths)."""
    mono = os.path.abspath(mono)
    started = utcnow()
    state = git_state(mono)
    n = names.derive(provider)
    base = "%s-monorepo-%s" % (provider, state["head"])
    env = build_env(prefix)
    work = tempfile.mkdtemp(prefix="verify-mono-", dir=os.environ.get("VERIFY_TMPDIR") or None)
    gate = Gate(os.path.join(report_dir, base + ".logs"), env)
    shutil.rmtree(gate.logdir, ignore_errors=True)
    os.makedirs(gate.logdir)
    pdir = os.path.join(mono, "lib", "providers", provider)
    mod = os.path.join(pdir, "provider")
    versions = {}
    try:
        if not os.path.isfile(os.path.join(mod, "go.mod")):
            gate.add("identity", "provider module present", "FAIL", "%s/go.mod not found" % mod)
            return _end(gate, "monorepo", mono, state, versions, started, report_dir, base, {"provider": provider})
        gate.add("identity", "monorepo HEAD %s, provider %s" % (state["head"][:12], provider), "PASS",
                 "" if not state["porcelain"] else "note: monorepo worktree has %d uncommitted paths" % len(state["porcelain"]))
        versions = toolchain_step(gate, work, prefix)
        gate.cmd("go_build", "go build ./... (provider module)", ["go", "build", "./..."], mod)
        gate.cmd("go_vet", "go vet ./...", ["go", "vet", "./..."], mod)
        gate.cmd("go_test", "go test -count=1 ./...", ["go", "test", "-count=1", "-timeout", "30m", "./..."], mod)
        # Windows cross-compile is informational: a failure means --exclude-platform at render time.
        wenv = dict(env, GOOS="windows", GOARCH="amd64", CGO_ENABLED="0")
        lp = gate.log_path("windows_build")
        rc, text = gate.run(["go", "build", "-o", os.devnull, "./cmd/pulumi-resource-" + provider], mod, lp, wenv)
        gate.add("windows_build", "GOOS=windows go build of the provider (informational)", "PASS",
                 "" if rc == 0 else "FAILS: pass --exclude-platform windows/amd64 --reason \"...\" to render.py",
                 lp, 0, warnings=[] if rc == 0 else ["windows/amd64 build fails: " + tail_of(text, 3, 300)])
        schema, schema_file, bridged, bridged_meta = monorepo_schema(gate, provider, mod, work, n)
        gen = os.path.join(work, "gen")
        if schema is not None:
            # Rendered archetype supplies the Makefile build_* targets and sdk_fixups, as in a carved repo.
            render.write_repo(gen, render.make_options(provider, provider.title(), []))
            for lang in ("nodejs", "python", "go", "dotnet", "java"):
                gate.cmd("gen_sdk_%s" % lang, "pulumi package gen-sdk --language %s" % lang,
                         ["pulumi", "package", "gen-sdk", "--language", lang, schema_file, "--version", "0.0.1-dev"],
                         gen, error_res=CODEGEN_ERROR_RES, collect_warnings=True)
            rel = go_sdk_dir_for(gen, schema)
            sdk_dir = os.path.join(gen, rel)
            sdk_module = ((schema.get("language") or {}).get("go") or {}).get("importBasePath") or ""
            if os.path.isdir(sdk_dir) and not os.path.exists(os.path.join(sdk_dir, "go.mod")):
                gate.cmd("go_sdk_mod", "go mod init %s && go mod tidy" % sdk_module,
                         ["sh", "-c", "go mod init %s && go mod tidy" % sdk_module], sdk_dir, error_res=CODEGEN_ERROR_RES)
            gate.cmd("go_sdk_build", "go build ./... in %s" % rel, ["go", "build", "./..."], sdk_dir)
            gate.cmd("go_sdk_vet", "go vet ./... in %s" % rel, ["go", "vet", "./..."], sdk_dir)
            consumer_smoke(gate, "go_consumer_smoke", sdk_dir, sdk_module, work)
            go_examples(gate, pdir, sdk_dir, sdk_module, work)
            gate.cmd("sdk_fixups", "make sdk_fixups (archetype fixups)", ["make", "sdk_fixups"], gen)
            for lang in ("nodejs", "python", "dotnet", "java"):
                gate.env.update(prepare_sandbox_for(lang, prefix, gate.env))
                gate.cmd("build_%s_sdk" % lang, "build %s SDK (make build_%s_sdk)" % (lang, lang),
                         ["make", "build_%s_sdk" % lang], gen, fail_on_skip=True, collect_warnings=True)
            meta = (bridged_meta + github_org_warnings(mod)) if bridged else metadata_warnings(schema, n)
            gate.add("metadata_pulumi_labs", "builder already carries pulumi-labs Go path, repository, publisher, plugin URL (WARN only)",
                     "PASS", "\n".join(meta), warnings=meta)
        default_provider_steps(gate, schema, mod, pdir, os.path.isdir(os.path.join(mod, "cmd", "pulumi-tfgen-" + provider)))
        return _end(gate, "monorepo", mono, state, versions, started, report_dir, base, {"provider": provider})
    finally:
        if not keep:
            shutil.rmtree(work, ignore_errors=True)


def _end_examples(gate, mono, state, versions, started, report_dir, base, provider, entries):
    """Finish an examples-profile report: attach the failure keys and the example inventory."""
    ex = examples_gate.failure_report(provider, gate.steps)
    ex["inventory"] = {"total": len(entries), "by_language": {l: len(examples_gate.entries_of(entries, l))
                                                              for l in sorted({e["lang"] for e in entries})}}
    return _end(gate, "monorepo", mono, state, versions, started, report_dir, base,
                {"provider": provider, "profile": "examples", "examples": ex})


def run_monorepo_examples(mono, provider, prefix, report_dir, keep=False):
    """The `examples` profile: compile every example and template of lib/providers/<provider> against the local SDKs.

    Runs only what the examples need: the toolchain for the example languages, the provider build, the schema, gen-sdk and the
    SDK build for the languages present, one compile step per language, and the dp_* checks. Every failure gets a stable key
    (examples_gate.failure_report) that the ratchet in .github/scripts/provider-examples.py compares to the baseline.
    """
    mono = os.path.abspath(mono)
    started = utcnow()
    state = git_state(mono)
    n = names.derive(provider)
    base = "%s-monorepo-%s" % (provider, state["head"])
    env = build_env(prefix)
    work = tempfile.mkdtemp(prefix="verify-examples-", dir=os.environ.get("VERIFY_TMPDIR") or None)
    gate = Gate(os.path.join(report_dir, base + ".logs"), env)
    shutil.rmtree(gate.logdir, ignore_errors=True)
    os.makedirs(gate.logdir)
    pdir = os.path.join(mono, "lib", "providers", provider)
    mod = os.path.join(pdir, "provider")
    versions, entries = {}, []
    try:
        if not os.path.isfile(os.path.join(mod, "go.mod")):
            gate.add("identity", "provider module present", "FAIL", "%s/go.mod not found" % mod)
            return _end_examples(gate, mono, state, versions, started, report_dir, base, provider, entries)
        gate.add("identity", "monorepo HEAD %s, provider %s (profile examples)" % (state["head"][:12], provider), "PASS",
                 "" if not state["porcelain"] else "note: monorepo worktree has %d uncommitted paths" % len(state["porcelain"]))
        entries = examples_gate.classify(pdir)
        unknown = [e for e in entries if e["lang"] == "unknown"]
        gate.add("example_language", "every example and template has a recognisable language (Pulumi.yaml runtime or directory name)",
                 "FAIL" if unknown else "PASS", "\n".join("%s: no runtime and no language token in the name" % e["rel"] for e in unknown),
                 items=[{"name": e["rel"], "path": e["rel"], "status": "FAIL", "detail": "no runtime and no language token in the name"}
                        for e in unknown])
        has = lambda lang: bool(examples_gate.entries_of(entries, lang))  # noqa: E731
        langs = examples_gate.sdk_languages(entries)
        tools = {"go", "pulumi", "make"} | ({"python3"} if has("python") else set()) | ({"node", "npm"} if has("ts") else set()) \
            | ({"dotnet"} if has("csharp") else set()) | ({"java", "gradle"} if has("java") else set())
        versions = toolchain_step(gate, work, prefix, tools)
        if has("python"):
            examples_gate.compile_python(gate, pdir, examples_gate.entries_of(entries, "python"))
        schema, schema_file, bridged, _ = monorepo_schema(gate, provider, mod, work, n, build_binary=False)
        gen = os.path.join(work, "gen")
        sdk = {}
        if schema is not None and langs:
            render.write_repo(gen, render.make_options(provider, provider.title(), []))
            for lang in langs:
                gate.cmd("gen_sdk_%s" % lang, "pulumi package gen-sdk --language %s" % lang,
                         ["pulumi", "package", "gen-sdk", "--language", lang, schema_file, "--version", "0.0.1-dev"],
                         gen, error_res=CODEGEN_ERROR_RES, collect_warnings=True)
            if set(langs) - {"go"}:
                gate.cmd("sdk_fixups", "make sdk_fixups (archetype fixups)", ["make", "sdk_fixups"], gen)
        generated = lambda lang: step_passed(gate, "gen_sdk_" + lang)  # noqa: E731
        if schema is None:
            for sid, lang in (("compile_go", "go"), ("compile_ts", "ts"), ("import_python", "python"),
                              ("compile_csharp", "csharp"), ("compile_java", "java")):
                if has(lang):
                    examples_gate.skip(gate, sid, sid, "no schema: the provider build or schema step failed")
        else:
            sdk_dir = ""
            if has("go"):
                rel = go_sdk_dir_for(gen, schema)
                sdk_dir = os.path.join(gen, rel)
                sdk_module = ((schema.get("language") or {}).get("go") or {}).get("importBasePath") or ""
                if generated("go") and os.path.isdir(sdk_dir) and not os.path.exists(os.path.join(sdk_dir, "go.mod")):
                    gate.cmd("go_sdk_mod", "go mod init %s && go mod tidy" % sdk_module,
                             ["sh", "-c", "go mod init %s && go mod tidy" % sdk_module], sdk_dir, error_res=CODEGEN_ERROR_RES)
                if generated("go"):
                    gate.cmd("go_sdk_build", "go build ./... in %s" % rel, ["go", "build", "./..."], sdk_dir)
                if step_passed(gate, "gen_sdk_go", "go_sdk_build") and step_passed_if_present(gate, "go_sdk_mod"):
                    go_examples(gate, pdir, sdk_dir, sdk_module, work, sid="compile_go")
                else:
                    examples_gate.skip(gate, "compile_go", "Go examples", "the Go SDK did not generate or build")
            if has("ts"):
                if generated("nodejs") and step_passed(gate, "sdk_fixups"):
                    gate.env.update(prepare_sandbox_for("nodejs", prefix, gate.env))
                    gate.cmd("build_nodejs_sdk", "make build_nodejs_sdk", ["make", "build_nodejs_sdk"], gen, fail_on_skip=True,
                             collect_warnings=True)
                if step_passed(gate, "build_nodejs_sdk"):
                    examples_gate.compile_ts(gate, pdir, examples_gate.entries_of(entries, "ts"), os.path.join(gen, "sdk", "nodejs"), work)
                else:
                    examples_gate.skip(gate, "compile_ts", "TypeScript examples", "the Node SDK did not generate or build")
            if has("python"):
                if generated("python") and step_passed(gate, "sdk_fixups"):
                    examples_gate.import_python(gate, pdir, examples_gate.entries_of(entries, "python"),
                                                os.path.join(gen, "sdk", "python"), work)
                else:
                    examples_gate.skip(gate, "import_python", "Python imports", "the Python SDK did not generate")
            if has("csharp"):
                if generated("dotnet") and step_passed(gate, "sdk_fixups"):
                    gate.env.update(prepare_sandbox_for("dotnet", prefix, gate.env))
                    gate.cmd("build_dotnet_sdk", "make build_dotnet_sdk", ["make", "build_dotnet_sdk"], gen, fail_on_skip=True,
                             collect_warnings=True)
                if step_passed(gate, "build_dotnet_sdk"):
                    examples_gate.compile_csharp(gate, pdir, examples_gate.entries_of(entries, "csharp"),
                                                 os.path.join(gen, "sdk", "dotnet"), work)
                else:
                    examples_gate.skip(gate, "compile_csharp", "C# examples", "the .NET SDK did not generate or build")
            if has("java"):
                if generated("java") and step_passed(gate, "sdk_fixups"):
                    gate.env.update(prepare_sandbox_for("java", prefix, gate.env))
                    gate.cmd("build_java_sdk", "make build_java_sdk", ["make", "build_java_sdk"], gen, fail_on_skip=True,
                             collect_warnings=True)
                if step_passed(gate, "build_java_sdk"):
                    examples_gate.compile_java(gate, pdir, examples_gate.entries_of(entries, "java"), os.path.join(gen, "sdk", "java"),
                                               {provider, str(schema.get("name") or provider)}, work)
                else:
                    examples_gate.skip(gate, "compile_java", "Java examples", "the Java SDK did not generate or build")
        if has("yaml"):
            examples_gate.compile_yaml(gate, pdir, examples_gate.entries_of(entries, "yaml"), schema)
        default_provider_steps(gate, schema, mod, pdir, bridged)
        return _end_examples(gate, mono, state, versions, started, report_dir, base, provider, entries)
    finally:
        if not keep:
            shutil.rmtree(work, ignore_errors=True)


def step_passed(gate, *ids):
    """True if every named step was recorded and passed."""
    return examples_gate.step_ok(gate, *ids)


def step_passed_if_present(gate, sid):
    """True unless the named step was recorded and did not pass."""
    return all(s["status"] == "PASS" for s in gate.steps if s["id"] == sid)


# ------------------------------------------------------------------------ cli

def main(argv=None):
    """CLI entry point; returns the process exit code."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=".", help="the carved repo (default: current directory)")
    ap.add_argument("--assert-pushable", action="store_true",
                    help="exit 0 only if a passing report exists for the current HEAD and the worktree is clean")
    ap.add_argument("--monorepo", help="readiness mode: an autoregistry checkout (needs --provider)")
    ap.add_argument("--provider", help="provider slug for --monorepo")
    ap.add_argument("--profile", choices=("readiness", "examples"), default="readiness",
                    help="--monorepo profile: readiness (default, the full pre-carve gate) or examples (compile every example and "
                         "template against the locally generated SDKs, plus the dp_* checks; prints and records failure keys)")
    ap.add_argument("--prefix", default=None, help="toolchain prefix (default: $HOME/.local/pulumi-labs-export)")
    ap.add_argument("--report-dir", default=None, help="report directory (default: <prefix>/reports)")
    ap.add_argument("--keep-work", action="store_true", help="keep the scratch directory for debugging")
    args = ap.parse_args(argv)
    prefix = os.path.abspath(args.prefix or bootstrap.default_prefix())
    report_dir = os.path.abspath(args.report_dir or os.path.join(prefix, "reports"))
    try:
        if args.assert_pushable:
            ok, msg = assert_pushable(args.repo, report_dir)
            print(msg, file=sys.stdout if ok else sys.stderr)
            return 0 if ok else 1
        if args.monorepo:
            if not args.provider:
                ap.error("--monorepo needs --provider")
            names.validate_slug(args.provider)
            run = run_monorepo_examples if args.profile == "examples" else run_monorepo
            return run(args.monorepo, args.provider, prefix, report_dir, args.keep_work)[0]
        return run_repo(args.repo, prefix, report_dir, args.keep_work)[0]
    except (RuntimeError, names.SlugError) as e:
        print("verify: error: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
