"""Rearrangement after the inbound scenario (KAN-119): no parameter entry, a preview once inbound is decided.

While inbound requests wait for a decision the page says the scenario is running. Once every waiting inbound is
decided, it previews R2 on the observed yard plus the decided placements, ranked by the outbound order. A plan is
created only from an observation (ADR 0017); an automatic authorization that is still on can only be switched off.
"""
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import redirect
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from harbor_web import rearrangement, services
from harbor_web.auth import has_role, require_roles
from harbor_web.inbound_decision import inbound_progress, projected_snapshot
from harbor_web.models import Audit, Observation, Plan, RearrangeAuthorization
from harbor_web.views import page
from harbor_web.yard_rows import place_context, place_label

PREVIEW_K, PREVIEW_MAX_MOVES = 2, 6  # the defaults of a requested rearrangement (rearrangement.request_rearrangement)


def _preview(observation, progress, now):
    if observation is None or observation.payload.get('schema') != 'grid-placement-v1':
        return None
    from harbor.spatial import plan_rearrangement
    snapshot = services.spatial_snapshot(observation)
    projected = projected_snapshot(snapshot, progress)
    try:
        proposal = plan_rearrangement(projected, services.spatial_policy(observation.condition.payload), services._outbound_schedule(),
            now=now, expected_revision=projected.revision, k=PREVIEW_K, max_moves=PREVIEW_MAX_MOVES,
            outbound_pending=False, requested=True)  # a preview ranks by the outbound order even while outbound waits
    except ValueError:
        return None
    labels, slot = place_context(observation.condition.payload)
    return dict(proposal=proposal, record=rearrangement.plan_record(proposal, {}), observation=observation,
                projected=len(projected.placements) != len(snapshot.placements),
                moves=[dict(number=i, cargo=m.cargo_id, source=place_label(vars(m.source_placement), labels, slot),
                            destination=place_label(vars(m.destination_placement), labels, slot)) for i, m in enumerate(proposal.moves, start=1)])


@require_roles('viewer', 'operator', 'admin')
@require_http_methods(['GET', 'POST'])
def rearrangement_page(request):
    progress = inbound_progress()
    observation = Observation.objects.select_related('condition').order_by('-pk').first()
    if request.method == 'POST':
        if not has_role(request.user, 'operator'):
            raise PermissionDenied
        action = request.POST.get('action')
        try:
            if action == 'disable':
                rearrangement.disable_authorization(request.user)
                messages.success(request, '자동 재배치를 껐습니다. 진행 중인 이동은 마칩니다.')
            elif action == 'request':
                if observation is None:
                    raise ValidationError('관측이 없습니다.')
                if progress['waiting']:
                    raise ValidationError('입고 결정이 남아 있습니다. 입고 시나리오가 끝난 뒤 재배치합니다.')
                plan = rearrangement.request_rearrangement(request.user, observation_id=observation.pk)
                return redirect(f'/plans/{plan.pk}/')
            else:
                raise ValidationError('지원하지 않는 요청입니다.')
            return redirect('/rearrangement/')
        except (ValidationError, services.Conflict) as error:
            for message in getattr(error, 'messages', [str(error)]):
                messages.error(request, message)
            return redirect('/rearrangement/')
    preview = None if progress['waiting'] else _preview(observation, progress, timezone.now())
    plans = Plan.objects.filter(order__kind='REARRANGE').select_related('order').order_by('-pk')[:25]
    rows = [dict(plan=plan, record=plan.result.get('rearrangement', {}), command=getattr(plan, 'command', None)) for plan in plans]
    return page(request, 'rearrangement.html', section='rearrangement', rows=rows, progress=progress, preview=preview,
        outbound_pending=rearrangement.outbound_pending(), authorization=rearrangement.current_authorization(),
        history=RearrangeAuthorization.objects.order_by('-pk')[:10],
        holds=Audit.objects.filter(action=rearrangement.HOLD_ACTION).order_by('-pk')[:10])
