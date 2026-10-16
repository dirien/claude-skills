#!/usr/bin/env python3
"""Pre-push verification gate for pulumi-labs provider repos.

Three modes (details in references/verify.md):

    python3 scripts/verify.py --repo ../pulumi-modal            # the gate (default repo: cwd)
    python3 scripts/verify.py --assert-pushable --repo ../pulumi-modal
    python3 scripts/verify.py --monorepo <autoregistry checkout> --provider modal

The gate builds a clean local clone of the repo's HEAD in a scratch directory
(so the checkout stays untouched and the proof is for the exact commit) and
runs, capturing full logs: toolchain presence, make provider/lint/test/
check_copyright, schema regeneration + git diff, make sdks (errors in codegen
or `go mod tidy` fail), Go SDK build/vet, Go module-path checks, a consumer
smoke test, every Go example and template against the LOCAL Go SDK, all four
non-Go SDK builds (a "skipping" message is a failure), the default-provider and
credential checks, and check.py. Missing tools FAIL, they never skip.

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
import gomod  # noqa: E402
import names  # noqa: E402
import render  # noqa: E402

SKILL_DIR = os.path.dirname(HERE)
GATE_VERSION = 1
TAIL_LINES = 25
TAIL_CHARS = 4000
CMD_TIMEOUT = 3600

# Steps every passing carved-repo report must contain (so a partial or forged
# report cannot satisfy --assert-pushable). Per-example detail lives in items.
REQUIRED_STEPS = [
    "identity", "toolchain", "make_provider", "make_lint", "make_test", "make_check_copyright",
    "make_schema", "schema_diff", "make_sdks", "go_sdk_build", "go_sdk_vet", "go_module_paths",
    "go_consumer_smoke", "go_examples", "build_nodejs_sdk", "build_python_sdk", "build_dotnet_sdk",
    "build_java_sdk", "dp_env_defaults", "dp_no_required_config", "dp_no_env_tags",
    "dp_no_explicit_provider", "dp_no_credential_require", "check_py", "worktree_clean_after",
]

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
SKIPPING_RE = re.compile(r"(?i)\bskipping\b")
CRED_RE = re.compile(r"token|key|secret|password|credential", re.I)


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


def has_c_compiler(env):
    """True if a C compiler is on PATH (needed by `go test -race`)."""
    path = env.get("PATH", "")
    return any(shutil.which(c, path=path) for c in ("gcc", "cc", "clang"))


# ------------------------------------------------------------ the gate state

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
CRED_REQUIRE_RE = re.compile(r"\.[Rr]equire\w*\s*\(\s*(?:ctx\s*,\s*)?[\"']([^\"']+)[\"']")


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


def find_credential_requires(ext, text):
    """Return [(lineno, key, line)] where a program requires a credential-named config key."""
    if ext.lower() not in LANG_BY_EXT or LANG_BY_EXT[ext.lower()] == "yaml":
        return []
    hits = []
    for i, line in enumerate(text.splitlines(), 1):
        if _is_comment(line):
            continue
        for m in CRED_REQUIRE_RE.finditer(line):
            if CRED_RE.search(m.group(1)):
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


def scan_examples(root):
    """Return (explicit_provider_hits, credential_require_hits) as lists of strings."""
    prov, cred = [], []
    for rel, ext, text in iter_example_files(root):
        for ln, line in find_explicit_provider(ext, text):
            prov.append("%s:%d: %s" % (rel, ln, line))
        for ln, key, line in find_credential_requires(ext, text):
            cred.append("%s:%d: requires credential-named config key %r: %s" % (rel, ln, key, line))
    return prov, cred


def default_provider_steps(gate, schema, code_root, examples_root):
    """Record the five static default-provider / credential steps."""
    def step(sid, title, items):
        bad = [i for i in items if i["status"] == "FAIL"]
        gate.add(sid, title, "FAIL" if bad else "PASS",
                 "\n".join("%s: %s" % (i["name"], i["detail"]) for i in bad[:20]) + (("\n... %d more" % (len(bad) - 20)) if len(bad) > 20 else ""),
                 items=items)

    if schema is None:
        for sid, title in (("dp_env_defaults", "credential config variables have env defaults"),
                           ("dp_no_required_config", "no required provider config")):
            gate.add(sid, title, "FAIL", "no schema available to inspect")
    else:
        step("dp_env_defaults", "credential config variables have defaultInfo.environment", check_config_env_defaults(schema))
        step("dp_no_required_config", "config.defaults and provider.requiredInputs are empty", check_no_required_config(schema))
    tags = scan_env_tags(code_root) if os.path.isdir(code_root) else []
    step("dp_no_env_tags", "no env:\"...\" struct tags on pulumi fields (infer ignores them)",
         [{"name": t.split(":", 2)[0] + ":" + t.split(":", 2)[1], "status": "FAIL", "detail": t} for t in tags]
         or [{"name": "provider code", "status": "PASS", "detail": "no env struct tags"}])
    prov, cred = scan_examples(examples_root)
    step("dp_no_explicit_provider", "examples and templates use the default provider (no explicit Provider)",
         [{"name": h.split(":", 1)[0], "status": "FAIL", "detail": h} for h in prov]
         or [{"name": "examples+templates", "status": "PASS", "detail": "no explicit provider instantiation"}])
    step("dp_no_credential_require", "examples and templates do not require credential config keys",
         [{"name": h.split(":", 1)[0], "status": "FAIL", "detail": h} for h in cred]
         or [{"name": "examples+templates", "status": "PASS", "detail": "no require of credential-named keys"}])


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
                status, detail = "FAIL", "`%s` failed (exit %d): %s" % (" ".join(argv), rc, tail_of(text, 6, 600))
                break
        items.append({"name": rel, "status": status, "detail": detail})
    bad = [i for i in items if i["status"] == "FAIL"]
    gate.add(sid, "Go examples and templates compile against the local Go SDK",
             "FAIL" if bad else "PASS",
             "\n".join("%s: %s" % (i["name"], i["detail"]) for i in bad) if bad else
             ("%d Go example/template dir(s) compiled" % len(items) if items else "no Go examples or templates present"),
             lp, time.time() - t0, items=items)


def go_module_paths(gate, clone, slug_names):
    """`go list -m` must equal the repo module and every `go list ./...` package must sit under it."""
    t0 = time.time()
    sid = "go_module_paths"
    lp = gate.log_path(sid)
    want = slug_names["go_module"]
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


def toolchain_step(gate, tmp, prefix):
    """FAIL for any missing/unrunnable tool; return {tool: version line}."""
    t0 = time.time()
    lp = gate.log_path("toolchain")
    versions, items = {}, []
    for tool, argv in TOOL_PROBES:
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
        problems = []
        if state["porcelain"]:
            problems.append("worktree is dirty (%d paths): commit or discard before the gate runs" % len(state["porcelain"]))
        opt = os.path.join(repo, render.OPTIONS_FILE)
        try:
            with open(opt, encoding="utf-8") as f:
                slug = json.load(f)["provider"]
            slug_names = names.derive(slug)
        except (OSError, ValueError, KeyError, names.SlugError) as e:
            problems.append("cannot read provider from %s: %s" % (render.OPTIONS_FILE, e))
        gate.add("identity", "HEAD %s, tree %s, clean worktree" % (state["head"][:12], state["tree"][:12]),
                 "FAIL" if problems else "PASS", "\n".join(problems))
        versions = toolchain_step(gate, work, prefix)
        if slug_names is None:
            return _end(gate, "repo", repo, state, versions, started, report_dir, base)
        n = slug_names
        clone = os.path.join(work, "build")
        lp = gate.log_path("clone")
        rc, text = gate.run(["git", "clone", "-q", "--no-checkout", "--no-hardlinks", repo, clone], work, lp)
        if rc == 0:
            rc, text = gate.run(["git", "-C", clone, "checkout", "-q", "--detach", state["head"]], work, lp)
        if rc != 0:
            gate.add("make_provider", "make provider", "FAIL", "cannot make a clean local clone of HEAD: " + tail_of(text, 5, 500))
            return _end(gate, "repo", repo, state, versions, started, report_dir, base)

        gate.cmd("make_provider", "make provider", ["make", "provider"], clone)
        gate.cmd("make_lint", "make lint", ["make", "lint"], clone)
        if has_c_compiler(env):
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

        sdkv = None
        try:
            with open(os.path.join(clone, "sdk/nodejs/package.json")) as f:
                sdkv = json.load(f).get("version")
        except (OSError, ValueError):
            pass
        argv = ["make", "sdks"] + (["VERSION=" + sdkv] if sdkv else [])
        gate.cmd("make_sdks", " ".join(argv), argv, clone, error_res=CODEGEN_ERROR_RES, collect_warnings=True)
        _, por = git(clone, "status", "--porcelain", "--", "sdk")
        if por.strip():
            gate.steps[-1]["warnings"].append("regenerated sdk/ differs from the committed sdk/ (%d paths); "
                                              "commit the regenerated SDK if the schema changed" % len(por.splitlines()))
        sdk_dir = os.path.join(clone, n["go_sdk_dir"])
        gate.cmd("go_sdk_build", "go build ./... in %s" % n["go_sdk_dir"], ["go", "build", "./..."], sdk_dir)
        gate.cmd("go_sdk_vet", "go vet ./... in %s" % n["go_sdk_dir"], ["go", "vet", "./..."], sdk_dir)
        go_module_paths(gate, clone, n)
        consumer_smoke(gate, "go_consumer_smoke", sdk_dir, n["go_import_base_path"], work)
        go_examples(gate, clone, sdk_dir, n["go_import_base_path"], work)
        for lang in ("nodejs", "python", "dotnet", "java"):
            gate.cmd("build_%s_sdk" % lang, "make build_%s_sdk" % lang, ["make", "build_%s_sdk" % lang], clone,
                     fail_on_skip=True, collect_warnings=True)
        schema = None
        try:
            with open(os.path.join(clone, n["schema_path"]), encoding="utf-8") as f:
                schema = json.load(f)
        except (OSError, ValueError):
            pass
        default_provider_steps(gate, schema, os.path.join(clone, "provider"), clone)
        gate.cmd("check_py", "check.py --repo", [sys.executable, os.path.join(HERE, "check.py"), "--repo", repo], work)
        after = git_state(repo)
        moved = after["head"] != state["head"] or after["porcelain"]
        gate.add("worktree_clean_after", "checkout unchanged by the gate (HEAD same, porcelain empty)",
                 "FAIL" if moved else "PASS", "the checkout changed during the gate" if moved else "")
        state["porcelain"] = after["porcelain"]
        return _end(gate, "repo", repo, state, versions, started, report_dir, base)
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
    missing = [i for i in REQUIRED_STEPS if i not in steps]
    if missing:
        return False, "NOT PUSHABLE: report lacks required step(s): %s" % ", ".join(missing)
    failed = [i for i, s in steps.items() if s.get("status") != "PASS"]
    if failed or r.get("overall") != "PASS":
        return False, "NOT PUSHABLE: failing step(s): %s" % (", ".join(failed) or "overall != PASS")
    return True, "PUSHABLE: gate passed for HEAD %s (tree %s), worktree clean. Report: %s" % (state["head"], state["tree"], path)


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
    return out


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
        binary = os.path.join(work, "bin", "pulumi-resource-" + provider)
        gate.cmd("provider_binary", "build provider binary", ["go", "build", "-o", binary, "./cmd/pulumi-resource-" + provider], mod)
        schema_file = os.path.join(work, "gen", "provider", "cmd", "pulumi-resource-" + provider, "schema.json")
        os.makedirs(os.path.dirname(schema_file))
        st, text = gate.cmd("get_schema", "pulumi package get-schema", ["pulumi", "package", "get-schema", binary], work)
        schema = None
        if st == "PASS":
            try:
                schema = json.loads(text[text.index("{"):])
                schema.pop("version", None)
                with open(schema_file, "w") as f:
                    json.dump(schema, f, indent=2)
                    f.write("\n")
            except ValueError as e:
                gate.steps[-1]["status"], gate.steps[-1]["detail"] = "FAIL", "schema output is not JSON: %s" % e
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
                gate.cmd("build_%s_sdk" % lang, "build %s SDK (make build_%s_sdk)" % (lang, lang),
                         ["make", "build_%s_sdk" % lang], gen, fail_on_skip=True, collect_warnings=True)
            meta = metadata_warnings(schema, n)
            gate.add("metadata_pulumi_labs", "builder already carries pulumi-labs Go path, repository, publisher, plugin URL (WARN only)",
                     "PASS", "\n".join(meta), warnings=meta)
        default_provider_steps(gate, schema, mod, pdir)
        return _end(gate, "monorepo", mono, state, versions, started, report_dir, base, {"provider": provider})
    finally:
        if not keep:
            shutil.rmtree(work, ignore_errors=True)


# ------------------------------------------------------------------------ cli

def main(argv=None):
    """CLI entry point; returns the process exit code."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=".", help="the carved repo (default: current directory)")
    ap.add_argument("--assert-pushable", action="store_true",
                    help="exit 0 only if a passing report exists for the current HEAD and the worktree is clean")
    ap.add_argument("--monorepo", help="readiness mode: an autoregistry checkout (needs --provider)")
    ap.add_argument("--provider", help="provider slug for --monorepo")
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
            return run_monorepo(args.monorepo, args.provider, prefix, report_dir, args.keep_work)[0]
        return run_repo(args.repo, prefix, report_dir, args.keep_work)[0]
    except (RuntimeError, names.SlugError) as e:
        print("verify: error: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
