# Minimal settings for the example (no database).
SECRET_KEY = "example-only-not-secret"
DEBUG = True
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
ROOT_URLCONF = "shop_site.urls"
WSGI_APPLICATION = "shop_site.wsgi.application"
INSTALLED_APPS = []
MIDDLEWARE = []
DATABASES = {}
USE_TZ = True
