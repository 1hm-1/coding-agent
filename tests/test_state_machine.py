from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from coding_agent.domain import InvariantViolation, RunPolicy, RuntimeState, Session
from coding_agent.runtime import StateMachine
from coding_agent.trajectory import JsonlEventStore, TrajectoryRecorder


class StateMachineTest(unittest.TestCase):
    def test_allows_declared_transition_and_rejects_illegal_transition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            recorder = TrajectoryRecorder(JsonlEventStore(Path(temporary)), "session-1")
            machine = StateMachine(recorder)
            session = Session(
                id="session-1",
                task="task",
                source_path="/unused",
                state=RuntimeState.CREATED,
                policy=RunPolicy(),
            )

            machine.transition(
                session,
                RuntimeState.PREPARING_WORKSPACE,
                reason="test",
            )
            self.assertIs(session.state, RuntimeState.PREPARING_WORKSPACE)
            with self.assertRaises(InvariantViolation):
                machine.transition(session, RuntimeState.COMPLETED, reason="illegal")


if __name__ == "__main__":
    unittest.main()

