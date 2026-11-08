from pathlib import Path
import os
import sys
import sqlite3
from contextlib import closing
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
MODE = os.environ.get('HARBOR_WEB_MODE')
if MODE not in {'development', 'live', 'test'}:
    raise ImproperlyConfigured('Set HARBOR_WEB_MODE to development, live, or test.')
SECRET_KEY = os.environ.get('HARBOR_WEB_SECRET_KEY', '')
if len(SECRET_KEY) < 50:
    raise ImproperlyConfigured('HARBOR_WEB_SECRET_KEY must contain at least 50 characters.')
DEBUG = MODE == 'development'
ALLOWED_HOSTS = ['localhost', '127.0.0.1', '[::1]']
if MODE == 'test':
    ALLOWED_HOSTS += ['testserver']
if MODE == 'live':
    ALLOWED_HOSTS = [host.strip() for host in os.environ.get('HARBOR_WEB_ALLOWED_HOSTS', '').split(',') if host.strip()]
    if not ALLOWED_HOSTS or any('*' in host for host in ALLOWED_HOSTS):
        raise ImproperlyConfigured('Live mode requires explicit HARBOR_WEB_ALLOWED_HOSTS.')
def validate_database_path(database):
    if str(database) == ':memory:':
        return
    path = Path(database).resolve()
    ledger = os.environ.get('HARBOR_LEDGER_PATH')
    if ledger:
        ledger_path = Path(ledger).resolve()
        if path == ledger_path or (path.exists() and ledger_path.exists() and path.samefile(ledger_path)):
            raise ImproperlyConfigured('Web and business ledger databases must be distinct.')
    if not path.exists() or path.stat().st_size == 0:
        return
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    except (sqlite3.Error, OSError) as exc:
        raise ImproperlyConfigured('Cannot verify web database ownership.') from exc
    if tables.intersection({'business', 'evidence', 'transport_intents', 'supply_clearance'}):
        raise ImproperlyConfigured('Business/transport schema cannot be used as the web database.')
    from harbor_web.runtime import require_profile
    try:
        require_profile(path, MODE, allow_initial=True)
    except (ValueError, sqlite3.Error) as exc:
        raise ImproperlyConfigured(str(exc)) from exc


DB_PATH = os.environ.get('HARBOR_WEB_DATABASE')
if MODE == 'live' and (not DB_PATH or not Path(DB_PATH).is_absolute() or 'legacy' in DB_PATH.lower() or Path(DB_PATH).name == 'web-development.sqlite3'):
    raise ImproperlyConfigured('Live mode requires an explicit absolute non-legacy HARBOR_WEB_DATABASE.')
DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': DB_PATH or (':memory:' if MODE == 'test' else BASE_DIR / 'runs/web-development.sqlite3'), 'OPTIONS': {'timeout': 20}}}
validate_database_path(DATABASES['default']['NAME'])
INSTALLED_APPS = ['django.contrib.admin', 'django.contrib.auth', 'django.contrib.contenttypes', 'django.contrib.sessions', 'django.contrib.messages', 'django.contrib.staticfiles', 'axes', 'harbor_web.apps.HarborWebConfig']
MIDDLEWARE = ['django.middleware.security.SecurityMiddleware', 'whitenoise.middleware.WhiteNoiseMiddleware', 'django.contrib.sessions.middleware.SessionMiddleware', 'django.middleware.common.CommonMiddleware', 'django.middleware.csrf.CsrfViewMiddleware', 'django.contrib.auth.middleware.AuthenticationMiddleware', 'harbor_web.rosy_account.RosyAccountMiddleware', 'django.contrib.messages.middleware.MessageMiddleware', 'django.middleware.clickjacking.XFrameOptionsMiddleware', 'harbor_web.middleware.SecurityHeadersMiddleware', 'axes.middleware.AxesMiddleware']
ROOT_URLCONF = 'harbor_web.urls'
WSGI_APPLICATION = 'harbor_web.wsgi.application'
TEMPLATES = [{'BACKEND': 'django.template.backends.django.DjangoTemplates', 'DIRS': [BASE_DIR / 'harbor_web/templates'], 'APP_DIRS': True, 'OPTIONS': {'context_processors': ['django.template.context_processors.request', 'django.contrib.auth.context_processors.auth', 'django.contrib.messages.context_processors.messages', 'harbor_web.presentation.context']}}]
AUTHENTICATION_BACKENDS = ['axes.backends.AxesStandaloneBackend', 'django.contrib.auth.backends.ModelBackend']
AUTH_PASSWORD_VALIDATORS = [{'NAME': 'django.contrib.auth.password_validation.' + name} for name in ['UserAttributeSimilarityValidator', 'MinimumLengthValidator', 'CommonPasswordValidator', 'NumericPasswordValidator']]
LOGIN_URL = '/accounts/login/'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = LOGIN_URL
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_SAMESITE = 'Lax'
SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = SECURE_SSL_REDIRECT = MODE == 'live'
SECURE_HSTS_SECONDS = 31536000 if MODE == 'live' else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = 'same-origin'
X_FRAME_OPTIONS = 'DENY'
AXES_FAILURE_LIMIT = 5
AXES_COOLOFF_TIME = 1
AXES_RESET_ON_SUCCESS = True
AXES_LOCKOUT_PARAMETERS = ['ip_address', 'username']
AXES_HANDLER = 'axes.handlers.database.AxesDatabaseHandler'
AXES_CLIENT_IP_CALLABLE = 'harbor_web.auth.client_ip'
AXES_LOCKOUT_CALLABLE = 'harbor_web.auth.lockout_response'
LANGUAGE_CODE = 'ko-kr'
TIME_ZONE = 'Asia/Seoul'
USE_I18N = USE_TZ = True
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'runs/web-static'
BRAND_DIR = BASE_DIR / 'assets/brand'
if not BRAND_DIR.is_dir():
    BRAND_DIR = Path(sys.prefix) / 'share/harborlogi/brand'
# Django 5.2 finder prefixes use os.sep on Windows; unique asset names avoid slash-URL mismatches.
STATICFILES_DIRS = [BASE_DIR / 'harbor/web_assets', BRAND_DIR]
WHITENOISE_USE_FINDERS = True
WHITENOISE_AUTOREFRESH = DEBUG
DATA_UPLOAD_MAX_MEMORY_SIZE = 1048576
DATA_UPLOAD_MAX_NUMBER_FIELDS = 200
FILE_UPLOAD_MAX_MEMORY_SIZE = 1048576
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ADR 0027: optional Rosy Account sign-in; unset keeps local accounts only.
ROSY_ACCOUNT = None
if os.environ.get('HARBOR_ROSY_ACCOUNT_FILE'):
    from harbor_web.rosy_account import load_config
    ROSY_ACCOUNT = load_config(os.environ['HARBOR_ROSY_ACCOUNT_FILE'])

if os.environ.get('HARBOR_WEB_TRUSTED_PROXY') == '1':
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
