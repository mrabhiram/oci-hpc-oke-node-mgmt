from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

import click

from oke_hpc_mgmt.commands.common import (
    CliState,
    confirm_plans,
    emit_plans,
    output_option,
    pass_state,
    print_discovery_warnings,
    progress,
    resolve_output,
)
from oke_hpc_mgmt.exit_codes import ACTION_REQUIRED
from oke_hpc_mgmt.render import print_records
from oke_hpc_mgmt.upgrades import UPGRADE_STRATEGIES
from oke_hpc_mgmt.workflows.upgrades import (
    PoolUpgradeSpec,
    abandon_upgrade,
    cleanup_upgrade,
    collect_upgrade_gate_evidence,
    execute_control_plane_upgrade,
    execute_pool_upgrade,
    execute_upgrade_apply,
    prepare_cluster_upgrade_plan,
    prepare_control_plane_upgrade,
    prepare_pool_upgrade,
    resume_upgrade,
)


CommandFunction = TypeVar("CommandFunction", bound=Callable[..., Any])


@click.group(help="Plan, execute, resume, and audit Kubernetes upgrades.")
def upgrades() -> None:
    pass


def target_options(function: CommandFunction) -> CommandFunction:
    function = click.option(
        "--allow-preview",
        is_flag=True,
        help="Allow an explicitly advertised preview .0 Kubernetes target.",
    )(function)
    function = click.option(
        "--to",
        "target",
        required=True,
        metavar="VERSION",
        help="Target exact patch or minor version, for example v1.36 or v1.36.2.",
    )(function)
    return function


def execution_options(function: CommandFunction) -> CommandFunction:
    function = click.option(
        "--yes",
        is_flag=True,
        help="Confirm the OCI mutation; this does not replace safety acknowledgements.",
    )(function)
    function = click.option(
        "--dry-run",
        is_flag=True,
        help="Run available validation and print the plan without mutation.",
    )(function)
    function = click.option(
        "--poll-interval",
        type=click.IntRange(min=1),
        default=30,
        show_default=True,
    )(function)
    function = click.option(
        "--timeout",
        type=click.IntRange(min=1),
        default=7200,
        show_default=True,
        help="Maximum seconds to wait for each converging upgrade operation.",
    )(function)
    return function


def safety_ack_options(function: CommandFunction) -> CommandFunction:
    function = click.option(
        "--emergency-ack-unverified-drain",
        is_flag=True,
        help=(
            "Acknowledge independently verified drain only when API/RBAC/exec "
            "verification is unavailable; detected workloads remain blocking."
        ),
    )(function)
    function = click.option(
        "--ack-workloads-drained",
        is_flag=True,
        help="Attest that the current target pool was externally drained.",
    )(function)
    function = click.option(
        "--ack-iac-drift",
        is_flag=True,
        help="Acknowledge direct OCI changes that must be reconciled into IaC.",
    )(function)
    function = click.option(
        "--ack-application-compatibility",
        is_flag=True,
        help="Acknowledge application compatibility with the target Kubernetes version.",
    )(function)
    return function


def control_plane_ack_options(function: CommandFunction) -> CommandFunction:
    function = click.option(
        "--ack-iac-drift",
        is_flag=True,
        help="Acknowledge direct OCI changes that must be reconciled into IaC.",
    )(function)
    function = click.option(
        "--ack-application-compatibility",
        is_flag=True,
        help="Acknowledge application compatibility with the target Kubernetes version.",
    )(function)
    return function


def pool_strategy_options(function: CommandFunction) -> CommandFunction:
    function = click.option(
        "--blue-green-gpu-memory-fabric-id",
        help="Explicit target GPU Memory Fabric OCID for GMC blue-green.",
    )(function)
    function = click.option(
        "--blue-green-compute-cluster-id",
        help="Explicit target Compute Cluster OCID for GMC blue-green.",
    )(function)
    function = click.option(
        "--blue-green-name",
        help="Name for the parallel blue-green worker backend.",
    )(function)
    function = click.option(
        "--maximum-surge",
        help="OKE managed-pool cycling maximumSurge value.",
    )(function)
    function = click.option(
        "--maximum-unavailable",
        help="OKE managed-pool cycling maximumUnavailable value.",
    )(function)
    function = click.option(
        "--image-id",
        help="Custom target worker image OCID; the current image is used by default.",
    )(function)
    function = click.option(
        "--strategy",
        type=click.Choice(UPGRADE_STRATEGIES),
        default="auto",
        show_default=True,
    )(function)
    return function


def orchestration_pool_options(function: CommandFunction) -> CommandFunction:
    function = click.option(
        "--pool-blue-green-gpu-memory-fabric",
        multiple=True,
        metavar="POOL=OCID",
    )(function)
    function = click.option(
        "--pool-blue-green-compute-cluster",
        multiple=True,
        metavar="POOL=OCID",
    )(function)
    function = click.option(
        "--pool-blue-green-name",
        multiple=True,
        metavar="POOL=NAME",
    )(function)
    function = click.option(
        "--pool-maximum-surge",
        multiple=True,
        metavar="POOL=VALUE",
    )(function)
    function = click.option(
        "--pool-maximum-unavailable",
        multiple=True,
        metavar="POOL=VALUE",
    )(function)
    return function


@upgrades.command("status", help="Show upgrade readiness and observed versions.")
@click.option("--to", "target", metavar="VERSION")
@click.option("--allow-preview", is_flag=True)
@output_option
@pass_state
def upgrade_status(
    state: CliState,
    target: str | None,
    allow_preview: bool,
    output_override: str | None,
) -> int:
    service = state.service(
        include_pod_counts=True,
        include_autoscaler=True,
        include_kueue=True,
        include_addons=True,
        include_cluster=True,
    )
    snapshot = service.discover()
    cluster = snapshot.cluster
    if cluster is None:
        raise click.ClickException(
            "OKE control-plane discovery is required for upgrade status."
        )
    resolved = None
    compatibility = []
    if target:
        from oke_hpc_mgmt.upgrades import resolve_upgrade_target

        resolved = resolve_upgrade_target(
            target,
            cluster.available_kubernetes_versions,
            allow_preview=allow_preview,
        )
        compatibility = service.oci_backend().get_addon_compatibility(
            str(resolved),
            snapshot.addons,
        )
    rows: list[dict[str, object]] = [
        {
            "kind": "control-plane",
            "name": cluster.cluster_id,
            "declared_version": cluster.kubernetes_version,
            "actual_versions": cluster.kubernetes_version,
            "target_version": str(resolved) if resolved else None,
            "state": cluster.lifecycle_state,
            "strategy": "OKE UpdateCluster",
            "scheduler_state": "n/a",
            "available_versions": list(
                cluster.available_kubernetes_versions
            ),
        }
    ]
    for virtual in snapshot.virtual_pools:
        rows.append(
            {
                "kind": "virtual-node-pool",
                "name": virtual.name,
                "declared_version": virtual.kubernetes_version,
                "actual_versions": virtual.kubernetes_version,
                "target_version": str(resolved) if resolved else None,
                "state": virtual.lifecycle_state,
                "strategy": "automatic-with-control-plane",
                "scheduler_state": "OKE-managed",
                "available_versions": [],
            }
        )
    for pool in snapshot.pools:
        nodes = [
            node for node in snapshot.nodes if node.pool_name == pool.name
        ]
        evidence = collect_upgrade_gate_evidence(service, snapshot, pool)
        scheduler_state = (
            "blocked"
            if evidence.positively_blocked
            else (
                "unverified"
                if not evidence.verification_available
                else (
                    "drained"
                    if evidence.externally_cordoned
                    else "not-drained"
                )
            )
        )
        rows.append(
            {
                "kind": pool.kind,
                "name": pool.name,
                "declared_version": pool.kubernetes_version,
                "actual_versions": sorted(
                    {
                        node.kubelet_version or "unknown"
                        for node in nodes
                    }
                ),
                "target_version": str(resolved) if resolved else None,
                "state": (
                    f"{pool.ready_k8s_nodes}/{pool.desired_size or 0} Ready"
                ),
                "strategy": (
                    "auto,boot-volume-replace,instance-replace,blue-green"
                ),
                "scheduler_state": scheduler_state,
                "available_versions": [],
            }
        )
    if compatibility:
        for addon in compatibility:
            rows.append(
                {
                    "kind": "addon",
                    "name": addon.name,
                    "declared_version": addon.installed_version,
                    "actual_versions": addon.installed_version,
                    "target_version": str(resolved) if resolved else None,
                    "state": (
                        "compatible" if addon.compatible else "blocked"
                    ),
                    "strategy": addon.update_mode or "unknown",
                    "scheduler_state": "n/a",
                    "available_versions": list(addon.supported_versions),
                }
            )
    else:
        for installed_addon in snapshot.addons:
            rows.append(
                {
                    "kind": "addon",
                    "name": installed_addon.name,
                    "declared_version": installed_addon.version,
                    "actual_versions": installed_addon.version,
                    "target_version": None,
                    "state": installed_addon.lifecycle_state,
                    "strategy": installed_addon.update_mode or "unknown",
                    "scheduler_state": "n/a",
                    "available_versions": [],
                }
            )
    print_records(rows, resolve_output(state, output_override))
    print_discovery_warnings(snapshot.warnings)
    return 0


@upgrades.command("plan", help="Generate the complete ordered upgrade plan.")
@target_options
@click.option(
    "--strategy",
    type=click.Choice(UPGRADE_STRATEGIES),
    default="auto",
    show_default=True,
)
@click.option(
    "--pool-order",
    multiple=True,
    metavar="POOL",
    help="Complete worker-pool order; repeat once per pool.",
)
@click.option(
    "--pool-strategy",
    multiple=True,
    metavar="POOL=STRATEGY",
    help="Per-pool strategy override.",
)
@click.option(
    "--pool-image",
    multiple=True,
    metavar="POOL=IMAGE_OCID",
    help="Per-pool custom image override.",
)
@orchestration_pool_options
@output_option
@pass_state
def plan_upgrades(
    state: CliState,
    target: str,
    allow_preview: bool,
    strategy: str,
    pool_order: tuple[str, ...],
    pool_strategy: tuple[str, ...],
    pool_image: tuple[str, ...],
    pool_maximum_unavailable: tuple[str, ...],
    pool_maximum_surge: tuple[str, ...],
    pool_blue_green_name: tuple[str, ...],
    pool_blue_green_compute_cluster: tuple[str, ...],
    pool_blue_green_gpu_memory_fabric: tuple[str, ...],
    output_override: str | None,
) -> int:
    plan = prepare_cluster_upgrade_plan(
        _upgrade_service(state),
        target,
        allow_preview=allow_preview,
        order=pool_order,
        default_strategy=strategy,
        strategy_overrides=_parse_assignments(pool_strategy, "pool strategy"),
        image_overrides=_parse_assignments(pool_image, "pool image"),
        pool_spec_overrides=_pool_spec_overrides(
            pool_maximum_unavailable,
            pool_maximum_surge,
            pool_blue_green_name,
            pool_blue_green_compute_cluster,
            pool_blue_green_gpu_memory_fabric,
        ),
    )
    emit_plans(state, output_override, plan.plans)
    print_discovery_warnings(plan.snapshot.warnings)
    return 0


@click.command("upgrade", help="Execute one valid OKE control-plane upgrade step.")
@target_options
@control_plane_ack_options
@execution_options
@output_option
@pass_state
def upgrade_cluster(
    state: CliState,
    target: str,
    allow_preview: bool,
    ack_application_compatibility: bool,
    ack_iac_drift: bool,
    timeout: int,
    poll_interval: int,
    dry_run: bool,
    yes: bool,
    output_override: str | None,
) -> int:
    service = _upgrade_service(state)
    prepared = prepare_control_plane_upgrade(
        service,
        target,
        allow_preview=allow_preview,
    )
    control_plans = [
        plan
        for plan in prepared.plans
        if plan.operation == "control-plane-upgrade"
    ]
    if dry_run:
        emit_plans(state, output_override, control_plans)
        return 0
    confirm_plans(control_plans, prepared.target_version, approved=yes)
    result = execute_control_plane_upgrade(
        service,
        prepared,
        acknowledge_application_compatibility=(
            ack_application_compatibility
        ),
        acknowledge_iac_drift=ack_iac_drift,
        timeout_seconds=timeout,
        poll_interval_seconds=poll_interval,
        progress=progress,
    )
    print_records([result.as_dict()], resolve_output(state, output_override))
    return 0


@click.command(
    "upgrade",
    help=(
        "Upgrade one externally prepared worker pool; mgmt-oke never cordons, "
        "drains, evicts, or uncordons it."
    ),
)
@click.argument("pool")
@target_options
@pool_strategy_options
@safety_ack_options
@execution_options
@output_option
@pass_state
def upgrade_pool(
    state: CliState,
    pool: str,
    target: str,
    allow_preview: bool,
    strategy: str,
    image_id: str | None,
    maximum_unavailable: str | None,
    maximum_surge: str | None,
    blue_green_name: str | None,
    blue_green_compute_cluster_id: str | None,
    blue_green_gpu_memory_fabric_id: str | None,
    ack_application_compatibility: bool,
    ack_iac_drift: bool,
    ack_workloads_drained: bool,
    emergency_ack_unverified_drain: bool,
    timeout: int,
    poll_interval: int,
    dry_run: bool,
    yes: bool,
    output_override: str | None,
) -> int:
    service = _upgrade_service(state)
    prepared = prepare_pool_upgrade(
        service,
        pool,
        target,
        PoolUpgradeSpec(
            strategy=strategy,
            image_id=image_id,
            maximum_unavailable=maximum_unavailable,
            maximum_surge=maximum_surge,
            blue_green_name=blue_green_name,
            blue_green_compute_cluster_id=(
                blue_green_compute_cluster_id
            ),
            blue_green_gpu_memory_fabric_id=(
                blue_green_gpu_memory_fabric_id
            ),
        ),
        allow_preview=allow_preview,
    )
    if dry_run:
        emit_plans(state, output_override, [prepared.plan])
        print_discovery_warnings(prepared.snapshot.warnings)
        return 0
    confirm_plans([prepared.plan], pool, approved=yes)
    if not ack_workloads_drained:
        response = click.prompt(
            f"Type 'DRAINED {pool}' to attest external workload preparation",
            type=str,
            show_default=False,
            err=True,
        )
        ack_workloads_drained = response.strip() == f"DRAINED {pool}"
    result = execute_pool_upgrade(
        service,
        prepared,
        acknowledge_application_compatibility=(
            ack_application_compatibility
        ),
        acknowledge_iac_drift=ack_iac_drift,
        acknowledge_workloads_drained=ack_workloads_drained,
        emergency_ack_unverified_drain=(
            emergency_ack_unverified_drain
        ),
        timeout_seconds=timeout,
        poll_interval_seconds=poll_interval,
        progress=progress,
    )
    print_records([result.as_dict()], resolve_output(state, output_override))
    return ACTION_REQUIRED if result.status == "action-required" else 0


@upgrades.command("apply", help="Run checkpointed full-cluster orchestration.")
@target_options
@click.option(
    "--strategy",
    type=click.Choice(UPGRADE_STRATEGIES),
    default="auto",
    show_default=True,
)
@click.option("--pool-order", multiple=True, metavar="POOL")
@click.option("--pool-strategy", multiple=True, metavar="POOL=STRATEGY")
@click.option("--pool-image", multiple=True, metavar="POOL=IMAGE_OCID")
@orchestration_pool_options
@safety_ack_options
@execution_options
@output_option
@pass_state
def apply_upgrades(
    state: CliState,
    target: str,
    allow_preview: bool,
    strategy: str,
    pool_order: tuple[str, ...],
    pool_strategy: tuple[str, ...],
    pool_image: tuple[str, ...],
    pool_maximum_unavailable: tuple[str, ...],
    pool_maximum_surge: tuple[str, ...],
    pool_blue_green_name: tuple[str, ...],
    pool_blue_green_compute_cluster: tuple[str, ...],
    pool_blue_green_gpu_memory_fabric: tuple[str, ...],
    ack_application_compatibility: bool,
    ack_iac_drift: bool,
    ack_workloads_drained: bool,
    emergency_ack_unverified_drain: bool,
    timeout: int,
    poll_interval: int,
    dry_run: bool,
    yes: bool,
    output_override: str | None,
) -> int:
    service = _upgrade_service(state)
    plan = prepare_cluster_upgrade_plan(
        service,
        target,
        allow_preview=allow_preview,
        order=pool_order,
        default_strategy=strategy,
        strategy_overrides=_parse_assignments(pool_strategy, "pool strategy"),
        image_overrides=_parse_assignments(pool_image, "pool image"),
        pool_spec_overrides=_pool_spec_overrides(
            pool_maximum_unavailable,
            pool_maximum_surge,
            pool_blue_green_name,
            pool_blue_green_compute_cluster,
            pool_blue_green_gpu_memory_fabric,
        ),
    )
    if dry_run:
        emit_plans(state, output_override, plan.plans)
        print_discovery_warnings(plan.snapshot.warnings)
        return 0
    confirm_plans(plan.plans, plan.target_version, approved=yes)
    results = execute_upgrade_apply(
        service,
        plan,
        acknowledge_application_compatibility=(
            ack_application_compatibility
        ),
        acknowledge_iac_drift=ack_iac_drift,
        acknowledge_workloads_drained=ack_workloads_drained,
        emergency_ack_unverified_drain=(
            emergency_ack_unverified_drain
        ),
        timeout_seconds=timeout,
        poll_interval_seconds=poll_interval,
        progress=progress,
    )
    print_records(
        [result.as_dict() for result in results],
        resolve_output(state, output_override),
    )
    return (
        ACTION_REQUIRED
        if any(result.status == "action-required" for result in results)
        else 0
    )


@upgrades.command("resume", help="Resume the active operation from observed state.")
@click.option("--ack-workloads-drained", is_flag=True)
@click.option("--emergency-ack-unverified-drain", is_flag=True)
@click.option("--timeout", type=click.IntRange(min=1), default=7200)
@click.option("--poll-interval", type=click.IntRange(min=1), default=30)
@output_option
@pass_state
def resume_upgrades(
    state: CliState,
    ack_workloads_drained: bool,
    emergency_ack_unverified_drain: bool,
    timeout: int,
    poll_interval: int,
    output_override: str | None,
) -> int:
    results = resume_upgrade(
        _upgrade_service(state),
        acknowledge_workloads_drained=ack_workloads_drained,
        emergency_ack_unverified_drain=(
            emergency_ack_unverified_drain
        ),
        timeout_seconds=timeout,
        poll_interval_seconds=poll_interval,
        progress=progress,
    )
    print_records(
        [result.as_dict() for result in results],
        resolve_output(state, output_override),
    )
    return (
        ACTION_REQUIRED
        if any(result.status == "action-required" for result in results)
        else 0
    )


@upgrades.command("abandon", help="Abandon checkpoint state without rollback.")
@click.option("--yes", is_flag=True)
@output_option
@pass_state
def abandon_upgrades(
    state: CliState,
    yes: bool,
    output_override: str | None,
) -> int:
    if not yes:
        click.confirm(
            "Abandon the active upgrade checkpoint without rolling back OCI resources?",
            abort=True,
            err=True,
        )
    result = abandon_upgrade(_upgrade_service(state))
    print_records([result.as_dict()], resolve_output(state, output_override))
    return 0


@upgrades.command(
    "cleanup",
    help="Delete only superseded operation-owned Instance Configurations.",
)
@click.option("--yes", is_flag=True)
@output_option
@pass_state
def cleanup_upgrades(
    state: CliState,
    yes: bool,
    output_override: str | None,
) -> int:
    if not yes:
        click.confirm(
            "Clean up superseded mgmt-oke upgrade artifacts?",
            abort=True,
            err=True,
        )
    result = cleanup_upgrade(_upgrade_service(state))
    print_records([result.as_dict()], resolve_output(state, output_override))
    return 0


def _upgrade_service(state: CliState):
    return state.service(
        include_pod_counts=True,
        include_autoscaler=True,
        include_kueue=True,
        include_addons=True,
        include_pools=True,
        include_cluster=True,
    )


def _parse_assignments(
    values: tuple[str, ...],
    label: str,
) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise click.BadParameter(
                f"{label} must use POOL=VALUE: {value!r}"
            )
        name, setting = value.split("=", 1)
        if not name.strip() or not setting.strip():
            raise click.BadParameter(
                f"{label} must use non-empty POOL=VALUE: {value!r}"
            )
        if name in parsed:
            raise click.BadParameter(f"Duplicate {label} for pool {name}.")
        parsed[name.strip()] = setting.strip()
    return parsed


def _pool_spec_overrides(
    maximum_unavailable_values: tuple[str, ...],
    maximum_surge_values: tuple[str, ...],
    blue_green_name_values: tuple[str, ...],
    blue_green_compute_values: tuple[str, ...],
    blue_green_fabric_values: tuple[str, ...],
) -> dict[str, PoolUpgradeSpec]:
    maximum_unavailable = _parse_assignments(
        maximum_unavailable_values,
        "pool maximum unavailable",
    )
    maximum_surge = _parse_assignments(
        maximum_surge_values,
        "pool maximum surge",
    )
    blue_green_names = _parse_assignments(
        blue_green_name_values,
        "pool blue-green name",
    )
    blue_green_compute = _parse_assignments(
        blue_green_compute_values,
        "pool blue-green Compute Cluster",
    )
    blue_green_fabric = _parse_assignments(
        blue_green_fabric_values,
        "pool blue-green GPU Memory Fabric",
    )
    names = (
        set(maximum_unavailable)
        | set(maximum_surge)
        | set(blue_green_names)
        | set(blue_green_compute)
        | set(blue_green_fabric)
    )
    return {
        name: PoolUpgradeSpec(
            maximum_unavailable=maximum_unavailable.get(name),
            maximum_surge=maximum_surge.get(name),
            blue_green_name=blue_green_names.get(name),
            blue_green_compute_cluster_id=blue_green_compute.get(name),
            blue_green_gpu_memory_fabric_id=blue_green_fabric.get(name),
        )
        for name in names
    }
