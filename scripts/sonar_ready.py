#!/usr/bin/env python3
"""Bring SonarQube up before an audit, or stop the audit with the reason.

Sonar is a precondition of `/code-health:audit`, not an optional layer: an
audit without reliability, security, coverage and maintainability measured
is not worth reading. This script does, in order:

1. Docker: the CLI must exist (never installed here); a stopped daemon is
   started on macOS with `open -a Docker` and awaited for a bounded time.
2. SonarQube: the newest Community Build image is pulled; a healthy
   container is reused, a stopped one started, a missing one created with
   persistent named volumes. A container on an older image is recreated on
   the new one with the same volumes; the data volume is copied first and
   restored if the database migration fails.
3. Token: `SONAR_TOKEN`, else the token file, else (fresh server or a
   stored admin password) a new token is minted into a private file.
4. Scanner: the newest native SonarScanner CLI is downloaded into a cache
   outside any repository.

Output: one JSON document on stdout, never a secret. Exit 0 when ready.
Exit 1 with `status: failed`, a reason and the one command that fixes it;
the same two lines go to stderr in Russian for the person reading.
"""

import argparse
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
DEFAULT_NAME = "sonarqube"
VOLUMES = ("sonarqube_data:/opt/sonarqube/data",
           "sonarqube_extensions:/opt/sonarqube/extensions",
           "sonarqube_logs:/opt/sonarqube/logs")
BACKUP_VOLUME = "code-health-sonarqube-data-backup"
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
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(value + "\n")
    os.chmod(path, 0o600)


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
    if env.run(["docker", "info"])[0] == 0:
        return
    if env.platform != "darwin":
        raise NotReady("Docker установлен, но не запущен.", "sudo systemctl start docker")
    code, _, err = env.run(["open", "-a", "Docker"])
    if code != 0:
        raise NotReady("Не удалось запустить Docker Desktop: %s" % (err.strip() or "ошибка open"),
                       "open -a Docker")
    actions.append("docker_started")
    if not _wait(env, wait, lambda: env.run(["docker", "info"])[0] == 0):
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


def _is_sonar(row):
    image = row.get("Image", "")
    return image.split("/")[-1].split(":")[0] == "sonarqube"


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


def _container_uses_external_db(env, name):
    code, out, _ = env.run(["docker", "inspect", "--format", "{{json .Config.Env}}", name])
    if code != 0:
        return False
    try:
        return any(str(v).startswith("SONAR_JDBC_URL=") for v in json.loads(out or "[]"))
    except ValueError:
        return False


def _docker_run(env, name, image):
    argv = ["docker", "run", "-d", "--name", name, "-p", "9000:9000",
            "-e", "SONAR_ES_BOOTSTRAP_CHECKS_DISABLE=true"]
    for volume in VOLUMES:
        argv += ["-v", volume]
    argv.append(image)
    code, _, err = env.run(argv)
    if code != 0:
        if "port is already allocated" in err or "address already in use" in err:
            raise NotReady("Порт 9000 занят другим процессом, SonarQube не запустить.",
                           "lsof -nP -iTCP:9000 -sTCP:LISTEN  # освободите порт и повторите аудит")
        raise NotReady("Не удалось создать контейнер SonarQube: %s" % err.strip(),
                       "docker run -d --name %s -p 9000:9000 %s %s"
                       % (name, " ".join("-v " + v for v in VOLUMES), image))


def _copy_volume(env, image, src, dst):
    return env.run(["docker", "run", "--rm", "--user", "0", "--entrypoint", "sh",
                    "-v", "%s:/from" % src, "-v", "%s:/to" % dst, image,
                    "-c", "rm -rf /to/* /to/.[!.]* 2>/dev/null; cp -a /from/. /to/"])[0] == 0


def _await_up(env, host, wait, name, allow_migration=True):
    """Wait for UP; run the DB migration when the server asks for one."""
    migrated = False

    def check():
        nonlocal migrated
        status, _ = _status(env, host)
        if status == "DB_MIGRATION_NEEDED" and allow_migration and not migrated:
            env.http("POST", host + "/api/system/migrate_db")
            migrated = True
        if status in ("UP", "DOWN"):
            return status
        return None

    status = _wait(env, wait, check)
    if status == "UP":
        return migrated
    reason = ("SonarQube не поднялся: сервер в состоянии DOWN." if status == "DOWN"
              else "SonarQube не поднялся за %d с." % wait)
    raise NotReady(reason, "docker logs --tail 200 %s" % name)


def _pull(env, actions, notes):
    before = _image_id(env, IMAGE)
    code, _, err = env.run(["docker", "pull", IMAGE], )
    if code != 0:
        notes.append("Образ %s не обновился (%s), работаю на имеющемся."
                     % (IMAGE, (err.strip().splitlines() or ["ошибка docker pull"])[-1]))
        return before
    after = _image_id(env, IMAGE)
    if before and after and before != after:
        actions.append("image_updated")
    return after


def _upgrade(env, host, name, old_image, new_image, wait, actions, notes):
    """Recreate `name` on `new_image` with the same volumes. On a failed
    migration restore the data copy and come back up on `old_image`."""
    if not _copy_volume(env, old_image, "sonarqube_data", BACKUP_VOLUME):
        notes.append("Не удалось сохранить копию данных SonarQube, обновление сервера пропущено.")
        return False
    env.run(["docker", "stop", name])
    env.run(["docker", "rm", name])
    _docker_run(env, name, IMAGE)
    try:
        migrated = _await_up(env, host, wait, name)
    except NotReady as failed:
        env.run(["docker", "stop", name])
        env.run(["docker", "rm", name])
        if not _copy_volume(env, old_image, BACKUP_VOLUME, "sonarqube_data"):
            raise NotReady("Обновление SonarQube не прошло (%s), и данные не удалось вернуть из "
                           "копии." % failed.reason,
                           "docker run --rm -v %s:/from -v sonarqube_data:/to %s sh -c "
                           "'cp -a /from/. /to/'" % (BACKUP_VOLUME, old_image))
        _docker_run(env, name, old_image)
        _await_up(env, host, wait, name, allow_migration=False)
        notes.append("Новая версия SonarQube не поднялась (%s); вернул прежнюю с прежними данными."
                     % failed.reason)
        return False
    actions.append("server_upgraded")
    if migrated:
        actions.append("db_migrated")
    return True


def ensure_sonar(env, host, wait, actions, notes, update=True):
    image_id = _pull(env, actions, notes) if update else _image_id(env, IMAGE)
    rows = _containers(env)
    sonar = [r for r in rows if _is_sonar(r)]
    running = [r for r in sonar if r.get("State") == "running"]
    if running:
        row = running[0]
        name = row["Names"]
    elif sonar:
        row = sonar[0]
        name = row["Names"]
        code, _, err = env.run(["docker", "start", name])
        if code != 0:
            raise NotReady("Контейнер %s не запускается: %s" % (name, err.strip()),
                           "docker logs --tail 200 %s" % name)
        actions.append("container_started")
    else:
        taken = {r.get("Names") for r in rows}
        name = DEFAULT_NAME if DEFAULT_NAME not in taken else "code-health-sonarqube"
        _docker_run(env, name, IMAGE)
        actions.append("container_created")
        row = None
    _await_up(env, host, wait, name)

    if row is not None and update and image_id:
        current = _container_image_id(env, name)
        if current and current != image_id:
            if _container_uses_external_db(env, name):
                notes.append("SonarQube на внешней базе, обновление сервера оставлено владельцу.")
            else:
                _upgrade(env, host, name, current, image_id, wait, actions, notes)
    _, version = _status(env, host)
    return name, version


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


def ensure_token(env, host, config_dir, legacy_dir, environ, actions):
    token_file = config_dir / "sonar-token"
    if environ.get("SONAR_TOKEN") and _valid(env, host, token=environ["SONAR_TOKEN"]):
        return "env", None
    for path in (token_file, legacy_dir / "token"):
        value = _read(path)
        if value and _valid(env, host, token=value):
            return "file", str(path)

    password_file = config_dir / "admin_password"
    for path in (password_file, legacy_dir / "admin_password"):
        password = _read(path)
        if password and _valid(env, host, basic=("admin", password)):
            if _mint(env, host, ("admin", password), token_file, actions):
                return "minted", str(token_file)

    if _valid(env, host, basic=("admin", "admin")):
        new = _new_password()
        code, _ = env.http("POST", host + "/api/users/change_password", basic=("admin", "admin"),
                           form={"login": "admin", "previousPassword": "admin", "password": new})
        if code in (200, 204):
            _write_private(password_file, new)
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
    actions, notes = [], []
    ensure_docker(env, docker_wait, actions)
    container, version = ensure_sonar(env, host, sonar_wait, actions, notes, update=update)
    source, token_file = ensure_token(env, host, Path(config_dir), Path(legacy_dir), environ,
                                      actions)
    result = {"status": "ready", "host": host, "container": container, "version": version,
              "token_source": source, "token_file": token_file,
              "actions": actions, "notes": notes}
    if scanner_cache is not None:
        scanner_version, scanner = ensure_scanner(env, Path(scanner_cache), actions, notes)
        result.update(scanner=scanner, scanner_version=scanner_version)
    return result


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
