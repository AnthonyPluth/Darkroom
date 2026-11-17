import json
import re
import unittest
from pathlib import Path
from xml.etree import ElementTree

from darkroom.domain.syncthing_version import TESTED_FROM, TESTED_UP_TO, named
from darkroom_init import plan

ROOT = Path(__file__).resolve().parent.parent.parent
SINGLE = (ROOT / "Dockerfile.single").read_text()
SINGLE_REQUIREMENTS = (ROOT / "deploy/single/requirements.txt").read_text()
APP_PYTHON = re.search(r"^FROM python:(\d+\.\d+)-slim AS deps$", (ROOT / "Dockerfile").read_text(), re.M).group(1)
SANDBOX = "--cap-drop=ALL --security-opt no-new-privileges:true --security-opt seccomp=deploy/browser/seccomp.json"
SINGLE_IMAGE = "ghcr.io/anthonypluth/darkroom-single"
UNRAID_SECCOMP = "/boot/config/darkroom/seccomp.json"
REMOVED_SETTINGS = ("DARKROOM_BROWSER_TOKEN", "DARKROOM_VNC_SECRET", "VNC_URL", "DARKROOM_URL")


def compose_service(name):
    compose = (ROOT / "docker-compose.yml").read_text()
    start = compose.index(f"\n  {name}:\n") + 1
    following = re.search(r"\n  (?:[a-z][a-z-]*:|# )", compose[start + 1:])
    return compose[start:start + 1 + following.start()] if following else compose[start:]


def requirement_blocks(requirements):
    blocks = [block for block in requirements.replace("\\\n", " ").splitlines() if block.strip()]
    names = [block.split()[0].split("==")[0] for block in blocks]
    if len(names) != len(set(names)):
        raise AssertionError("a requirement is listed twice")
    return dict(zip(names, blocks, strict=True))


def locked_package(name):
    lock = (ROOT / "poetry.lock").read_text()
    start = lock.index(f'[[package]]\nname = "{name}"\n')
    end = lock.find("[[package]]", start + 1)
    return lock[start:end if end != -1 else len(lock)]


class BrowserImageTests(unittest.TestCase):
    def test_a_requirement_listed_twice_is_not_hidden_from_the_hash_test(self):
        with self.assertRaises(AssertionError):
            requirement_blocks("pkg==1.0 --hash=sha256:aa\npkg==2.0\n")

    def test_both_images_and_compose_default_to_the_unraid_user_and_group(self):
        for name in ("Dockerfile", "Dockerfile.single"):
            contents = (ROOT / name).read_text()
            with self.subTest(image=name):
                self.assertIn("ARG PUID=99", contents)
                self.assertIn("ARG PGID=100", contents)
                self.assertIn('getent group "$PGID"', contents)
                self.assertIn('--uid "$PUID" --gid "$PGID"', contents)
        compose = (ROOT / "docker-compose.yml").read_text()
        self.assertEqual(compose.count('user: "${PUID:-99}:${PGID:-100}"'), 2)
        self.assertEqual(compose.count("PUID: ${PUID:-99}"), 2)
        self.assertEqual(compose.count("PGID: ${PGID:-100}"), 2)

    def test_the_seccomp_profile_allows_chromiums_namespace_and_chroot_sandbox(self):
        profile = json.loads((ROOT / "deploy/browser/seccomp.json").read_text())
        allowed = {name for rule in profile["syscalls"] if rule["action"] == "SCMP_ACT_ALLOW" and not rule.get("args") for name in rule["names"]}
        self.assertTrue({"clone", "unshare", "setns", "chroot", "seccomp", "prctl"} <= allowed)
        self.assertEqual(profile["defaultAction"], "SCMP_ACT_ERRNO")
        self.assertNotIn("mount", allowed)
        self.assertNotIn("ptrace", allowed)

    def test_ci_launches_a_real_renderer_under_the_deployment_policy(self):
        workflow = (ROOT / ".github/workflows/docker.yml").read_text()
        self.assertIn("--security-opt seccomp=deploy/browser/seccomp.json", workflow)
        self.assertIn("--cap-drop=ALL --security-opt no-new-privileges:true", workflow)
        self.assertIn("/app/smoke.sh", workflow)
        smoke = (ROOT / "deploy/browser/smoke.sh").read_text()
        self.assertIn("for channel in chrome chromium", smoke)
        self.assertIn("browser.content()", smoke)
        self.assertIn("chrome_launch_failed", smoke)

    def test_every_requirement_is_pinned_with_hashes(self):
        blocks = list(requirement_blocks(SINGLE_REQUIREMENTS).values())
        self.assertEqual({block.split()[0].split("==")[0] for block in blocks}, {"playwright", "greenlet"})
        for block in blocks:
            with self.subTest(block=block.split()[0]):
                self.assertRegex(block.split()[0], r"^[a-z][a-z0-9-]*==\d+\.\d+\.\d+$")
                self.assertRegex(block, r"--hash=sha256:[0-9a-f]{64}")

    def test_the_pyproject_extra_admits_the_pinned_version(self):
        pinned = re.search(r"^playwright==(\d+\.\d+\.\d+) ", SINGLE_REQUIREMENTS, re.M)
        version = tuple(int(part) for part in pinned.group(1).split("."))
        extra = re.search(r'browser = \["playwright \(>=(\d+\.\d+(?:\.\d+)?),<(\d+)\.0\)"\]', (ROOT / "pyproject.toml").read_text())
        self.assertIsNotNone(extra)
        floor = tuple(int(part) for part in extra.group(1).split("."))
        self.assertGreaterEqual(version, floor + (0,) * (3 - len(floor)))
        self.assertLess(version[0], int(extra.group(2)))

class SingleImageTests(unittest.TestCase):
    def test_the_worker_venv_pins_playwright_and_the_build_checks_that_version(self):
        pinned = re.search(r"^playwright==(\d+\.\d+\.\d+)", SINGLE_REQUIREMENTS, re.M)
        self.assertIsNotNone(pinned, "deploy/single/requirements.txt must pin playwright==<version>")
        version = pinned.group(1)
        self.assertIn("COPY deploy/single/requirements.txt /tmp/requirements.txt", SINGLE)
        self.assertIn("/opt/worker/bin/pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt", SINGLE)
        self.assertIn(f"metadata.version('playwright') == '{version}'", SINGLE)
        self.assertIn("import playwright.sync_api", SINGLE)

    def test_greenlet_is_poetry_locks_with_its_python_314_linux_wheels(self):
        locked = locked_package("greenlet")
        version = re.search(r'^version = "(\d+\.\d+\.\d+)"$', locked, re.M).group(1)
        block = requirement_blocks(SINGLE_REQUIREMENTS)["greenlet"]
        self.assertTrue(block.startswith(f"greenlet=={version} "))
        wheels = dict(re.findall(r'file = "(greenlet-[^"]+\.whl)", hash = "sha256:([0-9a-f]{64})"', locked))
        tag = "cp" + APP_PYTHON.replace(".", "")
        expected = {
            wheels[f"greenlet-{version}-{tag}-{tag}-manylinux_2_24_x86_64.manylinux_2_28_x86_64.whl"],
            wheels[f"greenlet-{version}-{tag}-{tag}-manylinux_2_24_aarch64.manylinux_2_28_aarch64.whl"],
        }
        self.assertEqual(set(re.findall(r"--hash=sha256:([0-9a-f]{64})", block)), expected)

    def test_the_repository_names_the_one_python_the_images_run(self):
        self.assertEqual(re.findall(r"^FROM (python:\S+)", (ROOT / "Dockerfile").read_text(), re.M), [f"python:{APP_PYTHON}-slim"] * 2)
        pyproject = (ROOT / "pyproject.toml").read_text()
        self.assertIn(f'requires-python = ">={APP_PYTHON},<4.0"', pyproject)
        self.assertIn(f'target-version = "py{APP_PYTHON.replace(".", "")}"', pyproject)
        self.assertIn(f'python_version = "{APP_PYTHON}"', pyproject)
        self.assertIn(f'default: "{APP_PYTHON}"', (ROOT / ".github/actions/setup-python-poetry/action.yml").read_text())
        self.assertIn(f"PYTHON_VERSION={APP_PYTHON}\n", (ROOT / ".claude/hooks/fleet.conf").read_text())
        self.assertIn(f'python-version: "{APP_PYTHON}"', (ROOT / ".github/workflows/control-smoke.yml").read_text())

    def test_the_worker_has_its_own_venv_on_the_apps_python(self):
        bases = re.findall(r"^FROM (\S+)", SINGLE, re.M)
        self.assertEqual(bases, ["node:22-slim", *[f"python:{APP_PYTHON}-slim"] * 4])
        self.assertIn("python -m venv /opt/worker", SINGLE)
        self.assertIn("COPY --from=worker-deps /opt/worker /opt/worker", SINGLE)
        self.assertIn("COPY --from=deps /app/.venv ./.venv", SINGLE)
        self.assertIn("COPY darkroom_browser ./darkroom_browser", SINGLE)
        self.assertIn("COPY deploy/browser/smoke.sh ./smoke.sh", SINGLE)
        self.assertIn("WORKDIR /opt/worker/app", SINGLE)

    def test_one_user_with_the_unraid_defaults_and_never_root(self):
        self.assertEqual(SINGLE.count("ARG PUID=99"), 1)
        self.assertEqual(SINGLE.count("ARG PGID=100"), 1)
        self.assertIn('if ! getent group "$PGID" >/dev/null; then groupadd --system --gid "$PGID" darkroom-share; fi', SINGLE)
        self.assertEqual(len(re.findall(r"\buseradd\b", SINGLE)), 1)
        self.assertIn('useradd --system --uid "$PUID" --gid "$PGID"', SINGLE)
        users = re.findall(r"^USER (\S+)$", SINGLE, re.M)
        self.assertEqual(users, ["darkroom"])
        runtime = SINGLE[SINGLE.index("AS runtime"):]
        self.assertLess(runtime.index("playwright install"), runtime.index("USER darkroom"), "browsers are installed as root")
        self.assertLess(runtime.index("useradd"), runtime.index("USER darkroom"))
        self.assertNotRegex(runtime[runtime.index("USER darkroom"):], r"(?m)^RUN ")

    def test_chrome_on_amd64_only_and_chromium_without_the_headless_shell(self):
        self.assertIn("/opt/worker/bin/python -m playwright install --with-deps --no-shell chromium", SINGLE)
        self.assertIn('if [ "$(dpkg --print-architecture)" = "amd64" ]; then /opt/worker/bin/python -m playwright install --with-deps chrome; fi', SINGLE)
        self.assertNotIn("firefox", SINGLE)
        self.assertNotIn("webkit", SINGLE)
        self.assertNotIn("--no-sandbox", SINGLE)
        self.assertNotRegex(SINGLE, r"curl[^\n]*\|\s*(ba)?sh")

    def test_the_playwright_driver_has_libatomic_before_it_runs(self):
        runtime = SINGLE[SINGLE.index("AS runtime"):]
        apt = runtime[runtime.index("apt-get install"):runtime.index("rm -rf /var/lib/apt/lists/*")]
        self.assertIn(" libatomic1 ", apt)
        self.assertLess(runtime.index("libatomic1"), runtime.index("playwright install"))

    def test_it_serves_the_app_on_8000_only_under_tini(self):
        self.assertEqual(re.findall(r"^EXPOSE .*$", SINGLE, re.M), ["EXPOSE 8000"])
        self.assertIn('ENTRYPOINT ["/usr/bin/tini", "--"]', SINGLE)
        self.assertIn("http://127.0.0.1:8000/healthz", SINGLE)

    def test_its_default_command_is_the_init_and_exec_still_starts_in_app(self):
        self.assertEqual(re.findall(r"^CMD .*$", SINGLE, re.M), ['CMD ["/usr/bin/env", "PYTHONPATH=/opt/init", "python", "-m", "darkroom_init"]'])
        self.assertEqual(re.findall(r"^WORKDIR (\S+)$", SINGLE, re.M)[-1], "/app")
        self.assertNotRegex(SINGLE, r"(?m)^ENV .*PYTHONPATH")

    def test_ci_checks_healthz_and_both_browsers_in_it_under_the_deployment_policy(self):
        workflow = (ROOT / ".github/workflows/docker.yml").read_text()
        job = workflow[workflow.index("\n  single:\n"):workflow.index("\n  publish:\n")]
        self.assertIn("file: Dockerfile.single", job)
        self.assertIn("tags: darkroom-single:smoke", job)
        run_lines = [line.strip() for line in job.splitlines() if line.strip().startswith("docker run")]
        self.assertEqual(len(run_lines), 4)
        healthz, smoke, copy, init = run_lines
        self.assertIn(SANDBOX, copy)
        self.assertIn("--tmpfs /photos:uid=99,gid=100 --tmpfs /data:uid=99,gid=100", copy)
        self.assertTrue(copy.endswith("-w /app darkroom-single:smoke bash /probe.sh"))
        self.assertIn('-v "$PWD/tests/browser/copy_under_profile.sh:/probe.sh:ro"', copy)
        self.assertIn(SANDBOX, healthz)
        self.assertIn("-p 8765:8000 darkroom-single:smoke", healthz)
        self.assertIn("curl -fsS http://127.0.0.1:8765/healthz", job)
        self.assertIn(SANDBOX, smoke)
        self.assertIn("--shm-size=1g", smoke)
        self.assertIn("-e PATH=/opt/worker/bin:", smoke)
        self.assertIn("test -x /opt/google/chrome/chrome && bash /opt/worker/app/smoke.sh", smoke)
        self.assertNotIn("platforms:", job)
        self.assertIn(f"{SANDBOX} --shm-size=1g", init)
        self.assertTrue(init.endswith("-p 8766:8000 darkroom-single:smoke"), "image CI runs the init through the image's default command")
        self.assertTrue(healthz.endswith("-p 8765:8000 darkroom-single:smoke"))
        self.assertIn("bash tests/browser/one_container.sh init 8766", job)
        self.assertIn('"child": "worker" "child": "viewer" "child": "vnc" "child": "display" "child": "app" ', job)

    def test_syncthing_is_the_pinned_release_verified_against_its_signed_checksums_at_build(self):
        stage = SINGLE[SINGLE.index("AS syncthing"):SINGLE.index("AS runtime")]
        self.assertIn("version=v2.1.7;", stage)
        self.assertEqual(named(TESTED_FROM), "v2.1.7")
        self.assertEqual(TESTED_FROM, TESTED_UP_TO)
        self.assertIn("amd64) sum=2edc416e7ed136b7c858ed5f3f02a3b8316589605e54c380228ceea7acbf0142 ;;", stage)
        self.assertIn("arm64) sum=48e456690cd2b97e7d26b02b5ac35f62a3b36172267b144cfeaf02c993ab955c ;;", stage)
        self.assertIn("2edc416e7ed136b7c858ed5f3f02a3b8316589605e54c380228ceea7acbf0142", (ROOT / "docs/spikes/syncthing-api.md").read_text())
        self.assertIn('"https://github.com/syncthing/syncthing/releases/download/$version/$name.tar.gz"', stage)
        self.assertLess(stage.index('sha256sum -c -'), stage.index("tar -xzf"))
        self.assertIn('*) echo "no pinned Syncthing for $arch" >&2; exit 1 ;;', stage)
        self.assertNotRegex(stage, r"(?m)^ARG ")
        runtime = SINGLE[SINGLE.index("AS runtime"):]
        self.assertLess(runtime.index("COPY --from=syncthing /syncthing /usr/local/bin/syncthing"), runtime.index("USER darkroom"))
        self.assertNotIn("8384", SINGLE)

    def test_ci_runs_syncthing_paused_against_a_peer_under_the_deployment_policy_and_checks_it_is_off_by_default(self):
        workflow = (ROOT / ".github/workflows/docker.yml").read_text()
        job = workflow[workflow.index("\n  single:\n"):workflow.index("\n  publish:\n")]
        self.assertIn("run: bash tests/browser/syncthing_paused.sh darkroom-single:smoke 8767", job)
        self.assertLess(job.index("bash tests/browser/one_container.sh init 8766\n          docker exec init test ! -e /data/syncthing"), job.index("syncthing_paused.sh"))
        script = (ROOT / "tests/browser/syncthing_paused.sh").read_text()
        self.assertIn("sandbox=(--cap-drop=ALL --security-opt no-new-privileges:true --security-opt seccomp=deploy/browser/seccomp.json)", script)
        self.assertEqual(script.count('"${sandbox[@]}"'), 2)
        self.assertIn("-e DARKROOM_SYNCTHING=1", script)
        self.assertNotRegex(script, r"-p [^ ]*(22000|21027|8384)")
        self.assertNotIn("--network host", script)
        for check in ("Syncthing is not the pinned release", "Syncthing did not start paused", "Syncthing's API answers from outside the container",
                      "Syncthing's API is reachable from another container", "the paused instance sent a file", "the peer got a different file",
                      "Syncthing's API key is in the container's log", "a restart did not pause the configured device and folder"):
            self.assertIn(check, script)
        self.assertLess(script.index("the paused instance sent a file"), script.index("probe darkroom resume"))

    def test_ci_runs_the_one_container_with_host_networking_and_checks_every_internal_listener_is_closed(self):
        workflow = (ROOT / ".github/workflows/docker.yml").read_text()
        job = workflow[workflow.index("\n  single:\n"):workflow.index("\n  publish:\n")]
        self.assertIn("run: bash tests/browser/host_network.sh darkroom-single:smoke\n", job)
        self.assertLess(job.index("syncthing_paused.sh"), job.index("host_network.sh"))
        script = (ROOT / "tests/browser/host_network.sh").read_text()
        self.assertIn("sandbox=(--cap-drop=ALL --security-opt no-new-privileges:true --security-opt seccomp=deploy/browser/seccomp.json)", script)
        self.assertIn('--network host "${sandbox[@]}"', script)
        self.assertIn("-e DARKROOM_SYNCTHING=1", script)
        self.assertNotRegex(script, r" -p ")
        for check in ("check refused 127.0.0.1 ::1", "sudo -u nobody python3 -I - refused", 'docker exec -i -u 4242:4242 "$name" python3 -I - inside', "sudo ss -Hltunp | check listeners",
                      "sudo ss -Hlxp | check unix-listeners", "probe relay", "syncthing reconciled"):
            self.assertIn(check, script)
        probe = (ROOT / "tests/browser/host_network.py").read_text()
        self.assertIn("INTERNAL_PORTS = (5900, 6080, 8384)", probe)
        self.assertIn("ALLOWED = {8000, 22000, 21027}", probe)
        self.assertIn(f'"{plan.WORKER_SOCKET}"', probe)
        self.assertIn('worker_route == 404', probe)
        smoke = (ROOT / "tests/browser/one_container.sh").read_text()
        self.assertIn('[ "$(worker_route)" = 404 ]', smoke)

    def test_the_worker_page_describes_the_internal_listeners_as_the_init_starts_them(self):
        page = (ROOT / "docs/deploy/browser.md").read_text()
        inside = page[page.index("## What runs inside"):page.index("## Google Chrome")]
        for socket_path in (plan.VNC_SOCKET, plan.VIEWER_SOCKET, plan.SYNCTHING_SOCKET, plan.WORKER_SOCKET):
            self.assertIn(f"`{socket_path}`", inside)
        for flag in ("-rfbport 0", "-rfbportv6 0", "-nolisten tcp -nolisten local", "mode `0700`", "tests/browser/host_network.sh"):
            self.assertIn(flag, inside)
        self.assertNotIn("-localhost", inside)
        self.assertNotRegex(inside, r"127\.0\.0\.1:6080|port 5900|127\.0\.0\.1:<DARKROOM_PORT>")

    def test_the_syncthing_script_reads_whole_logs_before_matching_and_starts_from_no_leftover_containers(self):
        workflow = (ROOT / ".github/workflows/docker.yml").read_text()
        scripts = {path.name: path.read_text() for path in sorted((ROOT / "tests/browser").glob("*.sh"))}
        self.assertLessEqual({"syncthing_paused.sh", "syncthing_peer.sh", "one_container.sh"}, set(scripts))
        for name, text in scripts.items():
            self.assertRegex(text, r"\A#!/usr/bin/env bash\nset -eu?o pipefail\n", name)
        for name, text in [*scripts.items(), ("docker.yml", workflow)]:
            self.assertNotRegex(text, r"\|\s*grep\s+(-[A-Za-z]*[qm]|--quiet|--silent|--max-count)",
                                f"{name}: grep -q quits at the first match, so under pipefail the producer dies of SIGPIPE and a match reads as a miss")
        peer = scripts["syncthing_peer.sh"]
        self.assertIn('logged_since_start() { local logs; logs=$(docker logs --since "$started" "$darkroom" 2>&1) && grep -qF "$1" <<<"$logs"; }', peer)
        self.assertIn('docker container inspect "$name" >/dev/null 2>&1 || continue', peer[peer.index("logs() {"):peer.index("fail() {")])
        leftovers = peer.index('docker rm -f "$darkroom" "$peer" >/dev/null 2>&1 || true\ndocker network rm "$net" >/dev/null 2>&1 || true\n', peer.index("trap cleanup EXIT"))
        self.assertLess(leftovers, peer.index('docker network create "$net"'))
        self.assertLess(peer.index('docker network create "$net"'), peer.index('docker run -d --name "$peer"'))
        script = scripts["syncthing_paused.sh"]
        self.assertIn('logged() { local logs; logs=$(docker logs "$1" 2>&1) && grep -qF "$2" <<<"$logs"; }', script)
        self.assertIn('docker container inspect "$name" >/dev/null 2>&1 || continue', script[script.index("fail() {"):script.index("wait_for() {")])
        cleanup = script.index("docker rm -f darkroom peer >/dev/null 2>&1 || true\ndocker network rm \"$net\" >/dev/null 2>&1 || true\n")
        self.assertLess(cleanup, script.index('docker network create "$net"'))
        self.assertLess(script.index('docker network create "$net"'), script.index("docker run -d --name darkroom"))

    def test_the_init_is_copied_in_and_opt_in_through_the_single_profile(self):
        self.assertLess(SINGLE.index("COPY darkroom_init /opt/init/darkroom_init"), SINGLE.index("USER darkroom"))
        single = compose_service("darkroom-single")
        for line in ("dockerfile: Dockerfile.single", f"image: {SINGLE_IMAGE}:${{DARKROOM_TAG:-latest}}", "profiles: [single]", "shm_size: 1g", "cap_drop: [ALL]", "- no-new-privileges:true",
                     "- seccomp:./deploy/browser/seccomp.json", "- ./data:/data"):
            self.assertIn(line, single)
        self.assertNotIn("working_dir:", single)
        self.assertNotIn("command:", single)
        self.assertFalse([name for name in REMOVED_SETTINGS if f"{name}:" in single])

    def test_the_one_container_runs_on_the_host_network_with_syncthing_on_and_its_web_port_on_loopback(self):
        single = compose_service("darkroom-single")
        self.assertRegex(single, r"(?m)^    network_mode: host$")
        self.assertNotRegex(single, r"(?m)^    ports:", "compose ignores published ports on the host network; the web port's address is DARKROOM_HOST")
        self.assertRegex(single, r'(?m)^      DARKROOM_HOST: "127\.0\.0\.1"$', "the web port stays on the host's loopback, as the published 127.0.0.1:8000 was")
        self.assertRegex(single, r"(?m)^      DARKROOM_SYNCTHING: \$\{DARKROOM_SYNCTHING:-1\}$")
        app = compose_service("darkroom")
        self.assertNotIn("network_mode", app)
        self.assertIn('- "127.0.0.1:8000:8000"', app)
        self.assertNotIn("DARKROOM_SYNCTHING", app)

    def test_the_postgresql_recipe_reaches_the_host_network_service_on_loopback_only(self):
        compose = (ROOT / "docker-compose.yml").read_text()
        recipe = compose[compose.index("  # PostgreSQL instead of the default SQLite"):]
        self.assertIn("DATABASE_URL=postgresql://darkroom:CHANGE-ME@127.0.0.1:5432/darkroom", recipe)
        self.assertIn("DATABASE_URL=postgresql://darkroom:CHANGE-ME@postgres:5432/darkroom", recipe)
        published = re.findall(r'^  #     - "([^"]+:5432)"$', recipe, re.M)
        self.assertEqual(published, ["127.0.0.1:5432:5432"], "PostgreSQL is published on the host's loopback only, never on every address")
        page = (ROOT / "docs/deploy/postgres.md").read_text()
        self.assertIn("@127.0.0.1:5432/darkroom", page)

    def test_the_one_container_comes_first_and_no_separate_browser_container_is_left(self):
        compose = (ROOT / "docker-compose.yml").read_text()
        self.assertLess(compose.index("  darkroom-single:"), compose.index("  darkroom:"))
        self.assertIn("docker compose --profile single up -d darkroom-single", compose[:compose.index("  darkroom-single:")])
        self.assertEqual(re.findall(r"^  ([a-z][a-z-]*):$", compose, re.M), ["darkroom-single", "darkroom"])
        app = compose_service("darkroom")
        self.assertNotIn("profiles:", app, "a plain `docker compose up -d` still starts the app alone, as before")
        self.assertIn("image: ghcr.io/anthonypluth/darkroom:${DARKROOM_TAG:-latest}", app)
        for name in ("darkroom-browser", "Dockerfile.browser", "profiles: [browser]", "6080", *REMOVED_SETTINGS):
            with self.subTest(name=name):
                self.assertNotIn(name, compose)
        self.assertFalse((ROOT / "Dockerfile.browser").exists())
        self.assertFalse((ROOT / "deploy/browser/entrypoint.sh").exists())
        self.assertFalse((ROOT / "deploy/browser/requirements.txt").exists())

    def test_ci_validates_both_compose_services_and_publishes_the_two_images(self):
        workflow = (ROOT / ".github/workflows/docker.yml").read_text()
        for command in ("docker compose --profile single config --quiet", "docker compose config --quiet"):
            self.assertIn(command, workflow)
        for gone in ("--profile browser", "Dockerfile.browser", "darkroom-browser", "BROWSER_IMAGE", "browser_image"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, workflow)
        publish = workflow[workflow.index("\n  publish:\n"):]
        self.assertIn("needs: [build, single]", publish)
        self.assertIn('echo "SINGLE_IMAGE=ghcr.io/${GITHUB_REPOSITORY_OWNER,,}/darkroom-single" >> "$GITHUB_ENV"', publish)
        self.assertIn("for name in IMAGE SINGLE_IMAGE; do", publish)
        steps = publish.split("      - name: ")
        single = [step for step in steps if "file: Dockerfile.single" in step]
        self.assertEqual(len(single), 1)
        self.assertIn("platforms: linux/amd64\n", single[0])
        self.assertIn("tags: ${{ steps.tags.outputs.single_image }}", single[0])
        self.assertEqual(len([step for step in steps if "docker/build-push-action@" in step]), 2)
        self.assertEqual(len([step for step in steps if "tags: ${{ steps.tags.outputs.image }}" in step]), 1)
        build = workflow[workflow.index("\n  build:\n"):workflow.index("\n  single:\n")]
        self.assertEqual(build.count("docker/build-push-action@"), 1)


def template():
    return ElementTree.parse(ROOT / "deploy/unraid/darkroom.xml").getroot()


def fleet_security_calls():
    workflow = (ROOT / ".github/workflows/security.yml").read_text()
    jobs = re.split(r"\n  (?=[a-z][a-z-]*:\n)", workflow[workflow.index("\njobs:\n"):])[1:]
    return [job for job in jobs if "uses: AnthonyPluth/fleet/.github/workflows/security.yml@" in job]


def call_input(call, name):
    return re.search(rf"^      {name}: (\S+)$", call, re.M).group(1)


class SecurityScanTests(unittest.TestCase):
    def test_every_image_gets_its_own_call_of_the_same_fleet_release_with_the_same_permissions(self):
        calls = fleet_security_calls()
        self.assertEqual(len(calls), 2)
        service, single = calls
        self.assertNotIn("dockerfile:", service)
        self.assertEqual(call_input(single, "dockerfile"), "Dockerfile.single")
        pins = {re.search(r"security\.yml@([0-9a-f]{40})", call).group(1) for call in calls}
        self.assertEqual(len(pins), 1, "every call runs the same fleet release")
        permissions = {call[call.index("    permissions:\n"):call.index("    uses:")] for call in calls}
        self.assertEqual(len(permissions), 1)

    def test_trivy_scans_the_one_container_image_as_it_scans_the_service_image(self):
        _, single = fleet_security_calls()
        image_paths = call_input(single, "image-paths")
        for changed in ("Dockerfile.single", "deploy/single/requirements.txt", "poetry.lock", "pyproject.toml", ".dockerignore"):
            self.assertRegex(changed, image_paths)
        self.assertIn("      sarif-upload: false\n", single)
        self.assertEqual(call_input(single, "code-paths"), "^$")

    def test_no_call_scans_the_removed_browser_image(self):
        workflow = (ROOT / ".github/workflows/security.yml").read_text()
        self.assertNotIn("Dockerfile.browser", workflow)
        self.assertNotIn("browser-image:", workflow)
        for call in fleet_security_calls():
            with self.subTest(call=call.split(":")[0]):
                self.assertNotRegex("Dockerfile.browser", call_input(call, "image-paths"))
                self.assertNotRegex("deploy/browser/entrypoint.sh", call_input(call, "image-paths"))

    def test_only_the_one_container_image_scan_names_the_accepted_findings(self):
        service, single = fleet_security_calls()
        self.assertEqual(call_input(single, "trivy-image-ignore-file"), ".trivyignore.yaml")
        self.assertNotIn("trivy-image-ignore-file:", service)

    def test_every_list_of_accepted_findings_is_owned_reviewed_and_explained(self):
        named = [call_input(call, "trivy-image-ignore-file") for call in fleet_security_calls() if "      trivy-image-ignore-file:" in call]
        self.assertTrue(named)
        owners = (ROOT / ".github/CODEOWNERS").read_text().splitlines()
        review = (ROOT / ".github/workflows/agent-review.yml").read_text()
        second_review = review[review.index("      second-review-paths: |\n"):].split("\n    secrets:", 1)[0].splitlines()[1:]
        security = (ROOT / "SECURITY.md").read_text().splitlines()
        for name in named:
            with self.subTest(name):
                self.assertNotIn("/", name, "the list sits at the repository's root")
                self.assertTrue((ROOT / name).is_file())
                self.assertIn(f"        {name}", second_review)
                self.assertIn(f"/{name} @AnthonyPluth", owners)
                self.assertIn("## Accepted scanner findings", security)


class UnraidTemplateTests(unittest.TestCase):
    def test_it_runs_the_published_one_container_image_unprivileged_on_the_host_network(self):
        root = template()
        self.assertEqual(root.findtext("Repository"), f"{SINGLE_IMAGE}:latest")
        self.assertEqual(root.findtext("Privileged"), "false")
        self.assertEqual(root.findtext("Network"), "host")
        self.assertIn("never forward them", root.findtext("Overview"))
        self.assertEqual(root.findtext("PostArgs") or "", "")

    def test_its_extra_parameters_are_the_compose_sandbox(self):
        params = template().findtext("ExtraParams").split()
        self.assertEqual(sorted(params), sorted(["--user=99:100", "--shm-size=1g", "--cap-drop=ALL", "--security-opt", "no-new-privileges:true", "--security-opt",
                                                     f"seccomp={UNRAID_SECCOMP}", "--stop-timeout=30"]))
        self.assertNotIn("--privileged", params)
        self.assertFalse([part for part in params if "unconfined" in part or "no-sandbox" in part or part.startswith("--cap-add")])
        single = compose_service("darkroom-single")
        for line in ('user: "${PUID:-99}:${PGID:-100}"', "shm_size: 1g", "stop_grace_period: 30s", "cap_drop: [ALL]", "- no-new-privileges:true"):
            self.assertIn(line, single)

    def test_the_seccomp_profile_lives_outside_every_mapped_folder(self):
        paths = [config.get("Default") or config.text or "" for config in template().iter("Config") if config.get("Type") == "Path"]
        self.assertFalse([path for path in paths if path and UNRAID_SECCOMP.startswith(path.rstrip("/") + "/")])
        self.assertTrue(UNRAID_SECCOMP.startswith("/boot/config/"))

    def test_its_ports_are_8000_and_syncthings_and_it_maps_appdata_and_the_share(self):
        configs = list(template().iter("Config"))
        ports = [(config.get("Target"), config.get("Mode"), config.text) for config in configs if config.get("Type") == "Port"]
        self.assertEqual(ports, [("8000", "tcp", "8000"), ("22000", "tcp", "22000"), ("22000", "udp", "22000"), ("21027", "udp", "21027")])
        self.assertFalse([port for port in ports if port[0] in ("5900", "6080", "8384")])
        paths = {config.get("Target"): (config.get("Mode"), config.get("Required"), config.get("Default")) for config in configs if config.get("Type") == "Path"}
        self.assertEqual(paths, {"/data": ("rw", "true", "/mnt/user/appdata/darkroom"), "/photos": ("rw", "true", "")})

    def test_its_variables_are_the_compose_services_and_none_of_the_fallbacks(self):
        configs = [config for config in template().iter("Config") if config.get("Type") == "Variable"]
        names = [config.get("Target") for config in configs]
        self.assertEqual(len(names), len(set(names)))
        environment = set(re.findall(r"^      ([A-Z_]+): ", compose_service("darkroom-single"), re.M))
        self.assertEqual(set(names), environment - {"TZ", "DARKROOM_ONE_CONTAINER", "DARKROOM_HOST"})
        self.assertNotIn("DARKROOM_HOST", names, "on Unraid the image's 0.0.0.0 stands: bridge networking already published 8000 on every address")
        syncthing = next(config for config in configs if config.get("Target") == "DARKROOM_SYNCTHING")
        self.assertEqual((syncthing.get("Default"), syncthing.text), ("1", "1"))
        self.assertRegex(SINGLE, r"(?m)^ENV .*\bDARKROOM_ONE_CONTAINER=1\b", "the image marks itself, so the template and `docker exec` need no variable for it")
        self.assertFalse(set(names) & set(REMOVED_SETTINGS))
        masked = {config.get("Target") for config in configs if config.get("Mask") == "true"}
        self.assertEqual(masked, {"OIDC_CLIENT_SECRET", "IMMICH_ADMIN_KEY", "DARKROOM_SECRET_KEY"})
        self.assertFalse([config.get("Target") for config in configs if (config.text or "") != (config.get("Default") or "")])

    def test_the_deploy_page_pairs_the_image_with_the_seccomp_profile_of_one_release(self):
        page = (ROOT / "docs/deploy/one-container.md").read_text()
        self.assertIn("printf 'DARKROOM_TAG=v1.2.3\\nPHOTOS_ROOT=/path/to/the/photo/share\\n' >> .env", page)
        self.assertIn("git checkout v1.2.3", page)
        self.assertNotIn("export DARKROOM_TAG", page, "a tag exported in one shell is gone for the next command: it goes in .env")
        self.assertIn("Change **Repository** from `ghcr.io/anthonypluth/darkroom-single:latest`", page)
        self.assertRegex((ROOT / ".env.example").read_text(), r"(?m)^# DARKROOM_TAG=$")
        self.assertIn("set Repository to that release's tag", template().findtext("Overview"))

    def test_every_service_takes_its_image_tag_from_darkroom_tag(self):
        compose = (ROOT / "docker-compose.yml").read_text()
        images = re.findall(r"^    image: (\S+)$", compose, re.M)
        self.assertEqual(images, [f"ghcr.io/anthonypluth/{name}:${{DARKROOM_TAG:-latest}}" for name in ("darkroom-single", "darkroom")])

    def test_the_deploy_pages_say_the_browser_container_is_gone_and_rollback_is_pinning_the_previous_release(self):
        page = (ROOT / "docs/deploy/one-container.md").read_text()
        back = page[page.index("## Going back to the two containers"):page.index("## Backup and restore")]
        self.assertIn("Rollback is pinning the previous release", back)
        self.assertIn("newest `v3` release", back)
        move = page[page.index("## Moving from the two containers"):page.index("## Going back to the two containers")]
        self.assertIn("newest `v3` release", move)
        for name in ("docs/deploy/one-container.md", "docs/deploy/browser.md", "docs/runbook.md", ".env.example"):
            text = (ROOT / name).read_text()
            with self.subTest(page=name):
                self.assertNotIn("--profile browser", text)
                self.assertNotIn("darkroom-browser:", text)
                self.assertNotIn("Dockerfile.browser", text)
                self.assertNotRegex(text, r"(?m)^(DARKROOM_BROWSER_TOKEN|VNC_URL|DARKROOM_URL|DARKROOM_VNC_SECRET)=")

    def test_the_deploy_page_keeps_the_first_run_setup_to_the_runbooks_conditions(self):
        page = (ROOT / "docs/deploy/one-container.md").read_text()
        self.assertNotIn("LAN address only", page)
        self.assertIn("every address the server has", page)
        self.assertIn("unless `DARKROOM_WIZARD=1` is set", page)
        self.assertNotIn("DARKROOM_WIZARD", {config.get("Target") for config in template().iter("Config")})

    def test_the_deploy_page_names_the_templates_seccomp_path_and_the_compose_command(self):
        page = (ROOT / "docs/deploy/one-container.md").read_text()
        self.assertIn(UNRAID_SECCOMP, page)
        self.assertIn("docker compose --profile single up -d darkroom-single", page)
        worker_page = (ROOT / "docs/deploy/browser.md").read_text()
        self.assertIn("python run.py copy-browser-state --source /photos/_browser --target /data/browser", worker_page)
        self.assertIn("python run.py copy-browser-state --source /data/browser --target /photos/_browser --set-aside", worker_page)
        self.assertIn("browser.md#copying-browser-profiles-copy-browser-state", page)
        self.assertIn(f"--security-opt seccomp={UNRAID_SECCOMP}", worker_page)


if __name__ == "__main__":
    unittest.main()
