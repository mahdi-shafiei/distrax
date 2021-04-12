# Copyright 2021 DeepMind Technologies Limited. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Distribution representing a Bijector applied to a Distribution."""

from typing import Optional, Tuple

from distrax._src.bijectors import bijector as bjct_base
from distrax._src.distributions import distribution as dist_base
from distrax._src.utils import conversion
import jax
import jax.numpy as jnp


PRNGKey = dist_base.PRNGKey
Array = dist_base.Array
DistributionLike = dist_base.DistributionLike
BijectorLike = bjct_base.BijectorLike


class Transformed(dist_base.Distribution):
  """Distribution of a random variable transformed by a bijective function.

  Let `X` be a continuous random variable and `Y = f(X)` be a random variable
  transformed by a differentiable bijection `f` (a "bijector"). Given the
  distribution of `X` (the "base distribution") and the bijector `f`, this class
  implements the distribution of `Y` (also known as the pushforward of the base
  distribution through `f`).

  The probability density of `Y` can be computed by:

  `log p(y) = log p(x) - log|det J(f)(x)|`

  where `p(x)` is the probability density of `X` (the "base density") and
  `J(f)(x)` is the Jacobian matrix of `f`, both evaluated at `x = f^{-1}(y)`.

  Note: the `batch_shape`, `event_shape`, and `dtype` properties of the
  transformed distribution are computed on-demand via JAX tracing when
  requested. This assumes that the `forward` function of the bijector is
  traceable; that is, it is a pure function that does not contain run-time
  branching. Functions that do not strictly meet this requirement can still be
  used, but we cannot guarantee that the shape and dtype properties of the
  resulting distribution can be correctly inferred.
  """

  def __init__(self, distribution: DistributionLike, bijector: BijectorLike):
    """Initializes a Transformed distribution.

    Args:
      distribution: the base distribution. Can be either a Distrax distribution
        or a TFP distribution.
      bijector: a differentiable bijective transformation. Can be a Distrax
        bijector, a TFP bijector, or a callable to be wrapped by `Lambda`.
    """
    super().__init__()
    distribution = conversion.as_distribution(distribution)
    bijector = conversion.as_bijector(bijector)

    if len(distribution.event_shape) != bijector.event_ndims_in:
      raise ValueError(
          f"Base distribution '{distribution.name}' has event shape "
          f"{distribution.event_shape}, but bijector '{bijector.name}' expects "
          f"events to have {bijector.event_ndims_in} dimensions. Perhaps use "
          f"`distrax.Block` or `distrax.Independent`?")

    self._distribution = distribution
    self._bijector = bijector
    self._batch_shape = None
    self._event_shape = None
    self._dtype = None

  @property
  def distribution(self):
    """The base distribution."""
    return self._distribution

  @property
  def bijector(self):
    """The bijector representing the transformation."""
    return self._bijector

  def _infer_shapes_and_dtype(self):
    """Infer the batch shape, event shape, and dtype by tracing `forward`."""
    dummy_shape = self.distribution.batch_shape + self.distribution.event_shape
    dummy = jnp.zeros(dummy_shape, dtype=self.distribution.dtype)
    shape_dtype = jax.eval_shape(self.bijector.forward, dummy)

    self._dtype = shape_dtype.dtype

    if self.bijector.event_ndims_out == 0:
      self._event_shape = ()
      self._batch_shape = shape_dtype.shape
    else:
      self._event_shape = shape_dtype.shape[-self.bijector.event_ndims_out:]
      self._batch_shape = shape_dtype.shape[:-self.bijector.event_ndims_out]

  @property
  def dtype(self) -> jnp.dtype:
    """See `Distribution.dtype`."""
    if self._dtype is None:
      self._infer_shapes_and_dtype()
    return self._dtype

  @property
  def event_shape(self) -> Tuple[int, ...]:
    """See `Distribution.event_shape`."""
    if self._event_shape is None:
      self._infer_shapes_and_dtype()
    return self._event_shape

  @property
  def batch_shape(self) -> Tuple[int, ...]:
    """See `Distribution.batch_shape`."""
    if self._batch_shape is None:
      self._infer_shapes_and_dtype()
    return self._batch_shape

  def log_prob(self, y: Array) -> Array:
    """See `Distribution.log_prob`."""
    x, ildj_y = self.bijector.inverse_and_log_det(y)
    lp_x = self.distribution.log_prob(x)
    lp_y = lp_x + ildj_y
    return lp_y

  def _sample_n(self, key: PRNGKey, n: int) -> Array:
    """Returns `n` samples."""
    x = self.distribution.sample(seed=key, sample_shape=n)
    y = jax.vmap(self.bijector.forward)(x)
    return y

  def _sample_n_and_log_prob(self, key: PRNGKey, n: int) -> Tuple[Array, Array]:
    """Returns `n` samples and their log probs.

    This function is more efficient than calling `sample` and `log_prob`
    separately, because it uses only the forward methods of the bijector. It
    also works for bijectors that don't implement inverse methods.

    Args:
      key: PRNG key.
      n: Number of samples to generate.

    Returns:
      A tuple of `n` samples and their log probs.
    """
    x, lp_x = self.distribution.sample_and_log_prob(seed=key, sample_shape=n)
    y, fldj = jax.vmap(self.bijector.forward_and_log_det)(x)
    lp_y = jax.vmap(jnp.subtract)(lp_x, fldj)
    return y, lp_y

  def mean(self) -> Array:
    """Calculates the mean."""
    if self.bijector.is_constant_jacobian:
      return self.bijector.forward(self.distribution.mean())
    else:
      raise NotImplementedError(
          "`mean` is not implemented for this transformed distribution, "
          "because its bijector's Jacobian is not known to be constant.")

  def mode(self) -> Array:
    """Calculates the mode."""
    if self.bijector.is_constant_log_det:
      return self.bijector.forward(self.distribution.mode())
    else:
      raise NotImplementedError(
          "`mode` is not implemented for this transformed distribution, "
          "because its bijector's Jacobian determinant is not known to be "
          "constant.")

  def entropy(self, input_hint: Optional[Array] = None) -> Array:
    """Calculates the Shannon entropy (in Nats).

    Only works for bijectors with constant Jacobian determinant.

    Args:
      input_hint: an example sample from the base distribution, used to compute
        the constant forward log-determinant. If not specified, it is computed
        using a zero array of the shape and dtype of a sample from the base
        distribution.

    Returns:
      the entropy of the distribution.
    """
    if self.bijector.is_constant_log_det:
      if input_hint is None:
        shape = self.distribution.batch_shape + self.distribution.event_shape
        input_hint = jnp.zeros(shape, dtype=self.distribution.dtype)
      entropy = self.distribution.entropy()
      fldj = self.bijector.forward_log_det_jacobian(input_hint)
      return entropy + fldj
    else:
      raise NotImplementedError(
          "`entropy` is not implemented for this transformed distribution, "
          "because its bijector's Jacobian determinant is not known to be "
          "constant.")