"""Network-free contracts for reproducible paper-sleeve experiments.

The models in this module intentionally contain no persistence, scheduling, provider,
or broker behavior.  They define the stable JSON boundary that those later layers can
store and reconstruct.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Self, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    StrictBool,
    ValidationInfo,
    field_validator,
    model_validator,
)

type ConfigurationValue = (
    bool | int | float | str | list["ConfigurationValue"] | dict[str, "ConfigurationValue"] | None
)

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_SETTLEMENT_RE = re.compile(r"^T\+[0-9]+$")


def normalize_configuration(value: object) -> ConfigurationValue:
    """Return a deterministic, JSON-compatible representation of ``value``.

    Object keys are Unicode-normalized and sorted, sequence order is retained, and
    tuples are converted to JSON arrays.  Integral floats and signed zero are reduced
    to their integer equivalents.  Values without a stable JSON representation fail
    closed instead of relying on ``repr`` or an implementation-specific encoder.
    """

    return _normalize_configuration(value, path="$", active_containers=set())


def canonical_configuration_json(value: object) -> str:
    """Serialize a configuration with the canonical rules used for hashing."""

    normalized = normalize_configuration(value)
    return json.dumps(
        normalized,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def deterministic_configuration_hash(value: object) -> str:
    """Return the SHA-256 digest of a normalized configuration."""

    canonical_json = canonical_configuration_json(value)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def _normalize_configuration(
    value: object,
    *,
    path: str,
    active_containers: set[int],
) -> ConfigurationValue:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite float")
        if value == 0 or value.is_integer():
            return int(value)
        return value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)

    if isinstance(value, (list, tuple)):
        container_id = id(value)
        if container_id in active_containers:
            raise ValueError(f"{path} contains a cyclic sequence")
        active_containers.add(container_id)
        try:
            sequence = cast(list[object] | tuple[object, ...], value)
            return [
                _normalize_configuration(
                    item,
                    path=f"{path}[{index}]",
                    active_containers=active_containers,
                )
                for index, item in enumerate(sequence)
            ]
        finally:
            active_containers.remove(container_id)

    if isinstance(value, dict):
        container_id = id(value)
        if container_id in active_containers:
            raise ValueError(f"{path} contains a cyclic object")
        active_containers.add(container_id)
        try:
            mapping = cast(dict[object, object], value)
            normalized: dict[str, ConfigurationValue] = {}
            for raw_key, item in mapping.items():
                if not isinstance(raw_key, str):
                    raise ValueError(f"{path} contains a non-string object key")
                key = unicodedata.normalize("NFC", raw_key)
                if key in normalized:
                    raise ValueError(
                        f"{path} contains object keys that collide after Unicode normalization"
                    )
                normalized[key] = _normalize_configuration(
                    item,
                    path=f"{path}.{key}",
                    active_containers=active_containers,
                )
            return {key: normalized[key] for key in sorted(normalized)}
        finally:
            active_containers.remove(container_id)

    if isinstance(value, (set, frozenset)):
        raise ValueError(f"{path} contains an unordered collection")
    raise ValueError(f"{path} contains unsupported configuration value type {type(value).__name__}")


def _required_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    if not normalized.isprintable():
        raise ValueError(f"{field_name} must not contain control characters")
    return normalized


def _normalized_label(value: object, *, field_name: str) -> str:
    return " ".join(_required_text(value, field_name=field_name).split()).casefold()


def _normalized_settlement(value: object) -> str:
    settlement = _required_text(value, field_name="settlement_model").upper()
    settlement = "".join(settlement.split())
    if not _SETTLEMENT_RE.fullmatch(settlement):
        raise ValueError("settlement_model must use the form 'T+N'")
    return settlement


def _normalized_decimal(value: object, *, field_name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, float, str)):
        raise ValueError(f"{field_name} must be a finite decimal number")
    try:
        normalized = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{field_name} must be a finite decimal number") from exc
    if not normalized.is_finite():
        raise ValueError(f"{field_name} must be a finite decimal number")
    return normalized


class StrategyDefinition(BaseModel):
    """Complete, versioned identity for deterministic strategy reconstruction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    strategy_id: str
    strategy_version: str
    implementation_name: str
    parameters: dict[str, ConfigurationValue]
    universe_definition: ConfigurationValue
    benchmark_symbol_or_sleeve: str
    decision_frequency: str
    decision_time: time
    data_requirements: tuple[str, ...]
    long_only: StrictBool
    leverage_allowed: StrictBool
    configuration_hash: str = ""

    @field_validator(
        "strategy_id",
        "strategy_version",
        "implementation_name",
        "benchmark_symbol_or_sleeve",
        mode="before",
    )
    @classmethod
    def _validate_identity_text(cls, value: object, info: ValidationInfo) -> str:
        field_name = info.field_name or "identity"
        return _required_text(value, field_name=field_name)

    @field_validator("decision_frequency", mode="before")
    @classmethod
    def _validate_decision_frequency(cls, value: object) -> str:
        return _normalized_label(value, field_name="decision_frequency")

    @field_validator("decision_time", mode="before")
    @classmethod
    def _validate_decision_time(cls, value: object) -> time:
        if isinstance(value, time):
            parsed = value
        elif isinstance(value, str):
            try:
                parsed = time.fromisoformat(value.strip())
            except ValueError as exc:
                raise ValueError("decision_time must be a valid ISO local time") from exc
        else:
            raise ValueError("decision_time must be a valid ISO local time")
        if parsed.tzinfo is not None:
            raise ValueError("decision_time must be a timezone-naive local market time")
        if parsed.microsecond:
            raise ValueError("decision_time must not include fractional seconds")
        return parsed

    @field_validator("parameters", mode="before")
    @classmethod
    def _validate_parameters(cls, value: object) -> dict[str, ConfigurationValue]:
        normalized = normalize_configuration(value)
        if not isinstance(normalized, dict):
            raise ValueError("parameters must be a JSON object")
        return normalized

    @field_validator("universe_definition", mode="before")
    @classmethod
    def _validate_universe_definition(cls, value: object) -> ConfigurationValue:
        normalized = normalize_configuration(value)
        if not isinstance(normalized, (dict, list)):
            raise ValueError("universe_definition must be a JSON object or array")
        return normalized

    @field_validator("data_requirements", mode="before")
    @classmethod
    def _validate_data_requirements(cls, value: object) -> tuple[str, ...]:
        if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
            raise ValueError("data_requirements must be an array of capability names")
        requirements: dict[str, None] = {}
        for item in cast(list[object] | tuple[object, ...], value):
            requirement = _normalized_label(item, field_name="data requirement")
            requirements[requirement] = None
        return tuple(sorted(requirements))

    @field_validator("configuration_hash", mode="before")
    @classmethod
    def _validate_configuration_hash_text(cls, value: object) -> str:
        if value is None or value == "":
            return ""
        if not isinstance(value, str):
            raise ValueError("configuration_hash must be a SHA-256 hexadecimal digest")
        normalized = value.strip().casefold()
        if not _HASH_RE.fullmatch(normalized):
            raise ValueError("configuration_hash must be a SHA-256 hexadecimal digest")
        return normalized

    @model_validator(mode="after")
    def _set_or_verify_configuration_hash(self) -> Self:
        expected = deterministic_configuration_hash(self.configuration_payload())
        if self.configuration_hash and self.configuration_hash != expected:
            raise ValueError("configuration_hash does not match the normalized definition")
        object.__setattr__(self, "configuration_hash", expected)
        return self

    def configuration_payload(self) -> dict[str, ConfigurationValue]:
        """Return every decision-relevant field in its normalized hash representation."""

        return {
            "benchmark_symbol_or_sleeve": self.benchmark_symbol_or_sleeve,
            "data_requirements": list(self.data_requirements),
            "decision_frequency": self.decision_frequency,
            "decision_time": self.decision_time.isoformat(timespec="seconds"),
            "implementation_name": self.implementation_name,
            "leverage_allowed": self.leverage_allowed,
            "long_only": self.long_only,
            "parameters": self.parameters,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "universe_definition": self.universe_definition,
        }


class ExperimentCohort(BaseModel):
    """Common comparison assumptions and membership for paper sleeves."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cohort_id: str
    name: str
    created_at: datetime
    start_session: date
    starting_cash_per_sleeve: Decimal
    settlement_model: str
    leverage: Decimal
    benchmark_sleeve: str
    decision_schedule: str
    cost_model_id: str
    member_sleeves: tuple[str, ...]
    status: str

    @field_validator(
        "cohort_id",
        "name",
        "benchmark_sleeve",
        "decision_schedule",
        "cost_model_id",
        mode="before",
    )
    @classmethod
    def _validate_required_text(cls, value: object, info: ValidationInfo) -> str:
        field_name = info.field_name or "value"
        return _required_text(value, field_name=field_name)

    @field_validator("status", mode="before")
    @classmethod
    def _validate_status(cls, value: object) -> str:
        status = _normalized_label(value, field_name="status")
        if " " in status:
            raise ValueError("status must be a single stable identifier")
        return status

    @field_validator("created_at")
    @classmethod
    def _validate_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must include a timezone")
        return value.astimezone(UTC)

    @field_validator("start_session", mode="before")
    @classmethod
    def _validate_start_session(cls, value: object) -> object:
        if isinstance(value, datetime):
            raise ValueError("start_session must be a calendar date, not a datetime")
        return value

    @field_validator("starting_cash_per_sleeve", mode="before")
    @classmethod
    def _validate_starting_cash(cls, value: object) -> Decimal:
        cash = _normalized_decimal(value, field_name="starting_cash_per_sleeve")
        if cash <= 0:
            raise ValueError("starting_cash_per_sleeve must be greater than zero")
        cash_exponent = cash.as_tuple().exponent
        if not isinstance(cash_exponent, int):
            raise ValueError("starting_cash_per_sleeve must be a finite decimal number")
        if cash_exponent < -2:
            raise ValueError("starting_cash_per_sleeve must not use fractions of a cent")
        return cash.quantize(Decimal("0.01"))

    @field_validator("settlement_model", mode="before")
    @classmethod
    def _validate_settlement_model(cls, value: object) -> str:
        return _normalized_settlement(value)

    @field_validator("leverage", mode="before")
    @classmethod
    def _validate_leverage(cls, value: object) -> Decimal:
        leverage = _normalized_decimal(value, field_name="leverage")
        if leverage <= 0:
            raise ValueError("leverage must be greater than zero")
        return leverage.normalize()

    @field_validator("member_sleeves", mode="before")
    @classmethod
    def _validate_member_sleeves(cls, value: object) -> tuple[str, ...]:
        if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
            raise ValueError("member_sleeves must be an array of sleeve identifiers")
        members: list[str] = []
        identities: set[str] = set()
        for item in cast(list[object] | tuple[object, ...], value):
            member = _required_text(item, field_name="member sleeve")
            identity = member.casefold()
            if identity in identities:
                raise ValueError(f"duplicate cohort member: {member}")
            identities.add(identity)
            members.append(member)
        if len(members) < 2:
            raise ValueError("member_sleeves must contain at least two distinct sleeves")
        return tuple(members)

    @model_validator(mode="after")
    def _validate_benchmark_membership(self) -> Self:
        member_by_identity = {member.casefold(): member for member in self.member_sleeves}
        benchmark_identity = self.benchmark_sleeve.casefold()
        if benchmark_identity not in member_by_identity:
            raise ValueError("benchmark_sleeve must also appear in member_sleeves")
        object.__setattr__(self, "benchmark_sleeve", member_by_identity[benchmark_identity])
        return self

    def validate_member_assumptions(
        self,
        *,
        starting_cash_per_sleeve: Decimal | int | float | str,
        settlement_model: str,
        leverage: Decimal | int | float | str,
    ) -> None:
        """Reject a sleeve whose paper-account assumptions differ from this cohort."""

        member_cash = self._validate_starting_cash(starting_cash_per_sleeve)
        if member_cash != self.starting_cash_per_sleeve:
            raise ValueError(
                "member starting cash is incompatible with cohort "
                f"starting_cash_per_sleeve={self.starting_cash_per_sleeve}"
            )

        member_settlement = _normalized_settlement(settlement_model)
        if member_settlement != self.settlement_model:
            raise ValueError(
                "member settlement model is incompatible with cohort "
                f"settlement_model={self.settlement_model}"
            )

        member_leverage = self._validate_leverage(leverage)
        if member_leverage != self.leverage:
            raise ValueError(
                f"member leverage is incompatible with cohort leverage={self.leverage}"
            )
