"""Convert validated Fleet ARM, Cell and TRIP facts into Harbor execution snapshots."""
from dataclasses import dataclass
from urllib.error import HTTPError
from harbor.fleet_contract import validate_arm_job, validate_arm_receipt, validate_trip_job, validate_trip_proposal
from harbor.model import identifier


_PHASES = dict(PROPOSED='awaiting_approval', READY='awaiting_execution', RUNNING='executing',
               ACTION_SUCCEEDED='awaiting_physical_evidence', GOAL_CONFIRMED='physical_confirmed', HOLD='held')


def execution_phase(job):
    return _PHASES[job['status']]


def _bound_job(binding, request_key, job, *, mission_id, principal):
    job = dict(validate_arm_job(binding, job, request_key))
    if mission_id is not None and job['mission_id'] != mission_id:
        raise ValueError('Physical job identity changed')
    if principal is not None and job.get('proposal_principal_id') != principal:
        raise ValueError('Fleet proposal principal differs')
    return job


def validate_arm_execution(binding, request_key, job, receipt, *, mission_id=None, principal=None):
    job = _bound_job(binding, request_key, job, mission_id=mission_id, principal=principal)
    return _with_receipt(binding, job, receipt)


@dataclass(frozen=True)
class ExecutionSnapshot:
    mission_id: str | None  # None while a trip proposal awaits another operator's approval
    request_key: str
    phase: str
    job: dict
    attempt_id: str | None = None
    receipt: dict | None = None

    @property
    def goal_confirmed(self):
        return self.phase == 'physical_confirmed' and self.attempt_id is not None

    @property
    def held(self):
        return self.phase == 'held'


def prepare_execution(client, binding):
    from harbor.process import ProcessBinding
    if isinstance(binding, ProcessBinding):
        client.compile(binding)


def cancel_execution(client, mission_id, reason):
    return client.cancel_arm(mission_id, reason=reason)


def submit_execution(client, binding, request_key, *, principal=None):
    from harbor.process import ArmTransferBinding
    if isinstance(binding, ArmTransferBinding):
        result = _bound_job(binding, request_key, client.propose_arm(binding, request_key=request_key)['job'],
                            mission_id=None, principal=principal or getattr(client, 'expected_principal', None))
        return read_arm_execution(client, binding, request_key, mission_id=result['mission_id'], principal=principal)
    result = client.propose(binding, request_key=request_key)
    proposal, mission = result['proposal'], result['mission']
    mission_id = proposal['proposal_id']
    identifier(mission_id)
    if (proposal['state'] != 'RESOLVED' or proposal['request_key'] != request_key
            or mission['mission_id'] != mission_id
            or mission.get('process_artifact_digest') != binding.process_artifact_digest):
        raise ValueError('Proposal readback does not match the submitted stage')
    return ExecutionSnapshot(mission_id, request_key, 'accepted', dict(mission))


def read_cell_execution(client, mission_id, request_key):
    job = client.job(mission_id)['job']
    if job['mission_id'] != mission_id or len(job['steps']) != 1:
        raise ValueError('Fleet job does not match the bound single-transfer stage')
    attempt = job['steps'][0].get('attempt_id')
    if attempt is not None:
        identifier(attempt)
    return ExecutionSnapshot(mission_id, request_key, execution_phase(job), dict(job), attempt)


def read_trip_execution(client, intent):
    job = dict(validate_trip_job(intent, client.trip_request(intent.request_key)['job']))
    return ExecutionSnapshot(job['mission_id'], intent.request_key, execution_phase(job), job,
                             job['steps'][0].get('attempt_id'))


def submit_trip_execution(client, intent):
    result = client.trip(intent.candidates, intent.steps, request_key=intent.request_key)
    execution = read_trip_execution(client, intent)
    if result['job']['mission_id'] != execution.mission_id:
        raise ValueError('Transport submission and readback identify different executions')
    return execution


def _with_receipt(binding, job, receipt):
    if receipt is not None:
        receipt = dict(validate_arm_receipt(binding, job, receipt))
    elif job['status'] == 'GOAL_CONFIRMED':
        raise ValueError('A confirmed physical job requires its receipt')
    return ExecutionSnapshot(job['mission_id'], job['request_key'], execution_phase(job), job,
                             receipt['attempt_id'] if receipt is not None else None, receipt)


def read_arm_execution(client, binding, request_key, *, mission_id=None, principal=None):
    response = client.arm_request(request_key)
    # Bind identity before requesting a receipt from the returned mission.
    principal = principal if principal is not None else getattr(client, 'expected_principal', None)
    job = _bound_job(binding, request_key, response['job'], mission_id=mission_id, principal=principal)
    receipt = (client.physical_receipt(job['mission_id'])['receipt']
               if job['status'] == 'GOAL_CONFIRMED' else None)
    return _with_receipt(binding, job, receipt)


def arm_execution_view(client, binding, request_key, mission_id):
    """Read-only projection keeps valid job facts when receipt verification fails."""
    receipt_view = {'status': 'NOT_VERIFIED'}
    try:
        job = binding.check(client.arm_job(mission_id)['job'],
                            request_key)
        if job['mission_id'] != mission_id:
            raise ValueError('Different mission')
        # Explicit projection: upstream extensions must not expose credentials or raw internals.
        fleet = {field: job.get(field) for field in
                           ('status', 'mission_id', 'proposal_digest', 'approved_by',
                            'authority_epoch', 'dispatch_generation')}
        if job['status'] == 'GOAL_CONFIRMED':
            try:
                receipt = binding.check_receipt(job, client.physical_receipt(mission_id)['receipt'])
                receipt_view = {'status': 'VERIFIED', **{field: receipt[field] for field in
                    ('receipt_id', 'action_id', 'attempt_id', 'physical_status', 'validated_at_ns')}}
            except (KeyError, ValueError, TypeError):
                receipt_view = {'status': 'MISMATCH'}
            except (OSError, HTTPError):
                receipt_view = {'status': 'UNAVAILABLE'}
    except (ValueError, KeyError, TypeError, IndexError):
        fleet = {'status': 'MISMATCH'}
    except HTTPError as exc:
        fleet = {'status': 'FORBIDDEN' if exc.code in (401, 403) else 'UNAVAILABLE'}
    except OSError:
        fleet = {'status': 'UNAVAILABLE'}
    return fleet, receipt_view


def read_trip_proposal_execution(client, intent):
    """Proposal path (D-908): PROPOSED is a waiting snapshot; ADMITTED must be the same job Fleet created."""
    proposal = dict(validate_trip_proposal(intent, client.trip_proposal_request(intent.request_key)['proposal'],
                                           getattr(client, 'expected_principal', None)))
    if proposal['mission_id'] is None:
        return ExecutionSnapshot(None, intent.request_key, execution_phase(proposal), proposal)
    execution = read_trip_execution(client, intent)
    if execution.mission_id != proposal['mission_id']:
        raise ValueError('Admitted proposal and trip job identify different executions')
    return execution


def submit_trip_proposal_execution(client, intent):
    sent = client.trip_propose(intent.candidates, intent.steps, request_key=intent.request_key,
                               object_ref=intent.object_ref or None)['proposal']
    validate_trip_proposal(intent, sent, getattr(client, 'expected_principal', None))
    return read_trip_proposal_execution(client, intent)
