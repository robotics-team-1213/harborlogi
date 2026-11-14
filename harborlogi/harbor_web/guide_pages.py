"""Role-scoped operating guidance; reading guidance never probes or controls Fleet."""
from harbor_web.auth import require_roles
from harbor_web.views import page
from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import redirect
from django.views.decorators.http import require_http_methods


class ConnectionVersionForm(forms.Form):
    expected_version = forms.IntegerField(min_value=0, widget=forms.HiddenInput)


class ConnectionForm(ConnectionVersionForm):
    origin = forms.ChoiceField(label='연결할 Fleet 주소')
    principal = forms.CharField(label='Fleet 서비스 신원', max_length=192,
        help_text='Fleet에서 발급한 토큰의 principal ID와 정확히 일치해야 합니다.')
    arm_console_enabled = forms.BooleanField(label='이 Fleet에 ARM 승인 화면을 설치했습니다', required=False,
        help_text='화면 설치 선언입니다. API 연결 검사와 실행 승인을 대신하지 않습니다.')

    def __init__(self, *args, origins, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['origin'].choices = [(value, value) for value in origins]


CONNECTION_REASONS = {
    'FLEET_SITE_CA_REQUIRED': '사이트 CA를 Harbor 서버에 설치하고 HARBOR_FLEET_CA_FILE을 설정하세요.',
    'FLEET_SITE_CA_UNREADABLE': 'Harbor 서버가 사이트 CA 파일을 읽을 수 없습니다. 설치 담당자가 파일 경로와 읽기 권한을 확인해야 합니다.',
    'FLEET_SITE_CA_INVALID': '사이트 CA 파일을 인증서로 읽을 수 없습니다. 설치 담당자가 올바른 PEM 사이트 CA를 설치해야 합니다.',
    'FLEET_NOT_DISCOVERED': '승인된 Fleet이 이 LAN에서 보이지 않습니다. Harbor 서버와 Fleet의 LAN·광고 서비스를 확인하세요. VPN이나 다른 서브넷으로 접속할 수 있어도 mDNS는 전달되지 않을 수 있습니다.',
    'ARM_API_ADVERTISED': '공개 작업 API 경로 있음 · 서비스 신원과 실제 조회·설치 조건은 별도 확인이 필요합니다.',
    'ARM_API_NOT_ADVERTISED': '작업 API 미공개 · 공개 API 문서에서 ARM 작업 경로를 찾지 못했습니다. ROSY 설치 담당자가 호환되는 서명 릴리스·운영 설정을 확인하고 실제 조회도 별도로 검증해야 합니다.',
    'PUBLIC_API_UNCONFIRMED': '공개 작업 API 확인 전 · API 문서를 읽을 수 없거나 형식이 달라 확인하지 못했습니다. 서비스 자격을 통한 실제 조회 확인이 필요합니다.',
    'FLEET_DISCOVERY_CONFLICT': '같은 사이트 이름에 여러 주소 또는 포트가 광고됩니다. 중복 광고를 정리한 뒤 다시 찾으세요.',
    'FLEET_DISCOVERY_PORT_MISMATCH': '발견한 Fleet 포트가 승인된 설치와 다릅니다. 설치 설정을 확인하세요.',
    'FLEET_DISCOVERY_CONFIGURATION': '자동 찾기는 승인된 HTTPS .local 사이트 이름을 사용합니다. 설치 설정을 확인하세요.',
    'FLEET_DISCOVERY_TIMEOUT': '사이트 자동 찾기 시간이 지났습니다. 네트워크와 광고 서비스를 확인하세요.',
    'FLEET_DISCOVERY_UNAVAILABLE': '사이트 자동 찾기를 시작하지 못했습니다. 서버의 LAN 접근과 Harbor web 설치를 확인하세요.',
    'FLEET_HEALTH_UNCONFIRMED': '사이트 인증 후 Fleet 상태가 정상인지 확인하지 못했습니다. Fleet 상태를 확인하세요.',
    'READ_API_VERIFIED': '서비스 신원과 ARM 조회 API를 확인했습니다. 제출 권한·설치 대응·실물 준비는 별도로 검증해야 합니다.',
    'PRINCIPAL_MISMATCH': '토큰의 신원이 설정과 다릅니다. 배포 자격과 서비스 신원을 확인하세요.',
    'ROLE_NOT_SUITABLE': '조회된 역할로는 작업 제안용 연결을 확인할 수 없습니다.',
    'AUTHENTICATION_FAILED': 'Fleet이 인증을 거절했습니다. 서버에 설치한 서비스 토큰을 확인하세요.',
    'AUTHORIZATION_FAILED': 'Fleet이 조회 권한을 거절했습니다.',
    'ARM_NOT_INSTALLED': 'Fleet의 ARM 실행 설치가 준비되지 않았습니다.',
    'ARM_API_UNCONFIRMED': '이 Fleet에서 호환되는 ARM 조회 API를 확인하지 못했습니다.',
    'SESSION_API_UNCONFIRMED': 'Fleet 세션 조회 API를 확인하지 못했습니다.',
    'SESSION_SCHEMA_UNCONFIRMED': 'Fleet 신원 응답 형식이 계약과 다릅니다.',
    'FLEET_UNREACHABLE': 'Fleet 응답을 받지 못했습니다. 연결 주소·네트워크를 확인하세요.',
    'FLEET_DNS_UNRESOLVED': '이 Harbor 서버에서 Fleet 이름을 찾지 못했습니다. 서버의 DNS 설정을 확인하세요.',
    'FLEET_TLS_UNTRUSTED': 'Fleet 인증서를 신뢰할 수 없어 연결을 중단했습니다. 서버 이름과 인증서 신뢰 설정을 확인하세요.',
    'FLEET_TLS_FAILED': 'Fleet과 보안 연결을 맺지 못했습니다. HTTPS와 인증서 구성을 확인하세요.',
    'FLEET_TIMEOUT': 'Fleet 응답 대기 시간이 지났습니다. 서버 상태와 네트워크를 확인한 뒤 다시 검사하세요.',
    'INVALID_FLEET_RESPONSE': 'Fleet 응답을 검증할 수 없습니다.',
    'CONNECTION_INCOMPLETE': '연결 주소·서비스 신원·서버 토큰이 필요합니다.',
    'ORIGIN_NOT_ALLOWED': '서버가 허용한 Fleet 주소만 사용할 수 있습니다.',
    'CONNECTION_CHANGED': '연결 설정이 변경됐습니다. 새로고침 후 확인하세요.',
    'DEPLOYMENT_MANAGED_CONNECTION': '배포 환경에서 관리하는 연결입니다. 서버 설정에서 변경해야 합니다.',
    'EXISTING_EXECUTION_IDENTITY_LOCKED': '원래 실행이 있어 연결 신원을 바꿀 수 없습니다. 원래 요청 대조를 유지하세요.',
    'INVALID_FLEET_PRINCIPAL': '서비스 신원의 길이와 문자를 확인하세요.',
}


def connection_guidance(config, check, *, token_present):
    from harbor_web.pipeline import fleet_links
    detail = check.detail if check else {}
    configured = bool(fleet_links(config['origin'])['console'] and config['principal'] and config['allowed'] and token_present)
    steps = [dict(label='연결 설정', state='준비됨' if configured else '설정 필요'),
             dict(label='서비스 신원', state='확인됨' if detail.get('session') == 'MATCHED' else
                  '확인 실패' if check and detail.get('reason') not in {'CONNECTION_INCOMPLETE', 'ORIGIN_NOT_ALLOWED'} else '확인 전'),
             dict(label='작업 조회 API', state='확인됨' if detail.get('arm') == 'READABLE' else
                  '설치 필요' if detail.get('reason') == 'ARM_NOT_INSTALLED' else
                  '조회 실패' if detail.get('session') == 'MATCHED' else '확인 전')]
    reason = detail.get('reason', 'NOT_CHECKED' if configured else 'CONNECTION_INCOMPLETE')
    actions = {
        'CONNECTION_INCOMPLETE': ('Harbor 설치 담당자', '연결 주소·서비스 신원·서버 토큰을 등록한 뒤 연결을 확인하세요.'),
        'ORIGIN_NOT_ALLOWED': ('Harbor 설치 담당자', '승인된 Fleet 주소를 서버 허용 목록에 등록하세요.'),
        'FLEET_DNS_UNRESOLVED': ('Harbor 설치 담당자', 'Harbor가 실행되는 PC나 서버에서 Fleet 이름이 해석되는지 확인한 뒤 다시 검사하세요.'),
        'FLEET_TLS_UNTRUSTED': ('Harbor·ROSY 설치 담당자', '서버 이름과 인증서의 유효성·신뢰 체인을 확인한 뒤 다시 검사하세요.'),
        'FLEET_TLS_FAILED': ('Harbor·ROSY 설치 담당자', '양쪽 서버의 HTTPS 구성을 확인한 뒤 다시 검사하세요.'),
        'FLEET_TIMEOUT': ('ROSY 설치 담당자', 'Fleet 상태와 네트워크 응답을 확인한 뒤 다시 검사하세요.'),
        'FLEET_UNREACHABLE': ('Harbor·ROSY 설치 담당자', '연결 주소와 네트워크 상태를 확인한 뒤 다시 검사하세요.'),
        'AUTHENTICATION_FAILED': ('Harbor 설치 담당자', '서버에 설치한 서비스 토큰을 확인한 뒤 다시 검사하세요.'),
        'AUTHORIZATION_FAILED': ('ROSY 권한 담당자', '이 서비스 신원의 조회 권한을 확인한 뒤 다시 검사하세요.'),
        'ROLE_NOT_SUITABLE': ('ROSY 권한 담당자', '사람의 운영자 자격 대신 Harbor 전용 service 역할의 기계 신원을 발급한 뒤 다시 검사하세요.'),
        'PRINCIPAL_MISMATCH': ('Harbor 설치 담당자', '설정한 서비스 신원과 토큰의 신원을 일치시킨 뒤 다시 검사하세요.'),
        'ARM_NOT_INSTALLED': ('ROSY 설치 담당자', '호환되는 ARM 조회 API와 실행 설치 구성을 준비한 뒤 다시 검사하세요.'),
        'ARM_API_UNCONFIRMED': ('ROSY 설치 담당자', 'Fleet 버전과 ARM 조회 API 계약을 확인한 뒤 다시 검사하세요.'),
        'SESSION_API_UNCONFIRMED': ('ROSY 설치 담당자', 'Fleet 버전과 신원 조회 API 계약을 확인한 뒤 다시 검사하세요.'),
        'SESSION_SCHEMA_UNCONFIRMED': ('ROSY 설치 담당자', 'Fleet 버전과 신원 응답 계약을 확인한 뒤 다시 검사하세요.'),
        'INVALID_FLEET_RESPONSE': ('ROSY 설치 담당자', 'Fleet 응답과 API 계약을 확인한 뒤 다시 검사하세요.'),
        'READ_API_VERIFIED': ('Harbor·ROSY 운영 담당자', '실행 전에 설치 대응·자원·승인과 독립 업무 근거를 별도로 확인하세요.'),
    }
    owner, action = actions.get(reason, ('Harbor 관리자', '현재 설정으로 Fleet 연결 확인을 실행하세요.'))
    if reason in CONNECTION_REASONS and (reason.startswith('FLEET_DISCOVERY_') or
            reason in {'FLEET_NOT_DISCOVERED', 'FLEET_SITE_CA_REQUIRED', 'FLEET_SITE_CA_UNREADABLE',
                       'FLEET_SITE_CA_INVALID', 'FLEET_HEALTH_UNCONFIRMED'}):
        owner, action = 'Harbor·ROSY 설치 담당자', CONNECTION_REASONS[reason]
    return dict(steps=steps, owner=owner, action=action, can_check=configured)


@require_roles('viewer', 'operator', 'field', 'admin')
def workflow(request):
    return page(request, 'workflow.html', section='workflow')


@require_roles('admin')
@require_http_methods(['GET', 'POST'])
def integration(request):
    import os
    from harbor_web import fleet_connection as bridge
    from harbor_web import readiness
    from harbor_web.models import EvidenceProducerSetting
    from harbor_web.pipeline import connection_summary
    config = bridge.connection_settings()
    origins = bridge.allowed_origins()
    form = ConnectionForm(initial=dict(config, expected_version=config['version']), origins=origins)
    discovered = None
    if request.method == 'POST':
        action = request.POST.get('action')
        form = ConnectionForm(request.POST, origins=origins) if action == 'save' else form
        command = form if action == 'save' else ConnectionVersionForm(request.POST)
        if action not in {'save', 'check', 'producer', 'discover'}:
            from django.http import HttpResponseBadRequest
            return HttpResponseBadRequest('Unknown connection action.')
        if command.is_valid():
            try:
                if action == 'save':
                    bridge.save_connection(request.user, **command.cleaned_data)
                    messages.success(request, 'Fleet 연결 설정을 저장했습니다.')
                elif action == 'producer':
                    bridge.set_human_confirmation(request.user, enabled=request.POST.get('enabled') == '1',
                                                  expected_version=command.cleaned_data['expected_version'])
                    messages.success(request, '사람 확인 생산자 설정을 저장했습니다.')
                elif action == 'check':
                    bridge.check_connection(request.user, **command.cleaned_data)
                else:
                    discovered = bridge.discover_connections(request.user, **command.cleaned_data)
                if action != 'discover':
                    return redirect('/settings/integration/')
            except ValidationError as error:
                messages.error(request, CONNECTION_REASONS.get(error.messages[0], '연결 설정을 확인하세요.'))
        else:
            messages.error(request, '연결 입력 또는 설정 버전을 확인한 뒤 다시 시도하세요.')
    saved_discovery = bridge.last_discovery()
    if discovered is None and saved_discovery:
        discovered = saved_discovery.detail
    check = bridge.last_check()
    guidance = connection_guidance(config, check, token_present=bool(os.environ.get('HARBOR_FLEET_TOKEN')))
    return page(request, 'integration.html', section='settings', connection=connection_summary(),
        connection_config=config, connection_form=form, connection_check=check,
        discovery_result=discovered, discovered_origins=discovered['origins'] if discovered else [],
        discovery_checked_at=saved_discovery.created_at if saved_discovery else None,
        discovery_sites=[dict(site, message=CONNECTION_REASONS.get(site['api'], CONNECTION_REASONS['PUBLIC_API_UNCONFIRMED']))
                         for site in discovered.get('sites', [])] if discovered else [],
        discovery_failures=[dict(item, message=CONNECTION_REASONS.get(item['reason'], '사이트 연결을 확인하세요.'))
                            for item in discovered['failures']] if discovered else [],
        check_message=CONNECTION_REASONS.get(check.detail['reason'], '') if check else '',
        connection_guidance=guidance, readiness=readiness.table(check),
        producer_setting=EvidenceProducerSetting.objects.first(), origins_available=bool(origins), server_inputs=[
            dict(label=label, present=bool(os.environ.get('HARBOR_' + key)), variable='HARBOR_' + key)
            for label, key in [('Fleet 서비스 토큰', 'FLEET_TOKEN'), ('업무 실행 장부', 'LEDGER_PATH'), ('설치된 이동 대응', 'BINDINGS_PATH')]])
