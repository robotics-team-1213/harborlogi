from django.test import SimpleTestCase
from waitress.adjustments import Adjustments
from waitress.proxy_headers import proxy_headers_middleware

from harbor_web.__main__ import serve_options


def scheme(options, remote):
    """What Waitress hands the app for one proxied request, using Waitress's own middleware and defaults."""
    adj = Adjustments(**{k: v for k, v in options.items() if k != 'host'})
    seen = {}
    app = proxy_headers_middleware(lambda environ, start: seen.update(environ) or [],
        trusted_proxy=adj.trusted_proxy, trusted_proxy_headers=adj.trusted_proxy_headers,
        clear_untrusted=adj.clear_untrusted_proxy_headers)
    app({'REMOTE_ADDR': remote, 'wsgi.url_scheme': 'http', 'HTTP_X_FORWARDED_PROTO': 'https'}, None)
    return seen['wsgi.url_scheme'], seen.get('HTTP_X_FORWARDED_PROTO')


class WebEntryTests(SimpleTestCase):
    def test_trusted_loopback_proxy_keeps_https(self):
        options = serve_options({'HARBOR_WEB_TRUSTED_PROXY': '1'})
        self.assertEqual(options['host'], '127.0.0.1')
        self.assertEqual(scheme(options, '127.0.0.1')[0], 'https')
        # Only the loopback proxy is trusted; any other peer's claim is dropped.
        self.assertEqual(scheme(options, '10.0.0.5'), ('http', None))

    def test_without_trusted_proxy_the_header_is_dropped(self):
        self.assertEqual(scheme(serve_options({}), '127.0.0.1'), ('http', None))
