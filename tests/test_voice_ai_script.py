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
    return repo


def _start_with_postgres_down(tmp_path, dotenv=None, extra_env=None):
    """An isolated repo whose PostgreSQL readiness check always fails.

    The fake ``uv`` records each launch's working directory, arguments and
    inherited ``ALLOW_SQLITE_FALLBACK`` value, so a test can see exactly
    what the script handed the agent.
    """
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
        'printf "%s\\n" "$PWD" "$*" "${ALLOW_SQLITE_FALLBACK-<unset>}" >> "$CAPTURE"\n'
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


def _agent_fallback_value(capture):
    """The ALLOW_SQLITE_FALLBACK the script handed the agent process."""
    commands = capture.read_text().splitlines()
    agent_index = next(
        index for index, line in enumerate(commands) if "python -m bot.bot" in line
    )
    return commands[agent_index + 1]


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


def test_start_allows_sqlite_fallback_when_postgres_is_unavailable(tmp_path):
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

        assert started.returncode == 0, started.stderr or started.stdout
        output = started.stdout + started.stderr
        assert "PostgreSQL is unavailable; falling back to SQLite" in output
        assert "ALLOW_SQLITE_FALLBACK=1" in output
        assert "will NOT appear in PostgreSQL" in output
        assert _agent_fallback_value(capture) == "1"
    finally:
        _terminate_pid_file(repo / "data/phoenix/phoenix.pid")
        _terminate_pid_file(repo / "data/run/agent.pid")


@pytest.mark.parametrize("value", ["0", "false", "off"])
def test_start_honors_an_environment_opt_out_of_sqlite_fallback(tmp_path, value):
    """An explicit opt-out survives `start`, even when PostgreSQL is down.

    The fallback is the script's choice, not the deployment's. When the
    deployment says 0, the script may not silently turn it back into 1.
    """
    repo, capture, env = _start_with_postgres_down(
        tmp_path, extra_env={"ALLOW_SQLITE_FALLBACK": value}
    )

    try:
        started = subprocess.run(
            [str(repo / "scripts/voice-ai.sh"), "start", "--background"],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert started.returncode == 0, started.stderr or started.stdout
        output = started.stdout + started.stderr
        assert "falling back to SQLite" not in output
        assert "ALLOW_SQLITE_FALLBACK is disabled" in output
        assert _agent_fallback_value(capture) == value
    finally:
        _terminate_pid_file(repo / "data/phoenix/phoenix.pid")
        _terminate_pid_file(repo / "data/run/agent.pid")


@pytest.mark.parametrize("key", ["ALLOW_SQLITE_FALLBACK", "allow_sqlite_fallback"])
def test_start_honors_a_dot_env_opt_out_of_sqlite_fallback(tmp_path, key):
    """`.env` is where the agent itself reads the setting, so it counts too."""
    repo, capture, env = _start_with_postgres_down(tmp_path, dotenv=f"{key}=0\n")

    try:
        started = subprocess.run(
            [str(repo / "scripts/voice-ai.sh"), "start", "--background"],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert started.returncode == 0, started.stderr or started.stdout
        output = started.stdout + started.stderr
        assert "falling back to SQLite" not in output
        assert "ALLOW_SQLITE_FALLBACK is disabled" in output
        # The script does not invent a value here: the agent reads .env
        # itself. What matters is that it was not handed 1.
        assert _agent_fallback_value(capture) in {"<unset>", "0"}
    finally:
        _terminate_pid_file(repo / "data/phoenix/phoenix.pid")
        _terminate_pid_file(repo / "data/run/agent.pid")


@pytest.mark.parametrize(
    ("dotenv_line", "expected_captured"),
    [
        ("Allow_Sqlite_Fallback=0\n", {"<unset>", "0"}),
        ("Allow_SQLite_Fallback=false\n", {"<unset>", "false"}),
        ("ALLOW_sqlite_FALLBACK=off\n", {"<unset>", "off"}),
        ("export ALLOW_SQLITE_FALLBACK=0\n", {"<unset>", "0"}),
        ("  export   Allow_Sqlite_Fallback   =   0\n", {"<unset>", "0"}),
        ("ALLOW_SQLITE_FALLBACK = 0 # inline comment\n", {"<unset>", "0"}),
        ("Allow_Sqlite_Fallback='false' # comment with single quotes\n", {"<unset>", "false"}),
        ('export Allow_SQLite_Fallback="off" # double quotes\n', {"<unset>", "off"}),
    ],
)
def test_start_honors_dotenv_mixed_case_export_and_comments(
    tmp_path, dotenv_line, expected_captured
):
    """Dotenv syntax variants (mixed-case keys, export, spaces, comments) must opt out."""
    repo, capture, env = _start_with_postgres_down(tmp_path, dotenv=dotenv_line)

    try:
        started = subprocess.run(
            [str(repo / "scripts/voice-ai.sh"), "start", "--background"],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert started.returncode == 0, started.stderr or started.stdout
        output = started.stdout + started.stderr
        assert "falling back to SQLite" not in output
        assert "ALLOW_SQLITE_FALLBACK=1" not in output
        assert "ALLOW_SQLITE_FALLBACK is disabled" in output
        assert _agent_fallback_value(capture) in expected_captured
    finally:
        _terminate_pid_file(repo / "data/phoenix/phoenix.pid")
        _terminate_pid_file(repo / "data/run/agent.pid")


@pytest.mark.parametrize(
    "invalid_val",
    ["invalid", "banana", "2", "maybe", ""],
)
def test_start_refuses_fallback_on_invalid_environment_value(tmp_path, invalid_val):
    """An invalid explicit environment setting must fail closed, not convert to opt-in."""
    repo, capture, env = _start_with_postgres_down(
        tmp_path, extra_env={"ALLOW_SQLITE_FALLBACK": invalid_val}
    )

    try:
        started = subprocess.run(
            [str(repo / "scripts/voice-ai.sh"), "start", "--background"],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert started.returncode == 0, started.stderr or started.stdout
        output = started.stdout + started.stderr
        assert "falling back to SQLite" not in output
        assert "ALLOW_SQLITE_FALLBACK=1" not in output
        assert "ALLOW_SQLITE_FALLBACK is invalid" in output
        assert _agent_fallback_value(capture) == invalid_val
    finally:
        _terminate_pid_file(repo / "data/phoenix/phoenix.pid")
        _terminate_pid_file(repo / "data/run/agent.pid")


@pytest.mark.parametrize(
    "dotenv_line",
    [
        "ALLOW_SQLITE_FALLBACK=invalid\n",
        "Allow_Sqlite_Fallback=banana # comment\n",
        "export allow_sqlite_fallback = 2\n",
        "ALLOW_SQLITE_FALLBACK=\n",
    ],
)
def test_start_refuses_fallback_on_invalid_dotenv_value(tmp_path, dotenv_line):
    """An invalid explicit .env setting must fail closed, not convert to opt-in."""
    repo, capture, env = _start_with_postgres_down(tmp_path, dotenv=dotenv_line)

    try:
        started = subprocess.run(
            [str(repo / "scripts/voice-ai.sh"), "start", "--background"],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert started.returncode == 0, started.stderr or started.stdout
        output = started.stdout + started.stderr
        assert "falling back to SQLite" not in output
        assert "ALLOW_SQLITE_FALLBACK=1" not in output
        assert "ALLOW_SQLITE_FALLBACK is invalid" in output
        assert _agent_fallback_value(capture) != "1"
    finally:
        _terminate_pid_file(repo / "data/phoenix/phoenix.pid")
        _terminate_pid_file(repo / "data/run/agent.pid")


def test_start_environment_takes_precedence_over_dotenv(tmp_path):
    """Environment setting overrides conflicting .env setting."""
    # .env opts in (1), but environment opts out (0): environment wins
    repo, capture, env = _start_with_postgres_down(
        tmp_path,
        dotenv="Allow_Sqlite_Fallback=1\n",
        extra_env={"ALLOW_SQLITE_FALLBACK": "0"},
    )
    try:
        started = subprocess.run(
            [str(repo / "scripts/voice-ai.sh"), "start", "--background"],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert started.returncode == 0, started.stderr or started.stdout
        output = started.stdout + started.stderr
        assert "falling back to SQLite" not in output
        assert "ALLOW_SQLITE_FALLBACK=1" not in output
        assert "ALLOW_SQLITE_FALLBACK is disabled" in output
        assert _agent_fallback_value(capture) == "0"
    finally:
        _terminate_pid_file(repo / "data/phoenix/phoenix.pid")
        _terminate_pid_file(repo / "data/run/agent.pid")


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
