"""Edge0 als Modellserver: Gateway (OpenAI-SSE), Umschalten in der Konfiguration, gemischter Betrieb, Run und Kalibrierung."""

from __future__ import annotations

import json
import threading
import time

import pytest
from orchestrator_support import FakeBroker, wait_for

from localai.contracts import ActivityKind, AgentState, OrchestratorCommandKind
from localai.orchestrator import calibrate, hardware
from localai.orchestrator.activity import ActivityReporter
from localai.orchestrator.fake_edge0 import CHAT_FIELDS, FakeEdge0
from localai.orchestrator.fake_ollama import FakeOllama
from localai.orchestrator.gateway import (
    Cancelled,
    CancelToken,
    ChatRequest,
    Edge0Gateway,
    GatewayError,
    OllamaGateway,
    OutOfMemory,
    RoutingGateway,
    StreamCallbacks,
)
from localai.orchestrator.models import CalibrationStore, ConfigError, ModelsConfig
from localai.orchestrator.run import Orchestrator

ALIAS = "edge0_35b"


@pytest.fixture
def edge0():
    fake = FakeEdge0().start()
    yield fake
    fake.stop()


@pytest.fixture
def ollama():
    fake = FakeOllama().start()
    yield fake
    fake.stop()


def request(model=ALIAS, text="hi", **kwargs):
    return ChatRequest(model=model, messages=[{"role": "system", "content": "You are the Coder agent"},
                                              {"role": "user", "content": text}], num_ctx=8192, num_predict=256, **kwargs)


def gateway(fake, **kwargs):
    return Edge0Gateway(fake.url, **{"poll": 0.02, "busy_retry": 0.02, **kwargs})


def edge0_config(fake_url, **extra):
    raw = {"requireCalibration": False, "backend": "edge0", "edge0": {"url": fake_url, "stallWarningSeconds": 5,
                                                                         "stallAbortSeconds": 60},
           "profiles": [{"name": "edge0", "model": ALIAS, "contextTokens": 8192, "maxOutputTokens": 2048}], **extra}
    return ModelsConfig.parse(raw)


# ---------------------------------------------------------------------- Gateway

def test_edge0_streamt_thinking_inhalt_und_kennzahlen(edge0):
    edge0.responder = lambda body: ("<think>inline idea</think>Answer text", "native thought")
    chunks, thoughts = [], []
    result = gateway(edge0).chat(request(), callbacks=StreamCallbacks(on_content=chunks.append, on_thinking=thoughts.append))
    assert result.content == "Answer text" == "".join(chunks)
    assert result.thinking == "native thoughtinline idea" == "".join(thoughts)
    assert result.prompt_tokens > 0 and result.output_tokens > 0 and result.first_token_seconds is not None
    assert result.done_reason == "stop" and result.model == ALIAS and result.total_seconds >= result.output_seconds
    sent = edge0.requests[-1]
    assert set(sent) <= CHAT_FIELDS and "num_ctx" not in json.dumps(sent), "Kontext legt der Serverstart fest"
    assert sent["max_tokens"] == 256 and sent["stream"] is True and sent["stream_options"] == {"include_usage": True}
    assert "reasoning_effort" not in sent and not edge0.rejected


def test_edge0_sendet_nur_bekannte_felder(edge0):
    req = request(extra_options={"repeat_penalty": 1.15, "repeat_last_n": 256, "top_k": 40.0, "seed": 7.0})
    req.messages[1]["thinking"] = "Ollama-Feld, das Edge0 nicht kennt"
    result = gateway(edge0).chat(req)
    sent = edge0.requests[-1]
    assert result.content and not edge0.rejected
    assert sent["repetition_penalty"] == 1.15 and sent["top_k"] == 40 and sent["seed"] == 7
    assert isinstance(sent["top_k"], int) and "repeat_last_n" not in sent
    assert sent["messages"][1] == {"role": "user", "content": "hi"}


def test_edge0_denkphase_abschalten_und_rueckfall(edge0):
    gw = gateway(edge0)
    gw.chat(request(think=False))
    assert edge0.requests[-1]["reasoning_effort"] == "none"
    gw.chat(request(reasoning_effort="low"))
    assert edge0.requests[-1]["reasoning_effort"] == "low"
    # Server kennt reasoning_effort nicht: think=false wird ohne das Feld wiederholt und danach nicht mehr gesendet.
    edge0.reject_reasoning_effort = True
    gw = gateway(edge0)
    assert gw.chat(request(think=False)).content
    assert edge0.rejected[-1] == 400 and "reasoning_effort" not in edge0.requests[-1]
    count = len(edge0.requests)
    gw.chat(request(think=False))
    assert len(edge0.requests) == count + 1 and "reasoning_effort" not in edge0.requests[-1]
    # Ausdrücklich im Profil gesetzt: der Fehler wird gemeldet statt still übergangen.
    with pytest.raises(GatewayError, match="reasoning_effort"):
        gw.chat(request(reasoning_effort="low"))


def test_edge0_belegt_wartet_und_gibt_auf(edge0):
    edge0.busy = 3
    assert gateway(edge0).chat(request()).content
    assert edge0.rejected.count(429) == 3
    edge0.busy = 10_000
    started = time.monotonic()
    with pytest.raises(GatewayError, match="belegt"):
        gateway(edge0, busy_wait=0.3).chat(request())
    assert time.monotonic() - started < 5
    edge0.busy = 0
    # Zwei gleichzeitige Anfragen: Edge0 rechnet nur eine – die zweite wartet und kommt danach an die Reihe.
    edge0.delay = 0.01
    gw = gateway(edge0)
    results = []
    threads = [threading.Thread(target=lambda: results.append(gw.chat(request()))) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert len(results) == 2 and all(r.content for r in results)


def test_edge0_abbruch_fehler_und_speichermangel(edge0):
    edge0.delay = 0.05
    edge0.responder = lambda body: ("x" * 3000, "")
    cancel = CancelToken()
    received = []

    def on_content(text):
        received.append(text)
        if len(received) == 5:
            cancel.cancel("Nutzer bricht ab")

    started = time.monotonic()
    with pytest.raises(Cancelled, match="Nutzer"):
        gateway(edge0).chat(request(), cancel, StreamCallbacks(on_content=on_content))
    assert time.monotonic() - started < 3
    wait_for(lambda: edge0.disconnects == 1, timeout=5)
    edge0.delay = 0.0
    edge0.responder = lambda body: ("some answer text", "")
    with pytest.raises(GatewayError, match="not found"):
        gateway(edge0).chat(request(model="anderes_modell"))
    edge0.stream_error = "CUDA error: out of memory"
    with pytest.raises(OutOfMemory):
        gateway(edge0).chat(request())
    edge0.stream_error = None
    edge0.drop_after_chunks = 2
    with pytest.raises(GatewayError, match="vorzeitig"):
        gateway(edge0).chat(request())
    edge0.drop_after_chunks = None
    edge0.error = "engine failed"
    with pytest.raises(GatewayError, match="engine failed"):
        gateway(edge0).chat(request())
    with pytest.raises(GatewayError, match="nicht erreichbar"):
        Edge0Gateway("http://127.0.0.1:9").version()


def test_edge0_funktionsaufrufe_und_abgeschnittene_antwort(edge0):
    edge0.responder = lambda body: ("", "", [{"name": "read_file", "arguments": {"path": "src/a.py"}}])
    result = gateway(edge0).chat(request())
    assert result.tool_calls == [{"name": "read_file", "arguments": {"path": "src/a.py"}}]
    assert result.done_reason == "tool_calls"
    edge0.responder = lambda body: ("abgeschnitten", "")
    edge0.finish_reasons = ["length"]
    assert gateway(edge0).chat(request()).done_reason == "length"


def test_edge0_version_modelle_und_token(edge0, tmp_path, monkeypatch):
    gw = gateway(edge0)
    assert gw.version() == "0.1.0-fake"
    assert gw.list_models() == [{"name": ALIAS, "model": ALIAS, "digest": ""}]
    assert gw.running() == [] and gw.show(ALIAS)["id"] == ALIAS
    assert edge0.authorizations[-1] is None, "auf Loopback ohne Token"
    edge0.api_key = "geheim"
    with pytest.raises(GatewayError, match="Token"):
        gateway(edge0).chat(request())
    key = tmp_path / "edge0.key"
    key.write_text("geheim\n", encoding="utf-8")
    assert gateway(edge0, api_key_file=str(key)).chat(request()).content
    assert edge0.authorizations[-1] == "Bearer geheim"
    monkeypatch.setenv("LOCALAI_EDGE0_API_KEY", "geheim")
    assert gateway(edge0).list_models()
    monkeypatch.delenv("LOCALAI_EDGE0_API_KEY")
    with pytest.raises(GatewayError, match="nicht lesbar"):
        gateway(edge0, api_key_file=str(tmp_path / "fehlt.key")).version()
    # Edge0 im LAN ohne Token: klare Meldung, bevor überhaupt verbunden wird.
    lan = Edge0Gateway("http://192.168.178.39:8000", allow_lan=True)
    with pytest.raises(GatewayError, match="verlangt ein Token"):
        lan.version()
    with pytest.raises(GatewayError, match="allowLan"):
        Edge0Gateway("http://192.168.178.39:8000")


# ---------------------------------------------------------------------- Konfiguration

def test_konfiguration_schaltet_zwischen_ollama_und_edge0(edge0):
    config = edge0_config(edge0.url)
    gw = config.gateway()
    assert isinstance(gw, Edge0Gateway) and gw.url == edge0.url and gw.stall_warning == 5
    assert config.primary_url == edge0.url and config.backends_in_use() == ["edge0"]
    assert "Edge0" in config.profiles["edge0"].label() and config.roles["Coder"] == ["edge0"]
    default = ModelsConfig.parse({"profiles": [
        {"name": "s", "model": "qwen2.5-coder:14b", "contextTokens": 8192, "maxOutputTokens": 2048}]})
    assert isinstance(default.gateway(), OllamaGateway) and default.backend == "ollama"
    assert "Edge0" not in default.profiles["s"].label()
    effort = edge0_config(edge0.url, profiles=[{"name": "e", "model": ALIAS, "contextTokens": 8192,
                                                "maxOutputTokens": 2048, "reasoningEffort": "low"}])
    assert effort.profiles["e"].reasoning_effort == "low"
    lan = edge0_config(edge0.url, edge0={"url": "http://192.168.178.39:8000", "allowLan": True,
                                          "apiKeyFile": "C:\\Edge0\\edge0.key"})
    assert lan.edge0.allow_lan and lan.edge0.api_key_file == "C:\\Edge0\\edge0.key"


def test_edge0_vorlage_ist_gueltig():
    from localai.orchestrator.models import merge, repo_root
    base = json.loads((repo_root() / "config" / "models.json").read_text(encoding="utf-8"))
    overlay = json.loads((repo_root() / "config" / "models.edge0.example.json").read_text(encoding="utf-8"))
    config = ModelsConfig.parse(merge(base, overlay))
    assert config.backend == "edge0" and config.backends_in_use() == ["edge0"] and list(config.profiles) == ["edge0"]
    assert config.edge0.url == "http://127.0.0.1:8000" and config.edge0.busy_wait_seconds == 600
    assert all(names == ["edge0"] for names in config.roles.values())
    assert isinstance(config.gateway(), Edge0Gateway)


@pytest.mark.parametrize("change, message", [
    ({"backend": "vllm"}, "unbekannter Modellserver"),
    ({"profiles": [{"name": "o", "model": "qwen2.5-coder:14b", "contextTokens": 8192, "maxOutputTokens": 2048,
                    "backend": "ollama", "reasoningEffort": "low"}]}, "nur für Edge0"),
    ({"profiles": [{"name": "a", "model": "qwen3:8b", "contextTokens": 8192, "maxOutputTokens": 2048},
                   {"name": "b", "model": "qwen3:8b", "contextTokens": 4096, "maxOutputTokens": 1024, "backend": "ollama"}]},
     "verschiedenen Modellservern"),
    ({"profiles": [{"name": "a", "model": "edge0 35b!", "contextTokens": 8192, "maxOutputTokens": 2048}]}, "Alias"),
    ({"edge0": {"url": "http://127.0.0.1:8000", "apiKeyFile": "edge0.key"}}, "absoluter Pfad"),
    ({"edge0": {"url": "http://192.168.178.39:8000"}}, "allowLan"),
    ({"edge0": {"url": "http://edge0.example.com:8000", "allowLan": True}}, "private LAN-IP"),
])
def test_ungueltige_edge0_konfiguration(change, message):
    with pytest.raises(ConfigError, match=message):
        edge0_config("http://127.0.0.1:8000", **change)


def test_gemischte_modellserver_verteilen_nach_modell(edge0, ollama):
    config = ModelsConfig.parse({
        "requireCalibration": False, "ollama": {"url": ollama.url}, "edge0": {"url": edge0.url},
        "profiles": [{"name": "standard", "model": "qwen2.5-coder:14b", "contextTokens": 8192, "maxOutputTokens": 2048},
                     {"name": "edge0", "model": ALIAS, "contextTokens": 8192, "maxOutputTokens": 2048,
                      "backend": "edge0"}],
        "roles": {"Coder": ["edge0", "standard"], "Reviewer": ["standard"]}})
    gw = config.gateway()
    assert isinstance(gw, RoutingGateway) and gw.url == ollama.url and config.backends_in_use() == ["ollama", "edge0"]
    names = {m["name"] for m in gw.list_models()}
    assert ALIAS in names and "qwen2.5-coder:14b" in names
    assert gw.chat(request()).content and edge0.requests and not ollama.requests
    assert gw.chat(request(model="qwen2.5-coder:14b")).content and len(ollama.requests) == 1
    assert "Ollama 0.0.0-fake" in gw.version() and "Edge0 0.1.0-fake" in gw.version()
    edge0.stop()
    names = {m["name"] for m in gw.list_models()}
    assert ALIAS not in names and "qwen2.5-coder:14b" in names and "edge0" in gw.unreachable
    assert "Edge0 nicht erreichbar" in gw.version()


# ---------------------------------------------------------------------- Orchestrator und Kalibrierung

def _finished(broker):
    return broker.summary() and any(e.kind == ActivityKind.State and e.state == AgentState.Done
                                    and "abgeschlossen" in (e.text or "") for e in broker.of("Orchestrator"))


def _orchestrator(broker, config, gw, tmp_path):
    orch = Orchestrator(broker, broker, "run-e", config, tmp_path, gateway=gw,
                        detect=lambda: hardware.Hardware(4, None, None, []),
                        reporter=ActivityReporter("run-e", broker.report_activity, interval=0.02))
    thread = threading.Thread(target=orch.serve, daemon=True)
    thread.start()
    return orch, thread


def test_run_mit_fuenf_rollen_ueber_edge0(edge0, tmp_path):
    edge0.delay = 0.002
    broker = FakeBroker(decision_delay=0.05)
    config = edge0_config(edge0.url)
    orch, thread = _orchestrator(broker, config, Edge0Gateway(edge0.url, 5, 60, poll=0.02, busy_retry=0.02), tmp_path)
    broker.send(OrchestratorCommandKind.Chat, "Bitte leere Eingaben in calc abfangen")
    wait_for(lambda: _finished(broker), timeout=60)
    for role in ("Planner", "Coder", "Tester", "Debugger", "Reviewer"):
        assert AgentState.Done in [e.state for e in broker.of(role) if e.kind == ActivityKind.State], role
    assert edge0.completed >= 5 and all(r["model"] == ALIAS for r in edge0.requests)
    assert 400 not in edge0.rejected and 404 not in edge0.rejected, "nur Felder, die Edge0 kennt"
    assert any("Edge0 0.1.0-fake" in (e.text or "") for e in broker.of("Orchestrator"))
    orch.stop()
    thread.join(5)


def test_edge0_nicht_erreichbar_meldet_deutsch(tmp_path):
    broker = FakeBroker()
    config = edge0_config("http://127.0.0.1:9")
    orch, thread = _orchestrator(broker, config, config.gateway(), tmp_path)
    broker.send(OrchestratorCommandKind.Chat, "Ziel")
    wait_for(lambda: any("Edge0 ist nicht erreichbar" in c and "edge0-infer serve" in c
                         for c in broker.chats("Orchestrator")))
    assert not broker.proposals
    orch.stop()
    thread.join(5)


def test_kalibrierung_ueber_edge0(edge0, tmp_path, capsys):
    models = tmp_path / "models.json"
    models.write_text(json.dumps({"backend": "edge0", "profiles": [
        {"name": "edge0", "model": ALIAS, "contextTokens": 4096, "maxOutputTokens": 1024, "think": True}],
        "calibration": {"runs": 10, "outputTokens": 16}}), encoding="utf-8")
    code = calibrate.main(["--models", str(models), "--daten", str(tmp_path), "--edge0", edge0.url, "--ohne-vram-messung"])
    out = capsys.readouterr().out
    measurement = CalibrationStore(tmp_path / "calibration.json").measurements[f"{ALIAS}@4096"]
    assert measurement.stable and measurement.output_tps > 0 and 3.0 <= measurement.chars_per_token <= 4.0, out
    assert "Edge0 0.1.0-fake" in out and code == 0
    assert all(r.get("reasoning_effort") == "none" for r in edge0.requests), "think-Profil: ohne Denkphase messen"
    report = next(tmp_path.glob("kalibrierung-*.md")).read_text(encoding="utf-8")
    assert "Modellserver: Edge0 0.1.0-fake" in report
