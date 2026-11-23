"""sonar_ready.py: Sonar is a precondition of the audit, never an optional layer.

Every test drives the real decision code through a fake environment: a fake
`docker` CLI, a fake Sonar HTTP API and a fake clock. Each one asserts the
outcome the audit acts on -- ready with a host and a token file, or a stop
with a reason and the one command that fixes it.
"""

import base64
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from helpers import SCRIPTS
import importlib.util

_spec = importlib.util.spec_from_file_location("sonar_ready", SCRIPTS / "sonar_ready.py")
SR = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(SR)

HOST = "http://localhost:9000"
TOKEN = "squ_" + "a" * 36


class FakeEnv:
    """docker + Sonar + clock. `docker_up_after` is the fake-time second at
    which `docker info` starts answering after `open -a Docker`."""

    def __init__(self, *, docker=True, docker_running=True, can_open=True,
                 docker_up_after=10, containers=(), platform="darwin",
                 sonar_up_after=20, port_taken=False, tokens_valid=(TOKEN,),
                 admin_password="admin", env=None, local_image="sha:old",
                 registry_image="sha:old", pull_fails=False, migration_fails=False,
                 needs_migration=False):
        self.docker = docker
        self.docker_running = docker_running
        self.can_open = can_open
        self.docker_up_after = docker_up_after
        self.containers = [dict(c) for c in containers]
        self.platform = platform
        self.sonar_up_after = sonar_up_after
        self.port_taken = port_taken
        self.tokens_valid = set(tokens_valid)
        self.admin_password = admin_password
        self.env = dict(env or {})
        self.local_image = local_image
        self.registry_image = registry_image
        self.pull_fails = pull_fails
        self.migration_fails = migration_fails
        self.needs_migration = needs_migration
        self.migrate_called = False
        self.volume_copies = []
        self.machine = "arm64"
        self.downloads = []
        for c in self.containers:
            c.setdefault("ImageId", local_image)
        self.t = 0.0
        self.opened_at = None
        self.sonar_started_at = None
        if any(c["State"] == "running" and "sonarqube" in c["Image"] for c in self.containers):
            self.sonar_started_at = -1000.0
        self.calls = []
        self.minted = []

    # clock
    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds

    def which(self, name):
        return "/usr/local/bin/docker" if (name == "docker" and self.docker) else None

    def _docker_ok(self):
        if self.docker_running:
            return True
        return self.opened_at is not None and self.t - self.opened_at >= self.docker_up_after

    def run(self, argv):
        self.calls.append(list(argv))
        if argv[:3] == ["open", "-a", "Docker"]:
            if not self.can_open:
                return 1, "", "Unable to find application named 'Docker'"
            if self.opened_at is None:
                self.opened_at = self.t
            return 0, "", ""
        if argv[0] != "docker":
            return 127, "", "unexpected"
        if not self._docker_ok():
            return 1, "", "Cannot connect to the Docker daemon"
        sub = argv[1]
        if sub == "info":
            return 0, "Server Version: 28", ""
        if sub == "ps":
            return 0, "\n".join(json.dumps(c) for c in self.containers), ""
        if sub == "start":
            for c in self.containers:
                if c["Names"] == argv[2]:
                    c["State"] = "running"
                    self.sonar_started_at = self.t
            return 0, argv[2], ""
        if sub == "run" and "--rm" in argv:
            vols = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
            self.volume_copies.append((vols[0].split(":")[0], vols[1].split(":")[0]))
            return 0, "", ""
        if sub == "run":
            if self.port_taken:
                return 125, "", "Bind for 0.0.0.0:9000 failed: port is already allocated"
            name = argv[argv.index("--name") + 1]
            image = argv[-1]
            image_id = self.local_image if image == "sonarqube:community" else image
            self.containers.append({"Names": name, "Image": image, "State": "running",
                                    "Ports": "0.0.0.0:9000->9000/tcp", "ImageId": image_id})
            self.running_new = image_id == self.registry_image and image_id != "sha:old"
            self.sonar_started_at = self.t
            return 0, "abc123", ""
        if sub == "pull":
            if self.pull_fails:
                return 1, "", "network unreachable"
            self.local_image = self.registry_image
            return 0, "", ""
        if argv[1:3] == ["image", "inspect"]:
            return (0, self.local_image + "\n", "") if self.local_image else (1, "", "no image")
        if sub == "inspect":
            c = next((c for c in self.containers if c["Names"] == argv[-1]), None)
            if c is None:
                return 1, "", "no such container"
            if "{{.Image}}" in argv:
                return 0, c["ImageId"] + "\n", ""
            return 0, json.dumps(c.get("Env", [])), ""
        if sub == "stop":
            return 0, argv[2], ""
        if sub == "rm":
            self.containers = [c for c in self.containers if c["Names"] != argv[2]]
            self.sonar_started_at = None
            return 0, argv[2], ""
        if sub == "logs":
            return 0, "ERROR es bootstrap", ""
        return 1, "", "unknown docker command"

    def _sonar_up(self):
        return self.sonar_started_at is not None and self.t - self.sonar_started_at >= self.sonar_up_after

    def http(self, method, url, *, token=None, basic=None, form=None):
        self.calls.append([method, url])
        if not self._sonar_up():
            if self.sonar_started_at is None:
                return None, None
            return 200, {"status": "STARTING"}
        if url.startswith("https://api.github.com"):
            return 200, {"tag_name": "8.1.0.5000"}
        path = url[len(HOST):]
        upgraded = getattr(self, "running_new", False)
        if path == "/api/system/migrate_db":
            self.migrate_called = True
            return 200, {}
        if path == "/api/system/status" and upgraded and self.needs_migration:
            if not self.migrate_called:
                return 200, {"status": "DB_MIGRATION_NEEDED"}
            if self.migration_fails:
                return 200, {"status": "DOWN"}
        if path == "/api/system/status":
            return 200, {"status": "UP", "version": "26.8.0"}
        if path == "/api/authentication/validate":
            ok = (token in self.tokens_valid) or (basic is not None and basic == ("admin", self.admin_password))
            return 200, {"valid": bool(ok)}
        user, password = basic if basic else (None, None)
        if basic is None or user != "admin" or password != self.admin_password:
            return 401, {}
        if path == "/api/users/change_password":
            self.admin_password = form["password"]
            return 204, {}
        if path == "/api/user_tokens/generate":
            new = "squ_" + "b" * 36
            self.tokens_valid.add(new)
            self.minted.append(form["name"])
            return 200, {"token": new}
        return 404, {}


SONAR_RUNNING = {"Names": "sonarqube", "Image": "sonarqube:community", "State": "running",
                 "Ports": "0.0.0.0:9000->9000/tcp"}
SONAR_STOPPED = dict(SONAR_RUNNING, State="exited")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conf = Path(self.tmp.name) / "code-health"
        self.legacy = Path(self.tmp.name) / "sonar-code-health"

    def write_token(self, value=TOKEN):
        self.conf.mkdir(parents=True, exist_ok=True)
        (self.conf / "sonar-token").write_text(value + "\n")

    def ready(self, env):
        return SR.ensure_ready(env, host=HOST, config_dir=self.conf, legacy_dir=self.legacy,
                               docker_wait=180, sonar_wait=300, environ=env.env)


class DockerTests(Base):
    def test_no_docker_cli_stops_with_install_hint(self):
        env = FakeEnv(docker=False)
        with self.assertRaises(SR.NotReady) as cm:
            self.ready(env)
        self.assertIn("Docker", cm.exception.reason)
        self.assertTrue(cm.exception.fix)
        self.assertNotIn(["open", "-a", "Docker"], env.calls)

    def test_docker_stopped_on_macos_is_started_and_awaited(self):
        self.write_token()
        env = FakeEnv(docker_running=False, containers=[SONAR_RUNNING], docker_up_after=30)
        result = self.ready(env)
        self.assertIn(["open", "-a", "Docker"], env.calls)
        self.assertIn("docker_started", result["actions"])
        self.assertEqual(result["status"], "ready")

    def test_docker_never_comes_up_stops_after_bounded_wait(self):
        env = FakeEnv(docker_running=False, docker_up_after=10_000)
        with self.assertRaises(SR.NotReady) as cm:
            self.ready(env)
        self.assertLessEqual(env.t, 180 + 10)
        self.assertIn("open -a Docker", cm.exception.fix)

    def test_docker_app_missing_on_macos_stops(self):
        env = FakeEnv(docker_running=False, can_open=False)
        with self.assertRaises(SR.NotReady) as cm:
            self.ready(env)
        self.assertIn("Docker", cm.exception.reason)

    def test_docker_stopped_on_linux_is_not_started_by_us(self):
        env = FakeEnv(docker_running=False, platform="linux")
        with self.assertRaises(SR.NotReady) as cm:
            self.ready(env)
        self.assertNotIn(["open", "-a", "Docker"], env.calls)
        self.assertIn("systemctl", cm.exception.fix)


class ContainerTests(Base):
    def test_running_container_is_reused(self):
        self.write_token()
        env = FakeEnv(containers=[SONAR_RUNNING])
        result = self.ready(env)
        self.assertEqual(result["container"], "sonarqube")
        self.assertFalse(any(c[:2] in (["docker", "run"], ["docker", "start"]) for c in env.calls))

    def test_stopped_container_is_started_not_duplicated(self):
        self.write_token()
        env = FakeEnv(containers=[SONAR_STOPPED])
        result = self.ready(env)
        self.assertIn(["docker", "start", "sonarqube"], env.calls)
        self.assertFalse(any(c[:2] == ["docker", "run"] for c in env.calls))
        self.assertIn("container_started", result["actions"])

    def test_missing_container_is_created_with_persistent_volumes(self):
        self.write_token()
        env = FakeEnv(containers=[{"Names": "pg", "Image": "postgres:15", "State": "running",
                                   "Ports": "5432/tcp"}])
        result = self.ready(env)
        run = next(c for c in env.calls if c[:2] == ["docker", "run"])
        for volume in ("sonarqube_data:/opt/sonarqube/data",
                       "sonarqube_extensions:/opt/sonarqube/extensions",
                       "sonarqube_logs:/opt/sonarqube/logs"):
            self.assertIn(volume, run)
        self.assertEqual(run[-1], "sonarqube:community")
        self.assertIn("container_created", result["actions"])

    def test_name_taken_by_other_container_picks_another_name(self):
        self.write_token()
        env = FakeEnv(containers=[{"Names": "sonarqube", "Image": "nginx", "State": "running",
                                   "Ports": "80/tcp"}])
        result = self.ready(env)
        self.assertNotEqual(result["container"], "sonarqube")

    def test_port_taken_stops_with_reason(self):
        env = FakeEnv(containers=[], port_taken=True)
        with self.assertRaises(SR.NotReady) as cm:
            self.ready(env)
        self.assertIn("9000", cm.exception.reason)

    def test_server_never_up_stops_with_logs_command(self):
        env = FakeEnv(containers=[SONAR_STOPPED], sonar_up_after=10_000)
        with self.assertRaises(SR.NotReady) as cm:
            self.ready(env)
        self.assertLessEqual(env.t, 300 + 10)
        self.assertIn("docker logs", cm.exception.fix)


class TokenTests(Base):
    def test_env_token_wins_and_no_file_is_reported(self):
        env = FakeEnv(containers=[SONAR_RUNNING], env={"SONAR_TOKEN": TOKEN})
        result = self.ready(env)
        self.assertEqual(result["token_source"], "env")
        self.assertIsNone(result["token_file"])

    def test_token_file_is_used(self):
        self.write_token()
        result = self.ready(FakeEnv(containers=[SONAR_RUNNING]))
        self.assertEqual(result["token_file"], str(self.conf / "sonar-token"))

    def test_legacy_token_file_is_used(self):
        self.legacy.mkdir()
        (self.legacy / "token").write_text(TOKEN)
        result = self.ready(FakeEnv(containers=[SONAR_RUNNING]))
        self.assertEqual(result["token_file"], str(self.legacy / "token"))

    def test_fresh_server_rotates_admin_and_mints_token_into_private_files(self):
        env = FakeEnv(containers=[SONAR_RUNNING], tokens_valid=())
        result = self.ready(env)
        token_file = Path(result["token_file"])
        self.assertTrue(env.minted)
        self.assertIn(token_file.read_text().strip(), env.tokens_valid)
        self.assertNotEqual(env.admin_password, "admin")
        self.assertEqual((self.conf / "admin_password").read_text().strip(), env.admin_password)
        for f in (token_file, self.conf / "admin_password"):
            self.assertEqual(stat.S_IMODE(os.stat(f).st_mode), 0o600)
        self.assertIn("token_minted", result["actions"])

    def test_invalid_token_with_stored_admin_password_mints_new_token(self):
        self.write_token("squ_dead")
        self.conf.joinpath("admin_password").write_text("S3cret!pass")
        env = FakeEnv(containers=[SONAR_RUNNING], tokens_valid=(), admin_password="S3cret!pass")
        result = self.ready(env)
        self.assertIn(Path(result["token_file"]).read_text().strip(), env.tokens_valid)

    def test_no_way_to_authenticate_stops_without_leaking_secrets(self):
        self.write_token("squ_dead")
        env = FakeEnv(containers=[SONAR_RUNNING], tokens_valid=(), admin_password="unknown!1")
        with self.assertRaises(SR.NotReady) as cm:
            self.ready(env)
        self.assertNotIn("squ_dead", cm.exception.reason + cm.exception.fix)
        self.assertIn("sonar-token", cm.exception.fix)

    def test_result_never_contains_a_token(self):
        env = FakeEnv(containers=[SONAR_RUNNING], tokens_valid=())
        result = self.ready(env)
        dumped = json.dumps(result)
        for token in env.tokens_valid:
            self.assertNotIn(token, dumped)


SONAR_OLD = dict(SONAR_RUNNING, ImageId="sha:old")


class UpgradeTests(Base):
    def test_newer_image_recreates_container_on_same_volumes_with_backup(self):
        self.write_token()
        env = FakeEnv(containers=[SONAR_OLD], registry_image="sha:new")
        result = self.ready(env)
        self.assertIn("server_upgraded", result["actions"])
        self.assertIn(("sonarqube_data", SR.BACKUP_VOLUME), env.volume_copies)
        run = [c for c in env.calls if c[:2] == ["docker", "run"] and "--rm" not in c][-1]
        self.assertIn("sonarqube_data:/opt/sonarqube/data", run)
        self.assertEqual(env.containers[-1]["ImageId"], "sha:new")

    def test_migration_is_requested_when_the_server_asks(self):
        self.write_token()
        env = FakeEnv(containers=[SONAR_OLD], registry_image="sha:new", needs_migration=True)
        result = self.ready(env)
        self.assertTrue(env.migrate_called)
        self.assertIn("db_migrated", result["actions"])

    def test_failed_migration_restores_data_and_old_version(self):
        self.write_token()
        env = FakeEnv(containers=[SONAR_OLD], registry_image="sha:new", needs_migration=True,
                      migration_fails=True)
        result = self.ready(env)
        self.assertIn((SR.BACKUP_VOLUME, "sonarqube_data"), env.volume_copies)
        self.assertEqual(env.containers[-1]["ImageId"], "sha:old")
        self.assertNotIn("server_upgraded", result["actions"])
        self.assertTrue(any("прежнюю" in n for n in result["notes"]))

    def test_same_image_is_not_recreated(self):
        self.write_token()
        env = FakeEnv(containers=[SONAR_OLD])
        self.ready(env)
        self.assertFalse(any(c[:2] == ["docker", "rm"] for c in env.calls))

    def test_failed_pull_keeps_current_server_and_says_so(self):
        self.write_token()
        env = FakeEnv(containers=[SONAR_OLD], pull_fails=True)
        result = self.ready(env)
        self.assertEqual(result["status"], "ready")
        self.assertTrue(result["notes"])
        self.assertFalse(any(c[:2] == ["docker", "rm"] for c in env.calls))

    def test_external_database_is_never_recreated(self):
        self.write_token()
        row = dict(SONAR_OLD, Env=["SONAR_JDBC_URL=jdbc:postgresql://db/sonar"])
        env = FakeEnv(containers=[row], registry_image="sha:new")
        self.ready(env)
        self.assertFalse(any(c[:2] == ["docker", "rm"] for c in env.calls))


class ScannerTests(Base):
    def make_env(self):
        env = FakeEnv(containers=[SONAR_RUNNING])

        def download(url, dest):
            env.downloads.append(url)
            import zipfile
            with zipfile.ZipFile(dest, "w") as zf:
                zf.writestr("sonar-scanner-8.1.0.5000-macosx-aarch64/bin/sonar-scanner", "#!/bin/sh\n")
            return True
        env.download = download
        return env

    def ready_with_scanner(self, env):
        return SR.ensure_ready(env, host=HOST, config_dir=self.conf, legacy_dir=self.legacy,
                               docker_wait=180, sonar_wait=300, environ=env.env,
                               scanner_cache=Path(self.tmp.name) / "scanner")

    def test_latest_native_scanner_is_downloaded_for_this_platform(self):
        self.write_token()
        env = self.make_env()
        result = self.ready_with_scanner(env)
        self.assertEqual(result["scanner_version"], "8.1.0.5000")
        self.assertIn("macosx-aarch64", env.downloads[0])
        self.assertTrue(os.access(result["scanner"], os.X_OK))

    def test_cached_latest_scanner_is_not_downloaded_again(self):
        self.write_token()
        env = self.make_env()
        self.ready_with_scanner(env)
        self.ready_with_scanner(env)
        self.assertEqual(len(env.downloads), 1)

    def test_unreachable_download_without_cache_stops(self):
        self.write_token()
        env = FakeEnv(containers=[SONAR_RUNNING])
        env.download = lambda url, dest: False
        with self.assertRaises(SR.NotReady):
            self.ready_with_scanner(env)


class CliTests(Base):
    def test_failure_prints_reason_and_fix_and_exits_1(self):
        env = FakeEnv(docker=False)
        code, out, err = SR.main_with(env, ["--config-dir", str(self.conf),
                                            "--legacy-dir", str(self.legacy)])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["status"], "failed")
        self.assertIn("Исправить:", err)

    def test_success_exits_0_with_json(self):
        self.write_token()
        code, out, _ = SR.main_with(FakeEnv(containers=[SONAR_RUNNING]),
                                    ["--config-dir", str(self.conf), "--legacy-dir", str(self.legacy)])
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual((doc["status"], doc["host"], doc["version"]), ("ready", HOST, "26.8.0"))


if __name__ == "__main__":
    unittest.main()
