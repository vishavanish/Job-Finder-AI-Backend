"""
Start the whole stack for local development with one command:

    dev            (from job_finder_api/, in PowerShell or cmd — runs dev.cmd)
    backend        (same, but only the API + worker — run the frontend yourself)
    python run_dev.py [--backend-only]

Runs database migrations, then the API (http://127.0.0.1:8000), the Celery
worker, Celery beat (job alerts) and the frontend (http://localhost:5173) in this one terminal, each
line prefixed with where it came from. Ctrl+C stops all three.

Local runs use output/dev.db (SQLite), never the Supabase DATABASE_URL in
.env, and only the *_dev Celery queues. Email sign-in codes are printed
in the [api] lines.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
FRONTEND = BACKEND.parent / "job-finder-frontend"
VENV_PYTHON = BACKEND.parent / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
PYTHON = str(VENV_PYTHON if VENV_PYTHON.exists() else Path(sys.executable))
NPM = shutil.which("npm") or "npm"

DEV_ENV = {
    "ENVIRONMENT": "development",
    "DATABASE_URL": "sqlite:///./output/dev.db",
    "REFRESH_COOKIE_SECURE": "false",
    "REFRESH_COOKIE_SAMESITE": "lax",
    "PYTHONUNBUFFERED": "1",
}

COLORS = {"api": "\033[36m", "worker": "\033[35m", "beat": "\033[34m", "web": "\033[32m", "dev": "\033[33m"}
RESET = "\033[0m"
_print_lock = threading.Lock()


def say(name: str, line: str) -> None:
    with _print_lock:
        print(f"{COLORS.get(name, '')}[{name:<6}]{RESET} {line}", flush=True)


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def fail(message: str) -> None:
    say("dev", message)
    sys.exit(1)


def preflight(with_web: bool) -> dict:
    if with_web and not FRONTEND.exists():
        fail(f"frontend repo not found at {FRONTEND} — clone Job-Finder-AI-Frontend next to job_finder_api")
    if not (BACKEND / ".env").exists():
        fail("job_finder_api/.env is missing — it needs at least JWT_SECRET_KEY and REDIS_URL")
    ports = ((8000, "API"), (5173, "frontend")) if with_web else ((8000, "API"),)
    for port, what in ports:
        if port_in_use(port):
            fail(f"port {port} ({what}) is already in use — stop whatever is running there first")
    (BACKEND / "output").mkdir(exist_ok=True)

    env = {**os.environ, **DEV_ENV}
    if with_web and not (FRONTEND / "node_modules").exists():
        say("dev", "installing frontend dependencies (first run only)...")
        subprocess.run([NPM, "install"], cwd=FRONTEND, check=True, shell=os.name == "nt")
    return env


def migrate(env: dict) -> None:
    say("dev", "applying database migrations to output/dev.db ...")
    result = subprocess.run([PYTHON, "-m", "alembic", "upgrade", "head"], cwd=BACKEND, env=env,
                            capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stdout + result.stderr)
        fail("migrations failed — see the output above")


def start(name: str, cmd: list[str], cwd: Path, env: dict) -> subprocess.Popen:
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
        shell=(os.name == "nt" and name == "web"), creationflags=flags,
    )

    def pump() -> None:
        for line in proc.stdout:
            say(name, line.rstrip())
        say(name, f"exited with code {proc.wait()}")

    threading.Thread(target=pump, daemon=True).start()
    return proc


def stop_all(procs: dict[str, subprocess.Popen]) -> None:
    for name, proc in procs.items():
        if proc.poll() is not None:
            continue
        say("dev", f"stopping {name} ...")
        if os.name == "nt":
            # /T kills the whole tree (uvicorn's reloader, npm -> node -> vite).
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        else:
            proc.terminate()
    for proc in procs.values():
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> None:
    os.system("")  # enables ANSI colours in the Windows console
    with_web = "--backend-only" not in sys.argv[1:]
    env = preflight(with_web)
    migrate(env)

    procs = {
        "api": start("api", [PYTHON, "-m", "uvicorn", "app.main:app", "--reload",
                             "--host", "127.0.0.1", "--port", "8000"], BACKEND, env),
        "worker": start("worker", [PYTHON, "-m", "celery", "-A", "app.core.celery_app:celery_app", "worker",
                                   "--pool=solo", "--loglevel=info", "-Q", "default_dev,apply_dev,maintenance_dev,email_dev"],
                        BACKEND, env),
        # One beat process: starts due job alerts (P3.2).
        "beat": start("beat", [PYTHON, "-m", "celery", "-A", "app.core.celery_app:celery_app", "beat",
                               "--loglevel=info", "--schedule", str(BACKEND / "output" / "celerybeat-schedule")],
                      BACKEND, env),
    }
    if with_web:
        procs["web"] = start("web", [NPM, "run", "dev"], FRONTEND, env)
        say("dev", "starting... open http://localhost:5173 once [web] shows 'Local:'. Press Ctrl+C to stop everything.")
    else:
        say("dev", "starting API (http://127.0.0.1:8000) + worker. Press Ctrl+C to stop both.")

    try:
        while True:
            for name, proc in procs.items():
                if proc.poll() is not None:
                    say("dev", f"{name} stopped unexpectedly — shutting the rest down")
                    raise KeyboardInterrupt
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        stop_all(procs)
        say("dev", "all stopped.")


if __name__ == "__main__":
    main()
