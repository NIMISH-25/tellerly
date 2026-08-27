"""Deployment guardrails: the outer safety envelope every run is checked
against — discovery and replay call the same gate.

The deployment policy comes from operator-owned config, not from artifacts.
An artifact's own safety block can only narrow this envelope (intersection);
nothing an artifact declares can widen it, and confirmation can only be
turned ON by that path.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field

from tellerly.schema import ActionType, SafetyPolicy


class PolicyViolation(Exception):
    """An action or destination outside the active policy envelope."""


def _canonical_host(entry: str) -> str:
    """Lowercased host[:port], userinfo stripped — comparison never depends on
    letter case or credentials embedded in a URL."""
    return entry.rsplit("@", 1)[-1].lower()


class DeploymentPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed_hosts: list[str] = Field(min_length=1)
    allowed_actions: list[ActionType] = Field(min_length=1)
    require_confirmation: bool = True

    @classmethod
    def from_yaml(cls, path: Path) -> "DeploymentPolicy":
        return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


class PolicyGate:
    """The single enforcement point. Both execution paths call this before
    touching the surface; neither implements its own checks."""

    def __init__(
        self, deployment: DeploymentPolicy, capability_safety: SafetyPolicy | None = None
    ) -> None:
        self._hosts = {_canonical_host(h) for h in deployment.allowed_hosts}
        self._actions = set(deployment.allowed_actions)
        self.require_confirmation = deployment.require_confirmation
        if capability_safety is not None:
            # Intersection: the artifact can only narrow the envelope.
            self._hosts &= {_canonical_host(h) for h in capability_safety.allowed_hosts}
            self._actions &= set(capability_safety.allowed_actions)
            self.require_confirmation = (
                self.require_confirmation or capability_safety.require_confirmation
            )

    def check_url(self, url: str) -> None:
        try:
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower()
            if parsed.port:
                host = f"{host}:{parsed.port}"
        except ValueError as exc:
            # An unparseable authority (e.g. a mangled port) fails closed.
            raise PolicyViolation(f"unparseable URL authority in {url!r}") from exc
        if host not in self._hosts:
            raise PolicyViolation(
                f"host '{host}' is not in the policy allowlist {sorted(self._hosts)}"
            )

    def check_action(self, action: ActionType) -> None:
        if action not in self._actions:
            raise PolicyViolation(
                f"action '{action.value}' is not in the policy allowlist "
                f"{sorted(a.value for a in self._actions)}"
            )
