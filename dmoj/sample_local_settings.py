#####################################
########## Django settings ##########
#####################################
# See <https://docs.djangoproject.com/en/1.11/ref/settings/>
# for more info and help. If you are stuck, you can try Googling about
# Django - many of these settings below have external documentation about them.
#
# The settings listed here are of special interest in configuring the site.

# SECURITY WARNING: keep the secret key used in production secret!
# You may use <http://www.miniwebtool.com/django-secret-key-generator/>
# to generate this key.
SECRET_KEY = "your-secret-key"

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = True  # Change to False once you are done with runserver testing.

# Uncomment and set to the domain names this site is intended to serve.
# You must do this once you set DEBUG to False.
ALLOWED_HOSTS = ["*"]

# Optional apps that DMOJ can make use of.
INSTALLED_APPS += ()

# Path to problem folder
DMOJ_PROBLEM_DATA_ROOT = "/path/to/problem/folder"

# Caching. You can use memcached or redis instead.
# Documentation: <https://docs.djangoproject.com/en/1.11/topics/cache/>
CACHES = {
    "default": {
        "BACKEND": "judge.cache_handler.CacheHandler",
    },
    "l0": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "OPTIONS": {"MAX_ENTRIES": 1000},
    },
    "primary": {
        "BACKEND": "django.core.cache.backends.memcached.PyMemcacheCache",
        "LOCATION": "127.0.0.1:11211",
    },
}

# Your database credentials. Only MySQL is supported by DMOJ.
# Documentation: <https://docs.djangoproject.com/en/1.11/ref/databases/>
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.mysql",
        "NAME": "your_db_name",
        "USER": "your_db_user",
        "PASSWORD": "your_db_password",
        "HOST": "your_db_host",
        "OPTIONS": {
            "charset": "utf8mb4",
            "sql_mode": "STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION",
        },
    }
}

# Sessions.
# Documentation: <https://docs.djangoproject.com/en/1.11/topics/http/sessions/>
# SESSION_ENGINE = 'django.contrib.sessions.backends.cached_db'

# Internationalization.
# Documentation: <https://docs.djangoproject.com/en/1.11/topics/i18n/>
USE_I18N = True
USE_L10N = True
USE_TZ = True

## django-compressor settings, for speeding up page load times by minifying CSS and JavaScript files.
# Documentation: https://django-compressor.readthedocs.io/en/latest/
COMPRESS_OUTPUT_DIR = "cache"
COMPRESS_CSS_FILTERS = [
    "compressor.filters.css_default.CssAbsoluteFilter",
    "compressor.filters.cssmin.CSSMinFilter",
]
COMPRESS_JS_FILTERS = ["compressor.filters.jsmin.JSMinFilter"]
COMPRESS_STORAGE = "compressor.storage.GzipCompressorFileStorage"
STATICFILES_FINDERS += ("compressor.finders.CompressorFinder",)

#########################################
########## Email configuration ##########
#########################################
# See <https://docs.djangoproject.com/en/1.11/topics/email/#email-backends>
# for more documentation. You should follow the information there to define
# your email settings.

# Use this if you are just testing.
# EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

# The following block is included for your convenience, if you want
# to use Gmail.
# EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
# EMAIL_USE_TLS = True
# EMAIL_HOST = 'smtp.example.com'
# EMAIL_HOST_USER = 'your_email@example.com'
# EMAIL_HOST_PASSWORD = 'your_email_password'
# EMAIL_PORT = 587

# To use Mailgun, uncomment this block.
# You will need to run `pip install django-mailgun-mime` to get `MailgunBackend`.
# EMAIL_BACKEND = 'django_mailgun_mime.backends.MailgunMIMEBackend'
# MAILGUN_API_KEY = '<your Mailgun access key>'
# MAILGUN_DOMAIN_NAME = '<your Mailgun domain>'

# You can also use Sendgrid, with `pip install sendgrid-django`.
# EMAIL_BACKEND = 'sgbackend.SendGridBackend'
# SENDGRID_API_KEY = '<Your SendGrid API Key>'

# The DMOJ site is able to notify administrators of errors via email,
# if configured as shown below.

# A tuple of (name, email) pairs that specifies those who will be mailed
# when the server experiences an error when DEBUG = False.
ADMINS = [
    ("Admin", "admin@example.com"),
]

# The sender for the aforementioned emails.
# SERVER_EMAIL = 'LQDOJ: Le Quy Don Online Judge <>'

##################################################
########### Static files configuration. ##########
##################################################
# See <https://docs.djangoproject.com/en/1.11/howto/static-files/>.

# Change this to somewhere more permanent, especially if you are using a
# webserver to serve the static files. This is the directory where all the
# static files DMOJ uses will be collected to.
# You must configure your webserver to serve this directory as /static/ in production.
STATIC_ROOT = "/path/to/static-root"

# URL to access static files.
# STATIC_URL = '/static/'

# Uncomment to use hashed filenames with the cache framework.
# STATICFILES_STORAGE = 'django.contrib.staticfiles.storage.CachedStaticFilesStorage'

MEDIA_ROOT = "/path/to/media"

# URL to access media files
# MEDIA_URL = '/media/'

# Enable image upload in editor
PAGEDOWN_IMAGE_UPLOAD_ENABLED = True

############################################
########## DMOJ-specific settings ##########
############################################

## DMOJ site display settings.
SITE_NAME = "YourSiteName"
SITE_LONG_NAME = "YourSiteLongName"
SITE_ADMIN_EMAIL = "admin@example.com"
TERMS_OF_SERVICE_URL = None  # Use a flatpage.

## Bridge controls.
# The judge connection address and port; where the judges will connect to the site.
# You should change this to something your judges can actually connect to
# (e.g., a port that is unused and unblocked by a firewall).
BRIDGED_JUDGE_ADDRESS = [("0.0.0.0", 9999)]

# The bridged daemon bind address and port to communicate with the site.
BRIDGED_DJANGO_ADDRESS = [("localhost", 9998)]

# Set this to True to to auto create judge in DB if any judge connects
# BRIDGED_AUTO_CREATE_JUDGE = False

## DMOJ features.
# Set to True to enable full-text searching for problems.
# ENABLE_FTS = True

# Set of email providers to ban when a user registers, e.g., {'throwawaymail.com'}.
BAD_MAIL_PROVIDERS = set()

# The number of submissions that a staff user can rejudge at once without
# requiring the permission 'Rejudge a lot of submissions'.
# Uncomment to change the submission limit.
REJUDGE_SUBMISSION_LIMIT = 10

## Event server (WebSocket for real-time updates like chat).
# Uncomment to enable live updating.
# EVENT_DAEMON_USE = True

# WebSocket daemon connection settings - used by Django to post events
# Must match the settings in websocket/config.js
# EVENT_DAEMON_URL = 'http://127.0.0.1:15100'
# EVENT_DAEMON_KEY = 'lqdoj'  # Must match backend_auth_token in websocket/config.js

# Public URL for client WebSocket connections
# In development, set to same value as EVENT_DAEMON_URL
# EVENT_DAEMON_PUBLIC_URL = 'http://127.0.0.1:15100'

# In production, use your domain with wss:// (nginx will proxy to port 15100)
# EVENT_DAEMON_PUBLIC_URL = 'wss://your-domain.com'

# Alternative AMQP-based event server (more complex setup)
# If you would like to use the AMQP-based event server from <https://github.com/DMOJ/event-server>,
# uncomment this section instead. This is more involved, and recommended to be done
# only after you have a working event server.
# EVENT_DAEMON_AMQP = 'amqp://username:password@127.0.0.1:5672/?heartbeat=0'
# EVENT_DAEMON_AMQP_EXCHANGE = 'ws'

EVENT_DAEMON_SUBMISSION_KEY = "your-event-submission-key"

## Celery
CELERY_BROKER_URL_SECRET = "redis://localhost:6379"
CELERY_RESULT_BACKEND_SECRET = "redis://localhost:6379"

## CDN control.
# Base URL for a copy of ace editor.
# Should contain ace.js, along with mode-*.js.
ACE_URL = "//cdnjs.cloudflare.com/ajax/libs/ace/1.2.3/"
JQUERY_JS = "//cdnjs.cloudflare.com/ajax/libs/jquery/2.2.4/jquery.min.js"
SELECT2_JS_URL = "//cdnjs.cloudflare.com/ajax/libs/select2/4.0.3/js/select2.min.js"
SELECT2_CSS_URL = "//cdnjs.cloudflare.com/ajax/libs/select2/4.0.3/css/select2.min.css"

# A map of Earth in Equirectangular projection, for timezone selection.
# Please try not to hotlink this poor site.
TIMEZONE_MAP = "http://example.com/timezone-map.jpg"

## Camo (https://github.com/atmos/camo) usage.
# CAMO_URL = "<URL to your camo install>"
# CAMO_KEY = "<The CAMO_KEY environmental variable you used>"

# Domains to exclude from being camo'd.
# CAMO_EXCLUDE = ("https://dmoj.ml", "https://dmoj.ca")

# Set to True to use https when dealing with protocol-relative URLs.
# See <http://www.paulirish.com/2010/the-protocol-relative-url/> for what they are.
# CAMO_HTTPS = False

# HTTPS level. Affects <link rel='canonical'> elements generated.
# Set to 0 to make http URLs canonical.
# Set to 1 to make the currently used protocol canonical.
# Set to 2 to make https URLs canonical.
DMOJ_SSL = 0
# SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
# SECURE_SSL_REDIRECT = True
# SESSION_COOKIE_SECURE = True
# CSRF_COOKIE_SECURE = True

## PDF rendering settings.
# Directory to cache the PDF.
# PROBLEM_PDF_CACHE = '/path/to/pdfcache'

# Path to use for nginx's X-Accel-Redirect feature.
# Should be an internal location mapped to the above directory.
# PROBLEM_PDF_INTERNAL = '/pdfcache'

# Path to a PhantomJS executable.
# PHANTOMJS = '/usr/local/bin/phantomjs'

# If you can't use PhantomJS or prefer wkhtmltopdf, set the path to wkhtmltopdf executable instead.
# WKHTMLTOPDF = '/usr/local/bin/wkhtmltopdf'

# Note that PhantomJS is preferred over wkhtmltopdf and would be used when both are defined.

# Used to encrypt chat url
# Generate it in a python shell:
# from cryptography.fernet import Fernet
# secret_key = Fernet.generate_key()
CHAT_SECRET_KEY = "your-chat-secret-key"

# Set to True to use subdomains for organizations
# USE_SUBDOMAIN = True
# SITE_DOMAIN = "localhost:8000"

## ======== Logging Settings ========
# Documentation: https://docs.djangoproject.com/en/1.9/ref/settings/#logging
#                https://docs.python.org/2/library/logging.config.html#logging-config-dictschema
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "file": {
            "format": "%(levelname)s %(asctime)s %(module)s %(message)s",
        },
        "simple": {
            "format": "%(levelname)s %(message)s",
        },
    },
    "handlers": {
        # You may use this handler as example for logging to other files.
        "bridge": {
            "level": "INFO",
            "class": "logging.handlers.RotatingFileHandler",
            "filename": "/tmp/bridge.log",
            "maxBytes": 10 * 1024 * 1024,
            "backupCount": 10,
            "formatter": "file",
        },
        "mail_admins": {
            "level": "ERROR",
            "class": "dmoj.throttle_mail.ThrottledEmailHandler",
        },
        "console": {
            "level": "DEBUG",
            "class": "logging.StreamHandler",
            "formatter": "file",
        },
        "user_ip": {
            "level": "INFO",
            "class": "logging.handlers.RotatingFileHandler",
            "filename": "/tmp/user_ip.log",
            "maxBytes": 10 * 1024 * 1024,
            "backupCount": 10,
            "formatter": "file",
        },
    },
    "loggers": {
        # Site 500 error mails.
        "django.request": {
            "handlers": ["mail_admins", "console"],
            "level": "ERROR",
            "propagate": False,
        },
        # Judging logs as received by bridged.
        "judge.bridge": {
            "handlers": ["mail_admins", "bridge"],
            "level": "INFO",
            "propagate": True,
        },
        # Error logs
        "judge.errors": {
            "handlers": ["mail_admins"],
            "level": "ERROR",
            "propagate": False,
        },
        "judge.debug": {
            "handlers": ["user_ip"],
            "level": "INFO",
            "propagate": False,
        },
        "judge.problem.pdf": {
            "handlers": ["console"],
        },
        "judge.user_ip": {
            "handlers": ["user_ip"],
        },
        # Other loggers of interest. Configure at will.
        #  - judge.user: logs naughty user behaviours.
        #  - judge.problem.pdf: PDF generation log.
        #  - judge.html: HTML parsing errors when processing problem statements etc.
        #  - judge.mail.activate: logs for the reply to activate feature.
        #  - event_socket_server
    },
}

# ML_OUTPUT_PATH = "/path/to/ml_output"

## ======== Integration Settings ========
## Python Social Auth
# Documentation: https://python-social-auth.readthedocs.io/en/latest/
# You can define these to enable authentication through the following services.
# SOCIAL_AUTH_GOOGLE_OAUTH2_KEY = ''
# SOCIAL_AUTH_GOOGLE_OAUTH2_SECRET = ''
# SOCIAL_AUTH_FACEBOOK_KEY = ''
# SOCIAL_AUTH_FACEBOOK_SECRET = ''
# SOCIAL_AUTH_GITHUB_SECURE_KEY = ''
# SOCIAL_AUTH_GITHUB_SECURE_SECRET = ''
# SOCIAL_AUTH_DROPBOX_OAUTH2_KEY = ''
# SOCIAL_AUTH_DROPBOX_OAUTH2_SECRET = ''

## ======== Custom Configuration ========
# You may add whatever django configuration you would like here.
# Do try to keep it separate so you can quickly patch in new settings.
