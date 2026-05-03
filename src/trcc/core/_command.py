"""@command decorator — collapses facade-method bookkeeping.

A facade method (LCDCommands.set_brightness, LEDCommands.set_color, …)
historically did four things by hand:

    1. Bounds-check the device index, return error if out of range
    2. Call the device's underlying method
    3. Wrap the device's dict result in a typed dataclass
    4. Publish a topic on success

That's 8-10 lines per method × 50+ methods = ~400 lines of bookkeeping
in `lcd_commands.py` + `led_commands.py`. This decorator collapses
items 3 and 4. The body does the call; the decorator wraps the result
and publishes the topic.

Body shape after::

    @command(result_cls=FrameResult, topic=Topic.LCD_BRIGHTNESS,
             include_frame=True)
    def set_brightness(self, lcd: int, percent: int) -> dict:
        if (dev := self._get(lcd)) is None:
            return {'success': False, 'error': f'LCD {lcd} not found'}
        return dev.set_brightness(percent)

Callers see ``set_brightness(lcd, percent) -> FrameResult``; bodies
return the raw device dict. Lean.
"""
from __future__ import annotations

import functools
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

if TYPE_CHECKING:
    from .results import OpResult

P = ParamSpec('P')
R = TypeVar('R', bound='OpResult')


def command(
    *,
    result_cls: type[R],
    topic: str | None = None,
    include_frame: bool = False,
    publish_args: tuple[int, ...] | None = None,
) -> Callable[[Callable[P, Any]], Callable[P, R]]:
    """Wrap a facade method.

    Args:
        result_cls: Dataclass to wrap the device dict in (`OpResult`,
            `FrameResult`, `ThemeResult`, `LEDResult`, …).
        topic: EventBus topic to publish on success. ``None`` = no publish.
        include_frame: When True, copy the device result's ``image`` into
            the result dataclass's ``frame`` field via ``Frame(native=…)``.
        publish_args: Positional arg indices (post-self) to pass as the
            payload to ``events.publish(topic, *payload)``. Default
            ``None`` → all positional args.

    Decorated method body should return the device's raw result dict.
    """
    def decorator(method: Callable) -> Callable:
        @functools.wraps(method)
        def wrapper(self: Any, *args: Any, **kwargs: Any) -> OpResult:
            r = method(self, *args, **kwargs)
            if not isinstance(r, dict):
                # Body returned a result dataclass already (e.g., early-exit
                # FrameResult on bounds-check failure) — pass through.
                return r
            success = r.get('success', False)
            out: dict[str, Any] = {
                'success': success,
                'message': r.get('message', ''),
                'error': r.get('error'),
            }
            if include_frame:
                from .results import Frame
                img = r.get('image')
                out['frame'] = Frame(native=img) if img is not None else None
            # Subclass-specific extras travel through if the dict has them
            # and the result_cls accepts them. Cheap to attempt.
            for k in ('is_animated', 'interval_ms', 'overlay_config',
                      'overlay_enabled', 'display_colors'):
                if k in r:
                    out[k] = r[k]
            if success and topic:
                payload = args if publish_args is None else tuple(args[i] for i in publish_args)
                self._events.publish(topic, *payload)
            try:
                return result_cls(**out)
            except TypeError:
                # result_cls doesn't accept one of the extras — strip them
                # and try the minimal constructor.
                return result_cls(
                    success=success,
                    message=r.get('message', ''),
                    error=r.get('error'),
                )
        return wrapper
    return decorator
