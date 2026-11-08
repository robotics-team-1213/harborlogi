from django.db import models


class FleetConnection(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    origin = models.CharField(max_length=500)
    principal = models.CharField(max_length=192)
    arm_console_enabled = models.BooleanField(default=False)
    version = models.PositiveIntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(id=1, version__gt=0), name='fleet_connection_singleton')]


class EvidenceProducerSetting(models.Model):
    """Admin-set switch: named field operators confirm completion evidence until an automatic producer exists (ADR0022)."""
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    human_confirmation = models.BooleanField(default=False)
    version = models.PositiveIntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(id=1, version__gt=0), name='evidence_producer_singleton')]


class DeploymentState(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    mode = models.CharField(max_length=11, choices=[(m, m) for m in ('development', 'test', 'live')])
    relocation = models.JSONField(default=dict, blank=True)
    restored_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(id=1), name='deployment_singleton')]
