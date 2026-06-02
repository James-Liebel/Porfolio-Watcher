"""Live-trading readiness gate — operationalizes NEG_RISK_ARB_BLUEPRINT.md §14.

A structural-arb bot is only safe to run with real money once the paper track record proves
the strategy actually clears fees + slippage and that multi-leg baskets complete reliably (a
half-built basket is naked directional risk, not an arb). This module turns those acceptance
criteria into an enforced, testable gate:

  - enough resolved trade units (settled / converted baskets),
  - basket completion rate above a floor (legs filled without a FAILED unwind),
  - net realized PnL after modeled fees/slippage is positive (or above a USD floor).

`evaluate_live_readiness` is a pure function over basket rows so it is trivially unit-tested and
can be reused by both the engine preflight and the operator CLI (`scripts/check_live_readiness.py`).
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Baskets that left the in-flight EXECUTING state — i.e. an execution attempt we can judge.
_TERMINAL_STATUSES = {"OPEN", "CLOSED", "SETTLED", "FAILED"}
# Baskets that built all legs successfully (did not end in a FAILED leg-unwind).
_BUILT_STATUSES = {"OPEN", "CLOSED", "SETTLED"}
# Baskets whose PnL is fully realized (resolved on settlement or closed via conversion/unwind).
_RESOLVED_STATUSES = {"CLOSED", "SETTLED"}


@dataclass(frozen=True)
class ReadinessThresholds:
    min_resolved_baskets: int = 200
    min_completion_rate: float = 0.99
    min_net_pnl_usd: float = 0.0
    require_positive_net_pnl: bool = True


@dataclass
class ReadinessReport:
    attempted_baskets: int
    built_baskets: int
    failed_baskets: int
    resolved_baskets: int
    in_flight_baskets: int
    completion_rate: float | None
    net_realized_pnl: float
    thresholds: ReadinessThresholds
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return bool(self.checks) and all(ok for _, ok, _ in self.checks)

    def summary(self) -> dict[str, object]:
        return {
            "ready": self.ready,
            "attempted_baskets": self.attempted_baskets,
            "built_baskets": self.built_baskets,
            "failed_baskets": self.failed_baskets,
            "resolved_baskets": self.resolved_baskets,
            "in_flight_baskets": self.in_flight_baskets,
            "completion_rate": self.completion_rate,
            "net_realized_pnl": round(self.net_realized_pnl, 6),
            "checks": [
                {"name": name, "passed": ok, "detail": detail} for name, ok, detail in self.checks
            ],
        }

    def failure_reasons(self) -> list[str]:
        return [f"{name}: {detail}" for name, ok, detail in self.checks if not ok]


def _status_of(row: dict) -> str:
    return str(row.get("status") or "").strip().upper()


def _pnl_of(row: dict) -> float:
    try:
        return float(row.get("realized_net_pnl") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def evaluate_live_readiness(
    basket_rows: list[dict], thresholds: ReadinessThresholds | None = None
) -> ReadinessReport:
    """Judge live-readiness from basket history (rows from ArbRepository.list_baskets)."""
    th = thresholds or ReadinessThresholds()

    attempted = built = failed = resolved = in_flight = 0
    net_pnl = 0.0
    for row in basket_rows:
        status = _status_of(row)
        if status in _TERMINAL_STATUSES:
            attempted += 1
            net_pnl += _pnl_of(row)
            if status == "FAILED":
                failed += 1
            if status in _BUILT_STATUSES:
                built += 1
            if status in _RESOLVED_STATUSES:
                resolved += 1
        elif status in {"EXECUTING", ""}:
            in_flight += 1

    completion_rate = (built / attempted) if attempted > 0 else None

    checks: list[tuple[str, bool, str]] = []
    checks.append(
        (
            "resolved_baskets",
            resolved >= th.min_resolved_baskets,
            f"{resolved} resolved (need >= {th.min_resolved_baskets})",
        )
    )
    if completion_rate is None:
        checks.append(("completion_rate", False, "no attempted baskets to measure"))
    else:
        checks.append(
            (
                "completion_rate",
                completion_rate + 1e-9 >= th.min_completion_rate,
                f"{completion_rate:.4f} ({built}/{attempted} built, {failed} failed; "
                f"need >= {th.min_completion_rate:.4f})",
            )
        )
    if th.require_positive_net_pnl:
        checks.append(
            (
                "net_realized_pnl",
                net_pnl >= th.min_net_pnl_usd and net_pnl > 0,
                f"${net_pnl:.2f} (need > 0 and >= ${th.min_net_pnl_usd:.2f})",
            )
        )
    else:
        checks.append(
            (
                "net_realized_pnl",
                net_pnl >= th.min_net_pnl_usd,
                f"${net_pnl:.2f} (need >= ${th.min_net_pnl_usd:.2f})",
            )
        )

    return ReadinessReport(
        attempted_baskets=attempted,
        built_baskets=built,
        failed_baskets=failed,
        resolved_baskets=resolved,
        in_flight_baskets=in_flight,
        completion_rate=completion_rate,
        net_realized_pnl=net_pnl,
        thresholds=th,
        checks=checks,
    )


def format_report(report: ReadinessReport) -> str:
    lines = ["Live-readiness evaluation (NEG_RISK_ARB_BLUEPRINT.md section 14):"]
    for name, ok, detail in report.checks:
        mark = "PASS" if ok else "FAIL"
        lines.append(f"  [{mark}] {name}: {detail}")
    lines.append(f"  in-flight baskets (excluded): {report.in_flight_baskets}")
    verdict = "READY for live" if report.ready else "NOT READY - stay in paper"
    lines.append(f"  >> {verdict}")
    return "\n".join(lines)
