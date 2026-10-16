import unittest

import helpers
import names


class NamesTest(unittest.TestCase):
    def test_nvidia_aicr(self):
        n = names.derive("nvidia-aicr")
        self.assertEqual(n["repo"], "pulumi-labs/pulumi-nvidia-aicr")
        self.assertEqual(n["go_module"], "github.com/pulumi-labs/pulumi-nvidia-aicr")
        self.assertEqual(n["go_pkg"], "nvidiaaicr")
        self.assertEqual(n["go_import_base_path"], "github.com/pulumi-labs/pulumi-nvidia-aicr/sdk/go/nvidiaaicr")
        self.assertEqual(n["npm_package"], "@pulumi-labs/nvidia-aicr")
        self.assertEqual(n["python_package"], "pulumi_labs_nvidia_aicr")
        self.assertEqual(n["pypi_name"], "pulumi-labs-nvidia-aicr")
        self.assertEqual(n["dotnet_pascal"], "NvidiaAicr")
        self.assertEqual(n["dotnet_package"], "Pulumi.Labs.NvidiaAicr")
        self.assertEqual(n["dotnet_root_namespace"], "Pulumi.Labs")
        self.assertEqual(n["dotnet_csproj"], "sdk/dotnet/Pulumi.Labs.NvidiaAicr.csproj")
        self.assertEqual(n["java_base_package"], "com.pulumi.labs")
        self.assertEqual(n["maven_coordinates"], "com.pulumi.labs:nvidia-aicr")
        self.assertEqual(n["pom_name"], "pulumi-labs-nvidia-aicr")
        self.assertEqual(n["binary"], "pulumi-resource-nvidia-aicr")
        self.assertEqual(n["ldflags_version_var"], "github.com/pulumi-labs/pulumi-nvidia-aicr/provider/pkg/version.Version")
        self.assertEqual(n["publisher"], "Pulumi Labs")
        self.assertEqual(n["repo_url"], "https://github.com/pulumi-labs/pulumi-nvidia-aicr")
        self.assertEqual(n["plugin_download_url"], "github://api.github.com/pulumi-labs/pulumi-nvidia-aicr")
        self.assertEqual(n["esc_environment"], "github-secrets/pulumi-labs-pulumi-nvidia-aicr")

    def test_modal(self):
        n = names.derive("modal")
        self.assertEqual(n["go_pkg"], "modal")
        self.assertEqual(n["python_package"], "pulumi_labs_modal")
        self.assertEqual(n["dotnet_pascal"], "Modal")
        self.assertEqual(n["maven_coordinates"], "com.pulumi.labs:modal")
        self.assertEqual(n["npm_package"], "@pulumi-labs/modal")

    def test_better_uptime(self):
        n = names.derive("better-uptime")
        self.assertEqual(n["go_pkg"], "betteruptime")
        self.assertEqual(n["python_package"], "pulumi_labs_better_uptime")
        self.assertEqual(n["dotnet_pascal"], "BetterUptime")
        self.assertEqual(n["dotnet_package"], "Pulumi.Labs.BetterUptime")
        self.assertEqual(n["go_import_base_path"], "github.com/pulumi-labs/pulumi-better-uptime/sdk/go/betteruptime")
        self.assertEqual(n["pypi_name"], "pulumi-labs-better-uptime")

    def test_bad_slugs(self):
        for bad in ["", "Modal", "1modal", "modal-", "-modal", "mo_dal", "a", "mo--dal", "mo dal"]:
            with self.assertRaises(names.SlugError, msg=bad):
                names.derive(bad)

    def test_cli(self):
        self.assertEqual(helpers.quiet(names.main, ["--provider", "modal", "--field", "go_pkg"]), 0)
        self.assertEqual(helpers.quiet(names.main, ["Bad"]), 1)


if __name__ == "__main__":
    unittest.main()
