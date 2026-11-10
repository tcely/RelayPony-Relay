import os

from pathlib import Path

from django.core.exceptions import ImproperlyConfigured


BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv('DJANGO_SECRET_KEY', None)
DEBUG = '1' == os.getenv('DJANGO_DEBUG', '0')
DB_ENGINE = os.getenv('DB_ENGINE', 'default').lower()
ALLOWED_HOSTS = [h for h in os.getenv('DJANGO_ALLOWED_HOSTS', 'localhost').split(',') if h]

INSTALLED_APPS = ['signaling']

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.middleware.common.CommonMiddleware',
]

ROOT_URLCONF = 'relay.urls'
WSGI_APPLICATION = 'relay.wsgi.application'

if SECRET_KEY is None:
    raise ImproperlyConfigured('DJANGO_SECRET_KEY must be provided with the environment variables!')

DATABASES = {
    'sqlite': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': os.getenv('SQLITE_PATH', str(BASE_DIR / 'data' / 'db.sqlite3')),
        'OPTIONS': {
            'journal_mode': 'WAL',
            # Take the write lock at BEGIN, avoiding "database is locked"
            # errors when a deferred transaction tries to upgrade to a write.
            'transaction_mode': 'IMMEDIATE',
            'timeout': 20,
        },
    }
}
if DB_ENGINE in ('default', 'sqlite', 'sqlite3'):
    DATABASES['default'] = DATABASES['sqlite']
elif DB_ENGINE in ('mysql', 'mariadb'):
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.mysql',
            'PORT': os.getenv('DB_PORT', '3306'),
            'OPTIONS': {'charset': 'utf8mb4'},
        }
    }
elif DB_ENGINE in ('postgres', 'postgresql'):
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'PORT': os.getenv('DB_PORT', '5432'),
            'CONN_MAX_AGE': 60,
        }
    }
else:
    raise ImproperlyConfigured(f"Unsupported DB_ENGINE: {DB_ENGINE!r} (try 'default' instead?)")
if DATABASES['default'].get('PORT'):
    DATABASES['default'].update(dict(
        NAME=os.getenv('DB_NAME', 'relaypony'),
        USER=os.getenv('DB_USER', 'relaypony'),
        PASSWORD=os.getenv('DB_PASSWORD', ''),
        HOST=os.getenv('DB_HOST', '127.0.0.1'),
    ))
    if not DATABASES['default']['DB_PASSWORD']:
        raise ImproperlyConfigured('DB_PASSWORD must be provided with the environment variables!')

USE_TZ = True
TIME_ZONE = 'UTC'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

