#!/usr/bin/env python3
"""Derive every pulumi-labs package name from a provider slug.

Names are computed here and nowhere else. Never type one by hand.

    python3 scripts/names.py nvidia-aicr
    python3 scripts/names.py --provider modal --field go_module
"""
import argparse
import json
import re
import sys

SLUG_RE = re.compile(r"^[a-z][a-z0-9-]*[a-z0-9]$")

# Tokens substituted into archetype templates. DISPLAY_NAME comes from the
# export options, the rest are derived from the slug.
PLACEHOLDERS = ("__PROVIDER__", "__GO_PKG__", "__PY_PKG__", "__PASCAL__", "__DISPLAY_NAME__")


class SlugError(ValueError):
    """Raised when a provider slug is not acceptable."""


def validate_slug(slug):
    """Return slug unchanged or raise SlugError with an actionable message."""
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        raise SlugError(
            "invalid provider slug %r: must match ^[a-z][a-z0-9-]*[a-z0-9]$ "
            "(lowercase, digits and hyphens; starts with a letter; no trailing hyphen)" % (slug,)
        )
    if "--" in slug:
        raise SlugError("invalid provider slug %r: consecutive hyphens are not allowed" % (slug,))
    return slug


def pascal(slug):
    """Capitalise each hyphen-separated part and join: nvidia-aicr -> NvidiaAicr."""
    return "".join(part[:1].upper() + part[1:] for part in slug.split("-"))


def derive(slug):
    """Return the full name table for slug as a dict."""
    p = validate_slug(slug)
    gopkg = p.replace("-", "")
    py = "pulumi_labs_" + p.replace("-", "_")
    pas = pascal(p)
    repo = "pulumi-labs/pulumi-" + p
    go_module = "github.com/pulumi-labs/pulumi-" + p
    return {
        "slug": p,
        "repo": repo,
        "repo_url": "https://github.com/" + repo,
        "go_module": go_module,
        "go_pkg": gopkg,
        "go_sdk_dir": "sdk/go/" + gopkg,
        "go_import_base_path": go_module + "/sdk/go/" + gopkg,
        "npm_package": "@pulumi-labs/" + p,
        "python_package": py,
        "pypi_name": "pulumi-labs-" + p,
        "dotnet_root_namespace": "Pulumi.Labs",
        "dotnet_pascal": pas,
        "dotnet_package": "Pulumi.Labs." + pas,
        "dotnet_csproj": "sdk/dotnet/Pulumi.Labs.%s.csproj" % pas,
        "java_base_package": "com.pulumi.labs",
        "maven_group": "com.pulumi.labs",
        "maven_artifact": p,
        "maven_coordinates": "com.pulumi.labs:" + p,
        "pom_name": "pulumi-labs-" + p,
        "binary": "pulumi-resource-" + p,
        "cmd_dir": "provider/cmd/pulumi-resource-" + p,
        "schema_path": "provider/cmd/pulumi-resource-%s/schema.json" % p,
        "ldflags_version_var": go_module + "/provider/pkg/version.Version",
        "publisher": "Pulumi Labs",
        "plugin_download_url": "github://api.github.com/" + repo,
        "esc_environment": "github-secrets/pulumi-labs-pulumi-" + p,
    }


def template_tokens(slug):
    """Return the slug-derived placeholder map used by render.py."""
    n = derive(slug)
    return {
        "__PROVIDER__": n["slug"],
        "__GO_PKG__": n["go_pkg"],
        "__PY_PKG__": n["python_package"],
        "__PASCAL__": n["dotnet_pascal"],
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("slug", nargs="?", help="provider slug, e.g. nvidia-aicr")
    ap.add_argument("--provider", help="provider slug (alternative to the positional)")
    ap.add_argument("--field", help="print only this field's value instead of the JSON table")
    args = ap.parse_args(argv)
    slug = args.provider or args.slug
    if not slug:
        ap.error("provider slug required")
    try:
        table = derive(slug)
    except SlugError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    if args.field:
        if args.field not in table:
            print("error: unknown field %r; known: %s" % (args.field, ", ".join(sorted(table))), file=sys.stderr)
            return 1
        print(table[args.field])
    else:
        print(json.dumps(table, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
