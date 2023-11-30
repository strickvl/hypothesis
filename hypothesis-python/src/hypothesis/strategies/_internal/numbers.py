# This file is part of Hypothesis, which may be found at
# https://github.com/HypothesisWorks/hypothesis/
#
# Copyright the Hypothesis Authors.
# Individual contributors are listed in AUTHORS.rst and the git log.
#
# This Source Code Form is subject to the terms of the Mozilla Public License,
# v. 2.0. If a copy of the MPL was not distributed with this file, You can
# obtain one at https://mozilla.org/MPL/2.0/.

import math
import operator
from decimal import Decimal
from fractions import Fraction
from sys import float_info
from typing import Any, Optional, Union

from hypothesis.control import assume, reject
from hypothesis.errors import InvalidArgument
from hypothesis.internal.conjecture import floats as flt, utils as d
from hypothesis.internal.conjecture.utils import calc_label_from_name
from hypothesis.internal.filtering import get_integer_predicate_bounds
from hypothesis.internal.floats import (
    count_between_floats,
    float_of,
    float_to_int,
    int_to_float,
    is_negative,
    next_down_normal,
    next_up,
    next_up_normal,
    width_smallest_normals,
)
from hypothesis.internal.validation import (
    check_type,
    check_valid_bound,
    check_valid_interval,
)
from hypothesis.strategies._internal.misc import just, nothing
from hypothesis.strategies._internal.strategies import SearchStrategy
from hypothesis.strategies._internal.utils import cacheable, defines_strategy

# See https://github.com/python/mypy/issues/3186 - numbers.Real is wrong!
Real = Union[int, float, Fraction, Decimal]
ONE_BOUND_INTEGERS_LABEL = d.calc_label_from_name("trying a one-bound int allowing 0")


class IntegersStrategy(SearchStrategy):
    def __init__(self, start, end):
        assert isinstance(start, int) or start is None
        assert isinstance(end, int) or end is None
        assert start is None or end is None or start <= end
        self.start = start
        self.end = end

    def __repr__(self):
        if self.start is None and self.end is None:
            return "integers()"
        if self.end is None:
            return f"integers(min_value={self.start})"
        if self.start is None:
            return f"integers(max_value={self.end})"
        return f"integers({self.start}, {self.end})"

    def do_draw(self, data):
        if self.start is None and self.end is None:
            return d.unbounded_integers(data)

        if self.start is None:
            if self.end <= 0:
                return self.end - abs(d.unbounded_integers(data))
            probe = self.end + 1
            while self.end < probe:
                data.start_example(ONE_BOUND_INTEGERS_LABEL)
                probe = d.unbounded_integers(data)
                data.stop_example(discard=self.end < probe)
            return probe

        if self.end is None:
            if self.start >= 0:
                return self.start + abs(d.unbounded_integers(data))
            probe = self.start - 1
            while probe < self.start:
                data.start_example(ONE_BOUND_INTEGERS_LABEL)
                probe = d.unbounded_integers(data)
                data.stop_example(discard=probe < self.start)
            return probe

        return d.integer_range(data, self.start, self.end, center=0)

    def filter(self, condition):
        kwargs, pred = get_integer_predicate_bounds(condition)

        start, end = self.start, self.end
        if "min_value" in kwargs:
            start = max(kwargs["min_value"], -math.inf if start is None else start)
        if "max_value" in kwargs:
            end = min(kwargs["max_value"], math.inf if end is None else end)

        if start != self.start or end != self.end:
            if start is not None and end is not None and start > end:
                return nothing()
            self = type(self)(start, end)
        return self if pred is None else super().filter(pred)


@cacheable
@defines_strategy(force_reusable_values=True)
def integers(
    min_value: Optional[int] = None,
    max_value: Optional[int] = None,
) -> SearchStrategy[int]:
    """Returns a strategy which generates integers.

    If min_value is not None then all values will be >= min_value. If
    max_value is not None then all values will be <= max_value

    Examples from this strategy will shrink towards zero, and negative values
    will also shrink towards positive (i.e. -n may be replaced by +n).
    """
    check_valid_bound(min_value, "min_value")
    check_valid_bound(max_value, "max_value")
    check_valid_interval(min_value, max_value, "min_value", "max_value")

    if min_value is not None:
        if min_value != int(min_value):
            raise InvalidArgument(
                "min_value=%r of type %r cannot be exactly represented as an integer."
                % (min_value, type(min_value))
            )
        min_value = int(min_value)
    if max_value is not None:
        if max_value != int(max_value):
            raise InvalidArgument(
                "max_value=%r of type %r cannot be exactly represented as an integer."
                % (max_value, type(max_value))
            )
        max_value = int(max_value)

    return IntegersStrategy(min_value, max_value)


SIGNALING_NAN = int_to_float(0x7FF8_0000_0000_0001)  # nonzero mantissa
assert math.isnan(SIGNALING_NAN) and math.copysign(1, SIGNALING_NAN) == 1

NASTY_FLOATS = sorted(
    [
        0.0,
        0.5,
        1.1,
        1.5,
        1.9,
        1.0 / 3,
        10e6,
        10e-6,
        1.175494351e-38,
        next_up(0.0),
        float_info.min,
        float_info.max,
        3.402823466e38,
        9007199254740992,
        1 - 10e-6,
        2 + 10e-6,
        1.192092896e-07,
        2.2204460492503131e-016,
    ]
    + [2.0**-n for n in (24, 14, 149, 126)]  # minimum (sub)normals for float16,32
    + [float_info.min / n for n in (2, 10, 1000, 100_000)]  # subnormal in float64
    + [math.inf, math.nan] * 5
    + [SIGNALING_NAN],
    key=flt.float_to_lex,
)
NASTY_FLOATS = list(map(float, NASTY_FLOATS))
NASTY_FLOATS.extend([-x for x in NASTY_FLOATS])

FLOAT_STRATEGY_DO_DRAW_LABEL = calc_label_from_name(
    "getting another float in FloatStrategy"
)


class FloatStrategy(SearchStrategy):
    """Generic superclass for strategies which produce floats."""

    def __init__(self, allow_infinity, allow_nan, allow_subnormal, width):
        super().__init__()
        assert isinstance(allow_infinity, bool)
        assert isinstance(allow_nan, bool)
        assert isinstance(allow_subnormal, bool)
        assert width in (16, 32, 64)
        self.allow_infinity = allow_infinity
        self.allow_nan = allow_nan
        self.allow_subnormal = allow_subnormal
        self.width = width

        self.nasty_floats = [
            float_of(f, self.width) for f in NASTY_FLOATS if self.permitted(f)
        ]
        weights = [0.2 * len(self.nasty_floats)] + [0.8] * len(self.nasty_floats)
        self.sampler = d.Sampler(weights)

    def __repr__(self):
        return f"{self.__class__.__name__}(allow_infinity={self.allow_infinity}, allow_nan={self.allow_nan}, width={self.width})"

    def permitted(self, f):
        assert isinstance(f, float)
        if not self.allow_infinity and math.isinf(f):
            return False
        if not self.allow_nan and math.isnan(f):
            return False
        if self.width < 64:
            try:
                float_of(f, self.width)
            except OverflowError:
                return False
        return bool(
            self.allow_subnormal
            or not 0 < abs(f) < width_smallest_normals[self.width]
        )

    def do_draw(self, data):
        while True:
            data.start_example(FLOAT_STRATEGY_DO_DRAW_LABEL)
            i = self.sampler.sample(data)
            if i == 0:
                result = flt.draw_float(data)
            else:
                result = self.nasty_floats[i - 1]
                flt.write_float(data, result)
            if self.permitted(result):
                data.stop_example()
                return float_of(result, self.width) if self.width < 64 else result
            data.stop_example(discard=True)


class FixedBoundedFloatStrategy(SearchStrategy):
    """A strategy for floats distributed between two endpoints.

    The conditional distribution tries to produce values clustered
    closer to one of the ends.
    """

    def __init__(self, lower_bound, upper_bound, allow_subnormal, width):
        super().__init__()
        assert isinstance(lower_bound, float)
        assert isinstance(upper_bound, float)
        assert 0 <= lower_bound < upper_bound
        assert math.copysign(1, lower_bound) == 1, "lower bound may not be -0.0"
        assert width in (16, 32, 64)
        self.lower_bound = lower_bound
        self.upper_bound = upper_bound
        self.allow_subnormal = allow_subnormal
        self.width = width

    def __repr__(self):
        return f"FixedBoundedFloatStrategy({self.lower_bound}, {self.upper_bound}, {self.width})"

    def do_draw(self, data):
        f = self.lower_bound + (
            self.upper_bound - self.lower_bound
        ) * d.fractional_float(data)
        if self.width < 64:
            f = float_of(f, self.width)
        assume(self.lower_bound <= f <= self.upper_bound)
        if not self.allow_subnormal:
            assume(f == 0 or abs(f) >= width_smallest_normals[self.width])
        return f


@cacheable
@defines_strategy(force_reusable_values=True)
def floats(
    min_value: Optional[Real] = None,
    max_value: Optional[Real] = None,
    *,
    allow_nan: Optional[bool] = None,
    allow_infinity: Optional[bool] = None,
    allow_subnormal: Optional[bool] = None,
    width: int = 64,
    exclude_min: bool = False,
    exclude_max: bool = False,
) -> SearchStrategy[float]:
    """Returns a strategy which generates floats.

    - If min_value is not None, all values will be ``>= min_value``
      (or ``> min_value`` if ``exclude_min``).
    - If max_value is not None, all values will be ``<= max_value``
      (or ``< max_value`` if ``exclude_max``).
    - If min_value or max_value is not None, it is an error to enable
      allow_nan.
    - If both min_value and max_value are not None, it is an error to enable
      allow_infinity.
    - If inferred values range does not include subnormal values, it is an error
      to enable allow_subnormal.

    Where not explicitly ruled out by the bounds,
    :wikipedia:`subnormals <Subnormal_number>`, infinities, and NaNs are possible
    values generated by this strategy.

    The width argument specifies the maximum number of bits of precision
    required to represent the generated float. Valid values are 16, 32, or 64.
    Passing ``width=32`` will still use the builtin 64-bit ``float`` class,
    but always for values which can be exactly represented as a 32-bit float.

    The exclude_min and exclude_max argument can be used to generate numbers
    from open or half-open intervals, by excluding the respective endpoints.
    Excluding either signed zero will also exclude the other.
    Attempting to exclude an endpoint which is None will raise an error;
    use ``allow_infinity=False`` to generate finite floats.  You can however
    use e.g. ``min_value=-math.inf, exclude_min=True`` to exclude only
    one infinite endpoint.

    Examples from this strategy have a complicated and hard to explain
    shrinking behaviour, but it tries to improve "human readability". Finite
    numbers will be preferred to infinity and infinity will be preferred to
    NaN.
    """
    check_type(bool, exclude_min, "exclude_min")
    check_type(bool, exclude_max, "exclude_max")

    if allow_nan is None:
        allow_nan = min_value is None and max_value is None
    elif allow_nan and (min_value is not None or max_value is not None):
        raise InvalidArgument(
            f"Cannot have allow_nan={allow_nan!r}, with min_value or max_value"
        )

    if width not in (16, 32, 64):
        raise InvalidArgument(
            f"Got width={width!r}, but the only valid values "
            "are the integers 16, 32, and 64."
        )

    check_valid_bound(min_value, "min_value")
    check_valid_bound(max_value, "max_value")

    if math.copysign(1.0, -0.0) == 1.0:  # pragma: no cover
        raise FloatingPointError(
            "You Python install can't represent -0.0, which is required by the "
            "IEEE-754 floating-point specification.  This is probably because it was "
            "compiled with an unsafe option like -ffast-math; for a more detailed "
            "explanation see https://simonbyrne.github.io/notes/fastmath/"
        )
    if allow_subnormal and next_up(0.0, width=width) == 0:  # pragma: no cover
        # Not worth having separate CI envs and dependencies just to cover this branch;
        # discussion in https://github.com/HypothesisWorks/hypothesis/issues/3092
        #
        # Erroring out here ensures that the database contents are interpreted
        # consistently - which matters for such a foundational strategy, even if it's
        # not always true for all user-composed strategies further up the stack.
        raise FloatingPointError(
            f"Got allow_subnormal={allow_subnormal!r}, but we can't represent "
            f"subnormal floats right now, in violation of the IEEE-754 floating-point "
            f"specification.  This is usually because something was compiled with "
            f"-ffast-math or a similar option, which sets global processor state.  "
            f"See https://simonbyrne.github.io/notes/fastmath/ for a more detailed "
            f"writeup - and good luck!"
        )

    min_arg, max_arg = min_value, max_value
    if min_value is not None:
        min_value = float_of(min_value, width)
        assert isinstance(min_value, float)
    if max_value is not None:
        max_value = float_of(max_value, width)
        assert isinstance(max_value, float)

    if min_value != min_arg:
        raise InvalidArgument(
            f"min_value={min_arg!r} cannot be exactly represented as a float "
            f"of width {width} - use min_value={min_value!r} instead."
        )
    if max_value != max_arg:
        raise InvalidArgument(
            f"max_value={max_arg!r} cannot be exactly represented as a float "
            f"of width {width} - use max_value={max_value!r} instead."
        )

    if exclude_min and (min_value is None or min_value == math.inf):
        raise InvalidArgument(f"Cannot exclude min_value={min_value!r}")
    if exclude_max and (max_value is None or max_value == -math.inf):
        raise InvalidArgument(f"Cannot exclude max_value={max_value!r}")

    assumed_allow_subnormal = allow_subnormal is None or allow_subnormal
    if min_value is not None and (
        exclude_min or (min_arg is not None and min_value < min_arg)
    ):
        min_value = next_up_normal(min_value, width, assumed_allow_subnormal)
        if min_value == min_arg:
            assert min_value == min_arg == 0
            assert is_negative(min_arg) and not is_negative(min_value)
            min_value = next_up_normal(min_value, width, assumed_allow_subnormal)
        assert min_value > min_arg  # type: ignore
    if max_value is not None and (
        exclude_max or (max_arg is not None and max_value > max_arg)
    ):
        max_value = next_down_normal(max_value, width, assumed_allow_subnormal)
        if max_value == max_arg:
            assert max_value == max_arg == 0
            assert is_negative(max_value) and not is_negative(max_arg)
            max_value = next_down_normal(max_value, width, assumed_allow_subnormal)
        assert max_value < max_arg  # type: ignore

    if min_value == -math.inf:
        min_value = None
    if max_value == math.inf:
        max_value = None

    bad_zero_bounds = (
        min_value == max_value == 0
        and is_negative(max_value)
        and not is_negative(min_value)
    )
    if (
        min_value is not None
        and max_value is not None
        and (min_value > max_value or bad_zero_bounds)
    ):
        # This is a custom alternative to check_valid_interval, because we want
        # to include the bit-width and exclusion information in the message.
        msg = (
            "There are no %s-bit floating-point values between min_value=%r "
            "and max_value=%r" % (width, min_arg, max_arg)
        )
        if exclude_min or exclude_max:
            msg += f", exclude_min={exclude_min!r} and exclude_max={exclude_max!r}"
        raise InvalidArgument(msg)

    if allow_infinity is None:
        allow_infinity = min_value is None or max_value is None
    elif allow_infinity:
        if min_value is not None and max_value is not None:
            raise InvalidArgument(
                f"Cannot have allow_infinity={allow_infinity!r}, "
                "with both min_value and max_value"
            )
    elif min_value == math.inf:
        if min_arg == math.inf:
            raise InvalidArgument("allow_infinity=False excludes min_value=inf")
        raise InvalidArgument(
            f"exclude_min=True turns min_value={min_arg!r} into inf, "
            "but allow_infinity=False"
        )
    elif max_value == -math.inf:
        if max_arg == -math.inf:
            raise InvalidArgument("allow_infinity=False excludes max_value=-inf")
        raise InvalidArgument(
            f"exclude_max=True turns max_value={max_arg!r} into -inf, "
            "but allow_infinity=False"
        )

    smallest_normal = width_smallest_normals[width]
    if allow_subnormal is None:
        if min_value is not None and max_value is not None:
            if min_value == max_value:
                allow_subnormal = -smallest_normal < min_value < smallest_normal
            else:
                allow_subnormal = (
                    min_value < smallest_normal and max_value > -smallest_normal
                )
        elif min_value is not None:
            allow_subnormal = min_value < smallest_normal
        elif max_value is not None:
            allow_subnormal = max_value > -smallest_normal
        else:
            allow_subnormal = True
    if allow_subnormal:
        if min_value is not None and min_value >= smallest_normal:
            raise InvalidArgument(
                f"allow_subnormal=True, but minimum value {min_value} "
                f"excludes values below float{width}'s "
                f"smallest positive normal {smallest_normal}"
            )
        if max_value is not None and max_value <= -smallest_normal:
            raise InvalidArgument(
                f"allow_subnormal=True, but maximum value {max_value} "
                f"excludes values above float{width}'s "
                f"smallest negative normal {-smallest_normal}"
            )

    # Any type hint silences mypy when we unpack these parameters
    kw: Any = {"allow_subnormal": allow_subnormal, "width": width}
    unbounded_floats = FloatStrategy(
        allow_infinity=allow_infinity, allow_nan=allow_nan, **kw
    )
    if min_value is None and max_value is None:
        return unbounded_floats
    elif min_value is not None and max_value is not None:
        if min_value == max_value:
            assert isinstance(min_value, float)
            result = just(min_value)
        elif is_negative(min_value):
            if is_negative(max_value):
                return floats(min_value=-max_value, max_value=-min_value, **kw).map(
                    operator.neg
                )
            else:
                return floats(min_value=0.0, max_value=max_value, **kw) | floats(
                    min_value=0.0, max_value=-min_value, **kw
                ).map(
                    operator.neg  # type: ignore
                )
        elif (
            count_between_floats(min_value, max_value, width) > 1000
            or not allow_subnormal
        ):
            return FixedBoundedFloatStrategy(
                lower_bound=min_value, upper_bound=max_value, **kw
            )
        else:
            ub_int = float_to_int(max_value, width)
            lb_int = float_to_int(min_value, width)
            assert lb_int <= ub_int
            result = integers(min_value=lb_int, max_value=ub_int).map(
                lambda x: int_to_float(x, width)
            )
    elif min_value is not None:
        assert isinstance(min_value, float)
        if is_negative(min_value):
            # Ignore known bug https://github.com/python/mypy/issues/6697
            return unbounded_floats.map(abs) | floats(  # type: ignore
                min_value=min_value, max_value=-0.0, **kw
            )
        else:
            result = unbounded_floats.map(lambda x: min_value + abs(x))
    else:
        assert isinstance(max_value, float)
        if not is_negative(max_value):
            return floats(
                min_value=0.0, max_value=max_value, **kw
            ) | unbounded_floats.map(lambda x: -abs(x))
        else:
            result = unbounded_floats.map(lambda x: max_value - abs(x))

    if width < 64:

        def downcast(x):
            try:
                return float_of(x, width)
            except OverflowError:  # pragma: no cover
                reject()

        result = result.map(downcast)
    if not allow_infinity:
        result = result.filter(lambda x: not math.isinf(x))
    return result
