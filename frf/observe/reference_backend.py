"""Select the declared source runtime before builds and observations."""
from ..core.harbor import dockerfile_for


def source_runtime(backend, language):
    control = getattr(backend, 'control_backend', backend)
    factory = getattr(control, 'runtime', None)
    return factory(dockerfile_for(language, language)) if callable(factory) else backend
