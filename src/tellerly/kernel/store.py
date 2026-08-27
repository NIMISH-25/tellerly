"""The capability catalog: saved artifacts on disk, one directory per
capability, one JSON file per version — plus per-tenant overlays under
``<capability_id>/overlays/<tenant_id>.json``, so an overlay travels with
the base it patches."""
from __future__ import annotations

import re
from pathlib import Path

from tellerly.schema import Capability, TenantOverlay

_SEMVER = re.compile(r"\d+\.\d+\.\d+")
_TENANT_SLUG = re.compile(r"[a-z][a-z0-9_]*")


class CapabilityStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def save(self, capability: Capability) -> Path:
        directory = self.root / capability.id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"v{capability.version}.json"
        path.write_text(capability.to_json(), encoding="utf-8")
        return path

    def versions(self, capability_id: str) -> list[str]:
        directory = self.root / capability_id
        if not directory.is_dir():
            return []
        # Only well-formed v<semver>.json files count; a stray hand-copied
        # backup must not brick the whole catalog.
        found = [
            p.stem[1:]
            for p in directory.glob("v*.json")
            if _SEMVER.fullmatch(p.stem[1:])
        ]
        return sorted(found, key=lambda v: tuple(int(part) for part in v.split(".")))

    def load(self, capability_id: str, version: str | None = None) -> Capability:
        versions = self.versions(capability_id)
        if not versions:
            known = ", ".join(sorted(p.name for p in self.root.glob("*") if p.is_dir())) or "none"
            raise FileNotFoundError(
                f"no capability '{capability_id}' in {self.root} (known: {known})"
            )
        chosen = version or versions[-1]
        path = self.root / capability_id / f"v{chosen}.json"
        if not path.is_file():
            raise FileNotFoundError(f"no version {chosen} of '{capability_id}'")
        return Capability.from_json(path.read_text(encoding="utf-8"))

    # ----------------------------------------------------------- overlays

    def save_overlay(self, overlay: TenantOverlay) -> Path:
        directory = self.root / overlay.capability_id / "overlays"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{overlay.tenant_id}.json"
        path.write_text(overlay.to_json(), encoding="utf-8")
        return path

    def load_overlay(self, capability_id: str, tenant_id: str) -> TenantOverlay:
        # Both ids build a filesystem path — hold them to the slug vocabulary
        # BEFORE touching the filesystem so "../../whatever" never resolves.
        for name, value in (("capability", capability_id), ("tenant", tenant_id)):
            if not _TENANT_SLUG.fullmatch(value):
                raise FileNotFoundError(
                    f"{name} id {value!r} is not a valid slug "
                    "(lowercase letters, digits, underscores)"
                )
        path = self.root / capability_id / "overlays" / f"{tenant_id}.json"
        if not path.is_file():
            known = ", ".join(self.list_overlays(capability_id)) or "none"
            raise FileNotFoundError(
                f"no overlay '{tenant_id}' for capability '{capability_id}' "
                f"(known tenants: {known})"
            )
        return TenantOverlay.from_json(path.read_text(encoding="utf-8"))

    def list_overlays(self, capability_id: str) -> list[str]:
        directory = self.root / capability_id / "overlays"
        if not directory.is_dir():
            return []
        # Only slug-named files count — same reasoning as versions(): a stray
        # hand-copied backup must not brick the catalog.
        return sorted(
            p.stem for p in directory.glob("*.json") if _TENANT_SLUG.fullmatch(p.stem)
        )

    def list(self) -> list[Capability]:
        capabilities = []
        if self.root.is_dir():
            for directory in sorted(p for p in self.root.iterdir() if p.is_dir()):
                if self.versions(directory.name):
                    capabilities.append(self.load(directory.name))
        return capabilities
