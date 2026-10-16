PROJECT_NAME := nvidia-aicr
PROVIDER     := pulumi-resource-$(PROJECT_NAME)
VERSION      ?= 0.0.1-dev
PROVIDER_DIR := provider
BIN_DIR      := bin
SCHEMA_FILE  := $(PROVIDER_DIR)/cmd/$(PROVIDER)/schema.json

LDFLAGS := -X github.com/pulumi-labs/pulumi-nvidia-aicr/provider/pkg/version.Version=$(VERSION)

.PHONY: all provider schema test lint clean install \
        nodejs_sdk python_sdk go_sdk dotnet_sdk java_sdk sdks \
        build_nodejs_sdk build_python_sdk build_dotnet_sdk build_java_sdk \
        set_version sdk_fixups

all: provider

# Build the provider binary
provider:
	cd $(PROVIDER_DIR) && go build -ldflags "$(LDFLAGS)" -o ../$(BIN_DIR)/$(PROVIDER) ./cmd/$(PROVIDER)

# Generate the provider schema by querying the built provider plugin via the
# Pulumi CLI. The provider binary itself only exposes the gRPC
# ResourceProvider service; pulumi-go-provider has no built-in `schema`
# subcommand.
#
# The `version` field is stripped from the committed schema so it doesn't
# churn on every release. Version is injected at SDK codegen time via the
# `--version` flag on `pulumi package gen-sdk`, matching the reference
# pulumi-go-provider repos (pulumi-command, pulumi-docker-build, pulumi-eks,
# pulumi-std).
schema: provider
	pulumi package get-schema ./$(BIN_DIR)/$(PROVIDER) | jq 'del(.version)' >$(SCHEMA_FILE)

# Run unit tests
test:
	go test -v -count=1 -race ./provider/pkg/...

# Run linter
lint:
	go vet ./...

# Generate Node.js SDK
nodejs_sdk: schema
	rm -rf sdk/nodejs
	pulumi package gen-sdk --language nodejs $(SCHEMA_FILE) --version $(VERSION)

# Generate Python SDK
python_sdk: schema
	rm -rf sdk/python
	pulumi package gen-sdk --language python $(SCHEMA_FILE) --version $(VERSION)

# Generate Go SDK
go_sdk: schema
	rm -rf sdk/go
	pulumi package gen-sdk --language go $(SCHEMA_FILE) --version $(VERSION)
	cd sdk/go/nvidiaaicr && go mod init github.com/pulumi-labs/pulumi-nvidia-aicr/sdk/go/nvidiaaicr && go mod tidy

# Generate .NET SDK
dotnet_sdk: schema
	rm -rf sdk/dotnet
	pulumi package gen-sdk --language dotnet $(SCHEMA_FILE) --version $(VERSION)

# Generate Java SDK
java_sdk: schema
	rm -rf sdk/java
	pulumi package gen-sdk --language java $(SCHEMA_FILE) --version $(VERSION)

# Generate all SDKs
sdks: nodejs_sdk python_sdk go_sdk dotnet_sdk java_sdk sdk_fixups

# Re-apply tweaks that pulumi package gen-sdk does not (yet) emit:
#   - Python: point readme at the package-local README and use the SPDX
#     string-form license (the generator emits a deprecated [project.license]
#     table that ships an empty PyPI long description and prints a warning).
#     Also rewrite `pulumi.Input[Optional[T]]` -> `Optional[pulumi.Input[T]]`
#     in the generated .py files: the codegen emits the inner-Optional
#     nesting but the runtime's unwrap_type only recognizes the outer-Optional
#     form, so passing a list/dict to an optional input asserts in
#     _get_list_element_type. Tracked upstream at pulumi/pulumi#23414.
#   - .NET: declare PackageReadmeFile and pack README.md so NuGet stops
#     warning about a missing readme.
#   - Java: the generator pins the Gradle compile toolchain to Java 11 and
#     sets no release target. Gradle 9.x needs JVM 17+ to run and the CI
#     runner provides a single JDK, so compile on JDK 17 but emit Java 11
#     bytecode via `--release 11`, leaving SDK consumers on Java 11.
#   - Node.js: the generated package.json has no main/types/files and there
#     is no .npmignore, so npm falls back to .gitignore (which excludes
#     bin/) and `npm publish` would ship uncompiled .ts with no entry
#     point. Add main/types and a files allowlist pointing at bin/.
#     Also rewrite the build script so it copies package.json into bin/
#     after tsc; the gen-sdk utilities.ts does `require('./package.json')`
#     to read the version, and that path resolves next to the compiled
#     utilities.js at runtime.
sdk_fixups:
	@if [ -f sdk/python/pyproject.toml ]; then \
		python3 -c 'import re,sys,pathlib; \
p=pathlib.Path("sdk/python/pyproject.toml"); s=p.read_text(); \
s=re.sub(r"^( *)readme = \"README\.md\"", "\\1readme = \"pulumi_labs_nvidia_aicr/README.md\"", s, flags=re.M); \
s=re.sub(r"\n  \[project\.license\]\n    text = \"Apache-2\.0\"", "\n  license = \"Apache-2.0\"", s); \
p.write_text(s)'; \
	fi
	@if [ -d sdk/python ]; then \
		python3 scripts/fixup_python_input_optional.py sdk/python; \
	fi
	@if [ -d sdk/dotnet ] && [ -f README.md ] && [ ! -f sdk/dotnet/README.md ]; then \
		cp README.md sdk/dotnet/README.md; \
	fi
	@if [ -f sdk/dotnet/Pulumi.Labs.NvidiaAicr.csproj ] && ! grep -q PackageReadmeFile sdk/dotnet/Pulumi.Labs.NvidiaAicr.csproj; then \
		sed -i.bak -E 's|(<PackageIcon>logo.png</PackageIcon>)|\1\n    <PackageReadmeFile>README.md</PackageReadmeFile>|' sdk/dotnet/Pulumi.Labs.NvidiaAicr.csproj && \
		rm -f sdk/dotnet/Pulumi.Labs.NvidiaAicr.csproj.bak; \
	fi
	@if [ -f sdk/dotnet/Pulumi.Labs.NvidiaAicr.csproj ] && ! grep -q '<None Include="README.md">' sdk/dotnet/Pulumi.Labs.NvidiaAicr.csproj; then \
		python3 -c 'import pathlib; \
p=pathlib.Path("sdk/dotnet/Pulumi.Labs.NvidiaAicr.csproj"); s=p.read_text(); \
needle="<None Include=\"logo.png\">\n      <Pack>True</Pack>\n      <PackagePath></PackagePath>\n    </None>"; \
addition=needle + "\n    <None Include=\"README.md\">\n      <Pack>True</Pack>\n      <PackagePath></PackagePath>\n    </None>"; \
p.write_text(s.replace(needle, addition, 1))'; \
	fi
# String surgery on the generated (do-not-edit) build.gradle:
#   - bump the pinned compile toolchain from Java 11 to 17 (so Gradle,
#     which needs JVM 17+, runs on the single JDK the runner provides)
#     and add `options.release = 11` so emitted bytecode stays Java 11.
#   - fill the POM <name> (pulumi-java-gen emits an empty `name = ""`
#     unless Publisher == "pulumi", which would also clobber our
#     com.pulumi.labs groupId; tracked upstream).
	@if [ -f sdk/java/build.gradle ] && ! grep -q 'options.release = 11' sdk/java/build.gradle; then \
		python3 -c 'import pathlib; \
p=pathlib.Path("sdk/java/build.gradle"); s=p.read_text(); \
s=s.replace("JavaLanguageVersion.of(11)", "JavaLanguageVersion.of(17)", 1); \
s=s.replace("options.encoding = \"UTF-8\"", "options.encoding = \"UTF-8\"\n    options.release = 11", 1); \
p.write_text(s)'; \
	fi
	@if [ -f sdk/java/build.gradle ] && grep -q 'name = ""' sdk/java/build.gradle; then \
		python3 -c 'import pathlib; \
p=pathlib.Path("sdk/java/build.gradle"); s=p.read_text(); \
s=s.replace("name = \"\"", "name = \"pulumi-labs-nvidia-aicr\"", 1); \
p.write_text(s)'; \
	fi
# The generated package.json has no main/types/files; with no .npmignore npm
# falls back to .gitignore (which excludes bin/), so `npm publish` would ship
# raw .ts with no entry point. Add main/types and a files allowlist so the
# compiled bin/ is what gets published and imported.
#
# Also rewrite the build script. The generated utilities.ts reads its
# version via `require('./package.json')`; tsc emits that line unchanged
# into bin/utilities.js, which then resolves the require relative to
# bin/. Without a bin/package.json the first SDK call throws
# MODULE_NOT_FOUND. Copy package.json into bin/ after tsc so the require
# resolves at runtime.
	@if [ -f sdk/nodejs/package.json ] && ! grep -q '"main"' sdk/nodejs/package.json; then \
		python3 -c 'import json,pathlib; \
p=pathlib.Path("sdk/nodejs/package.json"); d=json.loads(p.read_text()); \
d["main"]="bin/index.js"; d["types"]="bin/index.d.ts"; d["files"]=["bin/"]; \
d.setdefault("scripts", {})["build"]="tsc && cp package.json bin/"; \
p.write_text(json.dumps(d, indent=4)+"\n")'; \
	fi

# Compile each SDK to catch generation breaks. These targets are best-effort:
# they no-op if the corresponding toolchain is not installed locally.
build_nodejs_sdk:
	@if command -v npm >/dev/null 2>&1 && [ -d sdk/nodejs ]; then \
		cd sdk/nodejs && npm install && npm run build; \
	else \
		echo "skipping nodejs SDK build (npm not installed or sdk/nodejs missing)"; \
	fi

build_python_sdk:
	@if command -v python3 >/dev/null 2>&1 && [ -d sdk/python ]; then \
		python3 -m venv $(BIN_DIR)/python-sdk-venv && \
		$(BIN_DIR)/python-sdk-venv/bin/python -m pip install --quiet --upgrade pip build && \
		cd sdk/python && $(CURDIR)/$(BIN_DIR)/python-sdk-venv/bin/python -m build --sdist --wheel --outdir dist/; \
	else \
		echo "skipping python SDK build (python3 not installed or sdk/python missing)"; \
	fi

build_dotnet_sdk:
	@if command -v dotnet >/dev/null 2>&1 && [ -d sdk/dotnet ]; then \
		cd sdk/dotnet && dotnet build; \
	else \
		echo "skipping dotnet SDK build (dotnet not installed or sdk/dotnet missing)"; \
	fi

build_java_sdk:
	@if command -v gradle >/dev/null 2>&1 && [ -d sdk/java ]; then \
		cd sdk/java && gradle build; \
	else \
		echo "skipping java SDK build (gradle not installed or sdk/java missing)"; \
	fi

# Substitute the release VERSION into language-SDK manifests.
# Note: in the normal flow, `make sdks VERSION=$(VERSION)` already produces SDKs
# with the right version baked in (the provider binary embeds VERSION via
# ldflags, schema inherits it, SDK generators read it from the schema). This
# target is a fallback for when SDKs have been generated with a placeholder
# version (e.g., running `make sdks` without VERSION set, then publishing).
set_version:
	@if [ -f sdk/nodejs/package.json ]; then \
		sed -i.bak -E 's/"version": "[^"]+"/"version": "$(VERSION)"/' sdk/nodejs/package.json && \
		rm -f sdk/nodejs/package.json.bak; \
	fi
	@if [ -f sdk/python/pyproject.toml ]; then \
		sed -i.bak -E 's/^( *version *= *)"[^"]+"/\1"$(VERSION)"/' sdk/python/pyproject.toml && \
		rm -f sdk/python/pyproject.toml.bak; \
	fi
	@if [ -f sdk/dotnet/Pulumi.NvidiaAicr.csproj ]; then \
		sed -i.bak -E 's|<Version>[^<]+</Version>|<Version>$(VERSION)</Version>|' sdk/dotnet/Pulumi.NvidiaAicr.csproj && \
		rm -f sdk/dotnet/Pulumi.NvidiaAicr.csproj.bak; \
	fi
	@if [ -f sdk/java/build.gradle ]; then \
		sed -i.bak -E 's/version = "[^"]+"/version = "$(VERSION)"/' sdk/java/build.gradle && \
		rm -f sdk/java/build.gradle.bak; \
	fi

# Install the provider binary locally
install: provider
	cp $(BIN_DIR)/$(PROVIDER) $(GOPATH)/bin/

# Clean build artifacts
clean:
	rm -rf $(BIN_DIR) sdk/nodejs sdk/python sdk/go sdk/dotnet sdk/java
	rm -f $(SCHEMA_FILE)

# Verify every hand-written Go source carries the Apache 2.0 copyright
# header. Generated trees (sdk/, schema.json) and examples are exempt by
# convention. CI runs this; add headers with the block from any provider/
# file when it fails.
.PHONY: check_copyright
check_copyright:
	@missing=$$(find provider -name '*.go' | xargs grep -L "Copyright .*Pulumi Corporation"); \
	if [ -n "$$missing" ]; then \
		echo "missing copyright header:"; echo "$$missing"; exit 1; \
	fi
	@echo "copyright headers OK"
