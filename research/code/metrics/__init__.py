#  ~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~
#  MIT License
#
#  Copyright (c) 2021 Nathan Juraj Michlo
#
#  Permission is hereby granted, free of charge, to any person obtaining a copy
#  of this software and associated documentation files (the "Software"), to deal
#  in the Software without restriction, including without limitation the rights
#  to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
#  copies of the Software, and to permit persons to whom the Software is
#  furnished to do so, subject to the following conditions:
#
#  The above copyright notice and this permission notice shall be included in
#  all copies or substantial portions of the Software.
#
#  THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
#  IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
#  FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
#  AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
#  LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
#  OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
#  SOFTWARE.
#  ~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~

# Nathan Michlo et. al
from research.code.metrics._flatness import metric_flatness
from research.code.metrics._flatness_components import metric_flatness_components


# ========================================================================= #
# Fast Metric Settings                                                      #
# ========================================================================= #


# helper imports
from disent.util.function import wrapped_partial as _wrapped_partial


# TODO: register with disent experiments
FAST_METRICS = {
    'flatness':            _wrapped_partial(metric_flatness,            factor_repeats=128),
    'flatness_components': _wrapped_partial(metric_flatness_components, factor_repeats=128),
    'distances':           _wrapped_partial(metric_flatness_components, factor_repeats=128, compute_distances=True, compute_linearity=False),
    'linearity':           _wrapped_partial(metric_flatness_components, factor_repeats=128, compute_distances=False, compute_linearity=True),
}


# TODO: register with disent experiments
DEFAULT_METRICS = {
    'flatness':            metric_flatness,
    'flatness_components': metric_flatness_components,
    'distances':           _wrapped_partial(metric_flatness_components, compute_distances=True, compute_linearity=False),
    'linearity':           _wrapped_partial(metric_flatness_components, compute_distances=False, compute_linearity=True),
}
