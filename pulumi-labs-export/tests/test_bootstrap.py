import http.client
import io
import json
import os
import socket
import stat
import tarfile
import unittest
import urllib.error
from unittest import mock

import helpers
import bootstrap


def make_ar(members):
    """Build an ar archive from (name, bytes) pairs."""
    out = b"!<arch>\n"
    for name, body in members:
        out += (name + "/").ljust(16).encode() + b"0".ljust(12) + b"0".ljust(6) + b"0".ljust(6) \
            + b"100644".ljust(8) + str(len(body)).ljust(10).encode() + b"`\n" + body
        if len(body) % 2:
            out += b"\n"
    return out


def make_tar(files, mode="w:xz"):
    """Build a tar in memory from {name: (bytes, mode)}."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode=mode) as tf:
        for name, (data, m) in files.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            ti.mode = m
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def make_deb(path, data_name="data.tar.xz", data=None):
    """Write a synthetic .deb whose data tarball holds a stub usr/bin/make."""
    if data is None:
        data = make_tar({"./usr/bin/make": (b"#!/bin/sh\necho GNU Make 4.3\n", 0o755),
                         "./usr/share/doc/make/README": (b"x", 0o644)})
    ctrl = make_tar({"./control": (b"Package: make\n", 0o644)})
    with open(path, "wb") as f:
        f.write(make_ar([("debian-binary", b"2.0\n"), ("control.tar.xz", ctrl), (data_name, data)]))


class PinTest(unittest.TestCase):
    def test_urls_amd64(self):
        s = bootstrap.tool_specs("amd64", "3.241.0")
        self.assertEqual(s["go"].url, "https://go.dev/dl/go1.27.3.linux-amd64.tar.gz")
        self.assertEqual(s["go"].sha256, bootstrap.GO_SHA256["amd64"])
        self.assertEqual(s["pulumi"].url, "https://github.com/pulumi/pulumi/releases/download/v3.241.0/pulumi-v3.241.0-linux-x64.tar.gz")
        self.assertEqual(s["pulumi"].checksums_url, "https://github.com/pulumi/pulumi/releases/download/v3.241.0/pulumi-3.241.0-checksums.txt")
        self.assertTrue(s["jq"].url.endswith("/jq-1.8.2/jq-linux-amd64"))
        self.assertTrue(s["gitleaks"].url.endswith("/v8.30.1/gitleaks_8.30.1_linux_x64.tar.gz"))
        self.assertTrue(s["gitleaks"].checksums_url.endswith("/gitleaks_8.30.1_checksums.txt"))

    def test_urls_arm64(self):
        s = bootstrap.tool_specs("arm64", "3.241.0")
        self.assertEqual(s["go"].url, "https://go.dev/dl/go1.27.3.linux-arm64.tar.gz")
        self.assertEqual(s["go"].sha256, bootstrap.GO_SHA256["arm64"])
        self.assertTrue(s["pulumi"].url.endswith("pulumi-v3.241.0-linux-arm64.tar.gz"))
        self.assertTrue(s["jq"].url.endswith("jq-linux-arm64"))
        self.assertTrue(s["gitleaks"].url.endswith("gitleaks_8.30.1_linux_arm64.tar.gz"))

    def test_make_deb_plan(self):
        for arch in ("amd64", "arm64"):
            url, sha = bootstrap.make_deb_plan(arch)
            self.assertEqual(url, "https://deb.debian.org/debian/pool/main/m/make-dfsg/make_4.3-4.1_%s.deb" % arch)
            self.assertEqual(len(sha), 64)

    def test_arch(self):
        self.assertEqual(bootstrap.detect_arch("x86_64"), "amd64")
        self.assertEqual(bootstrap.detect_arch("aarch64"), "arm64")
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.detect_arch("riscv64")

    def test_go_pin_matches_repo_go_version(self):
        path = os.path.join(helpers.SKILL_DIR, "..", "..", ".go-version")
        if not os.path.exists(path):
            self.skipTest("not inside pulumi/autoregistry")
        with open(path) as f:
            self.assertEqual(f.read().strip(), bootstrap.GO_VERSION)


class ExtraToolPinTest(helpers.TempDirMixin, unittest.TestCase):
    def test_node_urls(self):
        s = bootstrap.tool_specs("amd64", "3.241.0")["node"]
        self.assertEqual(s.url, "https://nodejs.org/dist/v22.23.3/node-v22.23.3-linux-x64.tar.gz")
        self.assertEqual(s.checksums_url, "https://nodejs.org/dist/v22.23.3/SHASUMS256.txt")
        self.assertEqual(s.version_token, "v22.23.3")
        a = bootstrap.tool_specs("arm64", "3.241.0")["node"]
        self.assertTrue(a.url.endswith("node-v22.23.3-linux-arm64.tar.gz"))

    def test_dotnet_urls_and_sha512(self):
        s = bootstrap.tool_specs("amd64", "3.241.0")["dotnet"]
        self.assertEqual(s.url, "https://builds.dotnet.microsoft.com/dotnet/Sdk/8.0.426/dotnet-sdk-8.0.426-linux-x64.tar.gz")
        self.assertEqual(s.checksums_url, "https://builds.dotnet.microsoft.com/dotnet/release-metadata/8.0/releases.json")
        self.assertEqual((s.hash_algo, s.checksums_style), ("sha512", "dotnet"))
        self.assertTrue(bootstrap.tool_specs("arm64", "3.241.0")["dotnet"].url.endswith("linux-arm64.tar.gz"))

    def test_temurin_urls(self):
        s = bootstrap.tool_specs("amd64", "3.241.0")["java"]
        self.assertEqual(s.url, "https://github.com/adoptium/temurin17-binaries/releases/download/jdk-17.0.20.1%2B1/"
                                "OpenJDK17U-jdk_x64_linux_hotspot_17.0.20.1_1.tar.gz")
        self.assertEqual(s.checksums_url, s.url + ".sha256.txt")
        a = bootstrap.tool_specs("arm64", "3.241.0")["java"]
        self.assertIn("jdk_aarch64_linux_hotspot_17.0.20.1_1", a.url)
        self.assertEqual(s.version_token, "17.0.20.1")

    def test_gradle_urls(self):
        s = bootstrap.tool_specs("amd64", "3.241.0")["gradle"]
        self.assertEqual(s.url, "https://services.gradle.org/distributions/gradle-9.8.1-bin.zip")
        self.assertEqual(s.checksums_url, s.url + ".sha256")
        self.assertEqual((s.kind, s.checksums_style, s.strip), ("zip", "bare", 1))

    def test_profiles(self):
        self.assertEqual(bootstrap.PROFILES["minimal"], ["go", "pulumi", "jq", "gitleaks", "make"])
        self.assertEqual(bootstrap.PROFILES["full"][-4:], ["node", "dotnet", "java", "gradle"])

    def test_bare_hash(self):
        self.assertEqual(bootstrap.parse_bare_hash("A" * 64 + "\n", "x"), "a" * 64)
        self.assertEqual(bootstrap.parse_bare_hash("%s  f.zip\n" % ("b" * 64), "x"), "b" * 64)
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.parse_bare_hash("abc", "x")
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.parse_bare_hash("a" * 64, "x", "sha512")

    def test_dotnet_releases_lookup(self):
        h = "c" * 128
        doc = {"releases": [{"sdk": {"files": [{"name": "x.tar.gz", "url": "https://h/Sdk/8.0.426/dotnet-sdk-8.0.426-linux-x64.tar.gz", "hash": h}]},
                             "sdks": [{"files": [{"url": "https://h/other.tar.gz", "hash": "d" * 128}]}]}]}
        self.assertEqual(bootstrap.dotnet_hash_from_releases(json.dumps(doc), "dotnet-sdk-8.0.426-linux-x64.tar.gz"), h)
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.dotnet_hash_from_releases(json.dumps(doc), "missing.tar.gz")
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.dotnet_hash_from_releases("not json", "x")
        doc["releases"].append({"sdk": {"files": [{"url": "https://h/dotnet-sdk-8.0.426-linux-x64.tar.gz", "hash": "e" * 128}]}})
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.dotnet_hash_from_releases(json.dumps(doc), "dotnet-sdk-8.0.426-linux-x64.tar.gz")

    def test_expected_hash_dispatch(self):
        specs = bootstrap.tool_specs("amd64", "3.241.0")
        self.assertEqual(bootstrap.expected_hash(specs["node"], "%s  node-v22.23.3-linux-x64.tar.gz\n" % ("1" * 64)), "1" * 64)
        self.assertEqual(bootstrap.expected_hash(specs["gradle"], "2" * 64), "2" * 64)

    def test_sha512_verify(self):
        p = os.path.join(self.tmp, "f")
        with open(p, "wb") as f:
            f.write(b"hi")
        bootstrap.verify_hash(p, bootstrap.file_hash(p, "sha512"), "sha512")
        with self.assertRaises(bootstrap.BootstrapError) as cm:
            bootstrap.verify_hash(p, "0" * 128, "sha512")
        self.assertIn("SHA-512 mismatch", str(cm.exception))

    def test_zip_extract_strip_modes_and_traversal(self):
        import zipfile
        p = os.path.join(self.tmp, "g.zip")
        with zipfile.ZipFile(p, "w") as zf:
            zi = zipfile.ZipInfo("gradle-9/bin/gradle")
            zi.external_attr = 0o755 << 16
            zf.writestr(zi, "#!/bin/sh\n")
            zf.writestr("gradle-9/lib/a.jar", "x")
        dest = os.path.join(self.tmp, "d")
        bootstrap.extract_zip(p, dest, 1)
        self.assertTrue(os.access(os.path.join(dest, "bin", "gradle"), os.X_OK))
        self.assertTrue(os.path.exists(os.path.join(dest, "lib", "a.jar")))
        bad = os.path.join(self.tmp, "bad.zip")
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr("../evil", "x")
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.extract_zip(bad, os.path.join(self.tmp, "e"))

    def test_toolchain_env(self):
        specs = bootstrap.tool_specs("amd64", "3.241.0")
        prefix = os.path.join(self.tmp, "p")
        # Gradle's user home is always set, even before any tool is unpacked.
        self.assertEqual(bootstrap.toolchain_env(prefix, specs, icu=True), {"GRADLE_USER_HOME": os.path.join(prefix, "gradle-home"),
                                                                                "NUGET_PACKAGES": os.path.join(prefix, "nuget-packages")})
        os.makedirs(bootstrap.install_dir(prefix, specs["java"]))
        os.makedirs(bootstrap.install_dir(prefix, specs["dotnet"]))
        env = bootstrap.toolchain_env(prefix, specs, icu=False)
        self.assertEqual(env["JAVA_HOME"], os.path.join(prefix, "opt", "temurin-17.0.20.1+1"))
        self.assertEqual(env["DOTNET_ROOT"], os.path.join(prefix, "opt", "dotnet-8.0.426"))
        self.assertEqual(env["DOTNET_CLI_TELEMETRY_OPTOUT"], "1")
        self.assertEqual(env["DOTNET_SYSTEM_GLOBALIZATION_INVARIANT"], "1")
        self.assertNotIn("DOTNET_SYSTEM_GLOBALIZATION_INVARIANT", bootstrap.toolchain_env(prefix, specs, icu=True))
        self.assertEqual(bootstrap.export_line("JAVA_HOME", "/a b/$x"), 'export JAVA_HOME="/a b/\\$x"')

    def test_gradle_user_home_in_env_and_printed_exports(self):
        prefix = os.path.join(self.tmp, "p")
        want = os.path.join(prefix, "gradle-home")
        specs = bootstrap.tool_specs("amd64", "3.241.0")
        self.assertEqual(bootstrap.toolchain_env(prefix, specs, icu=True)["GRADLE_USER_HOME"], want)
        lines = []
        # --check on an empty prefix prints nothing to eval; drive the real export path with a stubbed install.
        orig = (bootstrap.install_spec, bootstrap.check_spec, bootstrap.install_make, bootstrap.detect_arch, bootstrap.sys.platform)
        try:
            bootstrap.install_spec = lambda spec, prefix, log: None
            bootstrap.check_spec = lambda spec, prefix, specs=None: ("ok", "stub")
            bootstrap.install_make = lambda prefix, arch, log: "stub"
            bootstrap.detect_arch = lambda machine=None: "amd64"
            bootstrap.sys.platform = "linux"
            with mock.patch.dict(os.environ, {"HOME": self.tmp, "GRADLE_USER_HOME": want}):
                os.environ.pop("XDG_DATA_HOME", None)
                rc = bootstrap.main(["--prefix", prefix], out=lines.append, err=lambda s: None)
        finally:
            bootstrap.install_spec, bootstrap.check_spec, bootstrap.install_make, bootstrap.detect_arch, bootstrap.sys.platform = orig
        self.assertEqual(rc, 0)
        self.assertIn('export GRADLE_USER_HOME="%s"' % want, lines)
        self.assertTrue(os.path.isdir(want))
        self.assertIn('export NUGET_PACKAGES="%s"' % os.path.join(prefix, "nuget-packages"), lines)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, ".local", "share", "NuGet", "Migrations", "1")))

    def test_gradle_is_run_with_the_gradle_home(self):
        prefix = os.path.join(self.tmp, "p")
        bindir = os.path.join(prefix, "bin")
        os.makedirs(bindir)
        helpers.write(bindir, "gradle", '#!/bin/sh\n[ -n "$GRADLE_USER_HOME" ] || exit 3\necho "Gradle 9.8.1 $GRADLE_USER_HOME"\n')
        os.chmod(os.path.join(bindir, "gradle"), 0o755)
        specs = bootstrap.tool_specs("amd64", "3.241.0")
        status, detail = bootstrap.check_spec(specs["gradle"], prefix, specs)
        self.assertEqual(status, "ok", detail)
        self.assertIn(os.path.join(prefix, "gradle-home"), detail)

    def test_full_check_reports_new_tools_missing(self):
        lines = []
        ok = bootstrap.run_check(os.path.join(self.tmp, "p"), bootstrap.tool_specs("amd64", "3.241.0"), None, lines.append, "full")
        self.assertFalse(ok)
        for tool in ("node", "dotnet", "java", "gradle"):
            self.assertTrue(any(l.startswith(tool) and "MISSING" in l for l in lines), tool)

    def test_multi_line_version_token_and_proxy_banner(self):
        exe = os.path.join(self.tmp, "j")
        helpers.write(self.tmp, "j", '#!/bin/sh\necho "Picked up JAVA_TOOL_OPTIONS: -x" >&2\necho \'openjdk version "17.0.20.1" 2026\' >&2\n')
        os.chmod(exe, 0o755)
        line, err = bootstrap.run_version([exe, "-version"])
        self.assertIsNone(err)
        self.assertIn("17.0.20.1", line)
        helpers.write(self.tmp, "g", "#!/bin/sh\necho ------\necho 'Gradle 9.8.1'\n")
        os.chmod(os.path.join(self.tmp, "g"), 0o755)
        line, err = bootstrap.run_version([os.path.join(self.tmp, "g")], None, "9.8.1")
        self.assertEqual(line, "Gradle 9.8.1")


class SandboxFixesTest(helpers.TempDirMixin, unittest.TestCase):
    def test_parse_proxy_url(self):
        p = bootstrap.parse_proxy_url
        self.assertEqual(p("http://10.1.2.3:3128"), ("10.1.2.3", 3128, None, None))
        self.assertEqual(p("http://proxy.example.com:8080/"), ("proxy.example.com", 8080, None, None))
        self.assertEqual(p("http://u%40x:p%3Aw@proxy.example.com:8080"), ("proxy.example.com", 8080, "u@x", "p:w"))
        self.assertEqual(p("http://proxy.example.com"), ("proxy.example.com", 80, None, None))
        self.assertEqual(p("https://proxy.example.com"), ("proxy.example.com", 443, None, None))
        self.assertEqual(p("10.0.0.1:99"), ("10.0.0.1", 99, None, None))
        self.assertIsNone(p(""))
        self.assertIsNone(p(None))

    def test_mask_proxy(self):
        self.assertEqual(bootstrap.mask_proxy("http://u:secret@h:1"), "http://***@h:1")
        self.assertEqual(bootstrap.mask_proxy("u:secret@h:1"), "***@h:1")
        self.assertEqual(bootstrap.mask_proxy("http://h:1"), "http://h:1")

    def test_env_case(self):
        up = bootstrap.gradle_proxy_lines({"HTTPS_PROXY": "http://a:1", "HTTP_PROXY": "http://b:2"})
        lo = bootstrap.gradle_proxy_lines({"https_proxy": "http://a:1", "http_proxy": "http://b:2"})
        self.assertEqual(up, lo)
        self.assertIn("systemProp.https.proxyHost=a", up)
        self.assertIn("systemProp.http.proxyPort=2", up)
        only = bootstrap.gradle_proxy_lines({"http_proxy": "http://b:2"})
        self.assertFalse(any(l.startswith("systemProp.https.") for l in only))
        self.assertEqual(bootstrap.gradle_proxy_lines({}), [])

    def test_no_proxy_translation(self):
        self.assertEqual(bootstrap.translate_no_proxy("localhost, 127.0.0.1,,*.local"), "localhost|127.0.0.1|*.local")
        self.assertEqual(bootstrap.translate_no_proxy(""), "")
        lines = bootstrap.gradle_proxy_lines({"HTTPS_PROXY": "http://a:1", "no_proxy": "x,y"})
        self.assertIn("systemProp.http.nonProxyHosts=x|y", lines)
        self.assertIn("systemProp.https.nonProxyHosts=x|y", lines)
        # no proxy, no nonProxyHosts
        self.assertEqual(bootstrap.gradle_proxy_lines({"NO_PROXY": "x"}), [])

    def test_userinfo_only_when_present(self):
        no = "\n".join(bootstrap.gradle_proxy_lines({"HTTPS_PROXY": "http://a:1"}))
        self.assertNotIn("proxyUser", no)
        yes = "\n".join(bootstrap.gradle_proxy_lines({"HTTPS_PROXY": "http://me:pw@a:1"}))
        self.assertIn("systemProp.https.proxyUser=me", yes)
        self.assertIn("systemProp.https.proxyPassword=pw", yes)

    def _props(self, env):
        prefix = os.path.join(self.tmp, "p")
        env = dict(env, GRADLE_USER_HOME=os.path.join(prefix, "gradle-home"))
        logs = []
        path = bootstrap.write_gradle_properties(prefix, env, logs.append)
        return path, env, logs

    def test_write_idempotent_preserves_foreign_lines_and_mode(self):
        path, env, _ = self._props({"HTTPS_PROXY": "http://a:1"})
        with open(path) as f:
            first = f.read()
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(path, "w") as f:
            f.write("org.gradle.daemon=false\n" + first + "# tail\nfoo=bar\n")
        os.chmod(path, 0o644)
        bootstrap.write_gradle_properties(os.path.join(self.tmp, "p"), env, lambda s: None)
        with open(path) as f:
            second = f.read()
        bootstrap.write_gradle_properties(os.path.join(self.tmp, "p"), env, lambda s: None)
        with open(path) as f:
            third = f.read()
        self.assertEqual(second, third)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(second.count(bootstrap.GRADLE_BEGIN), 1)
        self.assertIn("org.gradle.daemon=false", second)
        self.assertIn("foo=bar", second)
        # a changed proxy replaces the block, not appends
        env2 = dict(env, HTTPS_PROXY="http://z:9")
        bootstrap.write_gradle_properties(os.path.join(self.tmp, "p"), env2, lambda s: None)
        with open(path) as f:
            fourth = f.read()
        self.assertIn("proxyHost=z", fourth)
        self.assertNotIn("proxyHost=a", fourth)
        self.assertEqual(fourth.count(bootstrap.GRADLE_BEGIN), 1)
        self.assertIn("foo=bar", fourth)

    def test_block_removed_without_proxy(self):
        path, env, _ = self._props({"HTTPS_PROXY": "http://a:1"})
        with open(path, "a") as f:
            f.write("keep=me\n")
        env.pop("HTTPS_PROXY")
        bootstrap.write_gradle_properties(os.path.join(self.tmp, "p"), env, lambda s: None)
        with open(path) as f:
            text = f.read()
        self.assertEqual(text, "keep=me\n")
        # a file holding only the block disappears
        os.remove(path)
        path, env, _ = self._props({"HTTP_PROXY": "http://a:1"})
        env.pop("HTTP_PROXY")
        bootstrap.write_gradle_properties(os.path.join(self.tmp, "p"), env, lambda s: None)
        self.assertFalse(os.path.exists(path))

    def test_credentials_never_logged(self):
        path, env, logs = self._props({"HTTPS_PROXY": "http://me:topsecret@a:1", "http_proxy": "http://me:topsecret@b:2"})
        self.assertTrue(logs)
        self.assertNotIn("topsecret", "\n".join(logs))
        self.assertNotIn("topsecret", "\n".join(logs).replace("***", ""))

    def test_nuget_marker_default(self):
        home = os.path.join(self.tmp, "home")
        os.makedirs(home)
        env = {"HOME": home}
        self.assertEqual(bootstrap.ensure_nuget_marker(os.path.join(self.tmp, "p"), env), {})
        marker = os.path.join(home, ".local", "share", "NuGet", "Migrations", "1")
        self.assertTrue(os.path.isfile(marker))
        self.assertEqual(os.path.getsize(marker), 0)
        self.assertEqual(bootstrap.ensure_nuget_marker(os.path.join(self.tmp, "p"), env), {})  # idempotent

    def test_nuget_marker_respects_xdg_data_home(self):
        xdg = os.path.join(self.tmp, "xdg")
        self.assertEqual(bootstrap.ensure_nuget_marker(os.path.join(self.tmp, "p"), {"HOME": self.tmp, "XDG_DATA_HOME": xdg}), {})
        self.assertTrue(os.path.isfile(os.path.join(xdg, "NuGet", "Migrations", "1")))

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores directory permissions")
    def test_nuget_marker_falls_back_when_not_writable(self):
        home = os.path.join(self.tmp, "home")
        os.makedirs(home)
        os.chmod(home, 0o500)
        prefix = os.path.join(self.tmp, "p")
        try:
            extra = bootstrap.ensure_nuget_marker(prefix, {"HOME": home})
        finally:
            os.chmod(home, 0o700)
        want = os.path.join(prefix, "xdg-data")
        self.assertEqual(extra, {"XDG_DATA_HOME": want})
        self.assertTrue(os.path.isfile(os.path.join(want, "NuGet", "Migrations", "1")))
        self.assertFalse(os.path.exists(os.path.join(home, ".local")))

    def test_apply_sandbox_fixes_runs_every_time(self):
        prefix = os.path.join(self.tmp, "p")
        env = {"HOME": self.tmp, "HTTPS_PROXY": "http://a:1", "GRADLE_USER_HOME": os.path.join(prefix, "gradle-home")}
        bootstrap.apply_sandbox_fixes(prefix, env, lambda s: None)
        props = os.path.join(prefix, "gradle-home", "gradle.properties")
        self.assertTrue(os.path.exists(props))
        self.assertTrue(os.path.isdir(os.path.join(prefix, "nuget-packages")))
        env.pop("HTTPS_PROXY")
        bootstrap.apply_sandbox_fixes(prefix, env, lambda s: None)
        self.assertFalse(os.path.exists(props))


class PulumiVersionTest(helpers.TempDirMixin, unittest.TestCase):
    def test_reads_vendored_file(self):
        v = bootstrap.read_pulumi_version()
        with open(os.path.join(helpers.SKILL_DIR, "archetype", "mise.toml.tmpl")) as f:
            self.assertIn('pulumi = "%s"' % v, f.read())

    def test_reads_custom_and_errors(self):
        helpers.write(self.tmp, "archetype/mise.toml.tmpl", '[tools]\npulumi = "9.8.7"\n')
        self.assertEqual(bootstrap.read_pulumi_version(self.tmp), "9.8.7")
        helpers.write(self.tmp, "archetype/mise.toml.tmpl", '[tools]\ngo = "1"\n')
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.read_pulumi_version(self.tmp)
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.read_pulumi_version(os.path.join(self.tmp, "nope"))


class ChecksumTest(helpers.TempDirMixin, unittest.TestCase):
    def test_parse(self):
        a, b = "a" * 64, "B" * 64
        got = bootstrap.parse_checksums("%s  one.tgz\n%s *two.tgz\njunk line\n" % (a, b))
        self.assertEqual(got, {"one.tgz": a, "two.tgz": "b" * 64})

    def test_lookup_missing(self):
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.expected_from_checksums("%s  x\n" % ("a" * 64), "y", "sums.txt")

    def test_verify_and_mismatch(self):
        p = os.path.join(self.tmp, "f")
        with open(p, "wb") as f:
            f.write(b"hello")
        good = bootstrap.sha256_file(p)
        bootstrap.verify_sha256(p, good)
        with self.assertRaises(bootstrap.BootstrapError) as cm:
            bootstrap.verify_sha256(p, "0" * 64)
        self.assertIn("mismatch", str(cm.exception))


class DebTest(helpers.TempDirMixin, unittest.TestCase):
    def test_extract_make(self):
        deb = os.path.join(self.tmp, "make.deb")
        make_deb(deb)
        dest = os.path.join(self.tmp, "out", "make")
        os.makedirs(os.path.dirname(dest))
        bootstrap.deb_extract_file(deb, "usr/bin/make", dest)
        self.assertTrue(os.stat(dest).st_mode & stat.S_IXUSR)
        line, err = bootstrap.run_version([dest, "--version"])
        self.assertIsNone(err)
        self.assertEqual(line, "GNU Make 4.3")

    def test_zstd_fails_clearly(self):
        deb = os.path.join(self.tmp, "m.deb")
        make_deb(deb, "data.tar.zst", b"\x28\xb5\x2f\xfd")
        with self.assertRaises(bootstrap.BootstrapError) as cm:
            bootstrap.deb_extract_file(deb, "usr/bin/make", os.path.join(self.tmp, "x"))
        self.assertIn("zstd", str(cm.exception))

    def test_bad_magic_and_missing_member(self):
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.read_ar(b"not an archive at all")
        deb = os.path.join(self.tmp, "m.deb")
        make_deb(deb, data=make_tar({"./etc/x": (b"1", 0o644)}))
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.deb_extract_file(deb, "usr/bin/make", os.path.join(self.tmp, "x"))

    def test_ar_odd_sizes(self):
        got = bootstrap.read_ar(make_ar([("a", b"123"), ("b", b"45")]))
        self.assertEqual(got, [("a", b"123"), ("b", b"45")])


class ExtractTest(helpers.TempDirMixin, unittest.TestCase):
    def test_strip_and_modes(self):
        data = make_tar({"go/bin/go": (b"x", 0o755), "go/VERSION": (b"v", 0o644)}, "w:gz")
        dest = os.path.join(self.tmp, "d")
        bootstrap.extract_tar(io.BytesIO(data), dest, 1)
        self.assertTrue(os.access(os.path.join(dest, "bin", "go"), os.X_OK))
        self.assertTrue(os.path.exists(os.path.join(dest, "VERSION")))

    def test_rejects_traversal(self):
        data = make_tar({"../evil": (b"x", 0o644)}, "w:gz")
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.extract_tar(io.BytesIO(data), os.path.join(self.tmp, "d"))


class CheckTest(helpers.TempDirMixin, unittest.TestCase):
    def run_check(self, prefix):
        lines = []
        ok = bootstrap.run_check(prefix, bootstrap.tool_specs("amd64", "3.241.0"), None, lines.append)
        return ok, lines

    def test_nothing_installed_downloads_nothing(self):
        def boom(*a, **k):
            raise AssertionError("network used")
        orig = bootstrap.download
        bootstrap.download = boom
        self.addCleanup(setattr, bootstrap, "download", orig)
        rc = helpers.quiet(bootstrap.main, ["--check", "--prefix", os.path.join(self.tmp, "p")])
        self.assertEqual(rc, 1)
        ok, lines = self.run_check(os.path.join(self.tmp, "p"))
        self.assertFalse(ok)
        for tool in ("go", "pulumi", "jq", "gitleaks"):
            self.assertTrue(any(l.startswith(tool) and "MISSING" in l for l in lines), tool)

    def test_fake_installed_is_ok_and_wrong_version_detected(self):
        prefix = os.path.join(self.tmp, "p")
        fake = {"go": "echo go version go1.27.3 linux/amd64", "pulumi": "echo v3.241.0",
                "jq": "echo jq-1.8.2", "gitleaks": "echo 8.30.1", "make": "echo GNU Make 4.3"}
        for name, cmd in fake.items():
            helpers.write(prefix, "bin/" + name, "#!/bin/sh\n%s\n" % cmd)
            os.chmod(os.path.join(prefix, "bin", name), 0o755)
        ok, lines = self.run_check(prefix)
        self.assertTrue(ok, lines)
        helpers.write(prefix, "bin/jq", "#!/bin/sh\necho jq-1.7\n")
        ok, lines = self.run_check(prefix)
        self.assertFalse(ok)
        self.assertTrue(any(l.startswith("jq") and "WRONG" in l for l in lines))

    def test_path_line_quotes(self):
        self.assertEqual(bootstrap.path_line("/h/x"), 'export PATH="/h/x/bin:$PATH"')
        self.assertIn('\\$', bootstrap.path_line("/h/$x"))


class FakeUrlopen:
    """Stand-in for urllib.request.urlopen: each call pops the next outcome (an exception or bytes)."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def __call__(self, req, timeout=None):
        self.calls += 1
        o = self.outcomes.pop(0)
        if isinstance(o, BaseException):
            raise o
        return io.BytesIO(o)


def http_error(code, headers=None):
    return urllib.error.HTTPError("https://example.invalid/f", code, "status %d" % code, headers or {},
                                  io.BytesIO(b"upstream says no"))


class DownloadRetryTest(helpers.TempDirMixin, unittest.TestCase):
    def fetch(self, outcomes, **kw):
        fake, sleeps, logs = FakeUrlopen(outcomes), [], []
        dest = os.path.join(self.tmp, "f")
        with mock.patch.object(bootstrap.urllib.request, "urlopen", fake):
            try:
                bootstrap.download("https://example.invalid/f", dest, log=logs.append, sleep=sleeps.append, **kw)
                err = None
            except bootstrap.BootstrapError as e:
                err = str(e)
        return fake, sleeps, logs, err, dest

    def test_transient_failures_then_success(self):
        fake, sleeps, logs, err, dest = self.fetch(
            [http_error(504), http_error(503), http_error(429), ConnectionResetError(104, "reset by peer"), b"payload"])
        self.assertIsNone(err)
        self.assertEqual(fake.calls, 5)
        with open(dest, "rb") as f:
            self.assertEqual(f.read(), b"payload")
        self.assertFalse(os.path.exists(dest + ".part"))
        self.assertEqual(len(sleeps), 4)
        # exponential with equal jitter: attempt n (0-based) waits between half and all of BASE * 2**n
        for n, d in enumerate(sleeps):
            full = min(bootstrap.BACKOFF_CAP, bootstrap.BACKOFF_BASE * 2 ** n)
            self.assertGreaterEqual(d, full / 2)
            self.assertLessEqual(d, full)
        self.assertIn("attempt 1 of 5 failed (HTTP 504)", logs[0])

    def test_gives_up_after_five_attempts(self):
        outcomes = [socket.timeout("timed out"), http.client.IncompleteRead(b"x", 10),
                    urllib.error.URLError(ConnectionResetError(104, "reset")), http_error(500), http_error(502)]
        fake, sleeps, logs, err, _ = self.fetch(outcomes)
        self.assertEqual(fake.calls, 5)
        self.assertEqual(len(sleeps), 4)
        self.assertIn("failed after 5 attempts: HTTP 502", err)

    def test_refusals_are_not_retried(self):
        for code in (401, 403, 404, 407, 451):
            fake, sleeps, logs, err, _ = self.fetch([http_error(code), b"never"])
            self.assertEqual(fake.calls, 1, code)
            self.assertEqual(sleeps, [], code)
            self.assertIn("HTTP %d upstream says no (not retrying)" % code, err)

    def test_retry_after_is_honored_and_capped(self):
        _, sleeps, _, err, _ = self.fetch([http_error(429, {"Retry-After": "30"}), b"ok"])
        self.assertIsNone(err)
        self.assertGreaterEqual(sleeps[0], 30)
        _, sleeps, _, _, _ = self.fetch([http_error(503, {"Retry-After": "86400"}), b"ok"])
        self.assertLessEqual(sleeps[0], bootstrap.BACKOFF_CAP)
        _, sleeps, _, _, _ = self.fetch([http_error(503, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}), b"ok"])
        self.assertLessEqual(sleeps[0], bootstrap.BACKOFF_BASE)

    def test_backoff_delay_bounds(self):
        self.assertEqual(bootstrap.backoff_delay(0, rand=lambda: 0.0), bootstrap.BACKOFF_BASE / 2)
        self.assertEqual(bootstrap.backoff_delay(0, rand=lambda: 1.0), bootstrap.BACKOFF_BASE)
        self.assertEqual(bootstrap.backoff_delay(3, rand=lambda: 1.0), bootstrap.BACKOFF_BASE * 8)
        self.assertEqual(bootstrap.backoff_delay(20, rand=lambda: 1.0), bootstrap.BACKOFF_CAP)
        self.assertEqual(bootstrap.backoff_delay(0, retry_after=12, rand=lambda: 0.0), 12)

    def test_install_passes_the_log_to_download(self):
        spec = bootstrap.tool_specs("amd64", "3.241.0")["jq"]
        seen, progress = [], []

        def fake_download(url, dest, log=None, **kw):
            seen.append(log)
            raise bootstrap.BootstrapError("stop")
        with mock.patch.object(bootstrap, "download", fake_download):
            with self.assertRaises(bootstrap.BootstrapError):
                bootstrap.install_spec(spec, os.path.join(self.tmp, "p"), progress.append)
        self.assertEqual(seen, [progress.append])
        self.assertIn("jq 1.8.2: downloading", progress[0])


class CacheKeyTest(unittest.TestCase):
    def key(self, arch="amd64", pulumi="3.241.0", names=None):
        specs = bootstrap.tool_specs(arch, pulumi)
        return bootstrap.cache_key(specs, names or bootstrap.PROFILES["full"], arch)

    def test_stable_and_short(self):
        self.assertEqual(self.key(), self.key())
        self.assertRegex(self.key(), r"^[0-9a-f]{16}$")

    def test_changes_with_pins_arch_profile_and_layout(self):
        base = self.key()
        self.assertNotEqual(base, self.key(pulumi="3.242.0"))
        self.assertNotEqual(base, self.key(arch="arm64"))
        self.assertNotEqual(base, self.key(names=bootstrap.PROFILES["minimal"]))
        with mock.patch.object(bootstrap, "GITLEAKS_VERSION", "8.30.2"):
            self.assertNotEqual(base, self.key())
        with mock.patch.object(bootstrap, "MAKE_DEB_VERSION", "4.3-4.2"):
            self.assertNotEqual(base, self.key())
        with mock.patch.object(bootstrap, "CACHE_LAYOUT", bootstrap.CACHE_LAYOUT + 1):
            self.assertNotEqual(base, self.key())

    def test_cli_prints_the_key_without_network(self):
        lines = []
        with mock.patch.object(bootstrap, "download", side_effect=AssertionError("network used")), \
                mock.patch.object(bootstrap.sys, "platform", "linux"), \
                mock.patch.object(bootstrap, "detect_arch", return_value="amd64"):
            rc = bootstrap.main(["--cache-key", "--prefix", "/nonexistent/p"], out=lines.append, err=lines.append)
        self.assertEqual(rc, 0)
        self.assertEqual(lines, [bootstrap.cache_key(bootstrap.tool_specs("amd64", bootstrap.read_pulumi_version()),
                                                     bootstrap.PROFILES["full"], "amd64")])
        self.assertFalse(os.path.exists("/nonexistent/p"))


if __name__ == "__main__":
    unittest.main()
