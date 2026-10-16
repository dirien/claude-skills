#!/usr/bin/env python3
"""The `examples` profile of verify.py --monorepo: compile every example and template of a provider.

verify.py owns the Gate (step recording, logged commands) and the provider/schema/SDK steps; this module owns
everything that is specific to the examples:

- classify(): which language each lib/providers/<p>/{examples,templates}/* directory is (Pulumi.yaml runtime,
  falling back to the directory name), and so which SDKs must be generated;
- one compile function per language, each building a scratch copy against the LOCALLY generated SDK, so the
  repository is never modified: compile_ts, compile_python / import_python, compile_csharp, compile_java
  (Go lives in verify.go_examples, shared with the readiness profile);
- failure_report(): the stable failure keys `<provider>:<check>:<relative path>` the ratchet in
  .github/scripts/provider-examples.py compares against .github/provider-examples-baseline.json.

Standard library only. Every function that runs a command takes the verify.Gate, so logs and timeouts are the
gate's.
"""
import ast
import json
import os
import re
import shutil
import time
import xml.etree.ElementTree as ET

SKIP_DIRS = {"node_modules", ".git", "vendor", "bin", "obj", "venv", ".venv", "__pycache__", "sdk", "dist", "build",
             ".gradle", "target"}
PLACEHOLDER_RE = re.compile(r"\$\{[A-Za-z_]+\}")
TEXT_EXTS = {".go", ".mod", ".ts", ".tsx", ".json", ".py", ".txt", ".toml", ".cs", ".csproj", ".xml", ".java",
             ".gradle", ".kts", ".yaml", ".yml"}

# runtime name in Pulumi.yaml -> language id used in check names
RUNTIME_LANG = {"go": "go", "nodejs": "ts", "python": "python", "dotnet": "csharp", "java": "java", "jvm": "java",
                "yaml": "yaml"}
# directory-name tokens (the same set tools/example-scaffold recognises), used when Pulumi.yaml has no runtime
NAME_LANG = {"go": "go", "ts": "ts", "typescript": "ts", "python": "python", "py": "python", "csharp": "csharp",
             "dotnet": "csharp", "java": "java", "yaml": "yaml"}
# the `pulumi package gen-sdk --language` name for each example language
GEN_LANG = {"go": "go", "ts": "nodejs", "python": "python", "csharp": "dotnet", "java": "java"}

# Where a failing step with no per-item detail points: `<provider>:<check>:<path>`.
STEP_PATH = {"toolchain": "toolchain", "provider_binary": "provider", "go_build": "provider", "get_schema": "provider",
             "go_sdk_mod": "sdk/go", "go_sdk_build": "sdk/go", "go_sdk_vet": "sdk/go", "go_consumer_smoke": "sdk/go",
             "sdk_fixups": "sdk", "build_nodejs_sdk": "sdk/nodejs", "install_python_sdk": "sdk/python",
             "build_dotnet_sdk": "sdk/dotnet", "build_java_sdk": "sdk/java"}
STEP_PATH.update({"gen_sdk_" + g: "sdk/" + g for g in ("nodejs", "python", "go", "dotnet", "java")})


def tail_of(text, lines=8, chars=800):
    """Return the last `lines` lines of text, capped to `chars` characters."""
    return "\n".join((text or "").rstrip().splitlines()[-lines:])[-chars:]


# ----------------------------------------------------------------- classification

def runtime_of(manifest_text):
    """Return the runtime name of a Pulumi.yaml (scalar or `runtime:\\n  name: x` form), or None."""
    m = re.search(r"^runtime:[ \t]*([A-Za-z0-9_-]+)[ \t]*(?:#.*)?$", manifest_text, re.M)
    if m:
        return m.group(1).lower()
    m = re.search(r"^runtime:[ \t]*(?:#.*)?\n(?:[ \t]+.*\n|\n)*?[ \t]+name:[ \t]*([A-Za-z0-9_-]+)", manifest_text, re.M)
    return m.group(1).lower() if m else None


def lang_of_name(name):
    """Return the language whose token appears in the hyphen-separated directory name, or None."""
    for tok in name.lower().split("-"):
        if tok in NAME_LANG:
            return NAME_LANG[tok]
    return None


def classify(root):
    """Return [{'rel','lang','kind'}] for every directory directly under root/examples and root/templates."""
    out = []
    for top, kind in (("examples", "example"), ("templates", "template")):
        base = os.path.join(root, top)
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            d = os.path.join(base, name)
            if not os.path.isdir(d):
                continue
            lang = None
            try:
                with open(os.path.join(d, "Pulumi.yaml"), encoding="utf-8", errors="replace") as f:
                    lang = RUNTIME_LANG.get(runtime_of(f.read()) or "")
            except OSError:
                pass
            lang = lang or lang_of_name(name)
            out.append({"rel": top + "/" + name, "lang": lang or "unknown", "kind": kind})
    return out


def sdk_languages(entries):
    """Return the gen-sdk language names needed to compile the given entries, in a stable order."""
    need = {GEN_LANG[e["lang"]] for e in entries if e["lang"] in GEN_LANG}
    return [g for g in ("nodejs", "python", "go", "dotnet", "java") if g in need]


def entries_of(entries, lang):
    """Return the entries of one example language."""
    return [e for e in entries if e["lang"] == lang]


# --------------------------------------------------------------------- scratch copies

def scratch_copy(root, rel, tmp, prefix="ex"):
    """Copy root/rel to a fresh scratch directory, replacing ${PROJECT}-style template placeholders; return its path."""
    dst = os.path.join(tmp, "%s-%s" % (prefix, re.sub(r"[^\w.-]", "_", rel)))
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(os.path.join(root, rel), dst, ignore=shutil.ignore_patterns(*SKIP_DIRS))
    for dp, _, fns in os.walk(dst):
        for fn in fns:
            if os.path.splitext(fn)[1].lower() not in TEXT_EXTS and fn != "go.mod":
                continue
            p = os.path.join(dp, fn)
            try:
                with open(p, encoding="utf-8") as f:
                    txt = f.read()
            except (OSError, UnicodeDecodeError):
                continue
            new = PLACEHOLDER_RE.sub("example", txt)
            if new != txt:
                with open(p, "w", encoding="utf-8") as f:
                    f.write(new)
    return dst


def record(gate, sid, title, items, seconds, lp, none_detail, ok_detail):
    """Record one check step from per-example items [{name,status,detail}]; every check is enforced."""
    for i in items:
        i["path"] = i["name"]
    bad = [i for i in items if i["status"] == "FAIL"]
    gate.add(sid, title, "FAIL" if bad else "PASS",
             "\n".join("%s: %s" % (i["name"], (i["detail"].splitlines() or [""])[0]) for i in bad) if bad else
             (ok_detail % len(items) if items else none_detail),
             lp, seconds, items=items)


def skip(gate, sid, title, why):
    """Record a check that could not run because an earlier step failed."""
    gate.add(sid, title, "SKIP", why)


def step_ok(gate, *ids):
    """True if every named step was recorded and passed."""
    got = {s["id"]: s["status"] for s in gate.steps}
    return all(got.get(i) == "PASS" for i in ids)


ERR_RE = re.compile(r"\berror\b|\bTS\d{4}\b|\bCS\d{4}\b|Could not|cannot find|cannot use|unknown field|undefined:|npm ERR! (?:code|404|notarget)|"
                   r"ModuleNotFound|No versions available|does not (?:contain|exist)|no required module|missing go\.sum|"
                   r"not found|invalid version", re.I)
NOISE_RE = re.compile(r"JAVA_TOOL_OPTIONS|warning\b|^\d+ errors?$|^FAIL$|^\s*at |Run with --|Get more help|^\s*$|^\* (Try|What went wrong)", re.I)
SCRATCH_PATH_RE = re.compile(r"(?:/[\w.@+~-]+)+?/(?:ex|consumer)-[\w.-]+/")


def summarize(text, hits=2, limit=420):
    """Return the most telling lines of a failed command's output on one line, with scratch paths removed.

    Prefers lines that look like compiler or resolver errors; falls back to the last lines. Stable across runs (no scratch
    directory names), because it ends up in baseline reasons.
    """
    lines = [SCRATCH_PATH_RE.sub("", l.strip()) for l in (text or "").splitlines() if l.strip()]
    picked = []
    for l in lines:
        if ERR_RE.search(l) and not NOISE_RE.search(l) and l not in picked:
            picked.append(l)
        if len(picked) >= hits:
            break
    if not picked:
        picked = [l for l in lines if not NOISE_RE.search(l)][-2:]
    return " | ".join(picked)[:limit]


def command_label(argv):
    """Short, path-free name of a command for failure details: `tsc --noEmit`, `npm install`, `gradle compileJava`."""
    if len(argv) > 1 and os.path.basename(argv[1]) == "tsc":
        return "tsc " + " ".join(a for a in argv[2:4] if a.startswith("--"))
    sub = next((a for a in argv[1:] if not a.startswith(("-", "/"))), "")
    return ("%s %s" % (os.path.basename(argv[0]), sub)).strip()


def run_chain(gate, cmds, cwd, lp, env=None, timeout=900):
    """Run commands in order; return (ok, detail) with the first failure's tail."""
    for argv in cmds:
        rc, text = gate.run(argv, cwd, lp, env, timeout)
        if rc != 0:
            return False, "`%s` failed (exit %d): %s" % (command_label(argv), rc, summarize(text))
    return True, "compiles against the local SDK"


# --------------------------------------------------------------------------- TypeScript

def ts_dep_rewrite(pkg_json_text, sdk_name, sdk_path):
    """Return package.json text with every dependency named sdk_name pointing at file:<sdk_path> (None if absent)."""
    data = json.loads(pkg_json_text)
    hit = False
    for sect in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        deps = data.get(sect)
        if isinstance(deps, dict) and sdk_name in deps:
            deps[sdk_name] = "file:" + sdk_path
            hit = True
    return json.dumps(data, indent=2) + "\n" if hit else None


DEFAULT_TSCONFIG = {"compilerOptions": {"strict": True, "target": "ES2020", "module": "commonjs", "moduleResolution": "node",
                                         "experimentalDecorators": True, "forceConsistentCasingInFileNames": True}}


def ts_tooling(gate, tmp, lp):
    """Install a pinned TypeScript once; return the path of its tsc.js or None."""
    d = os.path.join(tmp, "tsc")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "package.json"), "w") as f:
        f.write('{"name":"tsc","version":"1.0.0","private":true}\n')
    rc, _ = gate.run(["npm", "install", "--no-audit", "--no-fund", "--ignore-scripts", "typescript@5.6.3"], d, lp, None, 900)
    js = os.path.join(d, "node_modules", "typescript", "bin", "tsc")
    return js if rc == 0 and os.path.exists(js) else None


def compile_ts(gate, root, entries, sdk_root, tmp):
    """`npm install` against the local Node SDK (file:, copied so its deps dedupe) then `tsc --noEmit`, per TypeScript example."""
    sid, title = "compile_ts", "TypeScript examples and templates type-check (tsc --noEmit) against the local Node SDK"
    t0 = time.time()
    lp = gate.log_path(sid)
    items = []
    if not entries:
        return record(gate, sid, title, items, 0, lp, "no TypeScript examples or templates present", "%d")
    try:
        with open(os.path.join(sdk_root, "package.json"), encoding="utf-8") as f:
            sdk_name = json.load(f)["name"]
    except (OSError, ValueError, KeyError) as e:
        return skip(gate, sid, title, "cannot read the Node SDK package name: %s" % e)
    tsc = ts_tooling(gate, tmp, lp)
    if not tsc:
        return skip(gate, sid, title, "could not install TypeScript (see log)")
    for e in entries:
        dst = scratch_copy(root, e["rel"], tmp)
        pj = os.path.join(dst, "package.json")
        if not os.path.isfile(pj):
            items.append({"name": e["rel"], "status": "FAIL", "detail": "no package.json"})
            continue
        try:
            with open(pj, encoding="utf-8") as f:
                new = ts_dep_rewrite(f.read(), sdk_name, os.path.abspath(sdk_root))
        except ValueError as err:
            items.append({"name": e["rel"], "status": "FAIL", "detail": "package.json is not valid JSON: %s" % err})
            continue
        if new is None:
            items.append({"name": e["rel"], "status": "FAIL",
                          "detail": "package.json does not depend on %s, the Node SDK of this provider" % sdk_name})
            continue
        with open(pj, "w", encoding="utf-8") as f:
            f.write(new)
        if not os.path.isfile(os.path.join(dst, "tsconfig.json")):
            with open(os.path.join(dst, "tsconfig.json"), "w") as f:
                json.dump(DEFAULT_TSCONFIG, f)
        ok, detail = run_chain(gate, [["npm", "install", "--install-links", "--no-audit", "--no-fund", "--ignore-scripts",
                                       "--no-progress"],
                                      ["node", tsc, "--noEmit", "--skipLibCheck", "--pretty", "false", "-p", dst]], dst, lp)
        items.append({"name": e["rel"], "status": "PASS" if ok else "FAIL", "detail": detail})
    record(gate, sid, title, items, time.time() - t0, lp, "", "%d TypeScript example/template dir(s) type-check")


# ----------------------------------------------------------------------------- Python

def py_files(d):
    """Return sorted .py paths under d, skipping virtualenvs, caches and node_modules."""
    out = []
    for dp, dns, fns in os.walk(d):
        dns[:] = [x for x in dns if x not in SKIP_DIRS]
        out += [os.path.join(dp, f) for f in fns if f.endswith(".py")]
    return sorted(out)


def py_syntax_errors(files, base):
    """Compile each file in memory (nothing is written); return ['rel:line: message'] for syntax errors."""
    errs = []
    for p in files:
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                src = PLACEHOLDER_RE.sub("example", f.read())
            compile(src, p, "exec")
        except SyntaxError as e:
            errs.append("%s:%s: %s" % (os.path.relpath(p, base), e.lineno, e.msg))
        except ValueError as e:  # e.g. null bytes
            errs.append("%s: %s" % (os.path.relpath(p, base), e))
    return errs


def compile_python(gate, root, entries):
    """py_compile (in memory) every .py file of every Python example and template."""
    sid, title = "compile_python", "Python examples and templates byte-compile"
    t0 = time.time()
    items = []
    for e in entries:
        d = os.path.join(root, e["rel"])
        files = py_files(d)
        errs = py_syntax_errors(files, root)
        if not files:
            errs = ["no .py files"]
        items.append({"name": e["rel"], "status": "FAIL" if errs else "PASS", "detail": "; ".join(errs[:3])})
    record(gate, sid, title, items, time.time() - t0, gate.log_path(sid), "no Python examples or templates present",
           "%d Python example/template dir(s) compile")


def sdk_references(source, module):
    """Return the names a program takes from the SDK package `module`, as sorted unique dotted references.

    `import pkg as p` / `import pkg.sub as p` followed by `p.A.b` yields `pkg.A` (the chain is cut where it leaves modules, which
    the resolver decides); `from pkg import A, B` yields `pkg.A`, `pkg.B`; `from pkg.sub import C` yields `pkg.sub.C`; and every
    imported module path itself is listed.
    """
    try:
        tree = ast.parse(PLACEHOLDER_RE.sub("example", source))
    except SyntaxError:
        return []
    refs, aliases = set(), {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == module or a.name.startswith(module + "."):
                    refs.add(a.name)
                    aliases[a.asname or a.name.split(".")[0]] = a.name if a.asname else module
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module == module or node.module.startswith(module + "."):
                refs.add(node.module)
                for a in node.names:
                    if a.name != "*":
                        refs.add(node.module + "." + a.name)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            chain, cur = [], node
            while isinstance(cur, ast.Attribute):
                chain.append(cur.attr)
                cur = cur.value
            if isinstance(cur, ast.Name) and cur.id in aliases:
                chain.reverse()
                refs.add(".".join([aliases[cur.id]] + chain[:2]))
    return sorted(refs)


# Run by the venv's python: resolve each reference by importing the longest module prefix, then getattr for the rest.
RESOLVER = r'''
import importlib, json, sys
bad = []
for ref in json.load(open(sys.argv[1])):
    parts = ref.split(".")
    mod, i = None, len(parts)
    while i > 0:
        try:
            mod = importlib.import_module(".".join(parts[:i]))
            break
        except ImportError:
            i -= 1
    if mod is None:
        bad.append([ref, "no importable module"])
        continue
    cur = mod
    try:
        for attr in parts[i:]:
            cur = getattr(cur, attr)
    except AttributeError:
        bad.append([ref, "%s has no attribute %r" % (".".join(parts[:i]), attr)])
print(json.dumps(bad))
'''


def python_sdk_module(sdk_py):
    """Return the importable package name in a generated Python SDK dir (the directory holding __init__.py)."""
    for name in sorted(os.listdir(sdk_py)):
        if os.path.isfile(os.path.join(sdk_py, name, "__init__.py")) and not name.startswith((".", "_")):
            return name
    return None


def import_python(gate, root, entries, sdk_py, tmp):
    """Install the local Python SDK in a venv and check every name the programs take from it resolves."""
    sid, title = "import_python", "Python programs import only names that exist in the local Python SDK"
    t0 = time.time()
    lp = gate.log_path(sid)
    if not entries:
        return record(gate, sid, title, [], 0, lp, "no Python examples or templates present", "%d")
    module = python_sdk_module(sdk_py) if os.path.isdir(sdk_py) else None
    if not module:
        return skip(gate, sid, title, "no importable package found in %s" % sdk_py)
    venv = os.path.join(tmp, "pyvenv")
    env = dict(gate.env, PIP_DISABLE_PIP_VERSION_CHECK="1")
    for argv in (["python3", "-m", "venv", venv], [os.path.join(venv, "bin", "python"), "-m", "pip", "install", "--quiet", sdk_py]):
        rc, text = gate.run(argv, tmp, lp, env, 1800)
        if rc != 0:
            gate.add("install_python_sdk", "install the local Python SDK into a venv", "FAIL",
                     "`%s` failed (exit %d): %s" % (" ".join(argv[-3:]), rc, summarize(text)), lp, time.time() - t0)
            return skip(gate, sid, title, "the Python SDK did not install")
    items = []
    for e in entries:
        bad = []
        for p in py_files(os.path.join(root, e["rel"])):
            with open(p, encoding="utf-8", errors="replace") as f:
                refs = sdk_references(f.read(), module)
            if not refs:
                continue
            jp = os.path.join(tmp, "refs.json")
            with open(jp, "w") as f:
                json.dump(refs, f)
            rc, text = gate.run([os.path.join(venv, "bin", "python"), "-c", RESOLVER, jp], tmp, lp, env, 600)
            try:
                found = json.loads(text.strip().splitlines()[-1]) if rc == 0 else [["(resolver)", tail_of(text, 3, 300)]]
            except (ValueError, IndexError):
                found = [["(resolver)", tail_of(text, 3, 300)]]
            bad += ["%s: %s: %s" % (os.path.relpath(p, root), r, why) for r, why in found]
        items.append({"name": e["rel"], "status": "FAIL" if bad else "PASS", "detail": "; ".join(bad[:3])})
    record(gate, sid, title, items, time.time() - t0, lp, "", "%d Python example/template dir(s) resolve their SDK imports")


# ------------------------------------------------------------------------------- C#

def csproj_rewrite(text, sdk_id, sdk_csproj):
    """Replace the PackageReference to the SDK package with a ProjectReference to the local SDK csproj.

    Returns the new text, or None when the project does not reference the package.
    """
    pat = re.compile(r'[ \t]*<PackageReference\s+Include="%s"[^>]*?(?:/>|>.*?</PackageReference>)' % re.escape(sdk_id), re.S | re.I)
    if not pat.search(text):
        return None
    return pat.sub('    <ProjectReference Include="%s" />' % sdk_csproj, text, count=1)


def compile_csharp(gate, root, entries, sdk_dir, tmp):
    """`dotnet build` each C# example against a ProjectReference to the local .NET SDK, in a scratch copy."""
    sid, title = "compile_csharp", "C# examples and templates build against the local .NET SDK"
    t0 = time.time()
    lp = gate.log_path(sid)
    items = []
    if not entries:
        return record(gate, sid, title, items, 0, lp, "no C# examples or templates present", "%d")
    csprojs = sorted(f for f in os.listdir(sdk_dir) if f.endswith(".csproj")) if os.path.isdir(sdk_dir) else []
    if len(csprojs) != 1:
        return skip(gate, sid, title, "expected one .csproj in %s, found %d" % (sdk_dir, len(csprojs)))
    sdk_csproj = os.path.abspath(os.path.join(sdk_dir, csprojs[0]))
    sdk_id = csprojs[0][:-len(".csproj")]
    for e in entries:
        dst = scratch_copy(root, e["rel"], tmp)
        projs = []
        for dp, dns, fns in os.walk(dst):
            dns[:] = [d for d in dns if d not in SKIP_DIRS]
            projs += [os.path.join(dp, f) for f in fns if f.endswith(".csproj")]
        if not projs:
            items.append({"name": e["rel"], "status": "FAIL", "detail": "no .csproj"})
            continue
        patched = False
        for p in projs:
            with open(p, encoding="utf-8", errors="replace") as f:
                new = csproj_rewrite(f.read(), sdk_id, sdk_csproj)
            if new is not None:
                patched = True
                with open(p, "w", encoding="utf-8") as f:
                    f.write(new)
        if not patched:
            items.append({"name": e["rel"], "status": "FAIL",
                          "detail": "the project does not reference the NuGet package %s, the .NET SDK of this provider" % sdk_id})
            continue
        ok, detail = run_chain(gate, [["dotnet", "build", "-nologo", "-v", "q", "-clp:ErrorsOnly", projs[0]]], dst, lp)
        items.append({"name": e["rel"], "status": "PASS" if ok else "FAIL", "detail": detail})
    record(gate, sid, title, items, time.time() - t0, lp, "", "%d C# example/template dir(s) build")


# ------------------------------------------------------------------------------ Java

def _local(tag):
    return tag.split("}", 1)[-1]


def pom_info(text):
    """Parse a pom.xml into {'props': {name: value}, 'deps': [(group, artifact, version, scope)]}; raises ValueError."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise ValueError("pom.xml is not valid XML: %s" % e)
    props, deps = {}, []
    for child in root:
        if _local(child.tag) == "properties":
            props = {_local(p.tag): (p.text or "").strip() for p in child}
        elif _local(child.tag) == "dependencies":
            for d in child:
                f = {_local(x.tag): (x.text or "").strip() for x in d}
                deps.append((f.get("groupId", ""), f.get("artifactId", ""), f.get("version", ""), f.get("scope", "compile")))

    def sub(s):
        return re.sub(r"\$\{([^}]+)\}", lambda m: props.get(m.group(1), m.group(0)), s)

    return {"props": props, "deps": [tuple(sub(x) for x in d) for d in deps]}


def gradle_for_pom(info, sdk_names, sdk_jar):
    """Return build.gradle text equivalent to the pom: its dependencies, minus the SDK package, plus the local SDK jar.

    sdk_names are artifactIds that mean "this provider's Java SDK" when under a com.pulumi* group.
    """
    rel = info["props"].get("maven.compiler.release") or info["props"].get("maven.compiler.source") or "11"
    lines = ["plugins { id 'java' }", "repositories { mavenCentral() }",
             "java { sourceCompatibility = JavaVersion.toVersion('%s'); targetCompatibility = JavaVersion.toVersion('%s') }" % (rel, rel),
             "dependencies {"]
    for g, a, v, scope in info["deps"]:
        if g.startswith("com.pulumi") and a in sdk_names:
            continue
        conf = {"test": "testImplementation", "provided": "compileOnly", "runtime": "runtimeOnly"}.get(scope, "implementation")
        lines.append("    %s '%s:%s%s'" % (conf, g, a, (":" + v) if v else ""))
    lines.append("    implementation files('%s')" % sdk_jar.replace("\\", "\\\\").replace("'", "\\'"))
    lines.append("}")
    return "\n".join(lines) + "\n"


def sdk_jar_of(sdk_java):
    """Return the main jar `gradle build` wrote under sdk/java/build/libs (not -sources/-javadoc), or None."""
    d = os.path.join(sdk_java, "build", "libs")
    jars = sorted(f for f in os.listdir(d) if f.endswith(".jar") and not re.search(r"-(sources|javadoc)\.jar$", f)) if os.path.isdir(d) else []
    return os.path.abspath(os.path.join(d, jars[0])) if jars else None


def compile_java(gate, root, entries, sdk_java, sdk_names, tmp):
    """Compile each Java example with Gradle in a scratch copy: its pom dependencies, with the local SDK jar replacing the SDK package."""
    sid, title = "compile_java", "Java examples and templates compile against the local Java SDK"
    t0 = time.time()
    lp = gate.log_path(sid)
    items = []
    if not entries:
        return record(gate, sid, title, items, 0, lp, "no Java examples or templates present", "%d")
    jar = sdk_jar_of(sdk_java)
    if not jar:
        return skip(gate, sid, title, "no SDK jar under %s/build/libs" % sdk_java)
    for e in entries:
        dst = scratch_copy(root, e["rel"], tmp)
        pom = os.path.join(dst, "pom.xml")
        if not os.path.isfile(pom):
            items.append({"name": e["rel"], "status": "FAIL", "detail": "no pom.xml (the gate converts Maven projects to Gradle)"})
            continue
        try:
            with open(pom, encoding="utf-8") as f:
                info = pom_info(f.read())
        except (ValueError, OSError) as err:
            items.append({"name": e["rel"], "status": "FAIL", "detail": str(err)})
            continue
        with open(os.path.join(dst, "build.gradle"), "w", encoding="utf-8") as f:
            f.write(gradle_for_pom(info, sdk_names, jar))
        ok, detail = run_chain(gate, [["gradle", "--console=plain", "-q", "compileJava"]], dst, lp, None, 1200)
        items.append({"name": e["rel"], "status": "PASS" if ok else "FAIL", "detail": detail})
    record(gate, sid, title, items, time.time() - t0, lp, "", "%d Java example/template dir(s) compile")


# ------------------------------------------------------------------------------ YAML

YAML_TYPE_RE = re.compile(r"^\s*type:\s*[\"']?([A-Za-z0-9_-]+:[^\s\"':]*:[A-Za-z0-9_]+)[\"']?\s*(?:#.*)?$", re.M)
YAML_FN_RE = re.compile(r"^\s*function:\s*[\"']?([A-Za-z0-9_-]+:[^\s\"':]*:[A-Za-z0-9_]+)[\"']?\s*(?:#.*)?$", re.M)


def _tok(token):
    """Normalise a type token for comparison: (package, module before any '/', lower-case member), case-insensitive."""
    pkg, mod, name = token.split(":")
    return pkg.lower(), mod.split("/")[0].lower(), name.lower()


def yaml_tokens(text, package):
    """Return the resource type and function tokens of this provider referenced in a YAML program."""
    toks = YAML_TYPE_RE.findall(text) + YAML_FN_RE.findall(text)
    return sorted({t for t in toks if t.split(":")[0].lower() == package.lower()})


def compile_yaml(gate, root, entries, schema):
    """Schema-level check of YAML programs: every resource `type:` and invoke `function:` of this provider must exist in the schema.

    Token forms are compared as (package, module, member) ignoring case and the `/x` in `pkg:mod/x:Name`. Property names are not
    checked (that needs a real YAML parser, which this standard-library-only gate does not have).
    """
    sid, title = "compile_yaml", "YAML programs reference only resources and functions that exist in the schema"
    t0 = time.time()
    lp = gate.log_path(sid)
    if not entries:
        return record(gate, sid, title, [], 0, lp, "no YAML examples or templates present", "%d")
    if schema is None:
        return skip(gate, sid, title, "no schema")
    package = schema.get("name") or ""
    known = {_tok(t) for t in list((schema.get("resources") or {}).keys()) + list((schema.get("functions") or {}).keys())
             if t.count(":") == 2}
    items = []
    for e in entries:
        try:
            with open(os.path.join(root, e["rel"], "Pulumi.yaml"), encoding="utf-8", errors="replace") as f:
                text = PLACEHOLDER_RE.sub("example", f.read())
        except OSError:
            items.append({"name": e["rel"], "status": "FAIL", "detail": "no Pulumi.yaml"})
            continue
        bad = [t for t in yaml_tokens(text, package) if _tok(t) not in known]
        items.append({"name": e["rel"], "status": "FAIL" if bad else "PASS",
                      "detail": "not in the schema: " + ", ".join(bad[:5]) if bad else ""})
    record(gate, sid, title, items, time.time() - t0, lp, "", "%d YAML program(s) reference only schema types")


# ------------------------------------------------------------------- failure keys

def key_for(provider, check, path):
    """Return the stable failure key `<provider>:<check>:<path>`."""
    return "%s:%s:%s" % (provider, check, path)


def failure_report(provider, steps):
    """Return the machine-readable failure summary of an examples-profile run.

    {'provider', 'failures': [{'key','check','path','detail','report_only'}], 'keys': [...enforced, sorted],
     'report_only_keys': [...], 'skipped_checks': [...], 'counts': {check: n}}.
    A failing step with failing items yields one key per item; a failing step without items (a toolchain, build or
    generation step) yields one key at STEP_PATH. SKIP steps list their check in skipped_checks: the ratchet then
    neither counts their baseline entries as stale nor as fixed.
    """
    fails, skipped = [], []
    for s in steps:
        if s["status"] == "SKIP":
            skipped.append(s["id"])
            continue
        bad = [i for i in s.get("items") or [] if i.get("status") == "FAIL"]
        if s["status"] == "FAIL" and not bad:
            fails.append({"check": s["id"], "path": STEP_PATH.get(s["id"], "."), "detail": ((s.get("detail") or "").splitlines() or [""])[0],
                          "report_only": False})
        for i in bad:
            fails.append({"check": s["id"], "path": i.get("path") or i["name"], "detail": i.get("detail") or "",
                          "report_only": bool(i.get("report_only"))})
    seen, out = set(), []
    for f in fails:
        f["key"] = key_for(provider, f["check"], f["path"])
        if f["key"] in seen:
            continue
        seen.add(f["key"])
        out.append(f)
    out.sort(key=lambda f: f["key"])
    counts = {}
    for f in out:
        if not f["report_only"]:
            counts[f["check"]] = counts.get(f["check"], 0) + 1
    return {"provider": provider, "failures": out,
            "keys": [f["key"] for f in out if not f["report_only"]],
            "report_only_keys": [f["key"] for f in out if f["report_only"]],
            "skipped_checks": sorted(set(skipped)), "counts": counts}
