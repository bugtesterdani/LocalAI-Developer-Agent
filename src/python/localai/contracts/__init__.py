"""Verträge (aus JSON Schema generiert) und Hilfsfunktionen."""

from localai import canonical
from localai.contracts.generated import *  # noqa: F401,F403
from localai.contracts.generated import ToolProposal


def proposal_sha256(proposal: ToolProposal) -> str:
    """SHA-256 über das kanonische JSON des Vorschlags – identisch zu ProposalHashing.Sha256 in C#."""
    return canonical.sha256_hex(proposal.to_json_obj())
