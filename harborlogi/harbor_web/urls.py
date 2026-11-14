from django.contrib import admin
from django.contrib.auth import views
from django.urls import include, path
from django.views.generic import RedirectView
from django.templatetags.static import static
from harbor_web.auth import has_role
from harbor_web import rosy_account

# Django account management still uses its native model permissions.
admin.site.has_permission = lambda request: request.user.is_active and request.user.is_staff and has_role(request.user, 'admin')
admin.site.site_header = 'HarborLogi 계정 관리'
admin.site.site_title = 'HarborLogi 관리'
admin.site.index_title = '팀 계정과 역할'
urlpatterns = [
    path('favicon.ico', RedirectView.as_view(url=static('harbor-app-icon-20261023.png'), permanent=True)),
    path('accounts/login/', rosy_account.LoginView.as_view(), name='login'),
    path('accounts/rosy/', rosy_account.start, name='rosy_account_start'),
    path('accounts/rosy/callback/', rosy_account.callback, name='rosy_account_callback'),
    path('accounts/rosy/session/', rosy_account.session, name='rosy_account_session'),
    path('accounts/logout/', views.LogoutView.as_view(), name='logout'),
    path('accounts/password_change/', views.PasswordChangeView.as_view(), name='password_change'),
    path('accounts/password_change/done/', views.PasswordChangeDoneView.as_view(), name='password_change_done'),
    path('admin/', admin.site.urls),
    path('', include('harbor_web.urls_product')),
]
