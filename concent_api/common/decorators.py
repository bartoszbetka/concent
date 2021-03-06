from functools import wraps
from logging import getLogger
from typing import Any
from typing import Callable
import traceback

from django.conf import settings

from common.exceptions import ConcentFeatureIsNotAvailable
from common.logging import LoggingLevel
from common.logging import log

logger = getLogger(__name__)
crash_logger = getLogger('concent.crash')


def provides_concent_feature(concent_feature: str) -> Callable:
    """
    Decorator for declaring that given `concent_feature` is required to be in setting CONCENT_FEATURES
    for decorated view or celery task function.
    """
    def decorator(_function: Callable) -> Callable:
        assert callable(_function)

        @wraps(_function)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if concent_feature not in settings.CONCENT_FEATURES:
                raise ConcentFeatureIsNotAvailable(
                    f'Concent feature `{concent_feature}` is not enabled. Function `{_function.__name__}` cannot be called in this configuration.'
                )
            return _function(*args, **kwargs)
        return wrapper
    return decorator


def log_task_errors(task: Callable) -> Callable:

    @wraps(task)
    def wrapper(*args: Any, **kwargs: Any) -> None:
        try:
            return task(*args, **kwargs)
        except Exception as exception:
            log(
                crash_logger,
                f'Exception occurred while executing task {task.__name__}: {exception}, Traceback: {traceback.format_exc()}',
                subtask_id=kwargs['subtask_id'] if 'subtask_id' in kwargs else None,
                logging_level=LoggingLevel.ERROR)
            raise
    return wrapper
