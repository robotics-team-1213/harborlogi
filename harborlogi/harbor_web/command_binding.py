"""Resolve a plan into installed arm/spatial bindings and the Fleet connection identity; no state, no I/O writes."""
import hashlib
import json
import os
from pathlib import Path

from django.core.exceptions import ValidationError

from harbor.process import SimulationCellTemplate, SpatialPlacementBinding, bind_plan, load_bindings
from harbor.outbound import CargoMove, OutboundProposal
from harbor.rosy_http import RosyClient


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _config():
    from harbor_web.fleet_connection import connection_settings
    from harbor_web.evidence import configured_sources
    connection = connection_settings()
    if connection['origin'] and not connection['allowed']:
        raise ValidationError('ORIGIN_NOT_ALLOWED')
    values = [os.environ.get('HARBOR_LEDGER_PATH', ''), connection['origin'],
        os.environ.get('HARBOR_FLEET_TOKEN', ''), connection['principal'], os.environ.get('HARBOR_BINDINGS_PATH', '')]
    if not all(values):
        raise ValidationError('EXECUTION_CONFIGURATION_REQUIRED')
    path, origin, token, principal, bindings = values
    RosyClient(origin, token)
    if len(principal) > 192 or not principal.strip() or any(ord(c) < 32 for c in principal):
        raise ValidationError('INVALID_FLEET_PRINCIPAL')
    evidence_sources = configured_sources()  # Raises on malformed or overlapping sets.
    return dict(path=Path(path).resolve(), origin=origin.rstrip('/'), token=token,
                principal=principal, bindings=bindings, evidence_sources=evidence_sources,
                ca_file=connection['ca_file'] or None)


def _identity(config):
    return hashlib.sha256(str(config['path']).casefold().encode()).hexdigest()


def _bindings(config):
    with open(config['bindings'], encoding='utf-8') as source:
        return load_bindings(json.load(source))


def _matching_spatial(plan, installed):
    """The one installed spatial binding that authorizes exactly this placement, or None.

    Design 29: the recommendation tuple, footprint revision, and cargo must all
    match a single installed binding; anything else keeps the execution gate.
    """
    recommendation = plan.result.get('recommendation')
    if not isinstance(recommendation, dict) or plan.result.get('status') != 'READY':
        return None
    matches = [binding for binding in installed
               if isinstance(binding, SpatialPlacementBinding)
               and binding.matches(recommendation, plan.result.get('footprint_revision'), plan.order.cargo.code)]
    return matches[0] if len(matches) == 1 else None


def _rearrangement(plan, installed):
    """Temporary simulation binding of an R2 plan: (moves, completing move), or None.

    The ledger completes on one cargo at its final place, so one cargo must move
    exactly once; its last destination stands in for the outbound stand.
    """
    templates = [b for b in installed if isinstance(b, SimulationCellTemplate)]
    if len(templates) != 1 or plan.result.get('status') != 'READY' or not plan.result.get('moves'):
        return None
    moves = templates[0].moves(plan.result['moves'])
    once = [m for m in moves if sum(o.cargo_id == m.cargo_id for o in moves) == 1]
    return (moves, once[-1]) if once else None


def _spatial_from_path(plan, path):
    if not path:
        return None
    try:
        with open(path, encoding='utf-8') as source:
            installed = load_bindings(json.load(source))
        if plan.result.get('plan_kind') == 'rearrangement':
            return _rearrangement(plan, installed)
        return _matching_spatial(plan, installed)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _spatial_binding(plan, config):
    return _spatial_from_path(plan, config['bindings'])


def _bound(plan, config):
    bindings = _bindings(config)
    if plan.result.get('plan_kind') == 'rearrangement':
        bound = _rearrangement(plan, bindings)
        if bound is None:
            raise ValueError('No simulation cell template binds this rearrangement')
        return bound[0]
    if plan.result.get('model') == 'grid-placement-v1':
        spatial = _matching_spatial(plan, bindings)
        if spatial is None:
            raise ValueError('No installed spatial binding matches this placement')
        return (spatial.move(),)
    proposal = OutboundProposal(plan.result['status'], plan.result['reason'], plan.result['source_revision'],
                               tuple(CargoMove(**move) for move in plan.result['moves']))
    return bind_plan(proposal, tuple(b for b in bindings
                                     if not isinstance(b, (SpatialPlacementBinding, SimulationCellTemplate))))
