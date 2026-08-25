"""Per-language toolchain descriptions for Dockerfile generation.

This lives in a `shims/` directory because the regex test in test_any_language.py allows
language names to be quoted here (as data) but bans them in ordinary core/ code (as branches).
The distinction is enforced by directory name: code that names languages as data belongs in a
shim or coverage table; code that branches on them belongs nowhere.

Each entry describes TWO things:
  - As SOURCE: the base_image to use when this language is the environment's primary language.
  - As TARGET (cross-language): how to layer the toolchain on top of an existing base image.

Fields:
  base_image        canonical Docker image, pinned to a specific version tag (never `latest`)
  apt_packages      debian/ubuntu packages to install on a bookworm-slim-compatible base
  install_cmds      shell commands run after apt; used when this language is the cross-language
                    target. Prefer registry-based installs (rustup, apt) over curl tarball
                    downloads -- those are blocked during docker build in some sandboxes.
  copy_from_image   when set, the toolchain is obtained via Docker multi-stage COPY from this
                    image rather than via install_cmds. Multi-stage registry pulls always work
                    even when outbound HTTP is blocked during docker build. copy_from_paths is
                    the list of (src_path, dst_path) tuples to COPY, and copy_from_env is the
                    ENV block to set after copying.
  env               ENV vars required by the toolchain
  verify_cmd        quick command confirming the toolchain is present and working
"""
from __future__ import annotations

_LANGUAGE_SETUP: dict[str, dict] = {
    "python": {
        "base_image": "python:3.12.8-slim-bookworm",
        "apt_packages": [],
        "install_cmds": [],
        "copy_from_image": None,
        "copy_from_paths": [],
        "env": {},
        "verify_cmd": "python3 --version",
    },
    "go": {
        "base_image": "golang:1.23-bookworm",
        "apt_packages": [],
        "install_cmds": [],
        # Multi-stage copy from the official Go image — avoids outbound HTTP during docker build
        # which is blocked in some sandbox environments (e.g. e2b DinD). Registry pulls always work.
        "copy_from_image": "golang:1.23-bookworm",
        "copy_from_paths": [
            ("/usr/local/go", "/usr/local/go"),
        ],
        "env": {
            "PATH": "/usr/local/go/bin:${PATH}",
            "GOPATH": "/go",
            "GOROOT": "/usr/local/go",
        },
        "verify_cmd": "go version",
    },
    "rust": {
        "base_image": "rust:1.82-bookworm",
        "apt_packages": [],
        "install_cmds": [
            "curl https://sh.rustup.rs -sSf"
            " | sh -s -- -y --default-toolchain 1.82.0 --no-modify-path",
        ],
        "copy_from_image": None,
        "copy_from_paths": [],
        "env": {"PATH": "/root/.cargo/bin:${PATH}"},
        "verify_cmd": "rustc --version",
    },
    "c": {
        "base_image": "debian:bookworm-slim",
        "apt_packages": ["gcc", "libc6-dev", "make"],
        "install_cmds": [],
        "copy_from_image": None,
        "copy_from_paths": [],
        "env": {},
        "verify_cmd": "gcc --version",
    },
    "cpp": {
        "base_image": "debian:bookworm-slim",
        "apt_packages": ["g++", "libc6-dev", "make"],
        "install_cmds": [],
        "copy_from_image": None,
        "copy_from_paths": [],
        "env": {},
        "verify_cmd": "g++ --version",
    },
    "java": {
        "base_image": "eclipse-temurin:21-jdk-jammy",
        "apt_packages": [],
        "install_cmds": [],
        "copy_from_image": "eclipse-temurin:21-jdk-jammy",
        "copy_from_paths": [
            ("/opt/java/openjdk", "/opt/java/openjdk"),
        ],
        "env": {
            "JAVA_HOME": "/opt/java/openjdk",
            "PATH": "${JAVA_HOME}/bin:${PATH}",
        },
        "verify_cmd": "java --version",
    },
    "javascript": {
        "base_image": "node:22-bookworm-slim",
        "apt_packages": [],
        "install_cmds": [],
        "copy_from_image": "node:22-bookworm-slim",
        "copy_from_paths": [
            ("/usr/local/bin/node", "/usr/local/bin/node"),
            ("/usr/local/bin/npm", "/usr/local/bin/npm"),
            ("/usr/local/bin/npx", "/usr/local/bin/npx"),
            ("/usr/local/lib/node_modules", "/usr/local/lib/node_modules"),
        ],
        "env": {},
        "verify_cmd": "node --version",
    },
    "typescript": {
        "base_image": "node:22-bookworm-slim",
        "apt_packages": [],
        "install_cmds": [
            "npm install -g typescript@5.6.3",
        ],
        "copy_from_image": "node:22-bookworm-slim",
        "copy_from_paths": [
            ("/usr/local/bin/node", "/usr/local/bin/node"),
            ("/usr/local/bin/npm", "/usr/local/bin/npm"),
            ("/usr/local/bin/npx", "/usr/local/bin/npx"),
            ("/usr/local/lib/node_modules", "/usr/local/lib/node_modules"),
        ],
        "env": {},
        "verify_cmd": "tsc --version",
    },
    "ruby": {
        "base_image": "ruby:3.3-slim-bookworm",
        "apt_packages": [],
        "install_cmds": [],
        "copy_from_image": "ruby:3.3-slim-bookworm",
        "copy_from_paths": [
            ("/usr/local/bin/ruby", "/usr/local/bin/ruby"),
            ("/usr/local/bin/gem", "/usr/local/bin/gem"),
            ("/usr/local/lib/ruby", "/usr/local/lib/ruby"),
        ],
        "env": {},
        "verify_cmd": "ruby --version",
    },
    "swift": {
        "base_image": "swift:5.10",
        "apt_packages": [],
        "install_cmds": [],
        "copy_from_image": "swift:5.10",
        "copy_from_paths": [
            ("/usr/bin/swift", "/usr/bin/swift"),
            ("/usr/bin/swiftc", "/usr/bin/swiftc"),
            ("/usr/lib/swift", "/usr/lib/swift"),
        ],
        "env": {},
        "verify_cmd": "swift --version",
    },
    "kotlin": {
        "base_image": "eclipse-temurin:21-jdk-jammy",
        "apt_packages": ["unzip"],
        "install_cmds": [
            # Download kotlinc zip from the official GitHub releases mirror
            "curl -fsSL https://github.com/JetBrains/kotlin/releases/download/v2.0.21/kotlin-compiler-2.0.21.zip"
            " -o /tmp/kotlin.zip"
            " && unzip -q /tmp/kotlin.zip -d /opt"
            " && rm /tmp/kotlin.zip"
            " && ln -s /opt/kotlinc/bin/kotlinc /usr/local/bin/kotlinc"
            " && ln -s /opt/kotlinc/bin/kotlin /usr/local/bin/kotlin",
        ],
        "copy_from_image": None,
        "copy_from_paths": [],
        "env": {},
        "verify_cmd": "kotlinc -version",
    },
    "scala": {
        # JDK base + Scala3 zip. When scala is a cross-language target, we also need JDK
        # (apt_packages includes default-jdk so it installs on any Debian/Ubuntu base).
        "base_image": "eclipse-temurin:21-jdk-jammy",
        "apt_packages": ["unzip", "curl", "default-jdk"],
        "install_cmds": [
            "curl -fsSL https://github.com/scala/scala3/releases/download/3.3.4/scala3-3.3.4.zip"
            " -o /tmp/scala.zip"
            " && unzip -q /tmp/scala.zip -d /opt"
            " && rm /tmp/scala.zip"
            " && ln -sf /opt/scala3-3.3.4/bin/scala /usr/local/bin/scala"
            " && ln -sf /opt/scala3-3.3.4/bin/scalac /usr/local/bin/scalac",
        ],
        "copy_from_image": None,
        "copy_from_paths": [],
        "env": {},
        "verify_cmd": "scala --version",
    },
    "haskell": {
        "base_image": "haskell:9.8",
        "apt_packages": ["libgmp-dev"],
        "install_cmds": [],
        "copy_from_image": "haskell:9.8",
        "copy_from_paths": [
            ("/usr/local/bin/ghc", "/usr/local/bin/ghc"),
            ("/usr/local/bin/cabal", "/usr/local/bin/cabal"),
            ("/usr/local/lib/ghc-9.8.4", "/usr/local/lib/ghc-9.8.4"),
        ],
        "env": {},
        "verify_cmd": "ghc --version",
    },
    "lua": {
        "base_image": "debian:bookworm-slim",
        "apt_packages": ["lua5.4"],
        "install_cmds": [],
        "copy_from_image": None,
        "copy_from_paths": [],
        "env": {},
        "verify_cmd": "lua5.4 -v",
    },
    "perl": {
        "base_image": "perl:5.38-slim",
        "apt_packages": [],
        "install_cmds": [],
        "copy_from_image": "perl:5.38-slim",
        "copy_from_paths": [
            ("/usr/local/bin/perl", "/usr/local/bin/perl"),
            ("/usr/local/lib/perl5", "/usr/local/lib/perl5"),
        ],
        "env": {},
        "verify_cmd": "perl --version",
    },
    "r": {
        "base_image": "r-base:4.4.1",
        "apt_packages": ["r-base"],
        "install_cmds": [],
        "copy_from_image": None,
        "copy_from_paths": [],
        "env": {},
        "verify_cmd": "R --version",
    },
    "julia": {
        "base_image": "julia:1.10-bookworm",
        "apt_packages": [],
        "install_cmds": [],
        "copy_from_image": "julia:1.10-bookworm",
        "copy_from_paths": [
            ("/usr/local/julia", "/usr/local/julia"),
        ],
        "env": {
            "PATH": "/usr/local/julia/bin:${PATH}",
        },
        "verify_cmd": "julia --version",
    },
    "zig": {
        "base_image": "debian:bookworm-slim",
        "apt_packages": ["xz-utils"],
        "install_cmds": [
            "curl -sSfL"
            " https://ziglang.org/download/0.13.0/zig-linux-x86_64-0.13.0.tar.xz"
            " | tar -C /opt -xJ"
            " && ln -s /opt/zig-linux-x86_64-0.13.0/zig /usr/local/bin/zig",
        ],
        "copy_from_image": None,
        "copy_from_paths": [],
        "env": {},
        "verify_cmd": "zig version",
    },
    "nim": {
        # nim is not in Debian bookworm apt; choosenim requires GLIBC_2.33+ which is not
        # available on all base images (e.g. haskell:9.8 uses Debian bullseye, glibc 2.31).
        # Use the prebuilt linux_x64 tarball directly from nim-lang.org instead.
        "base_image": "debian:bookworm-slim",
        "apt_packages": ["xz-utils"],
        "install_cmds": [
            "curl -fsSL https://nim-lang.org/download/nim-2.0.8-linux_x64.tar.xz"
            " | tar -C /opt -xJ"
            " && ln -s /opt/nim-2.0.8/bin/nim /usr/local/bin/nim"
            " && ln -s /opt/nim-2.0.8/bin/nimble /usr/local/bin/nimble",
        ],
        "copy_from_image": None,
        "copy_from_paths": [],
        "env": {},
        "verify_cmd": "nim --version",
    },
    "crystal": {
        # crystal 1.x is not in Debian apt; install from official apt repo
        "base_image": "crystallang/crystal:1.13",
        "apt_packages": [],
        "install_cmds": [
            "curl -fsSL https://crystal-lang.org/install.sh | bash",
        ],
        "copy_from_image": None,
        "copy_from_paths": [],
        "env": {},
        "verify_cmd": "crystal --version",
    },
    "elixir": {
        # COPY --from cannot replicate dynamic linker setup; apt handles all deps correctly
        "base_image": "elixir:otp-27",
        "apt_packages": ["elixir", "erlang-dev"],
        "install_cmds": [],
        "copy_from_image": None,
        "copy_from_paths": [],
        "env": {},
        "verify_cmd": "elixir --version",
    },
    "erlang": {
        # COPY --from cannot replicate dynamic linker setup; apt handles all deps correctly
        "base_image": "erlang:27-slim",
        "apt_packages": ["erlang-nox"],
        "install_cmds": [],
        "copy_from_image": None,
        "copy_from_paths": [],
        "env": {},
        "verify_cmd": (
            "erl -eval 'erlang:display(erlang:system_info(otp_release)), halt()' -noshell"
        ),
    },
    "ocaml": {
        # ocaml/opam image runs as opam user; needs_root=True emits USER root before apt-get.
        # ocaml is in Debian apt and works for both inplace and cross-language targets.
        "base_image": "ocaml/opam:debian-12-ocaml-5.2",
        "apt_packages": ["ocaml"],
        "install_cmds": [],
        "copy_from_image": None,
        "copy_from_paths": [],
        "needs_root": True,
        "env": {},
        "verify_cmd": "ocaml --version",
    },
    "fsharp": {
        "base_image": "mcr.microsoft.com/dotnet/sdk:8.0-bookworm-slim",
        "apt_packages": [],
        "install_cmds": [],
        "copy_from_image": "mcr.microsoft.com/dotnet/sdk:8.0-bookworm-slim",
        "copy_from_paths": [
            ("/usr/share/dotnet", "/usr/share/dotnet"),
        ],
        "env": {
            "DOTNET_ROOT": "/usr/share/dotnet",
            "PATH": "/usr/share/dotnet:${PATH}",
        },
        "verify_cmd": "dotnet fsi --version",
    },
    "csharp": {
        "base_image": "mcr.microsoft.com/dotnet/sdk:8.0-bookworm-slim",
        "apt_packages": [],
        "install_cmds": [],
        "copy_from_image": "mcr.microsoft.com/dotnet/sdk:8.0-bookworm-slim",
        "copy_from_paths": [
            ("/usr/share/dotnet", "/usr/share/dotnet"),
        ],
        "env": {
            "DOTNET_ROOT": "/usr/share/dotnet",
            "PATH": "/usr/share/dotnet:${PATH}",
        },
        "verify_cmd": "dotnet --version",
    },
    # Shell variants: used by process-seam tasks whose language is "sh" or "bash".
    "sh": {
        "base_image": "debian:bookworm-slim",
        "apt_packages": ["bash"],
        "install_cmds": [],
        "copy_from_image": None,
        "copy_from_paths": [],
        "env": {},
        "verify_cmd": "bash --version",
    },
    "bash": {
        "base_image": "debian:bookworm-slim",
        "apt_packages": ["bash"],
        "install_cmds": [],
        "copy_from_image": None,
        "copy_from_paths": [],
        "env": {},
        "verify_cmd": "bash --version",
    },
    "shell": {
        "base_image": "debian:bookworm-slim",
        "apt_packages": ["bash"],
        "install_cmds": [],
        "copy_from_image": None,
        "copy_from_paths": [],
        "env": {},
        "verify_cmd": "bash --version",
    },
}
