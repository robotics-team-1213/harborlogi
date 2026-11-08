"""Shared rules for the service modules: role checks, audited saves, domain-error mapping."""

from django.core.exceptions import PermissionDenied, ValidationError

from harbor_web.auth import has_role
from harbor_web.models import Audit


class Conflict(Exception):
    pass


def _role(actor, *roles):
    if not has_role(actor, *roles):
        raise PermissionDenied


def _save(actor, obj, action):
    obj.full_clean()
    obj.save()
    Audit.objects.create(actor=actor, action=action, object_type=type(obj).__name__, object_id=str(obj.pk))
    return obj


def _domain(call):
    try:
        return call()
    except (ValueError, TypeError, KeyError, OverflowError) as error:
        raise ValidationError('Invalid structured input: ' + str(error)) from error
