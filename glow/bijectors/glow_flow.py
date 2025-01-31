"""GlowFlow bijector flow."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf
import tensorflow_probability as tfp
import numpy as np

from .squeeze import Squeeze
from .parallel import Parallel


__all__ = [
    "GlowFlow",
]


tfb = tfp.bijectors


class GlowStep(tfb.Bijector):
    """TODO"""

    def __init__(self,
                 input_shape=None,
                 depth=3,
                 filters=64, #512
                 kernel_size=(3,3),
                 validate_args=False,
                 inverse_min_event_ndims=3,
                 forward_min_event_ndims=3,
                 name="glow_bijector",
                 *args, **kwargs):
        """Instantiates `GlowStep`, a single bijective step of `GlowFlow`.
        Args:
            TODO
            validate_args: Python `bool` indicating whether arguments should be
                checked for correctness.
            name: Python `str` name given to ops managed by this object.
        Raises:
            ValueError: if TODO happens
        """
        self._graph_parents = []
        self._name = name
        self._validate_args = validate_args

        self._depth = depth
        self._filters = filters
        self._kernel_size = kernel_size
        self._input_shape = input_shape

        self.built = False

        super(GlowStep, self).__init__(
            *args,
            validate_args=validate_args,
            name=name,
            inverse_min_event_ndims=inverse_min_event_ndims,
            forward_min_event_ndims=forward_min_event_ndims,
            **kwargs)

    def build(self, input_shape):
        self._input_shape = input_shape
        self._image_shape = input_shape[1:]

        flow_parts = []
        for i in range(self._depth):
            # It seems like the activation_normalization is just a
            # regular batch_normalization with axis=-1. Also, in the original
            # glow-paper, Kingma et al. do data dependent initialization,
            # which I don't do here.
            activation_normalization = tfb.BatchNormalization(
                batchnorm_layer=tf.layers.BatchNormalization(axis=-1))

            #convolution_permute = ConvolutionPermute(
            #    name=self._name + '/convolution_permute_{}'.format(i))
            convolution_permute = trainable_lu_factorization(
                    event_size=input_shape[-1], name=self._name + '/convolution_permute_{}'.format(i))

            # We need to reshape because `tfb.RealNVP` only supports 1d input
            # TODO(hartikainen): This should not require inverting
            flatten = tfb.Reshape(
                event_shape_in=(np.prod(self._image_shape),),
                event_shape_out=list(self._image_shape))
            affine_coupling = tfb.RealNVP(
                num_masked=np.prod(self._image_shape[:2])*(self._image_shape[2]//2),
                shift_and_log_scale_fn=glow_resnet_template(
                    image_shape=self._image_shape,
                    filters=(self._filters, self._filters),
                    kernel_sizes=(self._kernel_size, self._kernel_size),
                    activation=tf.nn.relu))
            # TODO(hartikainen): This should not require inverting
            unflatten = tfb.Reshape(
                event_shape_in=list(self._image_shape),
                event_shape_out=(np.prod(self._image_shape),))

            flow_parts += [
                activation_normalization,
                convolution_permute,
                flatten,
                affine_coupling,
                unflatten,
            ]

        # Note: tfb.Chain applies the list of bijectors in the _reverse_ order
        # of what they are inputted.
        # self.flow = tfb.Chain(list(reversed(flow_parts)))
        self.flow = tfb.Chain(flow_parts)

        self.built = True

    def _forward(self, x):
        if not self.built:
            self.build(x.get_shape())

        return self.flow.forward(x)

    def _inverse(self, y):
        if not self.built:
            self.build(y.get_shape())

        return self.flow.inverse(y)

    def _forward_log_det_jacobian(self, x):
        if not self.built:
            self.build(x.get_shape())

        x = self.flow.forward_log_det_jacobian(x, event_ndims=self.forward_min_event_ndims)
        return self.flow.forward_log_det_jacobian(x, event_ndims=self.forward_min_event_ndims)

    def _inverse_log_det_jacobian(self, y):
        if not self.built:
            self.build(y.get_shape())

        x = self.flow.inverse_log_det_jacobian(y, event_ndims=self.inverse_min_event_ndims)
        return  self.flow.inverse_log_det_jacobian(y, event_ndims=self.inverse_min_event_ndims)


class GlowFlow(tfb.Bijector):
    """TODO"""

    def __init__(self,
                 num_levels=2,
                 level_depth=2,
                 validate_args=False,
                 inverse_min_event_ndims=3,
                 forward_min_event_ndims=3,
                 name="glow_flow",
                 *args, **kwargs):
        """Instantiates the `GlowFlow` normalizing flow.
        Args:
            TODO
            validate_args: Python `bool` indicating whether arguments should be
                checked for correctness.
            name: Python `str` name given to ops managed by this object.
        Raises:
            ValueError: if TODO happens
        """
        self._graph_parents = []
        self._name = name
        self._validate_args = validate_args

        self._num_levels = num_levels
        self._level_depth = level_depth

        self.built = False

        super(GlowFlow, self).__init__(
            *args,
            validate_args=validate_args,
            name=name,
            inverse_min_event_ndims=inverse_min_event_ndims,
            forward_min_event_ndims=forward_min_event_ndims,
            **kwargs)

    def build(self, input_shape):
        self._input_shape = input_shape
        self._image_shape = input_shape[1:]

        levels = []
        for i in range(self._num_levels):
            # Every level split the input in half (on the channel-axis),
            # and applies the next level only to the half of the split.
            # The other half flows directly into the output z. NOTE:
            # In glow implementation, Kingma et al. parameterize the z
            # based on the previous levels. They don't mention this in the
            # paper however.
            # See: https://github.com/openai/glow/blob/master/model.py#L485
            levels.append(
                tfb.Chain([
                    tfb.Invert(Squeeze(factor=2**(i+1))),
                    Parallel(
                        bijectors=[
                            GlowStep(
                                # Infer at the time of first forward
                                input_shape=None,
                                depth=self._level_depth,
                                name="glow_step_{}".format(i)),
                            tfb.Identity(),
                        ],
                        split_axis=-1,
                        split_proportions=[1, 2**(i)-1]
                    ),
                    Squeeze(factor=2**(i+1))
                ])
            )

        # Note: tfb.Chain applies the list of bijectors in the _reverse_ order
        # of what they are inputted.
        self.levels = levels
        # self.flow = tfb.Chain(list(reversed(levels)))
        self.flow = tfb.Chain(levels)
        self.built = True

    def _forward(self, x):
        if not self.built:
            self.build(x.get_shape())

        return self.flow.forward(x)

    def _inverse(self, y):
        if not self.built:
            self.build(y.get_shape())

        return self.flow.inverse(y)

    def _forward_log_det_jacobian(self, x):
        if not self.built:
            self.build(x.get_shape())

        return self.flow.forward_log_det_jacobian(x, event_ndims=self.forward_min_event_ndims)

    def _inverse_log_det_jacobian(self, y):
        if not self.built:
            self.build(y.get_shape())

        return  self.flow.inverse_log_det_jacobian(y, event_ndims=self.inverse_min_event_ndims)

    def _maybe_assert_valid_x(self, x):
        """TODO"""
        if not self.validate_args:
            return x
        raise NotImplementedError("_maybe_assert_valid_x")

    def _maybe_assert_valid_y(self, y):
        """TODO"""
        if not self.validate_args:
            return y
        raise NotImplementedError("_maybe_assert_valid_y")


def glow_resnet_template(
        image_shape,
        filters=(512, 512),
        kernel_sizes=((3, 3), (3, 3)),
        shift_only=False,
        activation=tf.nn.relu,
        name=None,
        *args,
        **kwargs):
    """Build a scale-and-shift functions using a weight normalized resnet.
    This will be wrapped in a make_template to ensure the variables are only
    created once. It takes the `d`-dimensional input x[0:d] and returns the
    `D-d` dimensional outputs `loc` ("mu") and `log_scale` ("alpha").
    Arguments:
        TODO
    Returns:
        shift: `Float`-like `Tensor` of shift terms.
        log_scale: `Float`-like `Tensor` of log(scale) terms.
    Raises:
        NotImplementedError: if rightmost dimension of `inputs` is unknown prior
            to graph execution.
    #### References
    TODO
    """

    with tf.name_scope(name, "glow_resnet_template"):
        def _fn(x, output_units=None):
            """Resnet parameterized via `glow_resnet_template`."""

            output_units = output_units or image_shape[-1]

            x = tf.reshape(
                x, [-1] + image_shape[:2].as_list() + [(np.prod(image_shape) - output_units)//np.prod(image_shape[:2])])

            for filter_size, kernel_size in zip(filters, kernel_sizes):
                x = tf.layers.conv2d(
                    inputs=x,
                    filters=filter_size,
                    kernel_size=kernel_size,
                    strides=(1, 1),
                    padding='same',
                    kernel_initializer=tf.random_normal_initializer(0.0, 0.05),
                    kernel_constraint=lambda kernel: (
                        tf.nn.l2_normalize(
                            kernel, list(range(kernel.shape.ndims-1)))))

                x = tf.layers.batch_normalization(x, axis=-1)
                x = activation(x)

            output_filters = (1 if shift_only else 2) * (
                output_units // np.prod(image_shape[:2]))
            x = tf.layers.conv2d(
                inputs=x,
                filters=output_filters,
                kernel_size=(3, 3),
                strides=(1, 1),
                padding='same',
                kernel_initializer=tf.zeros_initializer())
            x = tf.layers.batch_normalization(x, axis=-1)

            x = tf.reshape(x, [-1, np.prod(image_shape[:2]) * output_filters])

            if shift_only:
                return x, None

            shift, log_scale = tf.split(x, 2, axis=-1)
            return shift, log_scale

        return tf.make_template("glow_resnet_template", _fn)

def trainable_lu_factorization(event_size, batch_shape=(), seed=None, dtype=tf.float32, name=None):
  with tf.name_scope(name, 'trainable_lu_factorization',
                     [event_size, batch_shape]):
    event_size = tf.convert_to_tensor(
        event_size, preferred_dtype=tf.int32, name='event_size')
    batch_shape = tf.convert_to_tensor(
        batch_shape, preferred_dtype=event_size.dtype, name='batch_shape')
    random_matrix = tf.random_uniform(
        shape=tf.concat([batch_shape, [event_size, event_size]], axis=0),
        dtype=dtype,
        seed=seed)
    random_orthonormal = tf.linalg.qr(random_matrix)[0]
    lower_upper, permutation = tf.linalg.lu(random_orthonormal)
    lower_upper = tf.Variable(
        initial_value=lower_upper,
        trainable=True,
        use_resource=True,
        name='lower_upper')
  return tfb.MatvecLU(lower_upper, permutation, validate_args=True)
