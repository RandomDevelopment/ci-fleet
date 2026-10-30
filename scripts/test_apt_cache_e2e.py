#!/usr/bin/env python3
"""Prove APT payload reuse on an inert Docker validation daemon, without real endpoints."""

import io
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import tarfile
import tempfile
import time
import uuid

from apt_cache import apt_config


ROOT = Path(__file__).resolve().parents[1]
BUILDKIT = "moby/buildkit:v0.32.2@sha256:28a898719c18a33f4e8000685287fa36fd0dd9560c6440227d3a732d79bb41d8"


def fixture_failure_details(output: bytes, max_lines=10) -> str:
    # BuildKit repeats the Dockerfile after the useful failing instruction output.
    text = output.decode(errors="replace").split("\nDockerfile:", 1)[0][-4096:]
    text = re.sub(r"https?://[^\s<>\"']+", "[URL]", text)
    text = re.sub(r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b", "[IP]", text)
    text = re.sub(r"(?<!\w)(?:[0-9a-fA-F]{0,4}:){2,}[0-9a-fA-F:.%]*", "[IP]", text)
    return "\n".join(text.splitlines()[-max_lines:])


def main():
    def terminate(signum, _frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, terminate)
    prefix = "ci-fleet-apt-test-" + uuid.uuid4().hex[:12]
    runner_image = os.environ.get("CI_FLEET_RUNNER_IMAGE", "ci-fleet-runner:dev")
    runner_definition = (ROOT / "runner/Dockerfile").read_text().splitlines()
    base_image = next(line.split()[1] for line in runner_definition if line.startswith("FROM "))
    snapshot = next(line.split("=", 1)[1] for line in runner_definition if line.startswith("ARG DEBIAN_SNAPSHOT="))
    epoch = next(line.split("=", 1)[1] for line in runner_definition if line.startswith("ARG SOURCE_DATE_EPOCH="))
    containers, images, builders = [], [], []
    build_client = cache = None
    measurements = []
    with tempfile.TemporaryDirectory(prefix=prefix) as directory:
        temporary = Path(directory)
        step = "setup"

        def run(*args, timeout=1200, input=None):
            result = subprocess.run(args, input=input, capture_output=True, timeout=timeout)
            with (temporary / "commands.log").open("ab") as log:
                log.write(result.stdout + result.stderr)
            if result.returncode:
                details = fixture_failure_details(result.stdout + result.stderr)
                raise RuntimeError(f"APT_CACHE_E2E_FAILED step={step} exit={result.returncode}\n{details}")
            return result.stdout.decode().strip()

        def cleanup(*args):
            try:
                return subprocess.run(args, capture_output=True, text=True, timeout=60)
            except subprocess.TimeoutExpired:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="")

        def totals(cache):
            text = run("docker", "exec", cache, "cat", "/var/log/apt-cacher-ng/apt-cacher.log")
            incoming = outgoing = 0
            for line in text.splitlines():
                fields = line.split("|")
                if len(fields) >= 5 and fields[-1].endswith(".deb"):
                    if fields[1] == "I": incoming += int(fields[2])
                    if fields[1] == "O": outgoing += int(fields[2])
            return incoming, outgoing

        def measure(cache, name, action, cold=False):
            nonlocal step
            step = name
            before = totals(cache)
            start = time.monotonic()
            action()
            after = totals(cache)
            incoming, outgoing = (after[index] - before[index] for index in (0, 1))
            if outgoing <= 0 or (incoming <= 0 if cold else incoming != 0):
                raise RuntimeError(f"APT_CACHE_E2E_FAILED step={name} missing expected package reuse")
            measurements.append(dict(path=name, elapsed_seconds=round(time.monotonic() - start, 2),
                                     upstream_package_bytes=incoming, served_package_bytes=outgoing))

        try:
            step = "disposable cache image"
            tools_image = prefix + "-tools"
            images.append(tools_image)
            run("docker", "build", "--build-arg", "RUNNER_IMAGE=" + runner_image, "-t", tools_image,
                str(ROOT / "scripts/fixtures/apt-cache"))
            cache = prefix + "-cache"
            containers.append(cache)
            run("docker", "run", "-d", "--name", cache, tools_image)
            address = run("docker", "inspect", "--format", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", cache)
            proxy = f"http://{address}:3142"
            secret = temporary / "apt.conf"
            secret.write_text(apt_config(proxy))
            os.chmod(secret, 0o600)
            step = "cache readiness"
            for attempt in range(30):
                ready = subprocess.run(["docker", "exec", cache, "curl", "--silent", "--fail",
                                        "--connect-timeout", "1", "--max-time", "2", "--noproxy", "*",
                                        "--output", "/dev/null", "--write-out", "%{http_code}",
                                        "http://127.0.0.1:3142/acng-report.html"], capture_output=True)
                if ready.returncode == 0: break
                time.sleep(1)
            else:
                state = run("docker", "inspect", "--format", "{{.State.Status}}/{{.State.ExitCode}}", cache)
                status = ready.stdout.decode().strip()
                status = status if status.isdigit() else "unknown"
                raise RuntimeError(f"APT_CACHE_E2E_FAILED step=cache readiness state={state} curl_exit={ready.returncode} http_status={status}")
            # Readiness may not create a transfer log until the first APT request.
            run("docker", "exec", cache, "touch", "/var/log/apt-cacher-ng/apt-cacher.log")
            step = "fresh runner package installs"
            install = "apt-get -o Acquire::Check-Valid-Until=false update && apt-get install -y --no-install-recommends hello"
            for index in range(2):
                client = prefix + f"-runner-{index}"
                containers.append(client)
                step = f"runner-{index} creation"
                run("docker", "create", "--name", client, "--user", "0", "--entrypoint", "/bin/sh",
                    runner_image, "-ec", install)
                step = f"runner-{index} APT configuration"
                run("docker", "cp", str(secret), client + ":/etc/apt/apt.conf.d/99-ci-fleet-cache")
                measure(cache, f"runner-{index}", lambda: run("docker", "start", "-a", client), cold=index == 0)
            concurrent = []
            for index in range(2):
                name = prefix + f"-parallel-{index}"
                containers.append(name)
                concurrent.append(name)
                run("docker", "create", "--name", name, "--user", "0", "--entrypoint", "/bin/sh",
                    runner_image, "-ec", install)
                run("docker", "cp", str(secret), name + ":/etc/apt/apt.conf.d/99-ci-fleet-cache")
            def install_concurrently():
                with ThreadPoolExecutor(max_workers=2) as executor:
                    list(executor.map(lambda name: run("docker", "start", "-a", name), concurrent))
            measure(cache, "concurrent-runners", install_concurrently)
            step = "disabled fresh runner"
            run("docker", "run", "--rm", "--entrypoint", "/bin/sh", runner_image, "-ec",
                'test ! -e /etc/apt/apt.conf.d/99-ci-fleet-cache; test -z "${CI_FLEET_APT_CONFIG:-}"')
            client = prefix + "-build-client"
            build_client = client
            containers.append(client)
            run("docker", "run", "-d", "--name", client, "--entrypoint", "/bin/sh",
                "--volume", "/var/run/docker.sock:/var/run/docker.sock", tools_image, "-c", "sleep infinity")
            run("docker", "cp", str(secret), client + ":/etc/apt/apt.conf.d/99-ci-fleet-cache")
            context = temporary / "context"
            context.mkdir()
            (context / "Dockerfile").write_text(
                "# syntax=docker/dockerfile:1.18@sha256:dabfc0969b935b2080555ace70ee69a5261af8a8f1b4df97b9e7fbcf6722eddf\n"
                "ARG BASE_IMAGE\nFROM ${BASE_IMAGE}\nUSER root\n"
                "RUN --mount=type=secret,id=ci_fleet_apt,target=/etc/apt/apt.conf.d/99-ci-fleet-cache "
                "rm -f /etc/apt/sources.list.d/debian.sources "
                f"&& echo 'deb http://snapshot.debian.org/archive/debian/{snapshot} trixie main' >/etc/apt/sources.list "
                "&& " + install + "\n")
            run("docker", "cp", str(context), client + ":/context")
            args = ["--no-cache", "--build-arg", "BASE_IMAGE=" + base_image,
                    "--secret", "id=ci_fleet_apt,src=/etc/apt/apt.conf.d/99-ci-fleet-cache"]
            for path in ("docker", "compose", "buildx"):
                step = path + " build"
                tag = prefix + "-" + path
                images.append(tag)
                if path == "compose":
                    compose = dict(services={"test": dict(image=tag, build=dict(context="/context",
                                   args=dict(BASE_IMAGE=base_image), secrets=["ci_fleet_apt"]))},
                                   secrets={"ci_fleet_apt": dict(file="/etc/apt/apt.conf.d/99-ci-fleet-cache")})
                    compose_file = temporary / "compose.json"
                    compose_file.write_text(json.dumps(compose))
                    run("docker", "cp", str(compose_file), client + ":/compose.json")
                    command = ["docker", "compose", "-p", prefix, "-f", "/compose.json", "build", "--no-cache"]
                elif path == "buildx":
                    builders.append((client, prefix))
                    containers.append("buildx_buildkit_" + prefix + "0")
                    run("docker", "exec", client, "docker", "buildx", "create", "--name", prefix,
                        "--driver", "docker-container", "--driver-opt", "image=" + BUILDKIT)
                    command = ["docker", "buildx", "build", "--builder", prefix, "--load", *args, "-t", tag, "/context"]
                else:
                    command = ["docker", "build", *args, "-t", tag, "/context"]
                measure(cache, path, lambda: run("docker", "exec", client, *command))
                exported = temporary / (path + ".tar")
                run("docker", "image", "save", "--output", str(exported), tag)
                with exported.open("rb") as image:
                    tail = b""
                    while data := image.read(1024 * 1024):
                        if proxy.encode() in tail + data:
                            raise RuntimeError("APT_CACHE_E2E_FAILED step=image endpoint redaction")
                        tail = data[-len(proxy):]
                run("docker", "run", "--rm", "--entrypoint", "/bin/sh", tag, "-ec",
                    "test ! -e /etc/apt/apt.conf.d/99-ci-fleet-cache")
            step = "managed images preserve reviewed identities"
            run("docker", "exec", client, "mkdir", "/release")
            tracked = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT)
            archive = io.BytesIO()
            with tarfile.open(fileobj=archive, mode="w") as tar:
                for raw in tracked.split(b"\0"):
                    name = os.fsdecode(raw)
                    if name.startswith(("controller/", "runner/", "scripts/")) and (ROOT / name).is_file():
                        tar.add(ROOT / name, arcname=name, recursive=False)
            run("docker", "cp", "-", client + ":/release", input=archive.getvalue())
            step = "uncached managed reference images"
            revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
            reference_builder = prefix + "-reference"
            builders.append((client, reference_builder))
            containers.append("buildx_buildkit_" + reference_builder + "0")
            reference_runner = prefix + "-reference-runner"
            reference_controller = prefix + "-reference-controller"
            images.extend([reference_runner, reference_controller])
            run("docker", "exec", client, "docker", "buildx", "create", "--name", reference_builder,
                "--driver", "docker-container", "--driver-opt", "image=" + BUILDKIT)
            # Keep the reviewed toolchain and arguments identical; omit only the APT secret.
            build = ["docker", "exec", client, "docker", "buildx", "build", "--builder", reference_builder,
                     "--no-cache", "--load", "--provenance=false", "--sbom=false",
                     "--build-arg", "CI_FLEET_COMMIT=" + revision, "--build-arg", "SOURCE_DATE_EPOCH=" + epoch]
            before = totals(cache)
            started = time.monotonic()
            run(*build, "--tag", reference_runner, "/release/runner")
            run(*build, "--build-arg", "CI_FLEET_VERSION=" + revision[:12], "--tag", reference_controller,
                "--file", "/release/controller/Dockerfile", "/release")
            if totals(cache) != before:
                raise RuntimeError("APT_CACHE_E2E_FAILED step=uncached reference used the cache")
            measurements.append(dict(path="managed-images-direct", served_package_bytes=0,
                                     elapsed_seconds=round(time.monotonic() - started, 2)))
            run("docker", "exec", client, "docker", "buildx", "rm", "--force", reference_builder)
            step = "managed images preserve reviewed identities"
            settings = dict(CI_FLEET_ENGINE_REF=revision, CI_FLEET_COMMIT=revision, CI_FLEET_VERSION=revision[:12],
                            CI_FLEET_RUNNER_IMAGE=prefix + "-managed-runner", CI_FLEET_CONTROLLER_IMAGE=prefix + "-managed-controller",
                            CI_FLEET_RUNNER_IMAGE_DIGEST=run("docker", "image", "inspect", "--format", "{{.Id}}", reference_runner),
                            CI_FLEET_CONTROLLER_IMAGE_DIGEST=run("docker", "image", "inspect", "--format", "{{.Id}}", reference_controller),
                            CI_FLEET_APT_HTTP_PROXY=proxy)
            images.extend([settings["CI_FLEET_RUNNER_IMAGE"], settings["CI_FLEET_CONTROLLER_IMAGE"]])
            environment = temporary / "managed.env"
            environment.write_text("".join(f"{key}={shlex.quote(value)}\n" for key, value in settings.items()))
            run("docker", "cp", str(environment), client + ":/managed.env")
            before = totals(cache)
            started = time.monotonic()
            run("docker", "exec", "--env", "BASH_ENV=/release/scripts/fixtures/apt-cache/trace-builds.sh",
                "--env", "CI_FLEET_BUILD_TRACE=/apt-build.log", client,
                "bash", "/release/scripts/build-managed-images.sh", "/release", "/managed.env")
            after = totals(cache)
            if after[1] <= before[1]:
                raise RuntimeError("APT_CACHE_E2E_FAILED step=managed image package traffic")
            # These builds use the cache for every APT stage and reject any identity mismatch.
            measurements.append(dict(path="managed-images", reviewed_identities="matched",
                                     elapsed_seconds=round(time.monotonic() - started, 2),
                                     upstream_package_bytes=after[0] - before[0], served_package_bytes=after[1] - before[1]))
            step = "cache outage and direct HTTPS"
            unreachable = temporary / "unreachable.conf"
            unreachable.write_text(apt_config("http://127.0.0.1:9"))
            run("docker", "cp", str(unreachable), client + ":/etc/apt/apt.conf.d/99-ci-fleet-cache")
            run("docker", "exec", client, "sh", "-ec", "if apt-get update >/dev/null 2>&1; then exit 1; fi", timeout=60)
            source = temporary / "https.list"
            source.write_text(f"deb https://snapshot.debian.org/archive/debian/{snapshot} trixie main\n")
            run("docker", "cp", str(source), client + ":/https.list")
            run("docker", "exec", client, "apt-get", "-o", "Dir::Etc::sourcelist=/https.list",
                "-o", "Dir::Etc::sourceparts=-", "update", timeout=120)
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            failure = str(exc) if isinstance(exc, RuntimeError) else f"APT_CACHE_E2E_FAILED step={step} timeout"
            if build_client:
                result = cleanup("docker", "exec", build_client, "tail", "-n", "200", "/apt-build.log")
                details = fixture_failure_details((result.stdout + result.stderr).encode(), max_lines=40)
                failure += f"\nAPT_CACHE_FIXTURE build\n{details}"
            if cache:
                for name, command in (
                    ("state", ["docker", "inspect", "--format", "{{.State.Status}}/{{.State.ExitCode}}/oom={{.State.OOMKilled}}", cache]),
                    ("server", ["docker", "logs", "--tail", "10", cache]),
                    ("errors", ["docker", "exec", cache, "tail", "-n", "10", "/var/log/apt-cacher-ng/apt-cacher.err"]),
                ):
                    result = cleanup(*command)
                    details = fixture_failure_details((result.stdout + result.stderr).encode())
                    failure += f"\nAPT_CACHE_FIXTURE {name}\n{details}"
            raise SystemExit(failure) from None
        finally:
            if build_client:
                # Builder registrations live in this fresh client, not in the host CLI.
                registered = cleanup("docker", "exec", build_client, "docker", "buildx", "ls", "--format", "{{.Name}}")
                for name in registered.stdout.splitlines():
                    if name.startswith("ci-fleet-managed-"):
                        builders.append((build_client, name))
                        containers.append("buildx_buildkit_" + name + "0")
            for owner, builder in builders:
                cleanup("docker", "exec", owner, "docker", "buildx", "rm", "--force", builder)
            for container in containers:
                cleanup("docker", "rm", "-f", "-v", container)
            for image in images:
                cleanup("docker", "image", "rm", image)
            remaining = cleanup("docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}")
            for tag in remaining.stdout.splitlines():
                if tag.startswith(prefix + "-"):
                    cleanup("docker", "image", "rm", tag)
            for name in containers:
                if cleanup("docker", "container", "inspect", name).returncode == 0:
                    raise SystemExit("APT_CACHE_E2E_FAILED step=owned container cleanup")
        print("APT_CACHE_E2E_OK " + json.dumps(measurements, sort_keys=True))


if __name__ == "__main__":
    main()
