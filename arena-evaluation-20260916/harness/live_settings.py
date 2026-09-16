from conversations.settings import Test

class Review(Test):
    ALLOWED_HOSTS = ['127.0.0.1', 'localhost', 'testserver']
    CORS_ALLOWED_ORIGINS = ['http://127.0.0.1:23000', 'http://127.0.0.1:23001']
    CSRF_TRUSTED_ORIGINS = ['http://127.0.0.1:23000', 'http://127.0.0.1:23001']
    SESSION_ENGINE = 'django.contrib.sessions.backends.db'
    SESSION_COOKIE_NAME = 'arena_review_session'
    DEBUG = False
    AUTO_TITLE_AFTER_USER_MESSAGES = 99
