"""Unit tests for the residency/sovereignty access helper and its compiler wiring.

The residency bar is a compliance control, so the tests center on the two
failure directions that matter: a bar must never silently relax (an unknown
value raises rather than passing everything), and an unattested provider
must fail closed against any bar above level 1.
"""

import pytest

from gateway.core.config import GatewayConfig
from gateway.models.routing import PolicySpec
from gateway.services.routing import NoEligibleCandidatesError, compile_policy
from gateway.services.sovereignty_access import (
    SovereigntyMap,
    SovereigntyRecord,
    build_sovereignty_map,
    combine_residency_bars,
    config_sovereignty,
    is_sovereign_enough,
    parse_residency_bar,
    residency_refusal_detail,
)


def _record(level: int) -> SovereigntyRecord:
    return SovereigntyRecord(level=level, hosted_in="CA", operator_jurisdiction="CA")


class _Row:
    """Stand-in for a ProviderSovereignty ORM row."""

    def __init__(self, provider: str, model: str | None, level: int, org: object = None) -> None:
        self.provider = provider
        self.model = model
        self.sovereignty_level = level
        self.hosted_in = "CA"
        self.operator_jurisdiction = "CA"
        self.model_origin = None
        self.vetted = False
        self.organization_id = org


class TestParseResidencyBar:
    def test_wire_mapping(self) -> None:
        assert parse_residency_bar("canadian") == 2
        assert parse_residency_bar("sovereign") == 3
        assert parse_residency_bar("sovereign_model") == 4

    def test_case_and_whitespace_tolerated(self) -> None:
        assert parse_residency_bar("  Sovereign ") == 3

    def test_unknown_bar_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown residency policy"):
            parse_residency_bar("european")


class TestIsSovereignEnough:
    def _map(self) -> SovereigntyMap:
        smap = SovereigntyMap()
        smap.set_provider("l1box", _record(1))
        smap.set_provider("l2box", _record(2))
        smap.set_provider("l3box", _record(3))
        smap.set_provider("l4box", _record(4))
        return smap

    def test_level_1_bar_passes_everything(self) -> None:
        smap = self._map()
        for inst in ("l1box", "l2box", "l3box", "l4box", "unattested"):
            allowed, _ = is_sovereign_enough(smap, inst, "m", 1)
            assert allowed

    def test_level_2_bar_excludes_level_1_and_unattested(self) -> None:
        smap = self._map()
        assert is_sovereign_enough(smap, "l1box", "m", 2)[0] is False
        assert is_sovereign_enough(smap, "unattested", "m", 2)[0] is False
        assert is_sovereign_enough(smap, "l2box", "m", 2)[0] is True
        assert is_sovereign_enough(smap, "l3box", "m", 2)[0] is True

    def test_level_3_bar_excludes_1_and_2(self) -> None:
        smap = self._map()
        assert is_sovereign_enough(smap, "l2box", "m", 3)[0] is False
        assert is_sovereign_enough(smap, "l3box", "m", 3)[0] is True

    def test_level_4_only_level_4(self) -> None:
        smap = self._map()
        assert is_sovereign_enough(smap, "l3box", "m", 4)[0] is False
        assert is_sovereign_enough(smap, "l4box", "m", 4)[0] is True

    def test_deny_reason_names_levels(self) -> None:
        smap = self._map()
        allowed, reason = is_sovereign_enough(smap, "l2box", "m", 3)
        assert not allowed
        assert "level 2" in reason and "required 3" in reason


class TestPrecedence:
    def test_model_row_wins_over_provider_row(self) -> None:
        smap = SovereigntyMap()
        smap.set_provider("box", _record(2))
        smap.set_model("box", "good-model", _record(3))
        assert smap.level_for("box", "good-model") == 3
        assert smap.level_for("box", "other-model") == 2

    def test_db_rows_overlay_config(self) -> None:
        sov2 = {"level": 2, "hosted_in": "CA", "operator_jurisdiction": "US_CLOUD_ACT"}
        config = GatewayConfig(
            master_key="k",
            providers={
                "box": {"api_key": "x", "sovereignty": sov2},
                "other": {"api_key": "x"},
            },
        )
        rows = [_Row("box", None, 3)]
        smap = build_sovereignty_map(config, rows)
        assert smap.level_for("box", "m") == 3  # DB wins
        assert smap.level_for("other", "m") == 1  # config has no attestation

    def test_org_row_wins_over_deployment_wide(self) -> None:
        org_id = object()
        rows = [
            _Row("box", None, 2, org=None),
            _Row("box", None, 4, org=org_id),
        ]
        smap = build_sovereignty_map(GatewayConfig(master_key="k"), rows)
        assert smap.level_for("box", "m") == 4

    def test_config_models_submap(self) -> None:
        config = GatewayConfig(
            master_key="k",
            providers={
                "box": {
                    "api_key": "x",
                    "sovereignty": {
                        "level": 2,
                        "hosted_in": "CA",
                        "operator_jurisdiction": "CA",
                        "models": {"premium": {"level": 4, "hosted_in": "CA", "operator_jurisdiction": "CA"}},
                    },
                }
            },
        )
        smap = config_sovereignty(config)
        assert smap.level_for("box", "premium") == 4
        assert smap.level_for("box", "basic") == 2


class TestCombineResidencyBars:
    """The plan-01b max() table: floor x request bar x absent."""

    def test_absent_both_is_unconstrained(self) -> None:
        assert combine_residency_bars(None, None) == (1, "none")

    def test_request_bar_alone(self) -> None:
        assert combine_residency_bars("canadian", None) == (2, "request")
        assert combine_residency_bars("sovereign_model", None) == (4, "request")

    def test_floor_alone_binds(self) -> None:
        assert combine_residency_bars(None, "sovereign") == (3, "organization")

    def test_lower_request_bar_cannot_relax_the_floor(self) -> None:
        # Request 'canadian' under a 'sovereign' floor: level 3 anyway.
        assert combine_residency_bars("canadian", "sovereign") == (3, "organization")

    def test_higher_request_bar_raises_above_the_floor(self) -> None:
        assert combine_residency_bars("sovereign_model", "canadian") == (4, "request")

    def test_tie_reports_both_sources(self) -> None:
        assert combine_residency_bars("sovereign", "sovereign") == (3, "both")

    def test_unknown_bar_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown residency policy"):
            combine_residency_bars("european", None)
        with pytest.raises(ValueError, match="unknown residency policy"):
            combine_residency_bars(None, "european")


class TestResidencyRefusalCopy:
    def test_request_source_names_the_bar(self) -> None:
        detail = residency_refusal_detail("sovereign", 3, "request")
        assert "Residency policy 'sovereign'" in detail

    def test_organization_source_names_the_floor(self) -> None:
        detail = residency_refusal_detail(None, 3, "organization")
        assert "This organization's residency policy" in detail
        assert "None" not in detail

    def test_both_source_names_both(self) -> None:
        detail = residency_refusal_detail("sovereign", 3, "both")
        assert "organization's residency floor" in detail
        assert "'sovereign'" in detail


class TestCompilerResidency:
    """The bar filters every candidate in the compiled plan, failover included."""

    def _config(self) -> GatewayConfig:
        sov2 = {"level": 2, "hosted_in": "CA", "operator_jurisdiction": "US_CLOUD_ACT"}
        sov3 = {"level": 3, "hosted_in": "CA", "operator_jurisdiction": "CA"}
        return GatewayConfig(
            master_key="k",
            model_discovery=False,
            providers={
                "l2box": {"provider_type": "openai", "api_key": "x", "sovereignty": sov2},
                "l3box": {"provider_type": "openai", "api_key": "y", "sovereignty": sov3},
            },
        )

    def _spec(self, default: str, *on_failure: str) -> PolicySpec:
        return PolicySpec.model_validate({"select": [{"default": default}], "on_failure": list(on_failure)})

    def _map(self) -> SovereigntyMap:
        smap = SovereigntyMap()
        smap.set_provider("l2box", _record(2))
        smap.set_provider("l3box", _record(3))
        return smap

    def test_head_below_bar_dropped_failover_kept(self) -> None:
        plan = compile_policy(
            self._config(),
            "p",
            self._spec("l2box:m", "l3box:m"),
            required_sovereignty=3,
            sovereignty=self._map(),
        )
        assert [a.instance for a in plan.attempts] == ["l3box"]
        assert any(d.reason == "residency" for d in plan.dropped)

    def test_all_below_bar_is_a_403(self) -> None:
        with pytest.raises(NoEligibleCandidatesError) as exc_info:
            compile_policy(
                self._config(),
                "p",
                self._spec("l2box:m"),
                required_sovereignty=3,
                sovereignty=self._map(),
            )
        assert exc_info.value.status_code == 403

    def test_no_bar_passes_all(self) -> None:
        plan = compile_policy(self._config(), "p", self._spec("l2box:m", "l3box:m"))
        assert len(plan.attempts) == 2

    def test_allowlist_and_residency_both_enforced(self) -> None:
        # Allow-list admits both instances; residency drops the level-2 one.
        plan = compile_policy(
            self._config(),
            "p",
            self._spec("l2box:m", "l3box:m"),
            allowlist=["l2box:*", "l3box:*"],
            required_sovereignty=3,
            sovereignty=self._map(),
        )
        assert [a.instance for a in plan.attempts] == ["l3box"]
        # Allow-list denies the level-3 one; residency cannot rescue it.
        with pytest.raises(NoEligibleCandidatesError):
            compile_policy(
                self._config(),
                "p",
                self._spec("l3box:m"),
                allowlist=["l2box:*"],
                required_sovereignty=3,
                sovereignty=self._map(),
            )
