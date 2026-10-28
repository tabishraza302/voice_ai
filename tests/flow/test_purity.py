"""Purity and air-gap import boundary tests for bot.flow.

Per docs/first-release/flow-contract.md:
- Section 1 line 28: Importing bot.flow must not initialize MLX, Ollama, TTS, STT, or Pipecat.
- Shared bot/flow must not import bot/agents or SQLAlchemy.
- Reducer performs no network, model, database, filesystem, or payment operation.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from bot.flow import (
    UserTurnAcceptedEvent,
    compile_flow,
    initial_session_state,
    reduce_event,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "flow"


class TestImportPurity:
    """Verify that importing bot.flow in a fresh subprocess loads no providers, ML, DB, or backend services."""

    def test_clean_subprocess_import_boundary(self) -> None:
        cmd = [
            sys.executable,
            "-c",
            (
                "import bot.flow; import sys; "
                "assert 'mlx.core' not in sys.modules, 'mlx.core leaked into bot.flow'; "
                "assert 'pipecat' not in sys.modules, 'pipecat leaked into bot.flow'; "
                "assert 'sqlalchemy' not in sys.modules, 'sqlalchemy leaked into bot.flow'; "
                "assert 'bot.agents' not in sys.modules, 'bot.agents leaked into bot.flow'; "
                "assert 'bot.db' not in sys.modules, 'bot.db leaked into bot.flow'; "
                "assert 'torch' not in sys.modules, 'torch leaked into bot.flow'; "
                "assert 'ollama' not in sys.modules, 'ollama leaked into bot.flow'"
            ),
        ]
        result = subprocess.run(
            cmd,
            cwd=str(Path(__file__).resolve().parent.parent.parent),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Purity check failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"


class TestReducerPurity:
    """Verify that reduce_event is purely functional and deterministic with zero side effects."""

    @pytest.fixture
    def flow_and_state(self):
        with open(FIXTURES_DIR / "valid-credit-card-en-hi.json", "r", encoding="utf-8") as f:
            flow = json.load(f)["flow"]
        compiled = compile_flow(flow)
        state = initial_session_state(compiled, "session:pure")
        return compiled, state

    def test_reducer_input_immutability(self, flow_and_state) -> None:
        compiled, state = flow_and_state
        state_copy = copy.deepcopy(state)

        event = UserTurnAcceptedEvent(
            event_id="e1",
            session_id="session:pure",
            transport_epoch="epoch:1",
            turn_id="t1",
            generation_id="g1",
            text="Hello there",
        )

        red = reduce_event(compiled, state, event)

        # Original state passed into reduce_event was not mutated
        assert state.version == state_copy.version
        assert state.last_accepted_turn_id == state_copy.last_accepted_turn_id
        assert state.generation_id == state_copy.generation_id
        assert state.fields == state_copy.fields
        assert state.steps == state_copy.steps

        # Returned new state has updated version
        assert red.state.version == state.version + 1
        assert red.state.last_accepted_turn_id == "t1"

    def test_reducer_determinism(self, flow_and_state) -> None:
        compiled, state = flow_and_state

        event = UserTurnAcceptedEvent(
            event_id="e1",
            session_id="session:pure",
            transport_epoch="epoch:1",
            turn_id="t1",
            generation_id="g1",
            text="Deterministic test",
        )

        red1 = reduce_event(compiled, state, event)
        red2 = reduce_event(compiled, state, event)

        assert red1.state == red2.state
        assert red1.commands == red2.commands
        assert red1.diagnostics == red2.diagnostics
