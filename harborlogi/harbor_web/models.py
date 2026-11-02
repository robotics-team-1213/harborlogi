import uuid
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


def clean_text(value):
    if not value or value != value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValidationError('앞뒤 공백이나 제어 문자 없이 값을 입력하세요.')


class Authored(models.Model):
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True

    def clean(self):
        super().clean()
        for field in self._meta.fields:
            if isinstance(field, models.CharField):
                value = getattr(self, field.name)
                if value:
                    clean_text(value)


class ImmutableQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError('Historical records cannot be edited.')

    def delete(self):
        raise ValidationError('Historical records cannot be deleted.')

    def bulk_update(self, *args, **kwargs):
        raise ValidationError('Historical records cannot be edited.')

    def bulk_create(self, objs, batch_size=None, ignore_conflicts=False, update_conflicts=False,
                    update_fields=None, unique_fields=None):
        if update_conflicts:
            raise ValidationError('Historical records cannot be edited.')
        return super().bulk_create(objs, batch_size=batch_size, ignore_conflicts=ignore_conflicts,
                                   update_conflicts=False, update_fields=update_fields, unique_fields=unique_fields)


class Immutable(Authored):
    objects = ImmutableQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, force_insert=False, force_update=False, using=None, update_fields=None):
        if not self._state.adding or force_update or update_fields is not None:
            raise ValidationError('Historical records cannot be edited.')
        # A fresh Python instance with an existing primary key must never become an UPDATE.
        return super().save(force_insert=True, using=using)

    def delete(self, *args, **kwargs):
        raise ValidationError('Historical records cannot be deleted.')


class Cargo(Immutable):
    code = models.CharField(max_length=96, unique=True)
    name = models.CharField(max_length=160)
    weight_kg = models.DecimalField(max_digits=12, decimal_places=3, validators=[MinValueValidator(Decimal('0.001'))])
    volume_m3 = models.DecimalField(max_digits=12, decimal_places=4, validators=[MinValueValidator(Decimal('0.0001'))])
    destination = models.CharField(max_length=160)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(weight_kg__gt=0, volume_m3__gt=0), name='cargo_positive_dimensions')]

    def __str__(self):
        return self.code


class CargoFootprint(Immutable):
    cargo = models.ForeignKey(Cargo, on_delete=models.PROTECT, related_name='footprints')
    length_cells = models.PositiveSmallIntegerField(choices=[(n, f'{n} × 1') for n in (1, 2, 3)])

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(length_cells__in=(1, 2, 3)), name='footprint_valid_length')]


class Condition(Immutable):
    name = models.CharField(max_length=120)
    calibration_ref = models.CharField(max_length=192)
    payload = models.JSONField()

    def __str__(self):
        return self.name


class Observation(Immutable):
    source_id = models.CharField(max_length=96)
    source_revision = models.CharField(max_length=96)
    observed_at = models.DateTimeField()
    provenance = models.CharField(max_length=12, choices=[('SYNTHETIC', 'Synthetic'), ('DECLARED', 'Declared')])
    payload = models.JSONField()
    condition = models.ForeignKey(Condition, on_delete=models.PROTECT)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['source_id', 'source_revision'], name='observation_source_revision_unique')]


class Order(Authored):
    auto_replan = models.BooleanField(default=False)
    planning_observation = models.ForeignKey(Observation, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    # Only a REARRANGE order has no single cargo (design 32).
    cargo = models.ForeignKey(Cargo, null=True, blank=True, on_delete=models.PROTECT)
    kind = models.CharField(max_length=9, choices=[('INBOUND', 'Inbound'), ('OUTBOUND', 'Outbound'), ('REARRANGE', 'Rearrange')])
    priority_group = models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)])
    sequence = models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)])
    tie_policy = models.CharField(max_length=5, default='FIXED', choices=[('FIXED', 'Fixed'), ('ANY', 'Any')])
    note = models.CharField(max_length=500, blank=True)
    version = models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)])
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(priority_group__gt=0, sequence__gt=0, version__gt=0), name='order_positive_versions'),
            models.CheckConstraint(condition=models.Q(cargo__isnull=False) | models.Q(kind='REARRANGE'), name='order_cargo_unless_rearrange'),
            models.UniqueConstraint(fields=['priority_group', 'sequence'], condition=models.Q(kind='OUTBOUND', tie_policy='FIXED'), name='fixed_order_sequence_unique'),
        ]


class RearrangeAuthorization(Authored):
    """Standing permission for automatic R2 rearrangement (design 32, ADR0017 decision 4).

    created_by is the enabling user. Values never change: a new setting is a new row and
    the old one is switched off. Only the disabled fields may be written, once.
    """
    k = models.PositiveSmallIntegerField(default=2, validators=[MinValueValidator(1), MaxValueValidator(10)])
    max_moves = models.PositiveSmallIntegerField(default=6, validators=[MinValueValidator(1), MaxValueValidator(12)])
    valid_until = models.DateTimeField()
    daily_limit = models.PositiveSmallIntegerField(default=20, validators=[MinValueValidator(1), MaxValueValidator(1000)])
    disabled_at = models.DateTimeField(null=True, blank=True)
    disabled_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='+')

    def save(self, *args, **kwargs):
        if self._state.adding:
            kwargs['force_insert'] = True
        else:
            old = type(self).objects.get(pk=self.pk)
            fixed = ('k', 'max_moves', 'valid_until', 'daily_limit', 'created_by_id', 'created_at')
            if old.disabled_at is not None or any(getattr(old, f) != getattr(self, f) for f in fixed):
                raise ValidationError('Historical records cannot be edited.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Historical records cannot be deleted.')


class Plan(Immutable):
    order = models.ForeignKey(Order, on_delete=models.PROTECT)
    order_version = models.PositiveIntegerField()
    observation = models.ForeignKey(Observation, on_delete=models.PROTECT)
    condition = models.ForeignKey(Condition, on_delete=models.PROTECT)
    queue_digest = models.CharField(max_length=64)
    result = models.JSONField()

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(order_version__gt=0), name='plan_positive_order_version')]


class Audit(models.Model):
    objects = ImmutableQuerySet.as_manager()
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    action = models.CharField(max_length=80)
    object_type = models.CharField(max_length=48)
    object_id = models.CharField(max_length=96)
    detail = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, force_insert=False, force_update=False, using=None, update_fields=None):
        if not self._state.adding or force_update or update_fields is not None:
            raise ValidationError('Historical records cannot be edited.')
        return super().save(force_insert=True, using=using)

    def delete(self, *args, **kwargs):
        raise ValidationError('Historical records cannot be deleted.')


class FieldEpisode(Authored):
    key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    order = models.ForeignKey(Order, on_delete=models.PROTECT)
    vehicle_id = models.CharField(max_length=96)
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    version = models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)])
    state = models.CharField(max_length=12, default='OPEN', choices=[(s, s) for s in ('OPEN', 'CONFIRMED', 'MISMATCH', 'UNABLE', 'REASSIGNED')])

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(version__gt=0), name='episode_positive_version')]


class FieldConfirmation(Immutable):
    episode = models.ForeignKey(FieldEpisode, on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    outcome = models.CharField(max_length=8, choices=[(s, s) for s in ('CLEAR', 'MISMATCH', 'UNABLE')])
    observed_at = models.DateTimeField()
    version = models.PositiveIntegerField(validators=[MinValueValidator(1)])

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['episode', 'version'], name='confirmation_episode_version_unique'),
            models.CheckConstraint(condition=models.Q(version__gt=0), name='confirmation_positive_version'),
        ]


class DepartureIntent(Immutable):
    episode = models.OneToOneField(FieldEpisode, on_delete=models.PROTECT)
    confirmation = models.OneToOneField(FieldConfirmation, on_delete=models.PROTECT)
    key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    vehicle_id = models.CharField(max_length=96)
    cargo = models.ForeignKey(Cargo, on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')


class Measurement(Immutable):
    label = models.CharField(max_length=120)
    scenario = models.CharField(max_length=96)
    conditions_ref = models.CharField(max_length=192)
    initial_layout_ref = models.CharField(max_length=192)
    information_ref = models.CharField(max_length=192)
    method = models.CharField(max_length=13, choices=[('BASELINE', 'Baseline'), ('COLLABORATIVE', 'Collaborative')])
    baseline_reach_verified = models.BooleanField(default=False)
    provenance = models.CharField(max_length=9, choices=[('SYNTHETIC', 'Synthetic'), ('MEASURED', 'Measured')])
    source_ref = models.CharField(max_length=240)
    vehicle_transfers = models.PositiveIntegerField(null=True, blank=True)
    human_interventions = models.PositiveIntegerField(null=True, blank=True)
    elapsed_seconds = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True, validators=[MinValueValidator(0)])

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(elapsed_seconds__gte=0) | models.Q(elapsed_seconds__isnull=True), name='measurement_nonnegative_elapsed')]


from harbor_web.command_models import Command, CommandFact

from harbor_web.runtime_models import DeploymentState, FleetConnection
