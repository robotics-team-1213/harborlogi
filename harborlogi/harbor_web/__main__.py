"""Single process execution owner; database migration is a separate operation."""
import argparse
import os


def serve_options(environ=os.environ):
    options = dict(host='127.0.0.1', threads=4)
    if environ.get('HARBOR_WEB_TRUSTED_PROXY') == '1':
        # Waitress 3 strips X-Forwarded-Proto from untrusted peers; Django would then redirect to HTTPS forever.
        options.update(trusted_proxy='127.0.0.1', trusted_proxy_headers={'x-forwarded-proto'})
    return options


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8060)
    args = parser.parse_args()
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'harbor_web.settings')
    import django
    django.setup()
    from django.conf import settings
    from harbor_web.commands import acquire_registry, registry_close
    from harbor_web.wsgi import application
    from harbor_web.fleet_connection import connection_settings
    from harbor.ownership import WriterLease
    from harbor_web.runtime import require_profile
    from pathlib import Path
    path = settings.DATABASES['default']['NAME']
    if str(path) == ':memory:' or not Path(path).is_absolute():
        raise RuntimeError('Server requires an absolute file-backed web database.')
    require_profile(path, settings.MODE)
    lease = WriterLease(path)
    lease.__enter__()
    try:
        connection = connection_settings()
        configured = bool(connection['origin'] and connection['principal'] and connection['allowed']) and all(
            os.environ.get('HARBOR_' + key) for key in ('LEDGER_PATH', 'FLEET_TOKEN', 'BINDINGS_PATH'))
        if configured:
            acquire_registry()
        from waitress import serve
        serve(application, port=args.port, **serve_options())
    finally:
        registry_close()
        lease.__exit__()


if __name__ == '__main__':
    main()
