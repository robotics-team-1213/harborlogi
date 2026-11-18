"""Abstract bases shared by models and command_models; a leaf module so neither imports the other."""
from django.conf import settings
from django.core.exceptions import ValidationError
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
