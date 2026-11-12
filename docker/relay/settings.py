import os

from pathlib import Path
from types import SimpleNamespace

from django.core.exceptions import ImproperlyConfigured

from signaling import settings as _signaling_settings


BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv('DJANGO_SECRET_KEY', None)
DEBUG = '1' == os.getenv('DJANGO_DEBUG', '0')
DB_ENGINE = os.getenv('DB_ENGINE', 'default').lower()
ALLOWED_HOSTS = [h for h in os.getenv('DJANGO_ALLOWED_HOSTS', 'localhost').split(',') if h]
ALLOWED_CIDR_NETS = [
    '192.168.0.0/16',
    '10.0.0.0/8',
]

INSTALLED_APPS = ['signaling']

# Include signaling.settings in its own namespace
SIGNALING = SimpleNamespace(
    **{
        k: v
        for k, v in vars(_signaling_settings).items()
        if k.isupper()
    }
)
del _signaling_settings

MIDDLEWARE = [
    'allow_cidr.middleware.AllowCIDRMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.middleware.common.CommonMiddleware',
]

ROOT_URLCONF = 'relay.urls'
WSGI_APPLICATION = 'relay.wsgi.application'

if SECRET_KEY is None:
    raise ImproperlyConfigured('DJANGO_SECRET_KEY must be provided with the environment variables!')

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
    },
    'signaling': {
        'BACKEND': 'django.core.cache.backends.db.DatabaseCache',
        'LOCATION': 'django_cache_table',
        'OPTIONS': {
            'MAX_ENTRIES': 10_000,
            'CULL_FREQUENCY': 3,
        },
    }
}

DATABASES = {
    'sqlite': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': os.getenv('SQLITE_DB_PATH', str(BASE_DIR / 'data' / 'db.sqlite3')),
        'OPTIONS': {
            # Take the write lock at BEGIN, avoiding "database is locked"
            # errors when a deferred transaction tries to upgrade to a write.
            'transaction_mode': 'IMMEDIATE',
            'timeout': 20,
            'init_command': """
                    PRAGMA locking_mode = NORMAL;
                    PRAGMA journal_mode = WAL;
                    PRAGMA journal_size_limit = 67108864;
                    PRAGMA legacy_alter_table = OFF;
                    PRAGMA auto_vacuum = INCREMENTAL;
                """,
        },
    },
    'cache': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': os.getenv('SQLITE_CACHE_PATH', str(BASE_DIR / 'data' / 'cache.sqlite3')),
        'OPTIONS': {
            'transaction_mode': 'IMMEDIATE',
            'timeout': 20,
            'init_command': """
                    PRAGMA locking_mode = NORMAL;
                    PRAGMA synchronous = NORMAL;
                    PRAGMA journal_mode = WAL;
                    PRAGMA journal_size_limit = 67108864;
                    PRAGMA legacy_alter_table = OFF;
                    PRAGMA mmap_size = 134217728;
                    PRAGMA temp_store = MEMORY;
                    PRAGMA auto_vacuum = FULL;
                """,
        },
    },
}
if DB_ENGINE in ('default', 'sqlite', 'sqlite3'):
    DATABASES['default'] = DATABASES['sqlite']
elif DB_ENGINE in ('mysql', 'mariadb'):
    import pymysql

    pymysql.version_info = (2, 2, 1, 'final', 0)
    pymysql.install_as_MySQLdb()

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
DATABASE_ROUTERS = ['relay.routers.CacheRouter']

USE_TZ = True
TIME_ZONE = 'UTC'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

