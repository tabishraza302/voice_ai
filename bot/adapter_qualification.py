"""Adapter qualification probe and report helper per flow-contract §8-9 and Task 2.

Exposes the qualification runner behind the repository's adapter qualification module path:
python -m bot.adapter_qualification [options]
"""

from __future__ import annotations

from bot.flow.qualification_runner import (
    ContractScenarioDefinition,
    ContractScenarioResult,
    QualificationReport,
    REQUIRED_CONTRACT_SCENARIOS,
    REQUIRED_SCENARIO_CLASSES,
    build_contract_scenario_results,
    build_unexecuted_scenarios,
    execute_contract_scenarios,
    is_literal_loopback_endpoint,
    main,
    main_async,
    parse_args,
    run_qualification,
)

__all__ = [
    "ContractScenarioDefinition",
    "ContractScenarioResult",
    "QualificationReport",
    "REQUIRED_CONTRACT_SCENARIOS",
    "REQUIRED_SCENARIO_CLASSES",
    "build_contract_scenario_results",
    "build_unexecuted_scenarios",
    "is_literal_loopback_endpoint",
    "execute_contract_scenarios",
    "main",
    "main_async",
    "parse_args",
    "run_qualification",
]

if __name__ == "__main__":
    main()
