"""Outbound order queue: create, reorder, and digest the queue that plans are computed against."""
import hashlib
import json

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from harbor_web.coordination import serialized
from harbor_web.models import Audit, Command, Order
from harbor_web.service_common import Conflict, _role


def _queue_policy(order):
    if order.kind != 'OUTBOUND':
        return
    peers = Order.objects.filter(kind='OUTBOUND', priority_group=order.priority_group).exclude(pk=order.pk)
    if peers.exclude(tie_policy=order.tie_policy).exists():
        raise ValidationError('All members of a priority group must use the same policy.')
    if order.tie_policy == 'FIXED' and peers.filter(sequence=order.sequence).exists():
        raise ValidationError('Fixed group sequence must be unique.')


def request_snapshot(order):
    return dict(id=order.pk, cargo_id=order.cargo_id, cargo_code=order.cargo.code if order.cargo_id else None, kind=order.kind,
        priority_group=order.priority_group, sequence=order.sequence, tie_policy=order.tie_policy,
        title=order.title, note=order.note, version=order.version)


def queue_snapshot():
    return [request_snapshot(order) for order in Order.objects.exclude(kind='REARRANGE').select_related('cargo').order_by('pk')]


@serialized
@transaction.atomic
def create_order(actor, *, cargo, kind, priority_group, sequence, tie_policy, note='', title=''):
    _role(actor, 'operator')
    obj = Order(created_by=actor, cargo=cargo, kind=kind, priority_group=priority_group,
                sequence=sequence, tie_policy=tie_policy, note=note, title=title)
    _queue_policy(obj)
    obj.full_clean()
    obj.save()
    Audit.objects.create(actor=actor, action='order.create', object_type='Order', object_id=str(obj.pk),
                         detail={'before': None, 'after': request_snapshot(obj)})
    from harbor_web.autoplanning import replan_enabled
    replan_enabled(actor, 'order.create')
    return obj


@serialized
@transaction.atomic
def update_order(actor, *, order_id, expected_version, priority_group, sequence, tie_policy, note='', title=None):
    _role(actor, 'operator')
    obj = Order.objects.get(pk=order_id)
    if obj.version != expected_version:
        raise Conflict('Order changed; reload before editing.')
    before = request_snapshot(obj)
    for key, value in dict(priority_group=priority_group, sequence=sequence, tie_policy=tie_policy, note=note).items():
        setattr(obj, key, value)
    if title is not None:  # None keeps the current title for callers that predate titles
        obj.title = title
    obj.version += 1
    obj.full_clean()
    _queue_policy(obj)
    updated = Order.objects.filter(pk=order_id, version=expected_version).update(
        priority_group=obj.priority_group, sequence=obj.sequence, tie_policy=obj.tie_policy,
        title=obj.title, note=obj.note, version=obj.version, updated_at=timezone.now())
    if not updated:
        raise Conflict('Order changed; reload before editing.')
    Audit.objects.create(actor=actor, action='order.update', object_type='Order', object_id=str(obj.pk),
                         detail={'before': before, 'after': request_snapshot(obj)})
    obj.refresh_from_db()
    from harbor_web.autoplanning import replan_enabled
    replan_enabled(actor, 'order.update')
    return obj


def outbound_lanes():
    """Open outbound orders as ordered lanes: one lane per priority group, sequence order inside."""
    lanes = []
    for order in open_outbound_orders().select_related('cargo').order_by('priority_group', 'sequence', 'pk'):
        if not lanes or lanes[-1]['group'] != order.priority_group:
            lanes.append(dict(group=order.priority_group, fixed=order.tie_policy == 'FIXED', orders=[]))
        lanes[-1]['orders'].append(order)
    return lanes


def next_outbound_group():
    """The group a new outbound order takes to go to the back of the queue."""
    return (Order.objects.filter(kind='OUTBOUND').aggregate(top=Max('priority_group'))['top'] or 0) + 1


def _kept_lane_targets(lanes, current):
    """Today's group numbers when lanes still hold today's groups in order; None when they must be renumbered."""
    groups = {}
    for order in sorted(current.values(), key=lambda item: (item.priority_group, item.sequence, item.pk)):
        groups.setdefault(order.priority_group, []).append(order)
    if [{order_id for order_id, _ in lane['orders']} for lane in lanes] != [{order.pk for order in members} for members in groups.values()]:
        return None
    target = {}
    for lane, (group, members) in zip(lanes, groups.items()):
        policy = 'FIXED' if lane['fixed'] else 'ANY'
        closed = Order.objects.filter(kind='OUTBOUND', priority_group=group).exclude(pk__in=current)
        if closed.exclude(tie_policy=policy).exists():
            return None  # a group holds one policy, and a finished order's cannot change
        wanted = [order_id for order_id, _ in lane['orders']]
        sequences = [order.sequence for order in members]
        if wanted != [order.pk for order in members] or (policy == 'FIXED' and len(set(sequences)) != len(sequences)):
            base = closed.aggregate(top=Max('sequence'))['top'] or 0
            sequences = [base + position + 1 for position in range(len(wanted))]
        target.update((order_id, (group, sequence, policy)) for order_id, sequence in zip(wanted, sequences))
    return target


@serialized
@transaction.atomic
def reorder_outbound(actor, *, lanes):
    """Apply ordered lanes to every open outbound order in one revision step.

    ``lanes`` is ``[dict(fixed=bool, orders=[(order_id, expected_version), ...]), ...]`` and must list each
    open outbound order exactly once. Lanes that still hold today's groups in order keep their group numbers;
    otherwise lane i becomes group ``closed_top + i + 1`` so a finished order's slot is never reused. Only
    orders whose group, sequence or tie policy changes get a new revision and an audit row.
    """
    _role(actor, 'operator')
    entries = [(order_id, version) for lane in lanes for order_id, version in lane['orders']]
    if not lanes or any(not lane['orders'] for lane in lanes):
        raise Conflict('Every lane must hold at least one request.')
    ids = [order_id for order_id, _ in entries]
    current = {order.pk: order for order in open_outbound_orders().select_related('cargo').select_for_update()}
    if len(set(ids)) != len(ids) or set(ids) != set(current):
        raise Conflict('The outbound queue changed; reload before reordering.')
    if any(current[order_id].version != version for order_id, version in entries):
        raise Conflict('Order changed; reload before reordering.')
    closed_top = Order.objects.filter(kind='OUTBOUND').exclude(pk__in=current).aggregate(top=Max('priority_group'))['top'] or 0
    target = _kept_lane_targets(lanes, current)
    if target is None:
        target = {}
        for lane_index, lane in enumerate(lanes):
            policy = 'FIXED' if lane['fixed'] else 'ANY'
            for position, (order_id, _) in enumerate(lane['orders']):
                target[order_id] = (closed_top + lane_index + 1, position + 1, policy)
    changed = [order for order_id, order in current.items()
               if (order.priority_group, order.sequence, order.tie_policy) != target[order_id]]
    if not changed:
        return []
    # Park changed orders above every live group first: the fixed-sequence constraint is checked per row.
    park = closed_top + len(lanes) + len(current) + 1
    for offset, order in enumerate(changed):
        Order.objects.filter(pk=order.pk).update(priority_group=park + offset, sequence=1, tie_policy='FIXED')
    befores = {order.pk: request_snapshot(order) for order in changed}
    now = timezone.now()
    for order in changed:
        order.priority_group, order.sequence, order.tie_policy = target[order.pk]
        order.version += 1
        order.full_clean()
        if not Order.objects.filter(pk=order.pk, version=order.version - 1).update(priority_group=order.priority_group,
                sequence=order.sequence, tie_policy=order.tie_policy, version=order.version, updated_at=now):
            raise Conflict('Order changed; reload before reordering.')
    for order in changed:
        _queue_policy(order)
        Audit.objects.create(actor=actor, action='order.update', object_type='Order', object_id=str(order.pk),
                             detail={'before': befores[order.pk], 'after': request_snapshot(order)})
    from harbor_web.autoplanning import replan_enabled
    replan_enabled(actor, 'order.update')
    return changed


def move_outbound_to_front(actor, order):
    """Put one open outbound order in its own first lane; the rest keep their lanes, one step back."""
    lanes = [dict(fixed=True, orders=[(order.pk, order.version)])]
    for lane in outbound_lanes():
        members = [(item.pk, item.version) for item in lane['orders'] if item.pk != order.pk]
        if members:
            lanes.append(dict(fixed=lane['fixed'], orders=members))
    return reorder_outbound(actor, lanes=lanes)


def completed_order_ids():
    """Orders have no close state: a ledger-completed Command is what closes one."""
    return Command.objects.filter(state='COMPLETED').values('order_id')


def open_outbound_orders():
    return Order.objects.filter(kind='OUTBOUND').exclude(pk__in=completed_order_ids())


def queue_digest():
    # REARRANGE orders are not queue entries: they must not invalidate other plans.
    rows = list(Order.objects.exclude(kind='REARRANGE').order_by('pk').values('id', 'cargo_id', 'kind', 'priority_group', 'sequence', 'tie_policy', 'note', 'version'))
    closed = sorted(set(Command.objects.filter(state='COMPLETED').exclude(order__kind='REARRANGE')
                        .values_list('order_id', flat=True)))
    # A completion leaves the queue, so plans made before it must be reviewed; the digest is unchanged until one exists.
    payload = dict(rows=rows, closed=closed) if closed else rows
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
