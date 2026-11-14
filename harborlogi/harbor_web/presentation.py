from django.conf import settings
from harbor_web import access
from harbor_web.auth import roles


def context(request):
    groups = roles(request.user)
    return dict(app_roles=sorted(groups), can_operate='operator' in groups,
                can_admin='admin' in groups, can_field='field' in groups,
                can_read=bool(groups & {'viewer', 'operator', 'admin'}),
                web_mode=settings.MODE, nav_groups=access.nav_groups(groups),
                persona_names=access.persona_names(groups), menu_pages=access.MENU_PAGES)
