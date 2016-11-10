"""Prevents inconsistent db updates for flow."""

import time
import random
from contextlib import contextmanager

from django.core.cache import cache as default_cache
from django.db import transaction, DatabaseError

from viewflow.exceptions import FlowLockFailed


class NoLock(object):
    """
    No pessimistic locking, just execute flow task in transaction.

    Not suitable when you have Join nodes in your flow.
    """

    def __call__(self, flow):
        @contextmanager
        def lock(flow_class, process_pk):
            with transaction.atomic():
                yield
        return lock


class SelectForUpdateLock(object):
    """
    Databace lock uses `select ... for update` on the process instance row.

    Recommended to use with PostgreSQL.
    """
    def __init__(self, nowait=True, attempts=5):
        self.nowait = nowait
        self.attempts = attempts

    def __call__(self, flow):
        @contextmanager
        def lock(flow_class, process_pk):
            for i in range(self.attempts):
                with transaction.atomic():
                    try:
                        process = flow_class.process_class._default_manager.filter(pk=process_pk)
                        if not process.select_for_update(nowait=self.nowait).exists():
                            raise DatabaseError('Process not exists')
                    except DatabaseError:
                        if i != self.attempts - 1:
                            sleep_time = (((i + 1) * random.random()) + 2 ** i) / 2.5
                            time.sleep(sleep_time)
                        else:
                            raise FlowLockFailed('Lock failed for {}'.format(flow_class))
                    else:
                        yield
                        break
        return lock


class CacheLock(object):
    """
    Task lock based on Django's cache.

    Use it if primary cache backend has transactional `add` functionality,
    like `memcached`.

    Example::

        class MyFlow(Flow):
            lock_impl = CacheLock(cache=caches['locks'])

    The example uses a different cache. The default cache
    is Django's ``default`` cache configuration.
    """

    def __init__(self, cache=default_cache, attempts=5, expires=120):  # noqa D102
        self.cache = cache
        self.attempts = attempts
        self.expires = expires

    def __call__(self, flow):  # noqa D102
        @contextmanager
        def lock(flow_class, process_pk):
            key = 'django-viewflow-lock-{}/{}'.format(flow_class._meta.flow_label, process_pk)

            for i in range(self.attempts):
                process = flow_class.process_class._default_manager.filter(pk=process_pk)
                if process.exists():
                    stored = self.cache.add(key, 1, self.expires)
                    if stored:
                        break
                if i != self.attempts - 1:
                    sleep_time = (((i + 1) * random.random()) + 2 ** i) / 2.5
                    time.sleep(sleep_time)
            else:
                raise FlowLockFailed('Lock failed for {}'.format(flow_class))

            try:
                with transaction.atomic():
                    yield
            finally:
                self.cache.delete(key)

        return lock


no_lock = NoLock()
cache_lock = CacheLock()
select_for_update_lock = SelectForUpdateLock()
