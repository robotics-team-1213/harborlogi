from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('harbor_web', '0015_rearrange_authorization')]
    operations = [migrations.CreateModel(name='EvidenceProducerSetting', fields=[
        ('id', models.PositiveSmallIntegerField(default=1, editable=False, primary_key=True, serialize=False)),
        ('human_confirmation', models.BooleanField(default=False)),
        ('version', models.PositiveIntegerField(default=1)),
        ('updated_at', models.DateTimeField(auto_now=True)),
    ], options={'constraints': [models.CheckConstraint(condition=models.Q(id=1, version__gt=0), name='evidence_producer_singleton')]})]
