"""Inbound trips and ROSY port events for the executor (ADR 0029); nothing here admits or moves a device.

`prepare_departure` saves a PLANNED trip that declares the departing cargo code (`object_ref`) and
adds the cargo to the intake manifest. The executor proposes that trip once (a different named
operator admits it in ROSY), records TRIP_ARRIVED when Fleet confirms the trip, and reads R's port
events into the intake ledger. Both are off until HARBOR_TRIP_DESTINATION names the intake place.
"""
import os

from harbor.port_events import RANK, IntakeLedger
from harbor.transport import TransportLedger, TripIntent


def trip_key(departure):
    return 'harbor-trip:' + str(departure.key)


def _destination():
    return os.environ.get('HARBOR_TRIP_DESTINATION', '')


def prepare_trip(departure, *, now):
    """Declare the departing cargo on a PLANNED trip; None while trips are not configured."""
    path = os.environ.get('HARBOR_LEDGER_PATH', '')
    if not path or not _destination():
        return None
    intent = TripIntent(trip_key(departure), (departure.vehicle_id,), _destination(),
                        object_ref=departure.cargo.code)
    TransportLedger(path).prepare(intent)
    IntakeLedger(path).expect(intent.object_ref, now=now)
    return intent


def tick(registry, *, now):
    """Propose planned trips, mark arrivals, then read one page of port events. Returns the poll result."""
    if not _destination():
        return None
    from harbor_web.models import DepartureIntent
    path, client = registry.config['path'], registry.client
    transport, intake = TransportLedger(path), IntakeLedger(path)
    # shortcut: scans every departure each tick and skips arrived ones; add an index when departures grow large.
    for departure in DepartureIntent.objects.select_related('cargo').order_by('pk'):
        key, cargo = trip_key(departure), departure.cargo.code
        try:
            row = transport.read(key)
            if RANK.get(intake.read(cargo)['state'], len(RANK)) >= RANK['TRIP_ARRIVED']:
                continue  # arrived, further along, or HOLD
            if row['state'] == 'PLANNED':
                transport.propose(key, client)
            elif row['state'] != 'SUBMITTING':
                job = transport.reconcile_proposal(key, client)
                if job.get('status') == 'GOAL_CONFIRMED':
                    intake.arrive(cargo, now=now)
        except Exception:
            continue  # One trip's failure never blocks the others; the original key is reconciled next tick.
    intake.expire(now=now, connection=registry.config['origin'])  # An R that answered before, then went silent.
    return intake.poll(client, connection=registry.config['origin'], now=now)
