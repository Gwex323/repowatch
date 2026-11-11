"""Keep all side effects of a request on the same side of the maintenance barrier."""

from functools import wraps

from fastapi import APIRouter

from repowatch.operations.locks import data_lock


class GuardedRouter(APIRouter):
    def __init__(self, settings, **kwargs):
        self.settings = settings
        super().__init__(**kwargs)

    def add_api_route(self, path, endpoint, **kwargs):
        @wraps(endpoint)
        def guarded_endpoint(*args, **values):
            with data_lock(self.settings):
                return endpoint(*args, **values)

        return super().add_api_route(path, guarded_endpoint, **kwargs)
