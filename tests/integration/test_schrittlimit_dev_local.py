"""Rückfrage beim Schrittlimit über den echten Broker: nur der laufende Orchestrator fragt, nur die Oberfläche
antwortet, die Antwort erreicht den Orchestrator als Auftrag LimitAnswer."""

from __future__ import annotations

import json

import pytest

from localai.contracts import LimitAction, LimitAnswer, LimitQuestion, OrchestratorCommandKind
from localai.pipe import BrokerError

from test_orchestrator_broker import setup

pytestmark = pytest.mark.integration


def question(run_id: str, qid: str = "q1", rest: str = "## Previous steps\n- 1: list") -> LimitQuestion:
    return LimitQuestion(question_id=qid, run_id=run_id, agent="Coder", task_id="T1", title="Funktion ergänzen", steps=20,
                         suggested_steps=10, system="You are the Coder agent.", task="Leere Eingaben abfangen", rest=rest)


def test_rueckfrage_und_antwort_ueber_den_broker(tmp_path, broker_factory):
    b = setup(tmp_path, broker_factory, ["-c", "import time; time.sleep(120)"])
    ui, orch = b.ui(), b.orchestrator()
    run_id = ui.chat_send("Start")
    after = max((c.seq for c in orch.wait_commands(0, 500)), default=0)

    with pytest.raises(BrokerError) as wrong_run:
        orch.limit_ask(question("run-fremd"))
    assert wrong_run.value.code == 409
    with pytest.raises(BrokerError) as ui_asks:
        ui.limit_ask(question(run_id))
    assert ui_asks.value.code == 403

    big = "x" * 300_000  # größer als ein Chat-Auftrag (16 000 Zeichen)
    assert orch.limit_ask(question(run_id, rest=big)) == "q1"
    pending = ui.limit_pending()
    assert [q.question_id for q in pending] == ["q1"] and pending[0].rest == big and pending[0].created_at
    with pytest.raises(BrokerError) as orch_answers:
        orch.limit_answer(LimitAnswer(question_id="q1", action=LimitAction.Stop))
    assert orch_answers.value.code == 403
    with pytest.raises(BrokerError) as no_steps:
        ui.limit_answer(LimitAnswer(question_id="q1", action=LimitAction.Continue, extra_steps=0, system="s", task="t", rest=""))
    assert no_steps.value.code == 422

    ui.limit_answer(LimitAnswer(question_id="q1", action=LimitAction.Continue, extra_steps=5, system="s", task="Neu", rest=big))
    assert ui.limit_pending() == []
    with pytest.raises(BrokerError) as twice:
        ui.limit_answer(LimitAnswer(question_id="q1", action=LimitAction.Stop))
    assert twice.value.code == 410
    command = next(c for c in orch.wait_commands(after, 2000) if c.kind == OrchestratorCommandKind.LimitAnswer)
    answer = LimitAnswer.model_validate(json.loads(command.text))
    assert command.agent == "Coder" and answer.extra_steps == 5 and answer.task == "Neu" and answer.rest == big

    # Zurückziehen und Abbruch des Runs räumen offene Fragen ab.
    orch.limit_ask(question(run_id, "q2"))
    assert orch.limit_withdraw("q2") is True and ui.limit_pending() == []
    orch.limit_ask(question(run_id, "q3"))
    ui.run_cancel()
    assert ui.limit_pending() == []
