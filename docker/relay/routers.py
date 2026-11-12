class CacheRouter:
    def db_for_read(self, model, **hints):
        if 'django_cache' == model._meta.app_label:
            return 'cache'

    def db_for_write(self, model, **hints):
        if 'django_cache' == model._meta.app_label:
            return 'cache'

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if 'django_cache' == app_label:
            return 'cache' == db
        elif 'cache' == db:
            return False

