#!/usr/bin/env python3
"""Bring SonarQube up before an audit, or stop the audit with the reason.

Sonar is a precondition of `/code-health:audit`, not an optional layer: an
audit without reliability, security, coverage and maintainability measured
is not worth reading. This script does, in order:

1. Docker: the CLI must exist (never installed here); a stopped daemon is
   started on macOS with `open -a Docker` and awaited for a bounded time.
2. SonarQube on PostgreSQL (the embedded H2 database cannot be upgraded):
   the newest Community Build image is pulled; the plugin's container is
   reused, started or created with persistent named volumes. A container on
   an older image is recreated on the new one; the database is dumped
   first, migrated when the server asks, and restored with the old image if
   the migration fails. A user's own server on an external database is
   used as is; an old embedded-database server is stopped, never deleted.
3. Token: `SONAR_TOKEN`, else the token file, else (fresh server or a
   stored admin password) a new token is minted into a private file.
4. Scanner: the newest native SonarScanner CLI is downloaded into a cache
   outside any repository.

Output: one JSON document on stdout, never a secret. Exit 0 when ready.
Exit 1 with `status: failed`, a reason and the one command that fixes it;
the same two lines go to stderr in Russian for the person reading.
"""

import argparse
import contextlib
import fcntl
import json
import os
import platform as _platform
import secrets
import shutil
import string
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import base64
import zipfile
from pathlib import Path

IMAGE = "sonarqube:community"
# Major PostgreSQL upgrades need a dump and restore, so the major is pinned.
# The image is pulled only when missing and the database container is never
# recreated: PostgreSQL minor updates are not applied automatically.
DB_IMAGE = "postgres:17-alpine"
NAME = "code-health-sonarqube"
DB_NAME = "code-health-sonar-db"
NETWORK = "code-health-sonar"
VOLUMES = ("code-health-sonarqube-data:/opt/sonarqube/data",
           "code-health-sonarqube-extensions:/opt/sonarqube/extensions",
           "code-health-sonarqube-logs:/opt/sonarqube/logs")
DB_VOLUME = "code-health-sonar-pgdata:/var/lib/postgresql/data"
BACKUP_DUMP = "/var/lib/postgresql/data/code-health-before-upgrade.dump"
SCANNER_RELEASES = "https://api.github.com/repos/SonarSource/sonar-scanner-cli/releases/latest"
SCANNER_ZIP = ("https://binaries.sonarsource.com/Distribution/sonar-scanner-cli/"
               "sonar-scanner-cli-{version}-{platform}.zip")
DOCKER_INSTALL_URL = "https://docs.docker.com/get-started/get-docker/"


class NotReady(Exception):
    def __init__(self, reason, fix):
        super().__init__(reason)
        self.reason = reason
        self.fix = fix


# ---------------------------------------------------------------- environment

class RealEnv:
    """The machine. Tests replace it with a fake of the same shape."""

    def __init__(self):
        self.platform = sys.platform
        self.machine = _platform.machine()
        self.env = dict(os.environ)

    def now(self):
        return time.monotonic()

    def sleep(self, seconds):
        time.sleep(seconds)

    def which(self, name):
        return shutil.which(name)

    def run(self, argv, timeout=900):
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            return 1, "", str(exc)
        return p.returncode, p.stdout, p.stderr

    def http(self, method, url, *, token=None, basic=None, form=None, timeout=15):
        data = urllib.parse.urlencode(form).encode() if form is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if token:
            req.add_header("Authorization", "Bearer " + token)
        elif basic:
            raw = ("%s:%s" % basic).encode()
            req.add_header("Authorization", "Basic " + base64.b64encode(raw).decode())
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                status = resp.status
        except urllib.error.HTTPError as exc:
            body, status = exc.read(), exc.code
        except (urllib.error.URLError, OSError):
            return None, None
        try:
            return status, json.loads(body or b"{}")
        except ValueError:
            return status, {}

    def download(self, url, dest, timeout=300):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp, open(dest, "wb") as fh:
                shutil.copyfileobj(resp, fh)
            return True
        except (urllib.error.URLError, OSError):
            return False


# ---------------------------------------------------------------- helpers

def _wait(env, limit, check, step=5):
    """Call `check` until it returns a truthy value or `limit` seconds pass."""
    deadline = env.now() + limit
    while True:
        value = check()
        if value:
            return value
        if env.now() >= deadline:
            return None
        env.sleep(step)


def _write_private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    # Written aside and renamed: a run killed midway never leaves half a file.
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(value + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _read(path):
    try:
        return path.read_text().strip() or None
    except OSError:
        return None


def _new_password():
    # SonarQube requires a special character; 24 random chars plus one of each class.
    alphabet = string.ascii_letters + string.digits
    core = "".join(secrets.choice(alphabet) for _ in range(24))
    return core + "aA1!"


# ---------------------------------------------------------------- 1. docker

def ensure_docker(env, wait, actions):
    if not env.which("docker"):
        raise NotReady("Docker не установлен, а без него SonarQube не поднять.",
                       "установите Docker Desktop (%s) и повторите аудит" % DOCKER_INSTALL_URL)
    if env.run(["docker", "info"], timeout=30)[0] == 0:
        return
    if env.platform != "darwin":
        raise NotReady("Docker установлен, но не запущен.", "sudo systemctl start docker")
    code, _, err = env.run(["open", "-a", "Docker"])
    if code != 0:
        raise NotReady("Не удалось запустить Docker Desktop: %s" % (err.strip() or "ошибка open"),
                       "open -a Docker")
    actions.append("docker_started")
    if not _wait(env, wait, lambda: env.run(["docker", "info"], timeout=30)[0] == 0):
        raise NotReady("Docker Desktop запущен, но за %d с так и не ответил." % wait,
                       "open -a Docker  # дождитесь зелёного статуса и повторите аудит")


# ---------------------------------------------------------------- 2. sonarqube

def _containers(env):
    code, out, err = env.run(["docker", "ps", "-a", "--format", "{{json .}}"])
    if code != 0:
        raise NotReady("docker ps не сработал: %s" % err.strip(), "docker info")
    rows = []
    for line in out.splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def _is_sonar_image(image):
    return image.split("/")[-1].split(":")[0] == "sonarqube"


def _is_sonar(env, row):
    """`docker ps` shows the old image's short ID once `docker pull` moved
    the tag; the reference the container was created from stays in
    `.Config.Image`."""
    if _is_sonar_image(row.get("Image", "")):
        return True
    code, out, _ = env.run(["docker", "inspect", "--format", "{{.Config.Image}}", row["Names"]])
    return code == 0 and _is_sonar_image(out.strip())


def _status(env, host):
    code, body = env.http("GET", host + "/api/system/status")
    if code != 200 or not isinstance(body, dict):
        return None, None
    return body.get("status"), body.get("version")


def _image_id(env, ref):
    code, out, _ = env.run(["docker", "image", "inspect", "--format", "{{.Id}}", ref])
    return out.strip() if code == 0 and out.strip() else None


def _container_image_id(env, name):
    code, out, _ = env.run(["docker", "inspect", "--format", "{{.Image}}", name])
    return out.strip() if code == 0 and out.strip() else None


def _uses_external_db(env, name):
    code, out, _ = env.run(["docker", "inspect", "--format", "{{json .Config.Env}}", name])
    if code != 0:
        return False
    try:
        return any(str(v).startswith("SONAR_JDBC_URL=") for v in json.loads(out or "[]"))
    except ValueError:
        return False


def _env_files(env, config_dir, rows):
    """Private env files for the database and the server. The password is
    generated once and never passed on a command line. A database that
    already exists keeps the password it was initialised with: a new one
    would lock the server out of it."""
    password_file = config_dir / "db_password"
    password = _read(password_file)
    if not password and _db_exists(env, rows):
        password = _db_password_from_container(env)
        if not password:
            raise NotReady("Пароль базы SonarQube потерян (%s), а база уже есть: с новым паролем "
                           "сервер к ней не подключится." % password_file,
                           "верните файл %s или удалите контейнер %s и том %s, чтобы начать с "
                           "пустой базы" % (password_file, DB_NAME, DB_VOLUME.split(":")[0]))
        _write_private(password_file, password)
    if not password:
        password = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(32))
        _write_private(password_file, password)
    db_env, sonar_env = config_dir / "db.env", config_dir / "sonar.env"
    _write_private(db_env, "POSTGRES_USER=sonar\nPOSTGRES_DB=sonar\nPOSTGRES_PASSWORD=%s" % password)
    _write_private(sonar_env, "SONAR_JDBC_URL=jdbc:postgresql://%s:5432/sonar\n"
                              "SONAR_JDBC_USERNAME=sonar\nSONAR_JDBC_PASSWORD=%s\n"
                              "SONAR_ES_BOOTSTRAP_CHECKS_DISABLE=true" % (DB_NAME, password))
    return db_env, sonar_env


def _db_exists(env, rows):
    if any(r.get("Names") == DB_NAME for r in rows):
        return True
    return env.run(["docker", "volume", "inspect", DB_VOLUME.split(":")[0]])[0] == 0


def _db_password_from_container(env):
    code, out, _ = env.run(["docker", "inspect", "--format", "{{json .Config.Env}}", DB_NAME])
    if code != 0:
        return None
    try:
        values = json.loads(out or "[]")
    except ValueError:
        return None
    for value in values or []:
        if str(value).startswith("POSTGRES_PASSWORD="):
            return str(value).split("=", 1)[1] or None
    return None


def _ensure_network(env):
    if env.run(["docker", "network", "inspect", NETWORK])[0] != 0:
        code, _, err = env.run(["docker", "network", "create", NETWORK])
        if code != 0:
            raise NotReady("Не удалось создать сеть Docker для SonarQube: %s" % err.strip(),
                           "docker network create %s" % NETWORK)


def _ensure_db(env, rows, db_env, actions):
    row = next((r for r in rows if r.get("Names") == DB_NAME), None)
    if row is None:
        if not _image_id(env, DB_IMAGE) and env.run(["docker", "pull", DB_IMAGE])[0] != 0:
            raise NotReady("Не скачался образ базы %s." % DB_IMAGE, "docker pull %s" % DB_IMAGE)
        code, _, err = env.run(["docker", "run", "-d", "--name", DB_NAME, "--network", NETWORK,
                                "--restart", "unless-stopped", "--env-file", str(db_env),
                                "-v", DB_VOLUME, DB_IMAGE])
        if code != 0:
            raise NotReady("Не удалось создать базу для SonarQube: %s" % err.strip(),
                           "docker logs --tail 200 %s" % DB_NAME)
        actions.append("database_created")
    elif row.get("State") != "running":
        code, _, err = env.run(["docker", "start", DB_NAME])
        if code != 0:
            raise NotReady("База SonarQube не запускается: %s" % err.strip(),
                           "docker logs --tail 200 %s" % DB_NAME)
    # Over TCP: while the image initialises a new database its temporary
    # server listens on the socket only.
    ready = _wait(env, 60, lambda: env.run(["docker", "exec", DB_NAME, "pg_isready", "-h",
                                            "127.0.0.1", "-U", "sonar", "-d", "sonar"])[0] == 0,
                  step=2)
    if not ready:
        raise NotReady("База SonarQube не ответила за 60 с.", "docker logs --tail 200 %s" % DB_NAME)


def _run_sonar(env, image, sonar_env):
    argv = ["docker", "run", "-d", "--name", NAME, "--network", NETWORK, "-p", "9000:9000",
            "--restart", "unless-stopped", "--env-file", str(sonar_env)]
    for volume in VOLUMES:
        argv += ["-v", volume]
    argv.append(image)
    code, _, err = env.run(argv)
    if code != 0:
        _port_taken(err)
        raise NotReady("Не удалось создать контейнер SonarQube: %s" % err.strip(),
                       "docker logs --tail 200 %s" % NAME)


def _port_taken(err):
    if "port is already allocated" in err or "address already in use" in err:
        raise NotReady("Порт 9000 занят другим процессом, SonarQube не запустить.",
                       "lsof -nP -iTCP:9000 -sTCP:LISTEN  # освободите порт и повторите аудит")


def _remove_sonar(env):
    env.run(["docker", "stop", "-t", "60", NAME])
    # -v: the image declares an anonymous temp volume; without it every
    # upgrade leaves one behind.
    env.run(["docker", "rm", "-v", NAME])


def _await_up(env, host, wait, name, allow_migration=True):
    """Wait for UP; start the database migration when the server asks."""
    state = {"migrated": False, "refused": None}

    def check():
        status, _ = _status(env, host)
        if status == "DB_MIGRATION_NEEDED":
            if not allow_migration:
                return "NEEDS_MIGRATION"
            if state["migrated"]:
                return None
            _, body = env.http("POST", host + "/api/system/migrate_db")
            answer = (body or {}).get("state") if isinstance(body, dict) else None
            if answer in ("NOT_SUPPORTED", "MIGRATION_FAILED"):
                state["refused"] = (body or {}).get("message") or answer
                return "REFUSED"
            state["migrated"] = True
        if status in ("UP", "DOWN"):
            return status
        return None

    status = _wait(env, wait, check)
    if status == "UP":
        return state["migrated"]
    if status == "REFUSED":
        raise NotReady("SonarQube не может обновить свою базу: %s" % state["refused"],
                       "docker logs --tail 200 %s" % name)
    if status == "NEEDS_MIGRATION":
        raise NotReady("SonarQube %s ждёт миграции базы, а обновлять этот сервер плагин не берётся."
                       % name, "docker logs --tail 200 %s" % name)
    reason = ("SonarQube не поднялся: сервер в состоянии DOWN." if status == "DOWN"
              else "SonarQube не поднялся за %d с." % wait)
    raise NotReady(reason, "docker logs --tail 200 %s" % name)


def _pull(env, notes, actions):
    before = _image_id(env, IMAGE)
    code, _, err = env.run(["docker", "pull", IMAGE])
    if code != 0:
        if not before:
            raise NotReady("Образ %s не скачался, а локального нет." % IMAGE, "docker pull %s" % IMAGE)
        notes.append("Образ SonarQube не обновился (%s), работаю на имеющемся."
                     % ((err.strip().splitlines() or ["ошибка docker pull"])[-1]))
        return before
    after = _image_id(env, IMAGE)
    if before and after and before != after:
        actions.append("image_updated")
    return after


# Upgrade state lives next to the env files, so a run that died midway is
# finished by the next one instead of being mistaken for a fresh start.
MARKER = "upgrade.json"
FAILED_IMAGE = "upgrade-failed-image"
# The previous image is tagged before the upgrade: an untagged image ID can be
# removed by `docker image prune`, and then there is nothing to roll back to.
ROLLBACK_TAG = "sonarqube:code-health-rollback"
RESTORE = ("dropdb -U sonar sonar && createdb -U sonar -O sonar sonar && "
           "pg_restore -U sonar -d sonar %s" % BACKUP_DUMP)


def _marker(config_dir):
    path = config_dir / MARKER
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        value = None
    if not isinstance(value, dict) or value.get("phase") not in ("upgrading", "restore_failed") \
            or not value.get("old_image"):
        raise NotReady("Файл состояния обновления SonarQube %s повреждён: непонятно, что "
                       "откатывать." % path,
                       "docker ps -a  # проверьте, что сервер и база на месте, затем: rm %s" % path)
    return value


def _set_marker(config_dir, value):
    _write_private(config_dir / MARKER, json.dumps(value))


def _clear_marker(config_dir):
    try:
        (config_dir / MARKER).unlink()
    except OSError:
        pass


def _dump(env):
    # Written aside and renamed on success: a dump that fails midway (a full
    # disk) never replaces the previous good copy.
    # The previous copy is kept as .prev: a dump taken from a database that
    # has just been restored by hand does not erase the only good copy.
    tmp, prev = BACKUP_DUMP + ".partial", BACKUP_DUMP + ".prev"
    return env.run(["docker", "exec", DB_NAME, "sh", "-c",
                    "pg_dump -U sonar -Fc -f %s sonar && { [ ! -f %s ] || mv %s %s; } && mv %s %s"
                    % (tmp, BACKUP_DUMP, BACKUP_DUMP, prev, tmp, BACKUP_DUMP)])[0] == 0


def _rollback(env, host, wait, old_image, sonar_env, config_dir):
    """Restore the dump and bring the old image back. A failed restore is
    remembered: the next run starts nothing, so the copy survives."""
    if not _image_id(env, old_image):
        raise NotReady("Обновление SonarQube прервано, а образ прежней версии (%s) удалён: "
                       "откатываться не на что. Копия базы цела." % old_image,
                       "docker pull <прежняя версия sonarqube> && docker tag <она> %s  "
                       "# либо верните базу вручную и удалите %s" % (old_image, config_dir / MARKER))
    _remove_sonar(env)
    if env.run(["docker", "exec", DB_NAME, "sh", "-c", RESTORE])[0] != 0:
        _set_marker(config_dir, {"phase": "restore_failed", "old_image": old_image})
        raise NotReady("Обновление SonarQube не прошло, и базу не удалось вернуть из копии. "
                       "Сервер не запущен, копия цела.", _restore_fix(config_dir))
    _run_sonar(env, old_image, sonar_env)
    _await_up(env, host, wait, NAME, allow_migration=False)
    _clear_marker(config_dir)


def _restore_fix(config_dir):
    return "docker exec %s sh -c '%s' && rm %s  # затем повторите аудит" % (
        DB_NAME, RESTORE, config_dir / MARKER)


def _upgrade(env, host, wait, old_image, new_image, sonar_env, actions, notes, config_dir):
    """Recreate the server on the new image. The database is dumped first; a
    failed migration restores the dump and brings the old image back, and
    that image is not tried again."""
    if _read(config_dir / FAILED_IMAGE) == new_image:
        notes.append("Новая версия SonarQube уже не поднималась на этой базе; обновление не "
                     "повторяю, работаю на прежней. Попробовать снова: rm %s"
                     % (config_dir / FAILED_IMAGE))
        return
    if env.run(["docker", "tag", old_image, ROLLBACK_TAG])[0] != 0:
        notes.append("Не удалось сохранить образ прежней версии SonarQube, обновление сервера "
                     "пропущено.")
        return
    if not _dump(env):
        notes.append("Не удалось сохранить копию базы SonarQube, обновление сервера пропущено.")
        return
    _set_marker(config_dir, {"phase": "upgrading", "old_image": ROLLBACK_TAG,
                             "new_image": new_image})
    try:
        _remove_sonar(env)
        _run_sonar(env, IMAGE, sonar_env)
    except NotReady as failed:
        # The new version never ran: it is not marked failed.
        _rollback(env, host, wait, ROLLBACK_TAG, sonar_env, config_dir)
        notes.append("Новую версию SonarQube не удалось запустить (%s); вернул прежнюю."
                     % failed.reason)
        return
    try:
        migrated = _await_up(env, host, wait, NAME)
    except NotReady as failed:
        # Written before the rollback: if the restore fails too, the next run
        # after a manual restore still knows not to start this version.
        _write_private(config_dir / FAILED_IMAGE, new_image)
        _rollback(env, host, wait, ROLLBACK_TAG, sonar_env, config_dir)
        notes.append("Новая версия SonarQube не поднялась (%s); вернул прежнюю с прежними данными."
                     % failed.reason)
        return
    _clear_marker(config_dir)
    env.run(["docker", "image", "rm", ROLLBACK_TAG])
    actions.append("server_upgraded")
    if migrated:
        actions.append("db_migrated")


def _start(env, name, actions):
    code, _, err = env.run(["docker", "start", name])
    if code != 0:
        _port_taken(err)
        raise NotReady("Контейнер %s не запускается: %s" % (name, err.strip()),
                       "docker logs --tail 200 %s" % name)
    actions.append("container_started")


def ensure_sonar(env, host, wait, actions, notes, config_dir, update=True):
    """The plugin's own server: SonarQube on PostgreSQL. The embedded H2
    database cannot be upgraded (`migrate_db` answers NOT_SUPPORTED), so a
    server that must stay on the latest release needs a real database."""
    marker = _marker(config_dir)
    if marker and marker.get("phase") == "restore_failed":
        raise NotReady("Прошлое обновление SonarQube не удалось откатить: база не восстановлена "
                       "из копии. Сервер не запускаю, чтобы не затереть копию.",
                       _restore_fix(config_dir))
    image_id = _pull(env, notes, actions) if update else _image_id(env, IMAGE)
    rows = _containers(env)
    ours = next((r for r in rows if r.get("Names") == NAME), None)
    foreign = [r for r in rows if r.get("Names") not in (NAME, DB_NAME) and _is_sonar(env, r)]
    external = [r for r in foreign if _uses_external_db(env, r["Names"])]

    if ours is None and external and not marker:
        row = external[0]
        if row.get("State") != "running":
            _start(env, row["Names"], actions)
        _await_up(env, host, wait, row["Names"], allow_migration=False)
        notes.append("SonarQube %s на внешней базе: использую его, обновлять не берусь."
                     % row["Names"])
        return row["Names"], _status(env, host)[1]

    for row in foreign:
        if row in external:
            continue
        if row.get("State") == "running":
            env.run(["docker", "stop", row["Names"]])
            notes.append("Старый SonarQube %s на встроенной базе остановлен: её нельзя обновлять. "
                         "Контейнер и тома не тронуты." % row["Names"])

    db_env, sonar_env = _env_files(env, config_dir, rows)
    _ensure_network(env)
    _ensure_db(env, rows, db_env, actions)

    if marker and marker.get("phase") == "upgrading":
        # The previous run died between removing the old server and
        # confirming the new one: finish its rollback.
        # Whether the new version failed is unknown (the run may have been
        # killed before it started), so it is not marked failed.
        _rollback(env, host, wait, marker["old_image"], sonar_env, config_dir)
        notes.append("Прерванное прошлое обновление SonarQube откачено: вернул прежнюю версию и "
                     "базу из копии.")
        actions.append("upgrade_rolled_back")
        return NAME, _status(env, host)[1]

    if ours is None:
        # The database outlived the server: the new server may migrate it,
        # so save it first.
        if any(r.get("Names") == DB_NAME for r in rows) and not _dump(env):
            raise NotReady("Не удалось сохранить копию базы SonarQube перед запуском сервера.",
                           "docker logs --tail 200 %s" % DB_NAME)
        image = IMAGE
        if image_id and _read(config_dir / FAILED_IMAGE) == image_id and _image_id(env, ROLLBACK_TAG):
            # The base was restored by hand after a failed upgrade: start the
            # version it belongs to, not the one that failed on it.
            image = ROLLBACK_TAG
            notes.append("Запускаю прежнюю версию SonarQube: новая на этой базе уже не поднималась.")
        _run_sonar(env, image, sonar_env)
        actions.append("container_created")
    elif ours.get("State") != "running":
        _start(env, NAME, actions)
    _await_up(env, host, wait, NAME)

    if ours is not None and update and image_id:
        current = _container_image_id(env, NAME)
        if current and current != image_id:
            _upgrade(env, host, wait, current, image_id, sonar_env, actions, notes, config_dir)
    return NAME, _status(env, host)[1]


# ---------------------------------------------------------------- 3. token

def _valid(env, host, token=None, basic=None):
    code, body = env.http("GET", host + "/api/authentication/validate", token=token, basic=basic)
    return code == 200 and isinstance(body, dict) and bool(body.get("valid"))


def _mint(env, host, basic, token_file, actions):
    code, body = env.http("POST", host + "/api/user_tokens/generate", basic=basic,
                          form={"name": "code-health-%d" % int(time.time())})
    token = body.get("token") if code == 200 and isinstance(body, dict) else None
    if not token:
        return False
    _write_private(token_file, token)
    actions.append("token_minted")
    return True


def ensure_token(env, host, config_dir, legacy_dir, environ, actions, allow_admin=True):
    token_file = config_dir / "sonar-token"
    if environ.get("SONAR_TOKEN") and _valid(env, host, token=environ["SONAR_TOKEN"]):
        return "env", None
    if not allow_admin:
        # The token files belong to the local server; sending them to
        # someone else's server would hand them a secret.
        raise NotReady("Для SonarQube %s нет действующего токена в SONAR_TOKEN." % host,
                       "export SONAR_TOKEN=<токен этого сервера>  # из %s/account/security"
                       % host)
    for path in (token_file, legacy_dir / "token"):
        value = _read(path)
        if value and _valid(env, host, token=value):
            return "file", str(path)

    password_file = config_dir / "admin_password"
    for path in (password_file, legacy_dir / "admin_password") if allow_admin else ():
        password = _read(path)
        if password and _valid(env, host, basic=("admin", password)):
            if _mint(env, host, ("admin", password), token_file, actions):
                return "minted", str(token_file)

    if allow_admin and _valid(env, host, basic=("admin", "admin")):
        new = _new_password()
        # Saved first: a password the server took but no file kept locks
        # the admin out.
        _write_private(password_file, new)
        code, _ = env.http("POST", host + "/api/users/change_password", basic=("admin", "admin"),
                           form={"login": "admin", "previousPassword": "admin", "password": new})
        if code in (200, 204):
            actions.append("admin_password_rotated")
            if _mint(env, host, ("admin", new), token_file, actions):
                return "minted", str(token_file)

    raise NotReady("Нет действующего токена SonarQube, и выпустить его автоматически нельзя.",
                   "создайте токен в %s/account/security и сохраните его в %s (права 600)"
                   % (host, token_file))


# ---------------------------------------------------------------- 4. scanner

def _scanner_platform(env):
    arch = "aarch64" if env.machine in ("arm64", "aarch64") else "x64"
    return ("macosx-" if env.platform == "darwin" else "linux-") + arch


def _cached_scanners(cache):
    if not cache.is_dir():
        return []
    found = []
    for d in cache.iterdir():
        binary = d / "bin" / "sonar-scanner"
        if binary.is_file():
            found.append((tuple(int(p) for p in d.name.split(".") if p.isdigit()), d))
    return [d for _, d in sorted(found)]


def ensure_scanner(env, cache, actions, notes):
    code, body = env.http("GET", SCANNER_RELEASES)
    latest = (body or {}).get("tag_name") if code == 200 and isinstance(body, dict) else None
    cached = _cached_scanners(cache)
    if latest and (cache / latest / "bin" / "sonar-scanner").is_file():
        return latest, str(cache / latest / "bin" / "sonar-scanner")
    if latest:
        cache.mkdir(parents=True, exist_ok=True)
        zip_path = cache / ("%s.zip" % latest)
        url = SCANNER_ZIP.format(version=latest, platform=_scanner_platform(env))
        if env.download(url, zip_path):
            try:
                with zipfile.ZipFile(zip_path) as zf:
                    top = zf.namelist()[0].split("/")[0]
                    zf.extractall(cache)
                target = cache / latest
                if target.exists():
                    shutil.rmtree(target)
                (cache / top).rename(target)
                for sub in ("bin", "jre/bin", "jre/lib"):
                    for f in (target / sub).glob("*"):
                        if f.is_file():
                            f.chmod(0o755)
                actions.append("scanner_installed")
                return latest, str(target / "bin" / "sonar-scanner")
            except (zipfile.BadZipFile, OSError, IndexError) as exc:
                notes.append("Сканер %s не распаковался: %s." % (latest, exc))
            finally:
                if zip_path.exists():
                    zip_path.unlink()
        else:
            notes.append("Сканер %s не скачался." % latest)
    else:
        notes.append("Не узнал последнюю версию сканера (GitHub недоступен).")
    if cached:
        best = cached[-1]
        notes.append("Работаю на сканере %s из кэша." % best.name)
        return best.name, str(best / "bin" / "sonar-scanner")
    raise NotReady("Сканер SonarScanner CLI не скачался, а в кэше его нет.",
                   "curl -I %s" % SCANNER_ZIP.format(version=latest or "<версия>",
                                                     platform=_scanner_platform(env)))


# ---------------------------------------------------------------- entry points

def ensure_ready(env, *, host, config_dir, legacy_dir, docker_wait, sonar_wait, environ,
                 scanner_cache=None, update=True):
    with _lock(env, Path(config_dir), sonar_wait):
        return _ensure_ready(env, host=host, config_dir=config_dir, legacy_dir=legacy_dir,
                             docker_wait=docker_wait, sonar_wait=sonar_wait, environ=environ,
                             scanner_cache=scanner_cache, update=update)


@contextlib.contextmanager
def _lock(env, config_dir, wait):
    """One preparation at a time: two audits from two worktrees must not
    create, upgrade or remove the same containers concurrently."""
    config_dir.mkdir(parents=True, exist_ok=True)
    with open(config_dir / "sonar.lock", "a") as fh:
        def take():
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError:
                return False
        if not _wait(env, wait, take, step=2):
            raise NotReady("Sonar сейчас готовит другой аудит или fix и не освободил его за %d с."
                           % wait, "повторите аудит, когда закончится другой запуск")
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _ensure_ready(env, *, host, config_dir, legacy_dir, docker_wait, sonar_wait, environ,
                  scanner_cache, update):
    actions, notes = [], []
    local = _is_local(host)
    if local:
        ensure_docker(env, docker_wait, actions)
        container, version = ensure_sonar(env, host, sonar_wait, actions, notes, Path(config_dir),
                                          update=update)
    else:
        # Someone else's server: only read it. Docker, upgrades, the admin
        # password and minting are for the plugin's own local server.
        status, version = _status(env, host)
        if status != "UP":
            raise NotReady("SonarQube %s не отвечает как работающий (%s)." % (host, status),
                           "curl -s %s/api/system/status" % host)
        container = None
    source, token_file = ensure_token(env, host, Path(config_dir), Path(legacy_dir), environ,
                                      actions, allow_admin=local)
    result = {"status": "ready", "host": host, "container": container, "version": version,
              "token_source": source, "token_file": token_file,
              "actions": actions, "notes": notes}
    if scanner_cache is not None:
        scanner_version, scanner = ensure_scanner(env, Path(scanner_cache), actions, notes)
        result.update(scanner=scanner, scanner_version=scanner_version)
    return result


def _is_local(host):
    return (urllib.parse.urlparse(host).hostname or "") in ("localhost", "127.0.0.1", "::1")


def _default_dirs():
    config = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    cache = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    return config / "code-health", config / "sonar-code-health", cache / "code-health" / "sonar-scanner"


def main_with(env, argv):
    config, legacy, cache = _default_dirs()
    parser = argparse.ArgumentParser(prog="sonar_ready.py")
    parser.add_argument("--host", default=env.env.get("SONAR_HOST_URL", "http://localhost:9000"))
    parser.add_argument("--config-dir", default=str(config))
    parser.add_argument("--legacy-dir", default=str(legacy))
    parser.add_argument("--scanner-cache", default=None,
                        help="download the newest native SonarScanner CLI into this directory")
    parser.add_argument("--docker-wait", type=int, default=180)
    parser.add_argument("--sonar-wait", type=int, default=300)
    parser.add_argument("--no-update", action="store_true",
                        help="do not pull a newer SonarQube image")
    args = parser.parse_args(argv)
    try:
        doc = ensure_ready(env, host=args.host.rstrip("/"), config_dir=args.config_dir,
                           legacy_dir=args.legacy_dir, docker_wait=args.docker_wait,
                           sonar_wait=args.sonar_wait, environ=env.env,
                           scanner_cache=args.scanner_cache, update=not args.no_update)
    except OSError as exc:
        stop = NotReady("Ошибка файловой системы: %s." % exc,
                        "проверьте место на диске и права на %s" % (exc.filename or args.config_dir))
        doc = {"status": "failed", "reason": stop.reason, "fix": stop.fix}
        err = "Sonar не готов, аудит остановлен: %s\nИсправить: %s\n" % (stop.reason, stop.fix)
        return 1, json.dumps(doc, ensure_ascii=False, indent=2), err
    except NotReady as stop:
        doc = {"status": "failed", "reason": stop.reason, "fix": stop.fix}
        err = "Sonar не готов, аудит остановлен: %s\nИсправить: %s\n" % (stop.reason, stop.fix)
        return 1, json.dumps(doc, ensure_ascii=False, indent=2), err
    return 0, json.dumps(doc, ensure_ascii=False, indent=2), ""


def main(argv=None):
    code, out, err = main_with(RealEnv(), sys.argv[1:] if argv is None else argv)
    print(out)
    if err:
        sys.stderr.write(err)
    return code


if __name__ == "__main__":
    sys.exit(main())
