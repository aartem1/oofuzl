"""sonar_ready.py: Sonar is a precondition of the audit, never an optional layer.

Every test drives the real decision code through a fake machine: a fake
`docker` CLI with containers, images and a database schema version, a fake
Sonar HTTP API and a fake clock. Each one asserts the outcome the audit acts
on -- ready with a host and a token file, or a stop with a reason and the one
command that fixes it.
"""

import importlib.util
import json
import os
import stat
import tempfile
import unittest
import urllib.parse
import zipfile
from pathlib import Path

from helpers import SCRIPTS

_spec = importlib.util.spec_from_file_location("sonar_ready", SCRIPTS / "sonar_ready.py")
SR = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(SR)

HOST = "http://localhost:9000"
TOKEN = "squ_" + "a" * 36


class FakeMachine:
    """`schema` is the SonarQube version the database was written by. A
    server whose image differs asks for a migration; on H2 (no JDBC url) the
    migration is refused, as SonarQube does."""

    def __init__(self, *, docker=True, docker_running=True, can_open=True, docker_up_after=10,
                 platform="darwin", containers=None, local="sha:old", registry="sha:old",
                 pull_fails=False, schema="sha:old", migration_fails=False, sonar_up_after=20,
                 port_taken=False, tokens_valid=(TOKEN,), admin_password="admin", env=None,
                 dump_fails=False, restore_fails=False):
        self.docker, self.docker_running, self.can_open = docker, docker_running, can_open
        self.docker_up_after, self.platform, self.machine = docker_up_after, platform, "arm64"
        self.containers = {k: dict(v) for k, v in (containers or {}).items()}
        self.images = {"sonarqube:community": local} if local else {}
        self.registry, self.pull_fails = registry, pull_fails
        self.schema, self.migration_fails, self.migrating = schema, migration_fails, False
        self.sonar_up_after, self.port_taken = sonar_up_after, port_taken
        self.tokens_valid, self.admin_password = set(tokens_valid), admin_password
        self.env, self.dump_fails, self.restore_fails = dict(env or {}), dump_fails, restore_fails
        self.sent_tokens = []
        self.networks, self.t, self.opened_at, self.timeouts = set(), 0.0, None, []
        self.calls, self.minted, self.downloads, self.dumps, self.restores = [], [], [], 0, 0
        for c in self.containers.values():
            c.setdefault("since", -1000.0)

    # clock and lookup
    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds

    def which(self, name):
        return "/usr/local/bin/docker" if (name == "docker" and self.docker) else None

    def _docker_ok(self):
        return self.docker_running or (self.opened_at is not None
                                       and self.t - self.opened_at >= self.docker_up_after)

    def _sonar(self):
        """The container serving port 9000."""
        for name, c in self.containers.items():
            if c["State"] == "running" and c["Image"].startswith("sonarqube") or \
                    (c["State"] == "running" and c.get("sonar")):
                return name, c
        return None, None

    # docker
    def run(self, argv, timeout=900):
        self.calls.append(list(argv))
        self.timeouts.append((argv[:2], timeout))
        if argv[:3] == ["open", "-a", "Docker"]:
            if not self.can_open:
                return 1, "", "Unable to find application named 'Docker'"
            self.opened_at = self.opened_at if self.opened_at is not None else self.t
            return 0, "", ""
        if argv[0] != "docker":
            return 127, "", "unexpected"
        if not self._docker_ok():
            return 1, "", "Cannot connect to the Docker daemon"
        sub = argv[1]
        if sub == "info":
            return 0, "Server Version: 28", ""
        if sub == "ps":
            rows = [{"Names": n, "Image": c["Image"], "State": c["State"]}
                    for n, c in self.containers.items()]
            return 0, "\n".join(json.dumps(r) for r in rows), ""
        if sub == "pull":
            if argv[2] == "sonarqube:community":
                if self.pull_fails:
                    return 1, "", "network unreachable"
                self.images[argv[2]] = self.registry
            else:
                self.images[argv[2]] = "sha:" + argv[2]
            return 0, "", ""
        if argv[1:3] == ["image", "inspect"]:
            ref = argv[-1]
            return (0, self.images[ref] + "\n", "") if ref in self.images else (1, "", "none")
        if argv[1:3] == ["network", "inspect"]:
            return (0, "[]", "") if argv[3] in self.networks else (1, "", "none")
        if argv[1:3] == ["network", "create"]:
            self.networks.add(argv[3])
            return 0, "", ""
        if sub == "inspect":
            c = self.containers.get(argv[-1])
            if c is None:
                return 1, "", "no such container"
            if "{{.Image}}" in argv:
                return 0, c["ImageId"] + "\n", ""
            if "{{.Config.Image}}" in argv:
                return 0, c.get("ConfigImage", c["Image"]) + "\n", ""
            return 0, json.dumps(c.get("Env", [])), ""
        if sub == "start":
            c = self.containers[argv[2]]
            c["State"], c["since"] = "running", self.t
            return 0, argv[2], ""
        if sub == "stop":
            if argv[-1] not in self.containers:
                return 1, "", "No such container: %s" % argv[-1]
            self.containers[argv[-1]]["State"] = "exited"
            return 0, argv[-1], ""
        if sub == "rm":
            self.containers.pop(argv[-1], None)
            return 0, argv[-1], ""
        if sub == "tag":
            if argv[2] not in self.images and not argv[2].startswith("sha:"):
                return 1, "", "no such image"
            self.images[argv[3]] = self.images.get(argv[2], argv[2])
            return 0, "", ""
        if sub == "volume":
            return 1, "", "no such volume"
        if sub == "run":
            return self._docker_run(argv)
        if sub == "exec":
            return self._exec(argv[3:])
        if sub == "logs":
            return 0, "log", ""
        return 1, "", "unknown docker command %s" % sub

    def _docker_run(self, argv):
        name = argv[argv.index("--name") + 1]
        image = argv[-1]
        is_sonar = image.startswith("sonarqube") or image.startswith("sha:")
        if is_sonar and self.port_taken:
            return 125, "", "Bind for 0.0.0.0:9000 failed: port is already allocated"
        env = []
        if "--env-file" in argv:
            env = Path(argv[argv.index("--env-file") + 1]).read_text().split()
        image_id = self.images.get(image, image)
        self.containers[name] = {"Image": image, "ImageId": image_id, "State": "running",
                                 "since": self.t, "Env": env, "sonar": is_sonar,
                                 "argv": list(argv)}
        return 0, "id", ""

    def _exec(self, cmd):
        if cmd[0] == "pg_isready":
            return 0, "", ""
        if cmd[:2] == ["sh", "-c"] and "pg_dump" in cmd[2]:
            if self.dump_fails:
                return 1, "", "dump failed"
            self.dumps += 1
            self.dumped_schema = self.schema
            return 0, "", ""
        if cmd[:2] == ["sh", "-c"] and "pg_restore" in cmd[2]:
            if self.restore_fails:
                self.schema = None
                return 1, "", "restore failed"
            self.restores += 1
            self.schema = self.dumped_schema
            return 0, "", ""
        return 1, "", "exec?"

    # sonar http
    def http(self, method, url, *, token=None, basic=None, form=None):
        self.calls.append([method, url])
        if token:
            self.sent_tokens.append((url, token))
        if url.startswith("https://api.github.com"):
            return 200, {"tag_name": "8.1.0.5000"}
        if not url.startswith(HOST):
            return self._remote(method, url, token=token, basic=basic)
        name, c = self._sonar()
        if c is None:
            return None, None
        if self.t - c["since"] < self.sonar_up_after:
            return 200, {"status": "STARTING"}
        h2 = not any(e.startswith("SONAR_JDBC_URL=") for e in c.get("Env", []))
        path = url[len(HOST):]
        if path == "/api/system/migrate_db":
            if h2:
                return 200, {"state": "NOT_SUPPORTED",
                             "message": "Upgrade is not supported on embedded database."}
            self.migrating = True
            return 200, {"state": "MIGRATION_RUNNING"}
        if path == "/api/system/status":
            if c["ImageId"] != self.schema:
                if self.migrating:
                    self.migrating = False
                    if self.migration_fails:
                        c["failed"] = True
                    else:
                        self.schema = c["ImageId"]
                if c.get("failed"):
                    return 200, {"status": "DOWN"}
                if c["ImageId"] != self.schema:
                    return 200, {"status": "DB_MIGRATION_NEEDED"}
            return 200, {"status": "UP", "version": "26.11.0"}
        if path == "/api/authentication/validate":
            ok = token in self.tokens_valid or basic == ("admin", self.admin_password)
            return 200, {"valid": bool(ok)}
        if basic != ("admin", self.admin_password):
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

    def _remote(self, method, url, *, token=None, basic=None):
        path = urllib.parse.urlparse(url).path
        if path == "/api/system/status":
            return 200, {"status": "UP", "version": "2025.1"}
        if path == "/api/authentication/validate":
            ok = token in self.tokens_valid or basic == ("admin", "admin")
            return 200, {"valid": bool(ok)}
        self.remote_writes = getattr(self, "remote_writes", []) + [path]
        return 204, {}


PG_ENV = ["SONAR_JDBC_URL=jdbc:postgresql://code-health-sonar-db:5432/sonar"]


def ours(state="running", image_id="sha:old"):
    return {SR.NAME: {"Image": "sonarqube:community", "ImageId": image_id, "State": state,
                      "Env": PG_ENV, "sonar": True},
            SR.DB_NAME: {"Image": SR.DB_IMAGE, "ImageId": "pg", "State": state,
                         "Env": ["POSTGRES_USER=sonar", "POSTGRES_PASSWORD=DbPass1"]}}


def h2_legacy(state="running"):
    return {"sonarqube": {"Image": "sonarqube:community", "ImageId": "sha:old", "State": state,
                          "Env": [], "sonar": True}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conf = Path(self.tmp.name) / "code-health"
        self.legacy = Path(self.tmp.name) / "sonar-code-health"

    def write_token(self, value=TOKEN):
        self.conf.mkdir(parents=True, exist_ok=True)
        (self.conf / "sonar-token").write_text(value + "\n")

    def ready(self, m, scanner=False):
        return SR.ensure_ready(m, host=HOST, config_dir=self.conf, legacy_dir=self.legacy,
                               docker_wait=180, sonar_wait=300, environ=m.env,
                               scanner_cache=(Path(self.tmp.name) / "scanner") if scanner else None)

    def stops(self, m):
        with self.assertRaises(SR.NotReady) as cm:
            self.ready(m)
        return cm.exception


class DockerTests(Base):
    def test_no_docker_cli_stops_with_install_hint_and_installs_nothing(self):
        m = FakeMachine(docker=False)
        stop = self.stops(m)
        self.assertIn("Docker", stop.reason)
        self.assertIn("docs.docker.com", stop.fix)
        self.assertEqual(m.calls, [])

    def test_stopped_docker_on_macos_is_started_and_awaited(self):
        self.write_token()
        m = FakeMachine(docker_running=False, docker_up_after=30, containers=ours())
        result = self.ready(m)
        self.assertIn(["open", "-a", "Docker"], m.calls)
        self.assertIn("docker_started", result["actions"])
        self.assertEqual(result["status"], "ready")

    def test_docker_that_never_answers_stops_after_bounded_wait(self):
        m = FakeMachine(docker_running=False, docker_up_after=10_000)
        stop = self.stops(m)
        self.assertLessEqual(m.t, 190)
        self.assertIn("open -a Docker", stop.fix)

    def test_each_docker_probe_is_short(self):
        m = FakeMachine(docker_running=False, docker_up_after=10_000)
        self.stops(m)
        probes = [t for argv, t in m.timeouts if argv == ["docker", "info"]]
        self.assertTrue(probes)
        self.assertTrue(all(t <= 30 for t in probes), probes)

    def test_missing_docker_app_on_macos_stops(self):
        self.assertIn("Docker", self.stops(FakeMachine(docker_running=False, can_open=False)).reason)

    def test_stopped_docker_on_linux_is_not_started_here(self):
        m = FakeMachine(docker_running=False, platform="linux")
        self.assertIn("systemctl", self.stops(m).fix)
        self.assertNotIn(["open", "-a", "Docker"], m.calls)


class ServerTests(Base):
    def test_fresh_machine_gets_postgres_backed_server_with_private_env_files(self):
        self.write_token()
        m = FakeMachine(local=None, registry="sha:new", schema="sha:new")
        result = self.ready(m)
        self.assertEqual(result["container"], SR.NAME)
        self.assertIn("database_created", result["actions"])
        self.assertIn("container_created", result["actions"])
        run = m.containers[SR.NAME]["argv"]
        for volume in SR.VOLUMES:
            self.assertIn(volume, run)
        self.assertIn(SR.NETWORK, run)
        password = (self.conf / "db_password").read_text().strip()
        self.assertFalse(any(password in " ".join(c) for c in m.calls if isinstance(c, list)))
        for f in ("db_password", "db.env", "sonar.env"):
            self.assertEqual(stat.S_IMODE(os.stat(self.conf / f).st_mode), 0o600)

    def test_running_server_is_reused_without_recreating(self):
        self.write_token()
        m = FakeMachine(containers=ours())
        self.ready(m)
        self.assertFalse(any(c[:2] in (["docker", "rm"], ["docker", "run"]) for c in m.calls))

    def test_stopped_server_and_database_are_started(self):
        self.write_token()
        m = FakeMachine(containers=ours(state="exited"))
        result = self.ready(m)
        self.assertIn(["docker", "start", SR.DB_NAME], m.calls)
        self.assertIn(["docker", "start", SR.NAME], m.calls)
        self.assertIn("container_started", result["actions"])

    def test_legacy_embedded_database_server_is_stopped_never_removed(self):
        self.write_token()
        m = FakeMachine(containers=h2_legacy(), registry="sha:new", schema="sha:new")
        result = self.ready(m)
        self.assertEqual(m.containers["sonarqube"]["State"], "exited")
        self.assertNotIn(["docker", "rm", "sonarqube"], m.calls)
        self.assertEqual(result["container"], SR.NAME)
        self.assertTrue(any("встроенной базе" in n for n in result["notes"]))

    def test_users_server_on_external_database_is_used_as_is(self):
        self.write_token()
        m = FakeMachine(containers={"corp-sonar": {"Image": "sonarqube:2025-lts",
                                                   "ImageId": "sha:old", "State": "running",
                                                   "Env": PG_ENV, "sonar": True}},
                        registry="sha:new")
        result = self.ready(m)
        self.assertEqual(result["container"], "corp-sonar")
        self.assertNotIn(SR.NAME, m.containers)

    def test_legacy_server_is_found_after_its_tag_moved_to_a_new_image(self):
        # After `docker pull` moves the tag, `docker ps` shows the old
        # image's short ID instead of `sonarqube:community`.
        self.write_token()
        legacy = h2_legacy()
        legacy["sonarqube"].update(Image="0123456789ab", ConfigImage="sonarqube:community")
        m = FakeMachine(containers=legacy, registry="sha:new", schema="sha:new")
        result = self.ready(m)
        self.assertEqual(m.containers["sonarqube"]["State"], "exited")
        self.assertEqual(result["container"], SR.NAME)

    def test_server_recreated_next_to_an_existing_database_dumps_it_first(self):
        # An upgrade interrupted after `docker rm`: the next run recreates
        # the server on the new image, so the database is saved first.
        self.write_token()
        m = FakeMachine(containers={SR.DB_NAME: ours()[SR.DB_NAME]}, registry="sha:new")
        self.ready(m)
        self.assertEqual(m.dumps, 1)
        dump = next(i for i, c in enumerate(m.calls) if "pg_dump" in " ".join(c))
        run = next(i for i, c in enumerate(m.calls) if c[:2] == ["docker", "run"])
        self.assertLess(dump, run)

    def test_port_taken_stops_with_reason(self):
        self.assertIn("9000", self.stops(FakeMachine(local=None, port_taken=True)).reason)

    def test_server_that_never_comes_up_stops_with_logs_command(self):
        m = FakeMachine(containers=ours(state="exited"), sonar_up_after=10_000)
        stop = self.stops(m)
        self.assertLessEqual(m.t, 400)
        self.assertIn("docker logs", stop.fix)

    def test_embedded_database_refusing_migration_stops_at_once(self):
        # what happened on the real machine: new image over an H2 volume
        m = FakeMachine(containers={SR.NAME: {"Image": "sonarqube:community", "ImageId": "sha:new",
                                              "State": "running", "Env": [], "sonar": True},
                                    SR.DB_NAME: ours()[SR.DB_NAME]},
                        local="sha:new", registry="sha:new", schema="sha:old")
        stop = self.stops(m)
        self.assertIn("embedded database", stop.reason)
        self.assertLess(m.t, 100)


class UpgradeTests(Base):
    def test_newer_image_upgrades_with_dump_and_migration(self):
        self.write_token()
        m = FakeMachine(containers=ours(), registry="sha:new")
        result = self.ready(m)
        self.assertEqual(m.dumps, 1)
        self.assertEqual(m.containers[SR.NAME]["ImageId"], "sha:new")
        self.assertEqual(m.schema, "sha:new")
        self.assertIn("server_upgraded", result["actions"])
        self.assertIn("db_migrated", result["actions"])

    def test_failed_migration_restores_dump_and_previous_image(self):
        self.write_token()
        m = FakeMachine(containers=ours(), registry="sha:new", migration_fails=True)
        result = self.ready(m)
        self.assertEqual(m.restores, 1)
        self.assertEqual(m.containers[SR.NAME]["ImageId"], "sha:old")
        self.assertEqual(m.schema, "sha:old")
        self.assertNotIn("server_upgraded", result["actions"])
        self.assertTrue(any("прежнюю" in n for n in result["notes"]))

    def test_failed_dump_skips_the_upgrade(self):
        self.write_token()
        m = FakeMachine(containers=ours(), registry="sha:new", dump_fails=True)
        result = self.ready(m)
        self.assertEqual(m.containers[SR.NAME]["ImageId"], "sha:old")
        self.assertTrue(result["notes"])

    def test_same_image_is_left_alone(self):
        self.write_token()
        m = FakeMachine(containers=ours())
        self.ready(m)
        self.assertEqual(m.dumps, 0)

    def test_failed_pull_keeps_the_installed_server(self):
        self.write_token()
        m = FakeMachine(containers=ours(), pull_fails=True)
        result = self.ready(m)
        self.assertEqual(result["status"], "ready")
        self.assertTrue(any("не обновился" in n for n in result["notes"]))

    def test_failed_pull_with_no_local_image_stops(self):
        self.assertIn("docker pull", self.stops(FakeMachine(local=None, pull_fails=True)).fix)


class RecoveryTests(Base):
    def test_failed_restore_leaves_a_marker_and_the_next_run_starts_nothing(self):
        # A server started over an unrestored database would overwrite the
        # only copy and come up empty.
        self.write_token()
        m = FakeMachine(containers=ours(), registry="sha:new", migration_fails=True,
                        restore_fails=True)
        with self.assertRaises(SR.NotReady):
            self.ready(m)
        dumps, runs = m.dumps, sum(1 for c in m.calls if c[:2] == ["docker", "run"])
        stop = self.stops(m)
        self.assertIn("pg_restore", stop.fix)
        self.assertEqual(m.dumps, dumps)
        self.assertEqual(sum(1 for c in m.calls if c[:2] == ["docker", "run"]), runs)

    def test_upgrade_cut_short_is_rolled_back_by_the_next_run(self):
        self.write_token()
        m = FakeMachine(containers=ours(), registry="sha:new", migration_fails=True)
        original = SR._await_up
        calls = {"n": 0}

        def killed(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:  # the new image's wait: the process dies here
                raise KeyboardInterrupt
            return original(*args, **kwargs)
        SR._await_up = killed
        try:
            with self.assertRaises(KeyboardInterrupt):
                self.ready(m)
        finally:
            SR._await_up = original
        result = self.ready(m)
        self.assertEqual(m.restores, 1)
        self.assertEqual(m.containers[SR.NAME]["ImageId"], "sha:old")
        self.assertTrue(any("прерванное" in n.lower() for n in result["notes"]))

    def test_an_image_that_failed_to_upgrade_is_not_retried(self):
        self.write_token()
        m = FakeMachine(containers=ours(), registry="sha:new", migration_fails=True)
        self.ready(m)
        dumps = m.dumps
        result = self.ready(m)
        self.assertEqual(m.dumps, dumps)
        self.assertEqual(m.containers[SR.NAME]["ImageId"], "sha:old")
        self.assertTrue(any("не повторяю" in n for n in result["notes"]))

    def test_lost_database_password_is_recovered_from_the_database_container(self):
        self.write_token()
        containers = ours()
        containers[SR.DB_NAME]["Env"] = ["POSTGRES_USER=sonar", "POSTGRES_PASSWORD=Kept0ldPass"]
        m = FakeMachine(containers=containers)
        self.ready(m)
        self.assertEqual((self.conf / "db_password").read_text().strip(), "Kept0ldPass")
        self.assertIn("Kept0ldPass", (self.conf / "sonar.env").read_text())

    def test_lost_database_password_that_cannot_be_recovered_stops(self):
        self.write_token()
        containers = ours()
        containers[SR.DB_NAME]["Env"] = []
        stop = self.stops(FakeMachine(containers=containers))
        self.assertIn("пароль", stop.reason.lower())

    def test_users_external_server_is_not_stopped_next_to_ours(self):
        self.write_token()
        containers = ours()
        containers["corp-sonar"] = {"Image": "sonarqube:2025-lts", "ImageId": "x",
                                    "State": "running", "Env": PG_ENV}
        m = FakeMachine(containers=containers)
        self.ready(m)
        self.assertEqual(m.containers["corp-sonar"]["State"], "running")

    def test_a_second_run_waits_for_the_first_and_gives_up_with_a_reason(self):
        import fcntl
        self.conf.mkdir(parents=True, exist_ok=True)
        with open(self.conf / "sonar.lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            stop = self.stops(FakeMachine(containers=ours()))
        self.assertIn("другой", stop.reason)


class RecoveryEdgeTests(Base):
    def failed_restore(self):
        self.write_token()
        m = FakeMachine(containers=ours(), registry="sha:new", migration_fails=True,
                        restore_fails=True)
        with self.assertRaises(SR.NotReady):
            self.ready(m)
        return m

    def test_after_a_manual_restore_the_failed_version_is_not_started_again(self):
        m = self.failed_restore()
        # the user follows the fix: restores by hand and removes the marker
        m.restore_fails, m.schema = False, "sha:old"
        (self.conf / SR.MARKER).unlink()
        result = self.ready(m)
        self.assertEqual(m.containers[SR.NAME]["ImageId"], "sha:old")
        self.assertEqual(result["status"], "ready")

    def test_rollback_without_the_previous_image_stops_with_its_own_reason(self):
        self.write_token()
        m = FakeMachine(containers=ours(), registry="sha:new", migration_fails=True)
        original = SR._await_up
        calls = {"n": 0}

        def killed(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise KeyboardInterrupt
            return original(*args, **kwargs)
        SR._await_up = killed
        try:
            with self.assertRaises(KeyboardInterrupt):
                self.ready(m)
        finally:
            SR._await_up = original
        m.images.pop(SR.ROLLBACK_TAG, None)  # `docker image rm` by the user
        stop = self.stops(m)
        self.assertIn("образ", stop.reason)
        self.assertIn(SR.MARKER, stop.fix)

    def test_an_upgrade_interrupted_before_the_new_server_answered_is_not_marked_failed(self):
        self.write_token()
        m = FakeMachine(containers=ours(), registry="sha:new")
        original = SR._remove_sonar
        SR._remove_sonar = lambda env: (_ for _ in ()).throw(KeyboardInterrupt())
        try:
            with self.assertRaises(KeyboardInterrupt):
                self.ready(m)
        finally:
            SR._remove_sonar = original
        self.ready(m)  # rolls back
        self.assertFalse((self.conf / SR.FAILED_IMAGE).exists())
        result = self.ready(m)  # tries the upgrade again
        self.assertIn("server_upgraded", result["actions"])

    def kill_while_new_server_starts(self, m):
        original = SR._await_up
        calls = {"n": 0}

        def killed(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise KeyboardInterrupt
            return original(*args, **kwargs)
        SR._await_up = killed
        try:
            with self.assertRaises(KeyboardInterrupt):
                self.ready(m)
        finally:
            SR._await_up = original

    def test_killed_upgrade_then_failed_restore_then_manual_restore_starts_the_old_version(self):
        self.write_token()
        m = FakeMachine(containers=ours(), registry="sha:new", migration_fails=True)
        self.kill_while_new_server_starts(m)
        m.restore_fails = True
        with self.assertRaises(SR.NotReady):
            self.ready(m)
        m.restore_fails, m.schema = False, "sha:old"
        (self.conf / SR.MARKER).unlink()
        self.assertEqual(self.ready(m)["status"], "ready")
        self.assertEqual(m.containers[SR.NAME]["ImageId"], "sha:old")

    def test_manual_restore_without_the_previous_image_stops_instead_of_retrying(self):
        m = self.failed_restore()
        m.restore_fails, m.schema = False, "sha:old"
        (self.conf / SR.MARKER).unlink()
        m.images.pop(SR.ROLLBACK_TAG)  # `docker image prune -a`
        stop = self.stops(m)
        self.assertIn(SR.ROLLBACK_TAG, stop.fix)
        self.assertNotIn(SR.NAME, m.containers)

    def test_new_image_that_could_not_start_leaves_the_database_alone(self):
        self.write_token()
        m = FakeMachine(containers=ours(), registry="sha:new")
        original = SR._run_sonar

        def port_taken_once(env, image, sonar_env):
            if image == SR.IMAGE:
                raise SR.NotReady("Порт 9000 занят", "lsof")
            return original(env, image, sonar_env)
        SR._run_sonar = port_taken_once
        try:
            self.ready(m)
        finally:
            SR._run_sonar = original
        self.assertEqual((m.restores, m.containers[SR.NAME]["ImageId"]), (0, "sha:old"))
        self.assertFalse((self.conf / SR.FAILED_IMAGE).exists())

    def test_broken_marker_stops_with_a_reason_not_a_traceback(self):
        self.write_token()
        self.conf.mkdir(parents=True, exist_ok=True)
        for text in ("{", '{"phase": "upgrading"}'):
            (self.conf / SR.MARKER).write_text(text)
            stop = self.stops(FakeMachine(containers=ours()))
            self.assertIn(SR.MARKER, stop.fix)


class RemoteTests(Base):
    REMOTE = "https://sonar.example.com"

    def remote_ready(self, m):
        return SR.ensure_ready(m, host=self.REMOTE, config_dir=self.conf, legacy_dir=self.legacy,
                               docker_wait=180, sonar_wait=300, environ=m.env)

    def test_remote_server_needs_no_docker_and_is_never_written_to(self):
        m = FakeMachine(docker=False, env={"SONAR_TOKEN": TOKEN})
        result = self.remote_ready(m)
        self.assertEqual(result["status"], "ready")
        self.assertFalse(any(c and c[0] == "docker" for c in m.calls))
        self.assertEqual(getattr(m, "remote_writes", []), [])

    def test_local_token_files_are_never_sent_to_a_remote_server(self):
        self.write_token()
        m = FakeMachine(docker=False)
        with self.assertRaises(SR.NotReady) as cm:
            self.remote_ready(m)
        self.assertEqual(m.sent_tokens, [])
        self.assertIn("SONAR_TOKEN", cm.exception.fix)

    def test_remote_server_without_a_token_stops_instead_of_rotating_admin(self):
        m = FakeMachine(docker=False, tokens_valid=())
        with self.assertRaises(SR.NotReady) as cm:
            self.remote_ready(m)
        self.assertEqual(getattr(m, "remote_writes", []), [])
        self.assertIn("SONAR_TOKEN", cm.exception.fix)


class TokenTests(Base):
    def test_env_token_wins_and_no_file_is_reported(self):
        m = FakeMachine(containers=ours(), env={"SONAR_TOKEN": TOKEN})
        result = self.ready(m)
        self.assertEqual((result["token_source"], result["token_file"]), ("env", None))

    def test_token_file_is_used(self):
        self.write_token()
        result = self.ready(FakeMachine(containers=ours()))
        self.assertEqual(result["token_file"], str(self.conf / "sonar-token"))

    def test_older_token_file_is_used(self):
        self.legacy.mkdir()
        (self.legacy / "token").write_text(TOKEN)
        result = self.ready(FakeMachine(containers=ours()))
        self.assertEqual(result["token_file"], str(self.legacy / "token"))

    def test_fresh_server_rotates_admin_and_mints_token_into_private_files(self):
        m = FakeMachine(containers=ours(), tokens_valid=())
        result = self.ready(m)
        token_file = Path(result["token_file"])
        self.assertIn(token_file.read_text().strip(), m.tokens_valid)
        self.assertNotEqual(m.admin_password, "admin")
        self.assertEqual((self.conf / "admin_password").read_text().strip(), m.admin_password)
        for f in (token_file, self.conf / "admin_password"):
            self.assertEqual(stat.S_IMODE(os.stat(f).st_mode), 0o600)
        self.assertIn("token_minted", result["actions"])

    def test_dead_token_with_stored_admin_password_mints_new_token(self):
        self.write_token("squ_dead")
        (self.conf / "admin_password").write_text("S3cret!pass")
        m = FakeMachine(containers=ours(), tokens_valid=(), admin_password="S3cret!pass")
        result = self.ready(m)
        self.assertIn(Path(result["token_file"]).read_text().strip(), m.tokens_valid)

    def test_no_way_to_authenticate_stops_without_leaking_secrets(self):
        self.write_token("squ_dead")
        stop = self.stops(FakeMachine(containers=ours(), tokens_valid=(),
                                      admin_password="unknown!1"))
        self.assertNotIn("squ_dead", stop.reason + stop.fix)
        self.assertIn("sonar-token", stop.fix)

    def test_admin_password_is_saved_before_the_server_changes_it(self):
        # A new password the server accepted but the file never got locks
        # the admin out for good.
        m = FakeMachine(containers=ours(), tokens_valid=())
        original = SR._write_private

        def failing(path, value):
            if path.name == "admin_password":
                raise OSError("disk full")
            return original(path, value)
        SR._write_private = failing
        try:
            with self.assertRaises(OSError):
                self.ready(m)
        finally:
            SR._write_private = original
        self.assertEqual(m.admin_password, "admin")

    def test_result_never_contains_a_secret(self):
        m = FakeMachine(containers=ours(), tokens_valid=())
        dumped = json.dumps(self.ready(m))
        for secret in list(m.tokens_valid) + [m.admin_password]:
            self.assertNotIn(secret, dumped)


class ScannerTests(Base):
    def machine(self):
        m = FakeMachine(containers=ours())

        def download(url, dest):
            m.downloads.append(url)
            with zipfile.ZipFile(dest, "w") as zf:
                zf.writestr("sonar-scanner-8.1.0.5000-macosx-aarch64/bin/sonar-scanner", "#!/bin/sh\n")
            return True
        m.download = download
        return m

    def test_newest_native_scanner_is_downloaded_for_this_platform(self):
        self.write_token()
        m = self.machine()
        result = self.ready(m, scanner=True)
        self.assertEqual(result["scanner_version"], "8.1.0.5000")
        self.assertIn("macosx-aarch64", m.downloads[0])
        self.assertTrue(os.access(result["scanner"], os.X_OK))

    def test_cached_newest_scanner_is_not_downloaded_again(self):
        self.write_token()
        m = self.machine()
        self.ready(m, scanner=True)
        self.ready(m, scanner=True)
        self.assertEqual(len(m.downloads), 1)

    def test_unreachable_download_without_cache_stops(self):
        self.write_token()
        m = FakeMachine(containers=ours())
        m.download = lambda url, dest: False
        with self.assertRaises(SR.NotReady):
            self.ready(m, scanner=True)


class CliTests(Base):
    def args(self):
        return ["--config-dir", str(self.conf), "--legacy-dir", str(self.legacy)]

    def test_failure_prints_reason_and_fix_and_exits_1(self):
        code, out, err = SR.main_with(FakeMachine(docker=False), self.args())
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["status"], "failed")
        self.assertIn("Исправить:", err)

    def test_success_exits_0_with_json(self):
        self.write_token()
        code, out, _ = SR.main_with(FakeMachine(containers=ours()), self.args())
        doc = json.loads(out)
        self.assertEqual((code, doc["status"], doc["host"], doc["version"]),
                         (0, "ready", HOST, "26.11.0"))


if __name__ == "__main__":
    unittest.main()
