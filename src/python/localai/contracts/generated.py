"""Automatisch erzeugt aus contracts/schemas/contracts.schema.json – nicht von Hand ändern."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    def to_json_obj(self) -> dict[str, Any]:
        """JSON-Form wie in C#: camelCase, None-Felder weggelassen."""
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


class AgentRole(str, Enum):
    Planner = "Planner"
    Coder = "Coder"
    Tester = "Tester"
    Debugger = "Debugger"
    Reviewer = "Reviewer"


class ProposalState(str, Enum):
    AwaitingApproval = "AwaitingApproval"
    Approved = "Approved"
    Running = "Running"
    Succeeded = "Succeeded"
    Failed = "Failed"
    Cancelled = "Cancelled"
    OutcomeUnknown = "OutcomeUnknown"
    Denied = "Denied"
    Expired = "Expired"
    Invalidated = "Invalidated"
    Superseded = "Superseded"
    Rejected = "Rejected"


class DecisionKind(str, Enum):
    Approve = "Approve"
    Deny = "Deny"


class ResultStatus(str, Enum):
    Succeeded = "Succeeded"
    Failed = "Failed"
    Cancelled = "Cancelled"
    OutcomeUnknown = "OutcomeUnknown"
    SkippedByUser = "SkippedByUser"


class ToolTarget(Contract):
    """Ein einzeln abwählbares Ziel eines Tool-Aufrufs (Datei, Dateibereich, Patch-Datei)."""

    target_id: str = Field(alias="targetId")
    path: str = Field(alias="path")
    expected_sha256: str | None = Field(default=None, alias="expectedSha256")
    line_start: StrictInt | None = Field(default=None, alias="lineStart")
    line_end: StrictInt | None = Field(default=None, alias="lineEnd")


class ProposalRequest(Contract):
    """Vom Orchestrator eingereichter Vorschlag. Serverfelder (ID, Zeiten, Policyrevision) setzt ausschließlich der Broker."""

    schema_version: StrictInt = Field(alias="schemaVersion")
    run_id: str = Field(alias="runId")
    task_id: str = Field(alias="taskId")
    agent: AgentRole = Field(alias="agent")
    tool: str = Field(alias="tool")
    tool_version: str = Field(alias="toolVersion")
    purpose: str = Field(alias="purpose")
    arguments: dict[str, Any] = Field(alias="arguments")
    targets: list[ToolTarget] = Field(alias="targets")
    snapshot_id: str = Field(alias="snapshotId")
    idempotency_key: str = Field(alias="idempotencyKey")


class ToolProposal(Contract):
    """Unveränderlicher Vorschlag. Sein SHA-256 über das kanonische JSON ist die Grundlage jeder Entscheidung."""

    schema_version: StrictInt = Field(alias="schemaVersion")
    proposal_id: str = Field(alias="proposalId")
    parent_proposal_id: str | None = Field(default=None, alias="parentProposalId")
    run_id: str = Field(alias="runId")
    task_id: str = Field(alias="taskId")
    agent: AgentRole = Field(alias="agent")
    tool: str = Field(alias="tool")
    tool_version: str = Field(alias="toolVersion")
    purpose: str = Field(alias="purpose")
    arguments: dict[str, Any] = Field(alias="arguments")
    targets: list[ToolTarget] = Field(alias="targets")
    snapshot_id: str = Field(alias="snapshotId")
    policy_revision: StrictInt = Field(alias="policyRevision")
    sandbox_id: str = Field(alias="sandboxId")
    created_at: str = Field(alias="createdAt")
    expires_at: str = Field(alias="expiresAt")


class ProposalView(Contract):
    proposal: ToolProposal = Field(alias="proposal")
    proposal_sha256: str = Field(alias="proposalSha256")
    state: ProposalState = Field(alias="state")


class UserDecision(Contract):
    proposal_id: str = Field(alias="proposalId")
    proposal_sha256: str = Field(alias="proposalSha256")
    decision: DecisionKind = Field(alias="decision")


class NarrowRequest(Contract):
    """Sammelfreigabe: behält nur die genannten Ziele. Erzeugt einen neuen Vorschlag mit neuem Hash."""

    proposal_id: str = Field(alias="proposalId")
    proposal_sha256: str = Field(alias="proposalSha256")
    keep_target_ids: list[str] = Field(alias="keepTargetIds")


class TargetResult(Contract):
    target_id: str = Field(alias="targetId")
    status: ResultStatus = Field(alias="status")
    content_sha256: str | None = Field(default=None, alias="contentSha256")
    text: str | None = Field(default=None, alias="text")
    message: str | None = Field(default=None, alias="message")


class ToolResult(Contract):
    """Ergebnis einer Ausführung. Inhalte sind immer untrusted."""

    invocation_id: str = Field(alias="invocationId")
    proposal_id: str = Field(alias="proposalId")
    status: ResultStatus = Field(alias="status")
    exit_code: StrictInt | None = Field(default=None, alias="exitCode")
    snapshot_id: str = Field(alias="snapshotId")
    target_results: list[TargetResult] = Field(alias="targetResults")
    skipped_target_ids: list[str] | None = Field(default=None, alias="skippedTargetIds")
    output_sha256: str = Field(alias="outputSha256")
    truncated: StrictBool = Field(alias="truncated")
    untrusted: StrictBool = Field(alias="untrusted")


class ProposalOutcome(Contract):
    """Endstand eines Vorschlags; folgt der Kette, falls der Nutzer per Abwahl einen neuen Vorschlag erzeugt hat."""

    requested_proposal_id: str = Field(alias="requestedProposalId")
    final_proposal_id: str = Field(alias="finalProposalId")
    state: ProposalState = Field(alias="state")
    is_final: StrictBool = Field(alias="isFinal")
    result: ToolResult | None = Field(default=None, alias="result")


class FolderRight(str, Enum):
    List = "List"
    Read = "Read"
    Create = "Create"
    Modify = "Modify"
    Delete = "Delete"
    Execute = "Execute"


class FolderGrant(Contract):
    """Ausdrücklich freigegebener Hostordner mit konkreten Rechten. Keine Vererbung auf andere Ordner."""

    grant_id: str = Field(alias="grantId")
    root: str = Field(alias="root")
    rights: list[FolderRight] = Field(alias="rights")
    recursive: StrictBool = Field(alias="recursive")
    exclude: list[str] | None = Field(default=None, alias="exclude")


class ProjectConfig(Contract):
    schema_version: StrictInt = Field(alias="schemaVersion")
    project_id: str = Field(alias="projectId")
    name: str = Field(alias="name")
    grants: list[FolderGrant] = Field(alias="grants")
    toolchains: list[str] | None = Field(default=None, alias="toolchains")


class SnapshotFile(Contract):
    path: str = Field(alias="path")
    size: StrictInt = Field(alias="size")
    sha256: str = Field(alias="sha256")


class SnapshotManifest(Contract):
    """Unveränderlicher Stand eines Ordners. snapshotId = SHA-256 über das kanonische JSON von {grantId, files}."""

    schema_version: StrictInt = Field(alias="schemaVersion")
    snapshot_id: str = Field(alias="snapshotId")
    grant_id: str = Field(alias="grantId")
    created_at: str = Field(alias="createdAt")
    files: list[SnapshotFile] = Field(alias="files")
    skipped: list[str] | None = Field(default=None, alias="skipped")


class SandboxJob(Contract):
    """Signierter Auftrag an den Worker in der Sandbox."""

    schema_version: StrictInt = Field(alias="schemaVersion")
    job_id: str = Field(alias="jobId")
    sequence: StrictInt = Field(alias="sequence")
    session_id: str = Field(alias="sessionId")
    invocation_id: str = Field(alias="invocationId")
    proposal: ToolProposal = Field(alias="proposal")
    proposal_sha256: str = Field(alias="proposalSha256")
    issued_at: str = Field(alias="issuedAt")
    expires_at: str = Field(alias="expiresAt")
    payload_sha256: str | None = Field(default=None, alias="payloadSha256")
    snapshot: SnapshotManifest | None = Field(default=None, alias="snapshot")


class JobEnvelope(Contract):
    job: SandboxJob = Field(alias="job")
    mac: str = Field(alias="mac")


class ResultEnvelope(Contract):
    """Antwort des Workers. Immer untrusted; der Host prüft Größe, Zuordnung und Ziele."""

    job_id: str = Field(alias="jobId")
    sequence: StrictInt = Field(alias="sequence")
    result: ToolResult = Field(alias="result")


class BrokerEvent(Contract):
    seq: StrictInt = Field(alias="seq")
    at: str = Field(alias="at")
    type: str = Field(alias="type")
    proposal_id: str | None = Field(default=None, alias="proposalId")
    run_id: str | None = Field(default=None, alias="runId")
    state: ProposalState | None = Field(default=None, alias="state")
    message: str | None = Field(default=None, alias="message")


class ToolchainInstallKind(str, Enum):
    Archive = "Archive"
    Installer = "Installer"
    None_ = "None"


class ToolchainParameterType(str, Enum):
    Path = "Path"
    Choice = "Choice"
    Text = "Text"
    Integer = "Integer"


class TestResultFormat(str, Enum):
    Trx = "Trx"
    JUnit = "JUnit"
    NUnit = "NUnit"
    ExitCode = "ExitCode"


class PackageSourceKind(str, Enum):
    NuGet = "NuGet"
    Pip = "Pip"
    None_ = "None"


class ToolchainCommand(Contract):
    """Fester Programmaufruf: deklariertes Binary plus Argumentliste, kein Shellstring."""

    binary: str = Field(alias="binary")
    arguments: list[str] = Field(alias="arguments")


class ToolchainCheck(Contract):
    binary: str = Field(alias="binary")
    arguments: list[str] = Field(alias="arguments")
    expect_output: str | None = Field(default=None, alias="expectOutput")


class ToolchainBinary(Contract):
    name: str = Field(alias="name")
    path: str = Field(alias="path")
    sha256: str | None = Field(default=None, alias="sha256")


class ToolchainInstall(Contract):
    kind: ToolchainInstallKind = Field(alias="kind")
    file: str | None = Field(default=None, alias="file")
    arguments: list[str] | None = Field(default=None, alias="arguments")
    expected_minutes: StrictInt = Field(alias="expectedMinutes")
    check: ToolchainCheck = Field(alias="check")


class ToolchainSelfTest(Contract):
    """Kleines Beispiel, das beim VM-Aufbau gebaut und ausgeführt wird."""

    directory: str = Field(alias="directory")
    steps: list[ToolchainCommand] = Field(alias="steps")
    expect_output: str = Field(alias="expectOutput")


class ToolchainParameter(Contract):
    type: ToolchainParameterType = Field(alias="type")
    values: list[str] | None = Field(default=None, alias="values")
    pattern: str | None = Field(default=None, alias="pattern")


class ToolchainAction(Contract):
    """Aktionsvorlage; Argumente sind Literale oder genau ein {parameter}."""

    binary: str = Field(alias="binary")
    arguments: list[str] = Field(alias="arguments")
    parameters: dict[str, ToolchainParameter] | None = Field(default=None, alias="parameters")
    timeout_seconds: StrictInt | None = Field(default=None, alias="timeoutSeconds")


class ToolchainLicense(Contract):
    proprietary: StrictBool = Field(alias="proprietary")
    note: str | None = Field(default=None, alias="note")
    files: list[str] | None = Field(default=None, alias="files")


class ToolchainPackageSource(Contract):
    kind: PackageSourceKind = Field(alias="kind")
    path: str | None = Field(default=None, alias="path")


class ToolchainManifest(Contract):
    """toolchain.json im Toolchain-Ordner (Spezifikation §8 Toolchains)."""

    schema_version: StrictInt = Field(alias="schemaVersion")
    name: str = Field(alias="name")
    version: str = Field(alias="version")
    display_name: str = Field(alias="displayName")
    guest_path: str = Field(alias="guestPath")
    license: ToolchainLicense = Field(alias="license")
    binaries: list[ToolchainBinary] = Field(alias="binaries")
    install: ToolchainInstall = Field(alias="install")
    post_install: list[ToolchainCommand] | None = Field(default=None, alias="postInstall")
    self_test: ToolchainSelfTest | None = Field(default=None, alias="selfTest")
    actions: dict[str, ToolchainAction] = Field(alias="actions")
    test_result_format: TestResultFormat = Field(alias="testResultFormat")
    environment: dict[str, str] | None = Field(default=None, alias="environment")
    package_source: ToolchainPackageSource | None = Field(default=None, alias="packageSource")
    files: dict[str, str] | None = Field(default=None, alias="files")


class ToolchainInfo(Contract):
    """Toolchain-Ordner bzw. Registrierung, wie die Oberfläche sie anzeigt."""

    name: str = Field(alias="name")
    version: str = Field(alias="version")
    display_name: str = Field(alias="displayName")
    directory: str = Field(alias="directory")
    digest: str = Field(alias="digest")
    file_count: StrictInt = Field(alias="fileCount")
    total_bytes: StrictInt = Field(alias="totalBytes")
    registered: StrictBool = Field(alias="registered")
    registered_digest: str | None = Field(default=None, alias="registeredDigest")
    proprietary: StrictBool = Field(alias="proprietary")
    license_note: str | None = Field(default=None, alias="licenseNote")
    problems: list[str] = Field(alias="problems")
    used_by_project: StrictBool = Field(alias="usedByProject")


class ToolchainRegistration(Contract):
    """Gespeicherte Registrierung mit den bei der Bestätigung festgehaltenen Datei-Hashes."""

    name: str = Field(alias="name")
    version: str = Field(alias="version")
    directory: str = Field(alias="directory")
    manifest_sha256: str = Field(alias="manifestSha256")
    digest: str = Field(alias="digest")
    files: dict[str, str] = Field(alias="files")
    registered_at: str = Field(alias="registeredAt")
