import os

from django.core.wsgi import get_wsgi_application

from bootstrap import bootstrap

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "shop_site.settings")
application = bootstrap(get_wsgi_application())
