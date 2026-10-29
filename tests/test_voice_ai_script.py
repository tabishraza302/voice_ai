import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/voice-ai.sh"


def _isolated_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "web/node_modules/next/dist/bin").mkdir(parents=True)
    shutil.copy2(SCRIPT, repo / "scripts/voice-ai.sh")
    if (ROOT / "bot").exists() and not (repo / "bot").exists():
        os.symlink(ROOT / "bot", repo / "bot")
    if (ROOT / ".venv").exists() and not (repo / ".venv").exists():
        os.symlink(ROOT / ".venv", repo / ".venv")
    return repo


def _start_with_postgres_down(tmp_path, dotenv=None, extra_env=None):
    """An isolated repo whose PostgreSQL readiness check always fails."""
    repo = _isolated_repo(tmp_path)
    if dotenv is not None:
        (repo / ".env").write_text(dotenv)
    capture = tmp_path / "launch.txt"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "sleep").write_text("#!/bin/sh\nexit 0\n")
    (fake_bin / "lsof").write_text("#!/bin/sh\nexit 1\n")
    (fake_bin / "curl").write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        f'  *7860*)\n'
        f'    for _ in $(seq 1 50); do\n'
        f'      if [ -s "{capture}" ]; then exit 0; fi\n'
        f'      /bin/sleep 0.05\n'
        f'    done\n'
        f'    [ -s "{capture}" ]\n'
        f'    ;;\n'
        f'  *)\n'
        f'    exit 0\n'
        f'    ;;\n'
        f'esac\n'
    )
    (fake_bin / "docker").write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "compose" ] && [ "$2" = "exec" ]; then exit 1; fi\n'
        "exit 0\n"
    )
    (fake_bin / "uv").write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$PWD" "$*" >> "$CAPTURE"\n'
    )
    phoenix = fake_bin / "phoenix"
    phoenix.write_text("#!/bin/sh\nexec sleep 60\n")
    for command in ("sleep", "lsof", "curl", "docker", "uv", "phoenix"):
        (fake_bin / command).chmod(0o755)
    env = os.environ.copy()
    # The host's own setting must not decide what this test observes.
    env.pop("ALLOW_SQLITE_FALLBACK", None)
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "PHOENIX_BIN": str(phoenix),
            "CAPTURE": str(capture),
        }
    )
    if extra_env:
        env.update(extra_env)
    return repo, capture, env



def _terminate_pid_file(pid_file):
    if not pid_file.exists():
        return
    try:
        os.kill(int(pid_file.read_text().strip()), 15)
    except (ValueError, ProcessLookupError):
        pass


def test_web_command_launches_next_in_the_web_project_directory(tmp_path):
    repo = _isolated_repo(tmp_path)
    (repo / ".env").write_text("web_port=49187\n")
    next_bin = repo / "web/node_modules/next/dist/bin/next"
    next_bin.write_text("placeholder")
    next_bin.chmod(0o755)
    capture = tmp_path / "launch.txt"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "node").write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$PWD" "$*" "${NEXT_TELEMETRY_DISABLED-<unset>}" > "$CAPTURE"\n'
    )
    (fake_bin / "node").chmod(0o755)
    (fake_bin / "lsof").write_text("#!/bin/sh\nexit 1\n")
    (fake_bin / "lsof").chmod(0o755)
    env = os.environ.copy()
    env.update({"PATH": f"{fake_bin}:{env['PATH']}", "CAPTURE": str(capture)})
    env.pop("WEB_PORT", None)
    env.pop("NEXT_TELEMETRY_DISABLED", None)

    result = subprocess.run(
        [str(repo / "scripts/voice-ai.sh"), "web"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    lines = capture.read_text().splitlines()
    assert lines[0] == str(repo / "web")
    assert lines[1].startswith(f"{next_bin} dev")
    assert lines[1].endswith("-p 49187")
    assert lines[2] == "1"


def test_stop_leaves_an_unrelated_process_with_a_reused_web_pid_alone(tmp_path):
    repo = _isolated_repo(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "pkill").write_text("#!/bin/sh\nexit 1\n")
    (fake_bin / "docker").write_text("#!/bin/sh\nexit 0\n")
    (fake_bin / "ps").write_text(
        "#!/bin/sh\ncase \"$*\" in *lstart=*) echo test-start ;; *command=*) echo 'sleep 30' ;; esac\n"
    )
    for command in ("pkill", "docker", "ps"):
        (fake_bin / command).chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    unrelated = subprocess.Popen(["sleep", "30"])
    try:
        started = "test-start"
        run_dir = repo / "data/run"
        run_dir.mkdir(parents=True)
        web_bin = repo / "web/node_modules/next/dist/bin/next"
        (run_dir / "web.pid").write_text(f"{unrelated.pid}|{started}|{web_bin}\n")

        result = subprocess.run(
            [str(repo / "scripts/voice-ai.sh"), "stop"],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr or result.stdout
        assert unrelated.poll() is None
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=5)


def test_start_web_and_stop_manage_only_the_project_process(tmp_path):
    repo = _isolated_repo(tmp_path)
    capture = tmp_path / "launch.txt"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    next_bin = repo / "web/node_modules/next/dist/bin/next"
    next_bin.write_text("placeholder")
    next_bin.chmod(0o755)
    (fake_bin / "node").write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$PWD" "$*" "${NEXT_TELEMETRY_DISABLED-<unset>}" > "$CAPTURE"\n'
        "trap 'kill \"$child\" 2>/dev/null; wait \"$child\" 2>/dev/null; exit 0' TERM\n"
        "while :; do sleep 1 & child=$!; wait \"$child\"; done\n"
    )
    (fake_bin / "lsof").write_text("#!/bin/sh\nexit 1\n")
    (fake_bin / "ps").write_text(
        f"#!/bin/sh\ncase \"$*\" in *lstart=*) echo test-start ;; *command=*) echo 'node {next_bin} dev' ;; esac\n"
    )
    (fake_bin / "curl").write_text("#!/bin/sh\nexit 0\n")
    (fake_bin / "pkill").write_text("#!/bin/sh\nexit 1\n")
    (fake_bin / "docker").write_text("#!/bin/sh\nexit 0\n")
    phoenix = fake_bin / "phoenix"
    phoenix.write_text("#!/bin/sh\nexec sleep 60\n")
    for command in ("node", "lsof", "ps", "curl", "pkill", "docker", "phoenix"):
        (fake_bin / command).chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "CAPTURE": str(capture),
            "PHOENIX_BIN": str(phoenix),
            "WEB_PORT": "49188",
        }
    )
    env.pop("NEXT_TELEMETRY_DISABLED", None)

    started = subprocess.run(
        [str(repo / "scripts/voice-ai.sh"), "start", "--no-agent", "--web"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert started.returncode == 0, started.stderr or started.stdout
    web_pid = int((repo / "data/run/web.pid").read_text().split("|", 1)[0])
    lines = capture.read_text().splitlines()
    assert lines[0] == str(repo / "web")
    assert lines[2] == "1"

    stopped = subprocess.run(
        [str(repo / "scripts/voice-ai.sh"), "stop"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert stopped.returncode == 0, stopped.stderr or stopped.stdout
    assert not (repo / "data/run/web.pid").exists()
    for _ in range(20):
        try:
            os.kill(web_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("the project-owned web process survived CLI stop")


def test_stt_command_launches_the_lookahead_wrapper_with_the_configured_value(
    tmp_path,
):
    """The lookahead lives in .env and must reach the server process.

    The wrapper is the only place the model context is changed, so the
    launcher has to run it — and has to export the value, because the
    wrapper reads the environment, not the shell's local variables.
    """
    repo = _isolated_repo(tmp_path)
    (repo / "scripts/stt_server.py").write_text("placeholder")
    (repo / ".env").write_text("STT_LOOKAHEAD_FRAMES=6\n")
    capture = tmp_path / "launch.txt"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "uv").write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$PWD" "$*" "${STT_LOOKAHEAD_FRAMES-<unset>}" > "$CAPTURE"\n'
    )
    (fake_bin / "uv").chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "CAPTURE": str(capture),
            "STT_PORT": "49189",
        }
    )
    env.pop("STT_LOOKAHEAD_FRAMES", None)

    result = subprocess.run(
        [str(repo / "scripts/voice-ai.sh"), "stt"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    workdir, command, lookahead = capture.read_text().splitlines()
    assert workdir == str(repo)
    assert command == (
        "run python scripts/stt_server.py --host 127.0.0.1 --port 49189"
    )
    assert lookahead == "6"


def test_lookahead_bench_command_forwards_arguments_to_the_benchmark(tmp_path):
    repo = _isolated_repo(tmp_path)
    (repo / "scripts/bench_stt_lookahead.py").write_text("placeholder")
    capture = tmp_path / "launch.txt"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "uv").write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$PWD" "$*" > "$CAPTURE"\n'
    )
    (fake_bin / "uv").chmod(0o755)
    env = os.environ.copy()
    env.update({"PATH": f"{fake_bin}:{env['PATH']}", "CAPTURE": str(capture)})

    result = subprocess.run(
        [
            str(repo / "scripts/voice-ai.sh"),
            "lookahead-bench",
            "--clips",
            "5",
            "--lookaheads",
            "13,6",
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    workdir, command = capture.read_text().splitlines()
    assert workdir == str(repo)
    assert command == (
        "run python scripts/bench_stt_lookahead.py --clips 5 --lookaheads 13,6"
    )


def test_start_aborts_when_postgres_is_unavailable(tmp_path):
    repo, capture, env = _start_with_postgres_down(tmp_path)

    try:
        started = subprocess.run(
            [str(repo / "scripts/voice-ai.sh"), "start", "--background"],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert started.returncode != 0
        output = started.stdout + started.stderr
        assert "PostgreSQL did not become ready" in output
        assert "falling back to SQLite" not in output
        assert "ALLOW_SQLITE_FALLBACK" not in output
        assert not capture.exists() or "python -m bot.bot" not in capture.read_text()
    finally:
        _terminate_pid_file(repo / "data/phoenix/phoenix.pid")
        _terminate_pid_file(repo / "data/run/agent.pid")

def test_launcher_default_database_url_uses_asyncpg_scheme(tmp_path):
    repo = _isolated_repo(tmp_path)
    capture = tmp_path / "launch.txt"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "uv").write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$DATABASE_URL" > "$CAPTURE"\n'
    )
    (fake_bin / "uv").chmod(0o755)
    env = os.environ.copy()
    env.update({"PATH": f"{fake_bin}:{env['PATH']}", "CAPTURE": str(capture)})
    env.pop("DATABASE_URL", None)
    env.pop("database_url", None)
    env.pop("POSTGRES_PORT", None)

    result = subprocess.run(
        [str(repo / "scripts/voice-ai.sh"), "run"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    captured_url = capture.read_text().strip()
    assert captured_url == "postgresql+asyncpg://voice_ai:voice_ai@127.0.0.1:5439/voice_ai"


def test_launcher_database_url_precedence_environment_over_dotenv(tmp_path):
    repo = _isolated_repo(tmp_path)
    (repo / ".env").write_text(
        "database_url=postgresql+asyncpg://dotenv-user:pass@127.0.0.1:5432/dotenv_db\n"
    )
    capture = tmp_path / "launch.txt"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "uv").write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$DATABASE_URL" > "$CAPTURE"\n'
    )
    (fake_bin / "uv").chmod(0o755)
    env = os.environ.copy()
    env.update({
        "PATH": f"{fake_bin}:{env['PATH']}",
        "CAPTURE": str(capture),
        "database_url": "postgresql+asyncpg://env-user:pass@127.0.0.1:5432/env_db",
    })
    env.pop("DATABASE_URL", None)

    result = subprocess.run(
        [str(repo / "scripts/voice-ai.sh"), "run"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    captured_url = capture.read_text().strip()
    assert captured_url == "postgresql+asyncpg://env-user:pass@127.0.0.1:5432/env_db"


def test_launcher_database_url_precedence_dotenv_over_default(tmp_path):
    repo = _isolated_repo(tmp_path)
    (repo / ".env").write_text(
        "DATABASE_URL=postgresql+asyncpg://dotenv-user:pass@127.0.0.1:5432/dotenv_db\n"
    )
    capture = tmp_path / "launch.txt"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "uv").write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$DATABASE_URL" > "$CAPTURE"\n'
    )
    (fake_bin / "uv").chmod(0o755)
    env = os.environ.copy()
    env.update({"PATH": f"{fake_bin}:{env['PATH']}", "CAPTURE": str(capture)})
    env.pop("DATABASE_URL", None)
    env.pop("database_url", None)

    result = subprocess.run(
        [str(repo / "scripts/voice-ai.sh"), "run"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    captured_url = capture.read_text().strip()
    assert captured_url == "postgresql+asyncpg://dotenv-user:pass@127.0.0.1:5432/dotenv_db"


def test_phoenix_receives_database_url_with_driver_conversion(tmp_path):
    repo = _isolated_repo(tmp_path)
    (repo / ".env").write_text(
        "DATABASE_URL=postgresql+asyncpg://voice_ai:secret@127.0.0.1:5439/voice_ai\n"
    )
    capture = tmp_path / "phoenix_env.txt"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "curl").write_text(
        "#!/bin/sh\n"
        'for _ in $(seq 1 50); do\n'
        '  if [ -s "$CAPTURE" ]; then exit 0; fi\n'
        '  sleep 0.05\n'
        'done\n'
        'exit 1\n'
    )
    (fake_bin / "phoenix").write_text(
        "#!/bin/sh\n"
        'printf "URL=%s\\nSCHEMA=%s\\n" "$PHOENIX_SQL_DATABASE_URL" "$PHOENIX_SQL_DATABASE_SCHEMA" > "$CAPTURE"\n'
        "exec sleep 60\n"
    )
    for command in ("curl", "phoenix"):
        (fake_bin / command).chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "CAPTURE": str(capture),
            "PHOENIX_BIN": str(fake_bin / "phoenix"),
        }
    )
    env.pop("DATABASE_URL", None)
    env.pop("database_url", None)

    try:
        result = subprocess.run(
            [str(repo / "scripts/voice-ai.sh"), "phoenix"],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr or result.stdout
        content = capture.read_text()
        assert "URL=postgresql://voice_ai:secret@127.0.0.1:5439/voice_ai" in content
        assert "SCHEMA=phoenix" in content
    finally:
        _terminate_pid_file(repo / "data/phoenix/phoenix.pid")

def test_start_aborts_when_prometheus_is_unavailable(tmp_path):
    repo = _isolated_repo(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    time_file = tmp_path / "time_counter.txt"
    time_file.write_text("1000\n")
    (fake_bin / "date").write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *+%s*)\n'
        '    if [ -f "$ADVANCE_TIME" ]; then\n'
        f'      current=$(cat "{time_file}" 2>/dev/null || echo 1000)\n'
        f'      next=$((current + 65))\n'
        f'      printf "%s\\n" "$next" > "{time_file}"\n'
        '      printf "%s\\n" "$current"\n'
        '    else\n'
        '      printf "1000\\n"\n'
        '    fi\n'
        '    ;;\n'
        '  *)\n'
        '    printf "1000\\n"\n'
        '    ;;\n'
        'esac\n'
    )
    advance_flag = tmp_path / "advance_time"
    (fake_bin / "sleep").write_text("#!/bin/sh\nexit 0\n")
    (fake_bin / "lsof").write_text("#!/bin/sh\nexit 1\n")
    (fake_bin / "curl").write_text(
        "#!/bin/sh\n"
        f'case "$*" in\n'
        f'  *9091*)\n'
        f'    touch "{advance_flag}"\n'
        f'    exit 1\n'
        f'    ;;\n'
        f'  *)\n'
        f'    exit 0\n'
        f'    ;;\n'
        f'esac\n'
    )
    (fake_bin / "docker").write_text("#!/bin/sh\nexit 0\n")
    phoenix = fake_bin / "phoenix"
    phoenix.write_text("#!/bin/sh\nexec sleep 60\n")
    for command in ("date", "sleep", "lsof", "curl", "docker", "phoenix"):
        (fake_bin / command).chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "PHOENIX_BIN": str(phoenix),
            "ADVANCE_TIME": str(advance_flag),
        }
    )

    try:
        started = subprocess.run(
            [str(repo / "scripts/voice-ai.sh"), "start", "--no-agent"],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert started.returncode != 0
        assert "Prometheus did not become ready" in (started.stdout + started.stderr)
    finally:
        phoenix_pid_file = repo / "data/phoenix/phoenix.pid"
        if phoenix_pid_file.exists():
            try:
                pid = int(phoenix_pid_file.read_text().strip())
                os.kill(pid, 15)
            except (ValueError, ProcessLookupError):
                pass
