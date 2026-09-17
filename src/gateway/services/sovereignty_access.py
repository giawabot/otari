"""Sovereignty access control: residency bars over provider attestations.

A request may carry a residency bar (``residency: "canadian"`` or
``"sovereign"`` on the wire) that maps to a minimum sovereignty level.
Every routing candidate must clear the bar or be dropped from the plan,
exactly alongside the per-key model allow-list.

The public wire vocabulary maps onto the internal 1-4 scale:

| wire value        | required level |
| ----------------- | -------------- |
| ``canadian``      | >= 2           |
| ``sovereign``     | >= 3           |
| ``sovereign_model`` | >= 4         |

The internal scale stays the source of truth so a new bar is a table entry,
not a wire change. An absent bar means level 1 (no constraint).

Like ``model_access.py``, everything here is pure and synchronous: the
caller resolves the provider->attestation map once per request (config
plus DB rows, loaded elsewhere) and this module answers questions from it
without I/O, so ``explain`` and the CLI compile without a database.

Precedence for a candidate's attestation (highest first):
1. DB model-specific row for the organization
2. DB provider-level row for the organization
3. DB model-specific deployment-wide row
4. DB provider-level deployment-wide row
5. ``providers.<instance>.sovereignty`` in config
6. No attestation: level 1 (``not_sovereign``), which fails any bar above 1.

Unknown providers fail closed against a bar: an unattested provider is
level 1, so ``residency: canadian`` refuses it rather than trusting the
absence of evidence.
"""

from dataclasses import dataclass
from typing import Any

# Wire value -> required minimum sovereignty level.
RESIDENCY_BARS: dict[str, int] = {
    "canadian": 2,
    "sovereign": 3,
    "sovereign_model": 4,
}

# The level an unattested provider sits at: no residency claim at all.
UNATTESTED_LEVEL = 1

# Bar names in wire order, for error messages.
_BAR_NAMES = ", ".join(f"'{name}'" for name in RESIDENCY_BARS)


def parse_residency_bar(value: str) -> int:
    """Map a wire residency value to its required minimum level.

    Raises ``ValueError`` (surfaced by the caller as a 400) for an unknown
    bar so a typo cannot silently relax to "no constraint".
    """
    value = value.strip().lower()
    if value not in RESIDENCY_BARS:
        raise ValueError(f"unknown residency policy '{value}'; supported bars: {_BAR_NAMES}")
    return RESIDENCY_BARS[value]


@dataclass(frozen=True)
class SovereigntyRecord:
    """One attestation as the resolver sees it."""

    level: int
    hosted_in: str | None = None
    operator_jurisdiction: str | None = None
    model_origin: str | None = None
    vetted: bool = False


class SovereigntyMap:
    """Resolved provider/model -> attestation, for one request.

    Built once per request from config plus DB rows (see
    :func:`build_sovereignty_map`); lookups are pure.
    """

    def __init__(self) -> None:
        self._provider: dict[str, SovereigntyRecord] = {}
        self._model: dict[tuple[str, str], SovereigntyRecord] = {}

    def set_provider(self, instance: str, record: SovereigntyRecord) -> None:
        self._provider[instance] = record

    def set_model(self, instance: str, model: str, record: SovereigntyRecord) -> None:
        self._model[(instance, model)] = record

    def record_for(self, instance: str | None, model: str | None) -> SovereigntyRecord | None:
        """The attestation for a candidate: model-specific row wins over provider row."""
        if instance is None:
            return None
        if model is not None:
            hit = self._model.get((instance, model))
            if hit is not None:
                return hit
        return self._provider.get(instance)

    def level_for(self, instance: str | None, model: str | None) -> int:
        """The candidate's level, or 1 when nothing attests it."""
        record = self.record_for(instance, model)
        return record.level if record is not None else UNATTESTED_LEVEL

    def is_empty(self) -> bool:
        return not self._provider and not self._model


def _record_from_dict(raw: dict[str, Any]) -> SovereigntyRecord:
    """Build a record from a config ``sovereignty:`` block or a DB row mapping."""
    level = raw.get("level")
    if not isinstance(level, int) or level < 1 or level > 4:
        raise ValueError(f"sovereignty level must be an int 1-4, got {level!r}")
    return SovereigntyRecord(
        level=level,
        hosted_in=raw.get("hosted_in"),
        operator_jurisdiction=raw.get("operator_jurisdiction"),
        model_origin=raw.get("model_origin"),
        vetted=bool(raw.get("vetted", False)),
    )


def config_sovereignty(config: Any) -> SovereigntyMap:
    """The config-file baseline: ``providers.<instance>.sovereignty`` blocks.

    Instances without a ``sovereignty`` key contribute nothing (they stay
    unattested, level 1). A malformed block raises ``ValueError``; the
    caller surfaces it as a configuration error.
    """
    smap = SovereigntyMap()
    providers = getattr(config, "providers", None) or {}
    for instance, entry in providers.items():
        if not isinstance(entry, dict):
            continue
        raw = entry.get("sovereignty")
        if raw is None:
            continue
        if not isinstance(raw, dict):
            raise ValueError(f"providers.{instance}.sovereignty must be a mapping")
        smap.set_provider(instance, _record_from_dict(raw))
        models = raw.get("models")
        if isinstance(models, dict):
            for model_name, model_raw in models.items():
                if isinstance(model_raw, dict):
                    smap.set_model(instance, model_name, _record_from_dict(model_raw))
    return smap


def build_sovereignty_map(config: Any, rows: list[Any]) -> SovereigntyMap:
    """Config baseline overlaid with DB attestation rows (DB wins).

    ``rows`` are ``ProviderSovereignty`` instances (or anything exposing the
    same attributes). Deployment-wide rows (``organization_id is None``)
    are applied first, then organization rows, so an organization's own
    attestation overrides the deployment-wide one; within each scope, a
    model row overrides the provider row.
    """

    def scope_key(row: Any) -> int:
        return 0 if getattr(row, "organization_id", None) is None else 1

    smap = config_sovereignty(config)
    for row in sorted(rows, key=scope_key):
        record = SovereigntyRecord(
            level=row.sovereignty_level,
            hosted_in=row.hosted_in,
            operator_jurisdiction=row.operator_jurisdiction,
            model_origin=row.model_origin,
            vetted=row.vetted,
        )
        if row.model is None:
            smap.set_provider(row.provider, record)
        else:
            smap.set_model(row.provider, row.model, record)
    return smap


def is_sovereign_enough(
    smap: SovereigntyMap,
    instance: str | None,
    model: str | None,
    required_level: int,
) -> tuple[bool, str]:
    """Whether a candidate clears the request's residency bar.

    Returns ``(allowed, reason)``. The reason is operator-facing (it lands
    in ``DroppedCandidate`` and the refusal detail) and names the level the
    candidate actually holds.
    """
    if required_level <= 1:
        return True, "no residency bar"
    level = smap.level_for(instance, model)
    if level >= required_level:
        return True, f"level {level} clears bar {required_level}"
    return False, f"sovereignty level {level} is below the required {required_level}"


def combine_residency_bars(request_bar: str | None, org_floor: str | None) -> tuple[int, str]:
    """Fold the caller's bar and the organization's floor into one requirement.

    ``required = max(request_bar_level, org_floor_level)``: a request bar can
    only raise the requirement, never lower the organization's. An absent
    value on either side is level 1 (no constraint). Returns
    ``(required_level, source)`` where source is ``"request"``,
    ``"organization"``, ``"both"`` (the two tie at the binding level), or
    ``"none"`` when nothing is in force. Pure, so the floor is an input to
    the compile rather than a database read: CLI ``explain`` keeps working.
    """
    request_level = parse_residency_bar(request_bar) if request_bar else 1
    floor_level = parse_residency_bar(org_floor) if org_floor else 1
    if floor_level <= 1:
        return request_level, ("request" if request_level > 1 else "none")
    if request_level < floor_level:
        return floor_level, "organization"
    if request_level > floor_level:
        return request_level, "request"
    return floor_level, "both"


def residency_refusal_detail(residency: str | None, required_level: int, source: str = "request") -> str:
    """The 403 detail naming the bar, its source, and what it means.

    A refusal caused by the organization's floor names the floor explicitly
    rather than resembling a failed caller-supplied bar: debugging a policy
    you did not write is much easier when the refusal says who set it.
    """
    if source == "organization":
        return (
            f"This organization's residency policy requires a provider at sovereignty level "
            f"{required_level} or higher, and no available provider clears that bar."
        )
    if source == "both":
        return (
            f"Residency policy '{residency}' and this organization's residency floor both require "
            f"a provider at sovereignty level {required_level} or higher, "
            "and no available provider clears that bar."
        )
    return (
        f"Residency policy '{residency}' requires a provider at sovereignty level "
        f"{required_level} or higher, and no available provider clears that bar."
    )


__all__ = [
    "RESIDENCY_BARS",
    "UNATTESTED_LEVEL",
    "SovereigntyMap",
    "SovereigntyRecord",
    "build_sovereignty_map",
    "combine_residency_bars",
    "config_sovereignty",
    "is_sovereign_enough",
    "parse_residency_bar",
    "residency_refusal_detail",
]
