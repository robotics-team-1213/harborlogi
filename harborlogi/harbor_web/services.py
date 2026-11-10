"""Persist declared inputs and planning results; never issue physical commands."""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction

from harbor_web.coordination import serialized
from harbor_web.models import Cargo, CargoFootprint, Condition, Observation, Measurement
# Callers keep using `services.<name>`; new code imports from the owning module (one file per responsibility).
from harbor_web.service_common import Conflict, _domain, _role, _save  # noqa: F401
from harbor_web.site_config import (area_presentation, constraints, map_areas, snapshot,  # noqa: F401
                                    spatial_policy, spatial_snapshot)
from harbor_web.order_queue import (TIERS, apply_tier_order, back_of_tier, create_order, move_outbound_to_front, register_order,  # noqa: F401
                                    outbound_lanes, queue_digest, queue_snapshot, reorder_outbound, request_snapshot,
                                    tier_of, update_order)
from harbor_web.plan_service import _calculate_plan, _outbound_schedule, make_plan, placement_projection  # noqa: F401
from harbor_web.field_episodes import assign_episode, confirm_episode, prepare_departure, reassign_episode  # noqa: F401


def _provenance(provenance):
    if provenance == 'SYNTHETIC' and settings.MODE == 'live':
        raise ValidationError('Synthetic records are forbidden in live mode.')


@serialized
@transaction.atomic
def register_cargo(actor, length_cells=1, **validated_fields):
    _role(actor, 'operator')
    cargo = _save(actor, Cargo(created_by=actor, **validated_fields), 'cargo.register')
    register_footprint(actor, cargo_id=cargo.pk, length_cells=length_cells)
    return cargo


@serialized
@transaction.atomic
def register_footprint(actor, *, cargo_id, length_cells):
    _role(actor, 'operator')
    if type(length_cells) is not int or length_cells not in (1, 2, 3):
        raise ValidationError('Footprint length must be 1, 2 or 3 cells.')
    obj = _save(actor, CargoFootprint(created_by=actor, cargo=Cargo.objects.get(pk=cargo_id),
        length_cells=length_cells), 'footprint.register')
    from harbor_web.autoplanning import replan_enabled
    replan_enabled(actor, 'footprint.register')
    return obj


@transaction.atomic
def register_condition(actor, *, name, calibration_ref, payload):
    _role(actor, 'admin')
    constraints(payload)
    return _save(actor, Condition(created_by=actor, name=name, calibration_ref=calibration_ref, payload=payload), 'condition.register')


@serialized
@transaction.atomic
def record_observation(actor, *, source_id, source_revision, observed_at, provenance, payload, condition):
    _role(actor, 'admin')
    _provenance(provenance)
    obj = Observation(created_by=actor, source_id=source_id, source_revision=source_revision,
                      observed_at=observed_at, provenance=provenance, payload=payload, condition=condition)
    snapshot(obj)
    existing = Observation.objects.filter(source_id=source_id, source_revision=source_revision).first()
    if existing:
        if any(getattr(existing, key) != getattr(obj, key) for key in ('observed_at', 'provenance', 'payload', 'condition_id')):
            raise Conflict('Source revision already has different content.')
        return existing
    obj = _save(actor, obj, 'observation.record')
    from harbor_web.autoplanning import replan_enabled
    replan_enabled(actor, 'observation.record', source_id=source_id)
    return obj


@transaction.atomic
def record_measurement(actor, **fields):
    _role(actor, 'operator')
    _provenance(fields.get('provenance'))
    return _save(actor, Measurement(created_by=actor, **fields), 'measurement.record')


def compare_measurements(left, right):
    keys = ('scenario', 'conditions_ref', 'initial_layout_ref', 'information_ref', 'provenance')
    reason = 'MATCHED_CONDITIONS'
    if any(getattr(left, key) != getattr(right, key) for key in keys):
        reason = 'CONDITIONS_DIFFER'
    elif not left.baseline_reach_verified or not right.baseline_reach_verified:
        reason = 'BASELINE_REACH_UNVERIFIED'
    elif left.method == right.method:
        reason = 'METHODS_MUST_DIFFER'
    comparable = reason == 'MATCHED_CONDITIONS'
    def row(name):
        a, b = getattr(left, name), getattr(right, name)
        return {'name': name, 'left': a, 'right': b, 'delta': b - a if comparable and a is not None and b is not None else None}
    return {'comparable': comparable, 'reason': reason, 'primary': [row('vehicle_transfers'), row('human_interventions')],
            'secondary': [row('elapsed_seconds')]}
