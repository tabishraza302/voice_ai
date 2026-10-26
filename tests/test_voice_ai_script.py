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
        "#!/bin/sh\nprintf '%s\\n' \"$PWD\" \"$*\" > \"$CAPTURE\"\n"
    )
    (fake_bin / "node").chmod(0o755)
    (fake_bin / "lsof").write_text("#!/bin/sh\nexit 1\n")
    (fake_bin / "lsof").chmod(0o755)
    env = os.environ.copy()
    env.update({"PATH": f"{fake_bin}:{env['PATH']}", "CAPTURE": str(capture)})
    env.pop("WEB_PORT", None)

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
        "#!/bin/sh\nprintf '%s\\n' \"$PWD\" \"$*\" > \"$CAPTURE\"\n"
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
    assert capture.read_text().splitlines()[0] == str(repo / "web")

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
