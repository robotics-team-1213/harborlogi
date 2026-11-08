import uuid
from django.conf import settings
from django.db import models
from django.core.exceptions import ValidationError


# Design 28: one ACTIVE command per order; terminal commands remain as history.
ACTIVE_COMMAND_STATES = ('PREPARED', 'DISPATCHING', 'RECONCILED', 'UNKNOWN', 'CANCEL_PENDING')


class CommandQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if set(kwargs) - {'state', 'mission_id', 'reason', 'version', 'updated_at'}:
            raise ValidationError('Prepared command identity is immutable.')
        return super().update(**kwargs)

    def delete(self):
        raise ValidationError('Command history cannot be deleted.')


class Command(models.Model):
    objects = CommandQuerySet.as_manager()
    key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    plan = models.OneToOneField('Plan', on_delete=models.PROTECT)
    order = models.ForeignKey('Order', on_delete=models.PROTECT, related_name='commands')
    supersedes = models.ForeignKey('self', null=True, blank=True, on_delete=models.PROTECT,
                                   related_name='superseded_by', editable=False)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    plan_version = models.PositiveIntegerField()
    ledger_identity = models.CharField(max_length=256)
    fleet_origin = models.CharField(max_length=256)
    fleet_principal = models.CharField(max_length=192)
    body_digest = models.CharField(max_length=64)
    binding = models.JSONField()
    state = models.CharField(max_length=16, default='PREPARED', choices=[(s, s) for s in (
        'PREPARED', 'DISPATCHING', 'RECONCILED', 'UNKNOWN', 'BLOCKED', 'CANCEL_PENDING', 'CANCELLED', 'COMPLETED')])
    mission_id = models.CharField(max_length=192, null=True)
    reason = models.CharField(max_length=192, blank=True)
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        if self._state.adding:
            kwargs['force_insert'] = True
        else:
            old = type(self).objects.get(pk=self.pk)
            immutable = ('key', 'plan_id', 'order_id', 'supersedes_id', 'actor_id', 'plan_version',
                         'ledger_identity', 'fleet_origin', 'fleet_principal', 'body_digest', 'binding')
            if any(getattr(old, name) != getattr(self, name) for name in immutable):
                raise ValidationError('Prepared command identity is immutable.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Command history cannot be deleted.')

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(plan_version__gt=0, version__gt=0), name='command_positive_versions'),
            models.UniqueConstraint(fields=('order',), condition=models.Q(state__in=ACTIVE_COMMAND_STATES),
                                    name='one_active_command_per_order'),
        ]


from harbor_web.models import Immutable


class CommandFact(Immutable):
    command = models.ForeignKey(Command, on_delete=models.PROTECT)
    command_key = models.UUIDField()
    body_digest = models.CharField(max_length=64)
    fleet_origin = models.CharField(max_length=256)
    fleet_principal = models.CharField(max_length=192)
    job = models.JSONField()
    receipt = models.JSONField(null=True)
    read_at = models.DateTimeField()


class EvidenceSubmission(Immutable):
    """One submitted placement/retreat observation pair, original identity preserved.

    The raw payloads are stored exactly as submitted; the adapter revalidates and binds
    them to the current business execution at consumption time (design 24/27 P2).
    """
    command = models.ForeignKey(Command, on_delete=models.PROTECT, related_name='evidence_submissions')
    placement = models.JSONField()
    retreat = models.JSONField()
    received_at = models.DateTimeField(auto_now_add=True)



class HumanAttestation(Immutable):
    """A named field person's visual verdict on one business stage (ADR 0023).

    Not a measurement: it holds the ledger stage it was bound to (mission, attempt, step),
    the cargo and location the person saw, the arms they saw waiting outside the zone, and an
    optional note or photo reference. created_by/created_at are the attester and server time.
    """
    command = models.ForeignKey(Command, on_delete=models.PROTECT, related_name='human_attestations')
    phase = models.CharField(max_length=5, choices=[('step', 'step'), ('final', 'final')])
    step_index = models.PositiveIntegerField()
    mission_id = models.CharField(max_length=192)
    attempt_id = models.CharField(max_length=192)
    revision = models.PositiveIntegerField()
    cargo_id = models.CharField(max_length=96)
    location = models.CharField(max_length=96)
    arms = models.JSONField()
    note = models.CharField(max_length=300, blank=True)
    photo_ref = models.CharField(max_length=200, blank=True)
