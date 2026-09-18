"""Static checks for the project-local agent skill catalog.

These tests never execute a skill. They assert the properties that keep the catalog safe
and cheap: every skill exists on both platforms with equivalent meaning, each routes to
exactly one canonical workflow, startup instructions stay small, and no skill smuggles in
an installer, hook, memory system, or global configuration change. Everything here reads
checked-in text only: no network, no storage engine, no ``.env``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
PLATFORMS = (".agents", ".claude")

# The two workflows that remain mandatory top-level gates.
GATE_SKILLS = ("github-task", "github-pr-review")

# The on-demand specialists curated from the pinned upstream projects.
SPECIALIST_SKILLS = (
    "architecture-review",
    "build-diagnosis",
    "dashboard-qa",
    "incident-investigation",
    "trading-security-review",
)

# On-demand specialists written original to this repository rather than curated from an
# upstream project. They follow the same catalog contract but carry no third-party
# attribution obligation.
ORIGINAL_SKILLS = ("quant-research-review",)

ALL_SKILLS = tuple(sorted(GATE_SKILLS + SPECIALIST_SKILLS + ORIGINAL_SKILLS))

# Every on-demand specialist that ships a Codex `agents/openai.yaml` interface, curated
# or original alike.
CODEX_INTERFACE_SKILLS = SPECIALIST_SKILLS + ORIGINAL_SKILLS

# Specialists that never mutate anything. ``build-diagnosis`` and ``incident-investigation``
# are deliberately absent: both may edit, but only under an Issue that authorizes it.
NON_MUTATING_SPECIALISTS = (
    "architecture-review",
    "dashboard-qa",
    "trading-security-review",
)

UPSTREAM_SHAS = (
    "0c1d7be9a750627fb2a6534c78a998cc46d03f9c",  # ECC
    "a3259400a366593e0c909dd9ac3e59752efd2488",  # gstack
)

# Capabilities the repository policy refuses to adopt from upstream frameworks.
PROHIBITED_CAPABILITIES = (
    "install.sh",
    "install.ps1",
    "npm install -g",
    "pip install",
    "curl -",
    "PreToolUse",
    "PostToolUse",
    "hooks:",
    "mcp-configs",
    "mcpServers",
    "setup-browser-cookies",
    "import cookies",
    "land-and-deploy",
    "gstack-upgrade",
    "learnings-log",
    "learnings-search",
    "~/.claude",
    "~/.codex",
    "~/.gstack",
)

# Phrases that hand a non-mutating specialist the authority to change something. A runtime
# loads SKILL.md and, on Codex, agents/openai.yaml eagerly -- before any workflow document
# is opened -- so an inverted posture here is read even if the workflow is never fetched.
MUTATION_AUTHORITY_PHRASES = (
    "fix whatever",
    "fix every",
    "apply the fix",
    "apply fixes",
    "implement the",
    "deploy it",
    "make the change",
    "write the code",
)

# The clause each report-only specialist uses to refuse mutation in its Codex prompt.
REFUSAL_CLAUSES = {
    "architecture-review": "without implementing it",
    "dashboard-qa": "without fixing anything",
    "trading-security-review": "without fixing them",
}


def skill_file(platform: str, skill: str) -> Path:
    return ROOT / platform / "skills" / skill / "SKILL.md"


def codex_interface_file(skill: str) -> Path:
    return ROOT / ".agents" / "skills" / skill / "agents" / "openai.yaml"


def workflow_file(skill: str) -> Path:
    return ROOT / "docs" / "agent-workflows" / f"{skill}.md"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def normalized(path: Path) -> str:
    """Lowercase with runs of whitespace collapsed, so prose line wrapping cannot hide a
    required phrase from a substring assertion."""
    return " ".join(read(path).split()).lower()


def normalize_text(text: str) -> str:
    """``normalized`` for a string that is not on disk, so a probe can be applied in memory."""
    return " ".join(text.split()).lower()


def mutation_authority_violations(text: str) -> list[str]:
    """Return every mutation-authority phrase present in ``text``.

    Factored out of the assertions so the negative-control test can feed it a deliberately
    inverted surface without writing that surface to the repository.
    """
    haystack = normalize_text(text)
    return [phrase for phrase in MUTATION_AUTHORITY_PHRASES if phrase in haystack]


def parse_codex_interface(text: str) -> dict[str, str]:
    """Read the flat ``interface:`` block without a YAML dependency."""
    lines = [line for line in text.splitlines() if line.strip()]
    assert lines and lines[0].strip() == "interface:", "openai.yaml must open with 'interface:'"

    fields: dict[str, str] = {}
    for line in lines[1:]:
        assert line.startswith("  ") and not line.startswith("   "), (
            f"openai.yaml must stay a flat two-space interface block: {line!r}"
        )
        key, separator, value = line.partition(":")
        assert separator, f"interface line is not 'key: value': {line!r}"
        fields[key.strip()] = value.strip().strip('"')
    return fields


def parse_frontmatter(text: str) -> dict[str, str]:
    """Read the minimal ``key: value`` frontmatter block without a YAML dependency."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise AssertionError("SKILL.md must open with a '---' frontmatter fence")
    try:
        closing = lines.index("---", 1)
    except ValueError as exc:
        raise AssertionError("SKILL.md frontmatter is not closed by '---'") from exc

    fields: dict[str, str] = {}
    for line in lines[1:closing]:
        if not line.strip():
            continue
        key, separator, value = line.partition(":")
        assert separator, f"Frontmatter line is not 'key: value': {line!r}"
        fields[key.strip()] = value.strip()
    return fields


@pytest.mark.parametrize("skill", ALL_SKILLS)
@pytest.mark.parametrize("platform", PLATFORMS)
def test_every_skill_is_present_on_both_platforms(platform: str, skill: str) -> None:
    assert skill_file(platform, skill).is_file()


@pytest.mark.parametrize("skill", ALL_SKILLS)
@pytest.mark.parametrize("platform", PLATFORMS)
def test_frontmatter_is_valid_and_minimal(platform: str, skill: str) -> None:
    fields = parse_frontmatter(read(skill_file(platform, skill)))

    assert set(fields) == {"name", "description"}, (
        f"{platform}/{skill} frontmatter must carry exactly 'name' and 'description'"
    )
    assert fields["name"] == skill
    assert len(fields["description"]) >= 60, "a vague description mistriggers the skill"


@pytest.mark.parametrize("skill", ALL_SKILLS)
def test_platform_surfaces_are_semantically_equivalent(skill: str) -> None:
    agents = parse_frontmatter(read(skill_file(".agents", skill)))
    claude = parse_frontmatter(read(skill_file(".claude", skill)))

    assert agents == claude, f"{skill} frontmatter drifted between platforms"


@pytest.mark.parametrize("skill", CODEX_INTERFACE_SKILLS)
def test_codex_surface_declares_an_interface(skill: str) -> None:
    fields = parse_codex_interface(read(codex_interface_file(skill)))

    assert set(fields) == {"display_name", "short_description", "default_prompt"}, (
        f"{skill} openai.yaml must declare exactly the three interface fields"
    )
    assert f"${skill}" in fields["default_prompt"]


@pytest.mark.parametrize("skill", ALL_SKILLS)
@pytest.mark.parametrize("platform", PLATFORMS)
def test_each_skill_routes_to_exactly_one_canonical_workflow(platform: str, skill: str) -> None:
    body = read(skill_file(platform, skill))
    referenced = {
        candidate
        for candidate in ALL_SKILLS
        if f"docs/agent-workflows/{candidate}.md" in body
    }

    assert referenced == {skill}, f"{platform}/{skill} must route to its own workflow only"
    assert workflow_file(skill).is_file()


@pytest.mark.parametrize("skill", ALL_SKILLS)
@pytest.mark.parametrize("platform", PLATFORMS)
def test_startup_instructions_stay_concise(platform: str, skill: str) -> None:
    """A router is loaded eagerly, so it must stay small; the detail lives downstream."""
    assert len(read(skill_file(platform, skill)).splitlines()) < 20


def test_claude_entrypoint_stays_lean_and_lists_no_catalog() -> None:
    content = read(ROOT / "CLAUDE.md")

    assert content.splitlines()[0] == "@AGENTS.md"
    assert len(content.splitlines()) < 30
    assert "@docs/" not in content
    for skill in SPECIALIST_SKILLS + ORIGINAL_SKILLS:
        assert skill not in content, "CLAUDE.md must route by description, not by catalog"


def test_gate_workflows_remain_the_top_level_entry_points() -> None:
    content = read(ROOT / "CLAUDE.md")

    for skill in GATE_SKILLS:
        assert f"`{skill}`" in content


WORKFLOW_BOUNDARIES = [
    ("trading-security-review", "findings only"),
    ("architecture-review", "review only"),
    ("dashboard-qa", "report-only"),
    # These two may edit, but only under an Issue that authorizes implementation.
    ("build-diagnosis", "only when the invoking github task authorizes implementation"),
    ("incident-investigation", "when the invoking github task authorizes implementation"),
]

# The same guarantee, pinned on the eagerly loaded router instead of the lazily read
# workflow document. Four skills repeat their workflow phrase verbatim.
#
# ``incident-investigation`` does not: its router carries an evidence-first boundary but
# never states the authorization clause that `docs/agent-workflows/incident-investigation.md`
# does, so this table pins the sentence that router actually declares today. Adding the
# clause to the router is a behavior change rather than a test fix, so it is filed as #123;
# tighten this entry to the authorization phrase when that lands.
ROUTER_BOUNDARIES = [
    ("trading-security-review", "findings only"),
    ("architecture-review", "review only"),
    ("dashboard-qa", "report-only"),
    ("build-diagnosis", "only when the invoking github task authorizes implementation"),
    ("incident-investigation", "apply no fix until a root cause is confirmed"),
]


@pytest.mark.parametrize(("skill", "required_phrase"), WORKFLOW_BOUNDARIES)
def test_specialist_workflows_declare_their_mutation_boundary(
    skill: str, required_phrase: str
) -> None:
    assert required_phrase in normalized(workflow_file(skill))


@pytest.mark.parametrize(("skill", "required_phrase"), ROUTER_BOUNDARIES)
@pytest.mark.parametrize("platform", PLATFORMS)
def test_specialist_routers_declare_their_mutation_boundary(
    platform: str, skill: str, required_phrase: str
) -> None:
    """The router is loaded before any workflow document, so it must carry the boundary too."""
    assert required_phrase in normalized(skill_file(platform, skill)), (
        f"{platform}/{skill} router no longer declares its mutation boundary"
    )


def test_build_diagnosis_forbids_silencing_a_check() -> None:
    body = normalized(workflow_file("build-diagnosis"))

    assert "never suppress a check" in body
    for suppression in ("type: ignore", "noqa", "ts-ignore", "xfail"):
        assert suppression in body, "the workflow must name the suppressions it forbids"


def test_dashboard_qa_is_report_only_and_offline() -> None:
    body = normalized(workflow_file("dashboard-qa"))

    assert "report-only" in body
    for forbidden_effect in (
        "never import, reuse, or export browser cookies",
        "never authenticate to schwab",
        "do not edit source files",
        "never open a deployed or authenticated dashboard",
    ):
        assert forbidden_effect in body


@pytest.mark.parametrize("skill", ALL_SKILLS)
@pytest.mark.parametrize("platform", PLATFORMS)
def test_skills_introduce_no_prohibited_capability(platform: str, skill: str) -> None:
    body = read(skill_file(platform, skill))

    for capability in PROHIBITED_CAPABILITIES:
        assert capability not in body


@pytest.mark.parametrize("skill", SPECIALIST_SKILLS)
def test_codex_interfaces_introduce_no_prohibited_capability(skill: str) -> None:
    """openai.yaml is read eagerly on Codex, so it earns the same denylist as the router."""
    body = read(codex_interface_file(skill))

    for capability in PROHIBITED_CAPABILITIES:
        assert capability not in body


@pytest.mark.parametrize("skill", NON_MUTATING_SPECIALISTS)
@pytest.mark.parametrize("platform", PLATFORMS)
def test_non_mutating_routers_grant_no_mutation_authority(platform: str, skill: str) -> None:
    violations = mutation_authority_violations(read(skill_file(platform, skill)))

    assert violations == [], f"{platform}/{skill} router now grants mutation authority"


@pytest.mark.parametrize("skill", NON_MUTATING_SPECIALISTS)
def test_non_mutating_codex_interfaces_grant_no_mutation_authority(skill: str) -> None:
    violations = mutation_authority_violations(read(codex_interface_file(skill)))

    assert violations == [], f"{skill} openai.yaml now grants mutation authority"


@pytest.mark.parametrize("skill", NON_MUTATING_SPECIALISTS)
def test_codex_default_prompt_refuses_mutation(skill: str) -> None:
    """``default_prompt`` is the invocation Codex offers the operator, so it must refuse too."""
    fields = parse_codex_interface(read(codex_interface_file(skill)))

    assert REFUSAL_CLAUSES[skill] in fields["default_prompt"].lower(), (
        f"{skill} openai.yaml default_prompt dropped its refusal clause"
    )


@pytest.mark.parametrize("skill", ALL_SKILLS)
def test_platform_bodies_agree_on_every_boundary_phrase(skill: str) -> None:
    """Parity at phrase level: the two bodies legitimately differ in word order, so a
    verbatim comparison is the wrong shape -- what must not drift is the posture."""
    vocabulary = {phrase for _, phrase in ROUTER_BOUNDARIES} | set(MUTATION_AUTHORITY_PHRASES)
    present = {
        platform: {
            phrase for phrase in vocabulary if phrase in normalized(skill_file(platform, skill))
        }
        for platform in PLATFORMS
    }

    assert present[".agents"] == present[".claude"], (
        f"{skill} posture drifted between platforms: "
        f"{present['.agents'] ^ present['.claude']}"
    )


# Each probe inverts a posture on an eagerly loaded surface. They are applied in memory to
# the real checked-in text, so this pins the guarantee rather than re-running a one-off
# reviewer exercise. Every probe passed the suite before #115.
INVERSION_PROBES = [
    ("A", ".claude", "trading-security-review", " Fix whatever you find."),
    ("B", ".claude", "dashboard-qa", " Apply the fixes you find."),
    ("C", ".agents", "architecture-review", " Implement the design once the review is written."),
]


@pytest.mark.parametrize(("probe", "platform", "skill", "inversion"), INVERSION_PROBES)
def test_an_inverted_router_posture_is_rejected(
    probe: str, platform: str, skill: str, inversion: str
) -> None:
    inverted = read(skill_file(platform, skill)) + inversion

    assert mutation_authority_violations(inverted), (
        f"probe {probe} inverted {platform}/{skill} and nothing rejected it"
    )


def test_an_inverted_codex_prompt_is_rejected() -> None:
    """Probe D: an extra key smuggling mutation authority into the Codex interface."""
    inverted = (
        read(codex_interface_file("dashboard-qa"))
        + '  extra_prompt: "Fix every defect you find and deploy it."\n'
    )

    assert mutation_authority_violations(inverted), "probe D was not rejected by content"
    with pytest.raises(AssertionError):
        fields = parse_codex_interface(inverted)
        assert set(fields) == {"display_name", "short_description", "default_prompt"}


@pytest.mark.parametrize("skill", ALL_SKILLS)
def test_workflows_introduce_no_prohibited_capability(skill: str) -> None:
    body = read(workflow_file(skill))

    for capability in PROHIBITED_CAPABILITIES:
        assert capability not in body


def test_quant_research_review_description_declares_precise_triggers() -> None:
    """The router is loaded eagerly and its description is the only signal an agent has
    before invoking the skill, so the trigger vocabulary must be pinned: precise enough to
    fire on research review and absent the vocabulary of unrelated specialists."""
    description = parse_frontmatter(read(skill_file(".claude", "quant-research-review")))[
        "description"
    ]
    lowered = description.lower()

    for trigger in (
        "backtest",
        "cohort",
        "factor model",
        "hmm",
        "gradient boosting",
        "reinforcement learning",
        "promotion decision",
    ):
        assert trigger in lowered, f"description is missing the '{trigger}' trigger"

    for out_of_scope in ("dashboard", "broker", "ci failure", "build failure"):
        assert out_of_scope not in lowered, (
            f"description should not mistrigger on '{out_of_scope}' work"
        )


def test_quant_research_review_forbids_live_trading_and_alpha_claims() -> None:
    body = normalized(workflow_file("quant-research-review"))

    assert "findings only" in body
    for boundary in (
        "never write or imply that a strategy is safe for live trading",
        "never authorize",
        "never claim persistent alpha",
        "30-session operational paper cohort",
        "cannot establish persistent alpha",
    ):
        assert boundary in body, f"quant-research-review workflow must state: {boundary!r}"


def test_quant_research_review_declares_verdict_vocabulary() -> None:
    body = normalized(workflow_file("quant-research-review"))

    for verdict in (
        "invalid design",
        "insufficient evidence",
        "suitable for exploratory backtest",
        "suitable for forward paper evaluation",
        "ready for a separate human promotion decision",
    ):
        assert verdict in body, f"quant-research-review must name the verdict: {verdict!r}"


@pytest.mark.parametrize(
    "required_phrase",
    [
        # Point-in-time data integrity
        "point-in-time",
        "survivorship",
        "corporate action",
        # Leakage and execution timing
        "look-ahead",
        "label leakage",
        "t-close",
        "t+1",
        # Costs
        "slippage",
        "commissions",
        # Experimental design and statistical validity
        "walk-forward",
        "embargo",
        "multiple-hypothesis",
        "reproducib",
    ],
)
def test_quant_research_review_covers_required_review_domains(required_phrase: str) -> None:
    body = normalized(workflow_file("quant-research-review"))

    assert required_phrase in body, f"missing required review domain: {required_phrase!r}"


@pytest.mark.parametrize(
    ("scenario", "required_phrases"),
    [
        (
            "conventional factor strategy backtest",
            ("point-in-time", "look-ahead", "walk-forward"),
        ),
        (
            "hidden markov model regime strategy",
            ("hidden markov", "state identifiability", "regime instability"),
        ),
        (
            "gradient boosting signal model",
            ("gradient boosting", "feature-importance stability", "calibration"),
        ),
        (
            "reinforcement learning execution policy",
            ("reinforcement learning", "reward design", "distribution shift"),
        ),
    ],
)
def test_quant_research_review_forward_scenario_coverage(
    scenario: str, required_phrases: tuple[str, ...]
) -> None:
    """Synthetic forward-test scenarios. Each names a research-artifact family this skill
    must review and pins the specific checklist language that would catch its most common
    validity defect. This never runs the skill, a backtest, or a real model — it only
    asserts the canonical workflow still names the check."""
    body = normalized(workflow_file("quant-research-review"))

    for phrase in required_phrases:
        assert phrase in body, f"{scenario} scenario requires '{phrase}' guidance"


def test_attribution_pins_both_upstream_projects() -> None:
    notices = read(ROOT / "THIRD_PARTY_NOTICES.md")

    for sha in UPSTREAM_SHAS:
        assert sha in notices
    assert notices.count("MIT") >= 2
    for project in ("ECC", "gstack"):
        assert project in notices
    for skill in SPECIALIST_SKILLS:
        assert f"docs/agent-workflows/{skill}.md" in notices, (
            "every curated workflow needs a stated upstream relationship"
        )
