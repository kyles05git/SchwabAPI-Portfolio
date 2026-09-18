"""The challenger-v1 bootstrap: preview, create, conflicts, and what it must not touch.

Every store here is a disposable ``tmp_path`` SQLite file and every fact is synthetic.
Nothing in this module reads ``.env``, opens a socket, or contacts Schwab, Neon, SEC
EDGAR, SMTP, a broker, or the user's real databases — the autouse fixture in
``conftest.py`` enforces the last of those globally and fails closed if a code path
tries.

The tests are grouped by the promise they defend: the plan is deterministic and pure,
preview writes nothing, create is atomic and idempotent, a conflict is rejected whole,
the five sleeves are independently funded and reconstructable, and the two July cohorts
are left exactly as they were.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from schwab_trader import challenger_cohort as cc
from schwab_trader import challenger_strategies as cs
from schwab_trader import cohort_lifecycle, execution_timing, strategy_registry
from schwab_trader.sleeves import SleeveStore
from schwab_trader.storage.database import Database
from schwab_trader.storage.sleeves import SqlAlchemySleeveStore
from schwab_trader.strategies import contract

#: A Monday well clear of a holiday, chosen so the plan is stable and reviewable.
START = date(2026, 8, 17)
NOW = datetime(2026, 7, 31, 16, 0, tzinfo=UTC)
NOW_ET = datetime(2026, 7, 31, 12, 0)

EXPECTED_MEMBERS = (
    "control-cash",
    "bench-spy",
    "dual-momentum-v1",
    "quality-profitability-v1",
    "short-term-mean-reversion-v1",
)

#: The July 28 cohort's persisted definition hashes, exactly as ``tests/test_dual_momentum.py``
#: pins them. Duplicated here on purpose rather than imported: this issue registers three
#: strategies in the shared registry, which is the change most able to move these values,
#: so the assertion has to be independently readable from the integration suite.
JULY_COHORT_HASHES = {
    "control-cash": "150d0d01fb2b5de052896de6683436a7ea81dbd7bf0a354cc7809ca68150b11e",
    "bench-spy": "1852156d8194ee4b707f791167cfda04bf09ee66b2eac239ceb9650e6e9ae243",
    "sector-momentum": "1570b08dd56bd96d2d9de8e9ec8987731b270594eea20343c1c5366785643a95",
    "trend-large": "021bd967366ee64ff6fab1bfe71b707fbb716a2014d4142bb7231ab1cd5f1738",
    "low-vol-large": "c5d8bc25e7fa26d1ff243de1eea9daf54c89b4f14afdcab6265f86d988d791a2",
    "momentum-large": "9d7c5ae22d1b90a6aeb8d667e6104c8f7e897191ef7cc8549697dbe4279aae00",
    "value-momentum-edgar": "2876fbe4e926764b71972a1ebb7a8ec1e83277daf9e1b1b67b49b37e70543bdc",
}


def _plan(**overrides) -> cc.ChallengerPlan:
    kwargs: dict[str, object] = {
        "start_session": START,
        "now": NOW,
        "now_et": NOW_ET,
    }
    kwargs.update(overrides)
    return cc.build_plan(**kwargs)  # type: ignore[arg-type]


def _store(tmp_path: Path) -> SleeveStore:
    return SleeveStore(tmp_path / "sleeves")


def _tree(root: Path) -> set[Path]:
    """Every path under ``root``, for proving an operation wrote nothing."""
    return set(root.rglob("*")) if root.exists() else set()


# --- the plan is deterministic and complete ----------------------------------


def test_the_preview_names_exactly_the_five_frozen_members_in_contract_order() -> None:
    plan = _plan()

    assert tuple(spec.name for spec in plan.specs) == EXPECTED_MEMBERS
    assert len(plan.specs) == contract.SLEEVE_COUNT == 5
    assert plan.manifest.cohort.member_sleeves == EXPECTED_MEMBERS
    # The benchmark is a funded member, not an external index.
    assert plan.manifest.cohort.benchmark_sleeve == "bench-spy"
    assert "bench-spy" in plan.manifest.cohort.member_sleeves


def test_two_previews_of_the_same_session_are_byte_identical() -> None:
    """Determinism is what makes a preview reviewable: diff it, then create it."""
    first = cc.render(cc.preview(_plan()))
    second = cc.render(cc.preview(_plan()))

    assert first == second
    # ``created_at`` is the only field that could move, and it is injected. Prove the
    # payload really does contain it, so this test cannot pass by omission.
    assert json.loads(first)["cohort"]["created_at"] == "2026-07-31T16:00:00Z"


def test_the_preview_records_every_immutable_property_the_experiment_needs() -> None:
    payload = cc.preview(_plan())

    assert payload["cohort"]["starting_cash_per_sleeve"] == "10000.00"
    assert payload["cohort"]["settlement_model"] == "T+1"
    assert payload["cohort"]["leverage"] == "1"
    assert payload["contract"]["contract_hash"] == contract.contract_hash()
    assert payload["timing"]["execution_methodology"] == execution_timing.NEXT_OPEN_METHODOLOGY_KEY
    assert payload["timing"]["methodology_hash"] == cc.METHODOLOGY.methodology_hash
    assert payload["costs"]["bps_round_trip"] == "10"
    assert payload["limits"]["gross_exposure_cap"] == "1.00"
    assert payload["limits"]["long_only"] is True
    assert payload["limits"]["leverage_allowed"] is False
    assert payload["cadences"] == {
        "control-cash": "never",
        "bench-spy": "buy-once",
        "dual-momentum-v1": "monthly",
        "quality-profitability-v1": "monthly",
        "short-term-mean-reversion-v1": "daily",
    }
    # An honest report repeats the limitations rather than showing a return alone.
    assert payload["known_limitations"] == list(contract.KNOWN_LIMITATIONS)
    assert payload["safety"]["paper_only"] is True
    assert payload["safety"]["brokerage_account_access"] is False
    assert payload["persistent_writes"] == "none"


def test_the_dual_momentum_universe_carries_its_defensive_asset() -> None:
    """The sleeve may have to buy IEF, so the snapshot must fetch it."""
    spec = next(s for s in _plan().specs if s.name == "dual-momentum-v1")

    assert spec.universe == ("SPY", "EFA", "EEM", "VNQ", "IEF")
    definition = spec.definition.universe_definition
    assert isinstance(definition, dict)
    # The ranking universe and the defensive asset stay distinguishable in the record.
    assert definition["risk_symbols"] == ["SPY", "EFA", "EEM", "VNQ"]
    assert definition["defensive_symbols"] == ["IEF"]


def test_the_large_cap_sleeves_pin_the_frozen_symbol_list_not_a_live_preset() -> None:
    """A later edit to the mutable preset must not redefine a running experiment."""
    for name in ("quality-profitability-v1", "short-term-mean-reversion-v1"):
        spec = next(s for s in _plan().specs if s.name == name)
        assert spec.universe == contract.QUALITY_PROFITABILITY.universe
        definition = spec.definition.universe_definition
        assert isinstance(definition, dict)
        assert definition["symbols"] == list(spec.universe)


# --- start-session eligibility ------------------------------------------------


@pytest.mark.parametrize(
    ("day", "reason"),
    [
        (date(2026, 8, 15), "not an XNYS trading session"),  # Saturday
        (date(2026, 9, 7), "not an XNYS trading session"),  # Labor Day
        (date(2026, 7, 30), "historical"),  # before now_et
    ],
)
def test_an_ineligible_start_session_is_refused(day: date, reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        _plan(start_session=day)


def test_a_session_that_has_already_closed_is_refused() -> None:
    """Starting on a closed session would record an ambiguous missed first observation."""
    with pytest.raises(ValueError, match="already closed"):
        cc.build_plan(
            start_session=date(2026, 7, 31),
            now=NOW,
            now_et=datetime(2026, 7, 31, 16, 30),
        )


def test_todays_still_open_session_is_allowed() -> None:
    plan = cc.build_plan(
        start_session=date(2026, 7, 31),
        now=NOW,
        now_et=datetime(2026, 7, 31, 9, 45),
    )
    assert plan.start_session == date(2026, 7, 31)


@pytest.mark.parametrize("protected", sorted(execution_timing.PROTECTED_LEGACY_COHORTS))
def test_a_july_cohort_id_can_never_be_reused_by_this_bootstrap(protected: str) -> None:
    """The strongest form of "do not touch July": the id is unreachable from here."""
    with pytest.raises(ValueError, match="must not be redefined, reused, or superseded"):
        _plan(cohort_id=protected)


def test_a_superseded_cohort_id_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A withdrawn experiment is never restarted in place, whatever its id.

    Today the only superseded cohort is also a protected legacy one, so the protected
    check fires first. Marking an arbitrary id historical exercises the second guard on
    its own, which is the one that will matter when a future cohort is superseded.
    """
    assert cohort_lifecycle.is_historical("paper-first-2026-07-27")
    monkeypatch.setattr(
        cohort_lifecycle, "is_historical", lambda cohort_id: cohort_id == "challenger-v1-retired"
    )
    monkeypatch.setattr(
        cc.cohort_lifecycle,
        "is_historical",
        lambda cohort_id: cohort_id == "challenger-v1-retired",
    )

    with pytest.raises(ValueError, match="never restarted in place"):
        _plan(cohort_id="challenger-v1-retired")


# --- preview writes nothing ---------------------------------------------------


def test_preview_performs_zero_persistent_writes(tmp_path: Path) -> None:
    root = tmp_path / "sleeves"
    root.mkdir()
    before = _tree(root)

    for _ in range(3):
        cc.preview(_plan())

    assert _tree(root) == before == set()


def test_preview_after_create_still_writes_nothing(tmp_path: Path) -> None:
    """A repeated preview against an existing cohort must not adopt or touch it."""
    store = _store(tmp_path)
    cc.create(_plan(), store=store)
    before = _tree(tmp_path)

    cc.preview(_plan())

    assert _tree(tmp_path) == before


# --- create is atomic, idempotent, and create-if-absent ----------------------


def test_create_produces_five_independently_funded_paper_accounts(tmp_path: Path) -> None:
    store = _store(tmp_path)

    result = cc.create(_plan(), store=store)

    assert result.created == EXPECTED_MEMBERS
    members = [cfg for cfg in store.list() if cfg.cohort_id == result.cohort_id]
    assert len(members) == 5
    for cfg in members:
        assert cfg.starting_cash == contract.STARTING_CASH_PER_SLEEVE == Decimal("10000.00")
        assert cfg.leverage == Decimal("1")
        assert cfg.settlement_t1 is True
        assert cfg.execution_methodology == execution_timing.NEXT_OPEN_METHODOLOGY_KEY
        assert cfg.reproducible
    # Five separate simulations, not one $50,000 book: every sleeve has its own paper
    # account file, so cash, positions, and fills cannot be shared.
    paths = {store.paper_path(cfg.name) for cfg in members}
    assert len(paths) == 5


def test_repeated_creation_is_idempotent_and_writes_nothing_the_second_time(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    first = cc.create(_plan(), store=store)
    manifest_before = Path(first.manifest_location).read_bytes()
    configs_before = {cfg.name: cfg.model_dump(mode="json") for cfg in store.list()}

    second = cc.create(_plan(), store=store)

    assert second.already_complete
    assert second.created == ()
    assert set(second.existing) == set(EXPECTED_MEMBERS)
    assert Path(second.manifest_location).read_bytes() == manifest_before
    assert {cfg.name: cfg.model_dump(mode="json") for cfg in store.list()} == configs_before


def test_creation_survives_repeated_invocation_after_a_restart(tmp_path: Path) -> None:
    """A fresh store object over the same directory is what a restart actually looks like."""
    cc.create(_plan(), store=_store(tmp_path))

    again = cc.create(_plan(), store=_store(tmp_path))

    assert again.already_complete
    members = [cfg for cfg in _store(tmp_path).list() if cfg.cohort_id == again.cohort_id]
    assert len(members) == 5


def test_sleeve_records_without_a_manifest_are_never_adopted(tmp_path: Path) -> None:
    """An un-manifested partial cohort is an investigation, not something to complete.

    Records with no manifest cannot be distinguished from another tool's sleeves, and
    silently adopting them would attach an experiment's identity to state nobody
    verified. In-process failures never reach this state — the rollback below covers
    them — so this is the residue of a hard crash, and it needs a human.
    """
    store = _store(tmp_path)
    plan = _plan()
    for spec in plan.specs[:2]:
        store.create(
            spec.name,
            strategy=spec.strategy,
            universe=list(spec.universe),
            starting_cash=contract.STARTING_CASH_PER_SLEEVE,
            max_positions=spec.max_positions,
            max_position_fraction=spec.max_position_fraction,
            settlement_t1=True,
            leverage=contract.LEVERAGE,
            definition=spec.definition,
            cohort_id=plan.cohort_id,
            execution_methodology=cc.METHODOLOGY.key,
        )
    before = {cfg.name: cfg.model_dump(mode="json") for cfg in store.list()}

    with pytest.raises(cc.ChallengerConflictError, match="sleeve records but no manifest"):
        cc.create(plan, store=store)

    assert {cfg.name: cfg.model_dump(mode="json") for cfg in store.list()} == before


def test_a_failure_part_way_through_rolls_back_every_record_it_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All-or-nothing: a half-built cohort is never left behind."""
    store = _store(tmp_path)
    plan = _plan()
    real_create = store.create
    calls = {"n": 0}

    def failing_create(name: str, **kwargs: object):
        calls["n"] += 1
        if calls["n"] == 4:  # after three sleeves already exist
            raise OSError("disk full")
        return real_create(name, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(store, "create", failing_create)

    with pytest.raises(OSError, match="disk full"):
        cc.create(plan, store=store)

    monkeypatch.undo()
    assert [cfg for cfg in _store(tmp_path).list() if cfg.cohort_id == plan.cohort_id] == []
    assert not cc.manifest_path(store.dir, plan.cohort_id).exists()


# --- conflicts are rejected whole --------------------------------------------


def test_a_conflicting_manifest_is_rejected_without_touching_the_stored_cohort(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    cc.create(_plan(), store=store)
    before = {cfg.name: cfg.model_dump(mode="json") for cfg in store.list()}

    # Same id, different start session: a different experiment wearing the same name.
    conflicting = _plan(start_session=date(2026, 8, 18), cohort_id="challenger-v1-2026-08-17")
    with pytest.raises(cc.ChallengerConflictError, match="conflicting immutable settings"):
        cc.create(conflicting, store=store)

    assert {cfg.name: cfg.model_dump(mode="json") for cfg in store.list()} == before


def test_a_conflicting_member_configuration_is_detected_before_anything_is_created(
    tmp_path: Path,
) -> None:
    """The whole operation is rejected; no partially created cohort is left behind."""
    store = _store(tmp_path)
    plan = _plan()
    # One pre-existing sleeve that disagrees about starting cash — and nothing else, so
    # a create that wrote before checking would leave four new records behind.
    spec = next(s for s in plan.specs if s.name == "bench-spy")
    store.create(
        spec.name,
        strategy=spec.strategy,
        universe=list(spec.universe),
        starting_cash=Decimal("5000.00"),
        max_positions=spec.max_positions,
        max_position_fraction=spec.max_position_fraction,
        settlement_t1=True,
        leverage=contract.LEVERAGE,
        definition=spec.definition,
        cohort_id=plan.cohort_id,
        execution_methodology=cc.METHODOLOGY.key,
    )

    with pytest.raises(cc.ChallengerConflictError, match="has sleeve records but no manifest"):
        cc.create(plan, store=store)

    names = {cfg.name for cfg in store.list() if cfg.cohort_id == plan.cohort_id}
    assert names == {"bench-spy"}


def test_a_stored_cohort_with_a_drifted_member_is_rejected_whole(tmp_path: Path) -> None:
    store = _store(tmp_path)
    plan = _plan()
    cc.create(plan, store=store)
    # Drift one stored member's immutable configuration behind the bootstrap's back.
    store.remove("bench-spy")
    spec = next(s for s in plan.specs if s.name == "bench-spy")
    store.create(
        spec.name,
        strategy=spec.strategy,
        universe=list(spec.universe),
        starting_cash=Decimal("5000.00"),
        max_positions=spec.max_positions,
        max_position_fraction=spec.max_position_fraction,
        settlement_t1=True,
        leverage=contract.LEVERAGE,
        definition=spec.definition,
        cohort_id=plan.cohort_id,
        execution_methodology=cc.METHODOLOGY.key,
    )
    before = {cfg.name: cfg.model_dump(mode="json") for cfg in store.list()}

    with pytest.raises(cc.ChallengerConflictError, match="starting_cash"):
        cc.create(plan, store=store)

    assert {cfg.name: cfg.model_dump(mode="json") for cfg in store.list()} == before


def test_an_unexpected_member_blocks_the_whole_cohort(tmp_path: Path) -> None:
    store = _store(tmp_path)
    plan = _plan()
    cc.create(plan, store=store)
    store.create(
        "an-intruder",
        strategy="hold",
        universe=["SPY"],
        starting_cash=contract.STARTING_CASH_PER_SLEEVE,
        max_positions=1,
        max_position_fraction=Decimal("1"),
        cohort_id=plan.cohort_id,
    )

    with pytest.raises(cc.ChallengerConflictError, match="not part of challenger-v1"):
        cc.create(plan, store=store)


def test_a_stored_cohort_missing_a_member_is_never_repaired_in_place(
    tmp_path: Path,
) -> None:
    """A running cohort with a vanished sleeve is an investigation, not an auto-fix."""
    store = _store(tmp_path)
    plan = _plan()
    cc.create(plan, store=store)
    store.remove("dual-momentum-v1")

    with pytest.raises(cc.ChallengerConflictError, match="never repaired in place"):
        cc.create(plan, store=store)


# --- the persisted experiment is reconstructable -----------------------------


def test_every_stored_definition_reconstructs_its_strategy(tmp_path: Path) -> None:
    """#83's promise, for all five members: persisted state alone rebuilds the sleeve.

    Empty history and an empty store are enough here — construction is what is being
    proven, not the decision. A strategy that needed evidence to *build* would be one
    that could not be reconstructed offline at all.
    """
    from schwab_trader.sec_store import SecStore

    store = _store(tmp_path)
    result = cc.create(_plan(), store=store)
    members = [cfg for cfg in store.list() if cfg.cohort_id == result.cohort_id]
    assert len(members) == 5

    resources = strategy_registry.StrategyResources(
        history={},
        benchmark_history=[],
        store=SecStore(tmp_path / "sec.sqlite3"),
    )
    for cfg in members:
        assert cfg.definition is not None
        assert cfg.configuration_hash == cfg.definition.configuration_hash
        rebuilt = strategy_registry.reconstruct(
            cfg.definition, list(cfg.universe), resources=resources
        )
        assert rebuilt.name == cfg.strategy
        assert list(rebuilt.universe) == list(cfg.universe)


def test_a_stored_definition_cannot_redefine_a_frozen_experiment(tmp_path: Path) -> None:
    """A tampered parameter fails closed rather than running a different experiment."""
    plan = _plan()
    spec = next(s for s in plan.specs if s.name == "dual-momentum-v1")
    tampered = spec.definition.model_copy(
        update={
            "parameters": {**spec.definition.parameters, "lookback_sessions": 60},
            "configuration_hash": "",
        }
    )

    with pytest.raises(cs.ContractViolationError, match="lookback_sessions"):
        strategy_registry.reconstruct(
            tampered,
            list(spec.universe),
            resources=strategy_registry.StrategyResources(history={}, benchmark_history=[]),
        )


def test_each_member_declares_only_the_capabilities_it_actually_needs() -> None:
    by_name = {spec.name: spec for spec in _plan().specs}

    assert by_name["control-cash"].definition.data_requirements == ()
    assert by_name["bench-spy"].definition.data_requirements == ()
    assert by_name["dual-momentum-v1"].definition.data_requirements == ("daily-price-history",)
    # No price enters the quality ranking, so it must not claim price history.
    assert by_name["quality-profitability-v1"].definition.data_requirements == (
        "sec-edgar-facts",
    )
    assert by_name["short-term-mean-reversion-v1"].definition.data_requirements == (
        "daily-price-history",
    )


def test_inspect_returns_the_exact_stored_manifest_and_records(tmp_path: Path) -> None:
    store = _store(tmp_path)
    plan = _plan()
    cc.create(plan, store=store)

    payload = cc.inspect_cohort(store=store, cohort_id=plan.cohort_id)

    assert payload["manifest"] == plan.manifest.model_dump(mode="json")
    stored = {record["name"]: record for record in payload["stored_sleeves"]}
    assert set(stored) == set(EXPECTED_MEMBERS)
    for spec in plan.specs:
        assert stored[spec.name]["configuration_hash"] == spec.configuration_hash
        assert stored[spec.name]["reproducible"] is True


def test_inspecting_an_unknown_cohort_raises_rather_than_inventing_one(
    tmp_path: Path,
) -> None:
    with pytest.raises(FileNotFoundError):
        cc.inspect_cohort(store=_store(tmp_path), cohort_id="challenger-v1-never-created")


# --- what must not have changed ----------------------------------------------


def test_the_july_cohort_definition_hashes_are_untouched() -> None:
    """The registry gained three strategies; July's definitions must not have moved.

    ``tests/test_dual_momentum.py`` pins the same hashes independently. Repeating the
    check from the integration issue's own suite is deliberate: this is the change most
    able to move them, so the assertion belongs next to it.
    """
    import importlib.util
    import sys

    path = Path(__file__).resolve().parents[1] / "scripts" / "bootstrap_paper_cohort.py"
    spec = importlib.util.spec_from_file_location("bootstrap_for_challenger_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    hashes = {
        built.name: built.definition.configuration_hash
        for built in (module._make_spec(t) for t in module._TEMPLATES)
    }
    assert hashes == JULY_COHORT_HASHES


def _july_28_control_cash(store: SleeveStore):
    """A stand-in for the running July 28 cohort's ``control-cash`` sleeve."""
    return store.create(
        "control-cash",
        strategy="hold",
        universe=["SPY"],
        starting_cash=Decimal("10000.00"),
        max_positions=1,
        max_position_fraction=Decimal("1"),
        definition=strategy_registry.make_definition("hold", universe_definition=["SPY"]),
        cohort_id="paper-first-2026-07-28",
    )


def test_both_cohorts_coexist_in_shared_storage_without_disturbing_each_other(
    tmp_path: Path,
) -> None:
    """The production layout: two active cohorts, scoped by cohort, sharing sleeve names."""
    database = Database(f"sqlite:///{tmp_path / 'shared.sqlite3'}", create_schema=True)
    store = SqlAlchemySleeveStore(database)
    before = _july_28_control_cash(store).model_dump(mode="json")

    plan = _plan()
    result = cc.create(plan, store=store)

    assert result.created == EXPECTED_MEMBERS
    after = store.resolve("control-cash", cohort_id="paper-first-2026-07-28")
    assert after is not None
    assert after.model_dump(mode="json") == before
    # The two same-named sleeves are distinct records in distinct cohorts, so neither
    # cohort's cash, positions, or observations can reach the other.
    challenger = store.resolve("control-cash", cohort_id=plan.cohort_id)
    assert challenger is not None
    assert challenger.sleeve_id != after.sleeve_id
    assert challenger.cohort_id == plan.cohort_id
    # July keeps the close-marked methodology it was actually recorded under.
    assert after.execution_methodology == ""
    assert challenger.execution_methodology == execution_timing.NEXT_OPEN_METHODOLOGY_KEY


def test_the_local_single_cohort_registry_refuses_a_name_collision_up_front(
    tmp_path: Path,
) -> None:
    """A real constraint, reported as a precondition rather than a mid-create surprise.

    The local registry keys sleeves by name alone, and challenger-v1 freezes two names
    July 28 also uses. Renaming is not available — they are contract values, and
    ``bench-spy`` is the benchmark every definition references — so the operator is told
    to use shared storage, and nothing is written.
    """
    store = _store(tmp_path)
    before = _july_28_control_cash(store).model_dump(mode="json")

    with pytest.raises(cc.ChallengerConflictError, match="SCHWAB_DATABASE_URL"):
        cc.create(_plan(), store=store)

    configs = store.list()
    assert [cfg.model_dump(mode="json") for cfg in configs] == [before]
    assert not cc.manifest_path(store.dir, _plan().cohort_id).exists()


def test_the_challenger_cohort_is_active_and_runnable_and_july_27_is_not() -> None:
    plan = _plan()

    assert cohort_lifecycle.run_refusal(plan.cohort_id) is None
    assert not cohort_lifecycle.is_historical(plan.cohort_id)
    assert cohort_lifecycle.run_refusal("paper-first-2026-07-27") is not None


def test_the_new_methodology_is_allowed_for_the_challenger_but_not_for_july() -> None:
    plan = _plan()

    execution_timing.ensure_methodology_allowed(plan.cohort_id, cc.METHODOLOGY)
    for protected in execution_timing.PROTECTED_LEGACY_COHORTS:
        with pytest.raises(execution_timing.ProtectedCohortError):
            execution_timing.ensure_methodology_allowed(protected, cc.METHODOLOGY)
