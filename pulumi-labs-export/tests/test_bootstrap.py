import io
import os
import stat
import tarfile
import unittest

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
        self.assertEqual(s["go"].url, "https://go.dev/dl/go1.27.2.linux-amd64.tar.gz")
        self.assertEqual(s["go"].sha256, bootstrap.GO_SHA256["amd64"])
        self.assertEqual(s["pulumi"].url, "https://github.com/pulumi/pulumi/releases/download/v3.241.0/pulumi-v3.241.0-linux-x64.tar.gz")
        self.assertEqual(s["pulumi"].checksums_url, "https://github.com/pulumi/pulumi/releases/download/v3.241.0/pulumi-3.241.0-checksums.txt")
        self.assertTrue(s["jq"].url.endswith("/jq-1.8.2/jq-linux-amd64"))
        self.assertTrue(s["gitleaks"].url.endswith("/v8.30.1/gitleaks_8.30.1_linux_x64.tar.gz"))
        self.assertTrue(s["gitleaks"].checksums_url.endswith("/gitleaks_8.30.1_checksums.txt"))

    def test_urls_arm64(self):
        s = bootstrap.tool_specs("arm64", "3.241.0")
        self.assertEqual(s["go"].url, "https://go.dev/dl/go1.27.2.linux-arm64.tar.gz")
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
        fake = {"go": "echo go version go1.27.2 linux/amd64", "pulumi": "echo v3.241.0",
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


if __name__ == "__main__":
    unittest.main()
