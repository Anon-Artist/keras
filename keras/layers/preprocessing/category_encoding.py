# Copyright 2020 The TensorFlow Authors. All Rights Reserved.
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
"""Keras CategoryEncoding preprocessing layer."""
# pylint: disable=g-classes-have-attributes
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf

import collections
import json
import numbers

import numpy as np
from keras import backend as K
from keras.engine import base_preprocessing_layer
from keras.engine.input_spec import InputSpec
from keras.utils import layer_utils
from tensorflow.python.util.tf_export import keras_export

TFIDF = "tf-idf"
INT = "int"
BINARY = "binary"
COUNT = "count"

# The string tokens in the extracted vocabulary
_NUM_ELEMENTS_NAME = "num_elements"
# The inverse-document-frequency weights
_IDF_NAME = "idf"


@keras_export("keras.layers.experimental.preprocessing.CategoryEncoding", v1=[])
class CategoryEncoding(base_preprocessing_layer.CombinerPreprocessingLayer):
  """Category encoding layer.

  This layer provides options for condensing data into a categorical encoding.
  It accepts integer values as inputs and outputs a dense representation
  (one sample = 1-index tensor of float values representing data about the
  sample's tokens) of those inputs.

  Examples:

  **Multi-hot encoding data if you know in advance the number of tokens**

  In this case, you can pass the `max_tokens` argument to the constructor.

  >>> layer = tf.keras.layers.experimental.preprocessing.CategoryEncoding(
  ...           max_tokens=4, output_mode="binary")
  >>> layer([[0, 1], [0, 0], [1, 2], [3, 1]])
  <tf.Tensor: shape=(4, 4), dtype=float32, numpy=
    array([[1., 1., 0., 0.],
           [1., 0., 0., 0.],
           [0., 1., 1., 0.],
           [0., 1., 0., 1.]], dtype=float32)>

  **Multi-hot encoding data where the number of tokens is unknown**

  In this case, you should `adapt()` the layer on a sample dataset.

  ```python
  layer = CategoryEncoding(output_mode="binary")
  layer.adapt(sample_dataset)  # Indexes the vocabulary of the data
  outputs = layer(inputs)
  ```

  **Using weighted inputs in `count` mode**

  >>> layer = tf.keras.layers.experimental.preprocessing.CategoryEncoding(
  ...           max_tokens=4, output_mode="count")
  >>> count_weights = np.array([[.1, .2], [.1, .1], [.2, .3], [.4, .2]])
  >>> layer([[0, 1], [0, 0], [1, 2], [3, 1]], count_weights=count_weights)
  <tf.Tensor: shape=(4, 4), dtype=float64, numpy=
    array([[0.1, 0.2, 0. , 0. ],
           [0.2, 0. , 0. , 0. ],
           [0. , 0.2, 0.3, 0. ],
           [0. , 0.2, 0. , 0.4]])>

  Args:
    max_tokens: The maximum size of the vocabulary for this layer. If None,
      there is no cap on the size of the vocabulary.
    output_mode: Specification for the output of the layer.
      Defaults to "binary". Values can
      be "binary", "count" or "tf-idf", configuring the layer as follows:
        "binary": Outputs a single int array per batch, of either vocab_size or
          max_tokens size, containing 1s in all elements where the token mapped
          to that index exists at least once in the batch item.
        "count": As "binary", but the int array contains a count of the number
          of times the token at that index appeared in the batch item.
        "tf-idf": As "binary", but the TF-IDF algorithm is applied to find the
          value in each token slot.
    sparse: Boolean. If true, returns a `SparseTensor` instead of a dense
      `Tensor`. Defaults to `False`.

  Call arguments:
    inputs: A 2D tensor `(samples, timesteps)`.
    count_weights: A 2D tensor in the same shape as `inputs` indicating the
      weight for each sample value when summing up in `count` mode. Not used in
      `binary` or `tfidf` mode.
  """

  def __init__(self,
               max_tokens=None,
               output_mode=BINARY,
               sparse=False,
               **kwargs):
    # 'output_mode' must be one of (COUNT, BINARY, TFIDF)
    layer_utils.validate_string_arg(
        output_mode,
        allowable_strings=(COUNT, BINARY, TFIDF),
        layer_name="CategoryEncoding",
        arg_name="output_mode")

    # If max_tokens is set, the value must be greater than 1 - otherwise we
    # are creating a 0-element vocab, which doesn't make sense.
    if max_tokens is not None and max_tokens < 1:
      raise ValueError("max_tokens must be > 1.")

    # We need to call super() before we call _add_state_variable().
    combiner = _CategoryEncodingCombiner(
        max_tokens=max_tokens,
        compute_idf=output_mode == TFIDF)
    super(CategoryEncoding, self).__init__(combiner=combiner, **kwargs)
    base_preprocessing_layer.keras_kpl_gauge.get_cell(
        "CategoryEncoding").set(True)

    self.max_tokens = max_tokens
    self.output_mode = output_mode
    self.sparse = sparse
    self._called = False

    if self.output_mode == TFIDF:
      # The TF-IDF weight may have a (None,) tensorshape. This creates
      # a 1D variable with arbitrary shape, which we can assign any weight to
      # so long as it has 1 dimension. In order to properly initialize this
      # weight in Keras, we need to provide a custom callable initializer which
      # does not depend on the shape of the weight (as all other initializers
      # do) since the weight is not known. Hence the lambda shape, dtype: [0].
      if max_tokens is None:
        initializer = lambda shape, dtype: [0]
      else:
        initializer = tf.compat.v1.zeros_initializer

      # We are adding these here instead of in build() since they do not depend
      # on the input shape at all.
      self.tf_idf_weights = self._add_state_variable(
          name=_IDF_NAME,
          shape=tf.TensorShape((max_tokens,)),
          dtype=K.floatx(),
          initializer=initializer)

      self.input_spec = InputSpec(ndim=2)

  def compute_output_shape(self, input_shape):
    return tf.TensorShape([input_shape[0], self.max_tokens])

  def compute_output_signature(self, input_spec):
    output_shape = self.compute_output_shape(input_spec.shape.as_list())
    output_dtype = K.floatx() if self.output_mode == TFIDF else tf.int64
    if self.sparse:
      return tf.SparseTensorSpec(
          shape=output_shape, dtype=output_dtype)
    else:
      return tf.TensorSpec(shape=output_shape, dtype=output_dtype)

  def adapt(self, data, reset_state=True):
    """Fits the state of the preprocessing layer to the dataset.

    Overrides the default adapt method to apply relevant preprocessing to the
    inputs before passing to the combiner.

    Args:
      data: The data to train on. It can be passed either as a tf.data Dataset,
        or as a numpy array.
      reset_state: Optional argument specifying whether to clear the state of
        the layer at the start of the call to `adapt`. This must be True for
        this layer, which does not support repeated calls to `adapt`.

    Raises:
      RuntimeError: if the layer cannot be adapted at this time.
    """
    if not reset_state:
      raise ValueError("CategoryEncoding does not support streaming adapts.")

    super(CategoryEncoding, self).adapt(data, reset_state)

  def _set_state_variables(self, updates):
    if not self.built:
      raise RuntimeError("_set_state_variables() must be called after build().")
    if _NUM_ELEMENTS_NAME in updates:
      if self.max_tokens is None:
        self.set_num_elements(updates[_NUM_ELEMENTS_NAME])
      elif self.max_tokens != updates[_NUM_ELEMENTS_NAME]:
        raise RuntimeError("Cannot update states if you construct the layer "
                           "with `max_tokens`={}".format(self.max_tokens))
    if self.output_mode == TFIDF:
      self.set_tfidf_data(updates[_IDF_NAME])

  def get_config(self):
    config = {
        "max_tokens": self.max_tokens,
        "output_mode": self.output_mode,
        "sparse": self.sparse,
    }
    base_config = super(CategoryEncoding, self).get_config()
    return dict(list(base_config.items()) + list(config.items()))

  def _convert_to_ndarray(self, x):
    if isinstance(x, tf.Tensor):
      return x
    else:
      return np.array(x)

  def _convert_to_sparse_inputs(self, inputs):
    if isinstance(inputs, tf.SparseTensor):
      return inputs
    elif isinstance(inputs, tf.RaggedTensor):
      return inputs.to_sparse()
    else:
      indices = tf.where(
          tf.greater_equal(inputs, tf.constant(0, inputs.dtype)))
      values = tf.compat.v1.gather_nd(inputs, indices)
      shape = tf.compat.v1.shape(inputs, out_type=tf.int64)
      return tf.SparseTensor(indices, values, shape)

  def set_num_elements(self, num_elements):
    if self.max_tokens is not None:
      raise RuntimeError(
          "In order to dynamically set the number of elements, the "
          "layer's 'max_tokens' arg must be set to None.")
    if not isinstance(num_elements, numbers.Integral):
      raise ValueError("num_elements must be a scalar integer.")
    if self._called:
      raise RuntimeError("num_elements cannot be changed after the layer is "
                         "called.")
    self.max_tokens = num_elements

  def set_tfidf_data(self, tfidf_data):
    tfidf_data = self._convert_to_ndarray(tfidf_data)
    if self.output_mode != TFIDF:
      raise RuntimeError(
          "In order to set TF-IDF data, the output mode must be 'tf-idf'.")
    if tfidf_data.ndim != 1:
      raise ValueError("TF-IDF data must be a 1-index array.")
    if self.max_tokens is not None:
      input_data_length = tfidf_data.shape[0]
      if input_data_length > self.max_tokens:
        raise ValueError("The array provided has %d elements. This layer is "
                         "configured to only allow %d elements." %
                         (input_data_length, self.max_tokens))
      if input_data_length < self.max_tokens:
        tfidf_data = np.resize(tfidf_data, (self.max_tokens,))
    K.set_value(self.tf_idf_weights, tfidf_data)

  def call(self, inputs, count_weights=None):
    if isinstance(inputs, (list, np.ndarray)):
      inputs = tf.convert_to_tensor(inputs)
    if inputs.shape.rank == 1:
      inputs = tf.compat.v1.expand_dims(inputs, 1)

    if count_weights is not None and self.output_mode != COUNT:
      raise ValueError("count_weights is not used in `output_mode='tf-idf'`, "
                       "or `output_mode='binary'`. Please pass a single input.")
    self._called = True
    if self.max_tokens is None:
      raise RuntimeError(
          "If you construct a `CategoryEncoding` layer with "
          "`max_tokens=None`, you need to call `adapt()` "
          "on it before using it")
    else:
      out_depth = self.max_tokens

    if self.output_mode == TFIDF:
      # If the input is a sparse tensor, we densify it with the default value of
      # -1. Because -1 is ignored by one_hot, this effectively drops the non-set
      # positions from the output encoding.
      if self.sparse:
        raise ValueError("`sparse=True` with `output_mode=tfidf` "
                         "is not supported.")
      if isinstance(inputs, tf.SparseTensor):
        inputs = tf.sparse.to_dense(inputs, default_value=-1)
      one_hot_data = tf.one_hot(inputs, depth=out_depth)
      counts = tf.reduce_sum(one_hot_data, axis=1)
      tf_idf_data = tf.multiply(counts, self.tf_idf_weights)
      tf_idf_data.set_shape(tf.TensorShape((None, out_depth)))
      return tf_idf_data

    binary_output = (self.output_mode == BINARY)
    if isinstance(inputs, tf.SparseTensor):
      max_value = tf.reduce_max(inputs.values)
      min_value = tf.reduce_min(inputs.values)
    else:
      max_value = tf.reduce_max(inputs)
      min_value = tf.reduce_min(inputs)
    condition = tf.logical_and(
        tf.greater(
            tf.cast(out_depth, max_value.dtype), max_value),
        tf.greater_equal(
            min_value, tf.cast(0, min_value.dtype)))
    tf.Assert(
        condition, ["Input values must be in the range 0 <= values < max_tokens"
                    " with max_tokens={}".format(out_depth)])
    if self.sparse:
      return sparse_bincount(inputs, out_depth, binary_output, count_weights)
    else:
      return dense_bincount(inputs, out_depth, binary_output, count_weights)


class _CategoryEncodingAccumulator(
    collections.namedtuple("Accumulator", ["data", "per_doc_count_dict"])):
  pass


class _CategoryEncodingCombiner(base_preprocessing_layer.Combiner):
  """Combiner for the CategoryEncoding preprocessing layer.

  This class encapsulates the logic for computing the number of elements in the
  input dataset and the document frequency for each element.

  Attributes:
    compute_max_element: (Optional) If set, this combiner will return the
      maximum element in this set as part of its `extract()` call.
    compute_idf: (Optional) If set, the inverse document frequency will be
      computed for each value.
  """
  # These are indices into the accumulator's `data` array.
  MAX_VALUE_IDX = 0
  DOC_ID_IDX = 1

  def __init__(self, max_tokens=None, compute_idf=False):
    self.max_tokens = max_tokens
    self._compute_idf = compute_idf

  def compute(self, values, accumulator=None):
    """Computes a step in this computation, returning a new accumulator."""
    values = base_preprocessing_layer.convert_to_list(values)

    if accumulator is None:
      accumulator = self._create_accumulator()

    # TODO(momernick): Benchmark improvements to this algorithm.
    for element in values:
      if not isinstance(element, list):
        element = [element]
      current_doc_id = accumulator.data[self.DOC_ID_IDX]
      for value in element:
        if self.max_tokens is None:
          current_max_value = accumulator.data[self.MAX_VALUE_IDX]
          if value > current_max_value:
            accumulator.data[self.MAX_VALUE_IDX] = value
        if self._compute_idf:
          doc_count = accumulator.per_doc_count_dict[value]
          if doc_count["last_doc_id"] != current_doc_id:
            doc_count["count"] += 1
            doc_count["last_doc_id"] = current_doc_id
      accumulator.data[self.DOC_ID_IDX] += 1

    return accumulator

  def merge(self, accumulators):
    """Merges several accumulators to a single accumulator."""
    if not accumulators:
      return accumulators

    base_accumulator = accumulators[0]

    for accumulator in accumulators[1:]:
      base_accumulator.data[self.DOC_ID_IDX] += accumulator.data[
          self.DOC_ID_IDX]
      if self.max_tokens is None:
        base_accumulator.data[self.MAX_VALUE_IDX] = max(
            base_accumulator.data[self.MAX_VALUE_IDX],
            accumulator.data[self.MAX_VALUE_IDX])
      if self._compute_idf:
        for token, value in accumulator.per_doc_count_dict.items():
          # Any newly created token counts in 'base_accumulator''s
          # per_doc_count_dict will have a last_doc_id of -1. This is always
          # less than the next doc id (which are strictly positive), so any
          # future occurrences are guaranteed to be counted.
          base_accumulator.per_doc_count_dict[token]["count"] += value["count"]

    return base_accumulator

  def _inverse_document_frequency(self, document_counts, num_documents):
    """Computes the inverse-document-frequency (IDF) component of TFIDF.

    Uses the default weighting scheme described in
    https://en.wikipedia.org/wiki/Tf%E2%80%93idf.

    Args:
      document_counts: An array of the # of documents each token appears in.
      num_documents: An int representing the total number of documents

    Returns:
      An array of "inverse document frequency" weights.
    """
    return np.log(1 + num_documents / (1 + np.array(document_counts)))

  def extract(self, accumulator):
    """Converts an accumulator into a dict of output values.

    Args:
      accumulator: An accumulator aggregating over the full dataset.

    Returns:
      A dict of:
        "num_elements": The number of unique elements in the data set. Only
          returned if `compute_max_element` is True.
        "idf": The inverse-document-frequency for each index, where idf[i] is
          the IDF value for index i. Only returned if `compute_idf` is True.
    """
    data, document_counts = accumulator
    if data[self.MAX_VALUE_IDX] is not None:
      max_element = data[self.MAX_VALUE_IDX] + 1
    else:
      max_element = self.max_tokens
    output_dict = {}
    if self.max_tokens is None:
      output_dict[_NUM_ELEMENTS_NAME] = max_element

    if self._compute_idf:
      num_documents = data[self.DOC_ID_IDX]
      # Here, we need to get the doc_counts for every token value, including
      # values we have not yet seen (and are not in the document_counts dict).
      # However, because document_counts is a defaultdict (see below), querying
      # the dict directly for those values gives us meaningful counts (of 0).
      # However, this also means we can't just extract the values in
      # document_counts - we need to do a deliberate indexing using range().
      doc_counts = [document_counts[i]["count"] for i in range(max_element)]
      idf = self._inverse_document_frequency(doc_counts, num_documents)
      output_dict[_IDF_NAME] = idf

    return output_dict

  def restore(self, output):
    """Creates an accumulator based on 'output'."""
    raise NotImplementedError(
        "CategoryEncoding does not restore or support streaming updates.")

  def serialize(self, accumulator):
    """Serializes an accumulator for a remote call."""
    output_dict = {}
    output_dict["data"] = accumulator.data
    if self._compute_idf:
      output_dict["idf_vocab"] = list(accumulator.per_doc_count_dict.keys())
      output_dict["idf_counts"] = [
          counter["count"]
          for counter in accumulator.per_doc_count_dict.values()
      ]
    return tf.compat.as_bytes(json.dumps(output_dict))

  def deserialize(self, encoded_accumulator):
    """Deserializes an accumulator received from 'serialize()'."""
    accumulator_dict = json.loads(tf.compat.as_text(encoded_accumulator))

    accumulator = self._create_accumulator()
    for i, value in enumerate(accumulator_dict["data"]):
      accumulator.data[i] = value

    if self._compute_idf:
      create_dict = lambda x: {"count": x, "last_doc_id": -1}
      idf_count_dicts = [
          create_dict(count) for count in accumulator_dict["idf_counts"]
      ]
      idf_dict = dict(zip(accumulator_dict["idf_vocab"], idf_count_dicts))
      accumulator.per_doc_count_dict.update(idf_dict)

    return accumulator

  def _create_accumulator(self):
    """Accumulates a sorted array of vocab tokens and corresponding counts."""

    if self._compute_idf:
      create_default_dict = lambda: {"count": 0, "last_doc_id": -1}
      per_doc_count_dict = collections.defaultdict(create_default_dict)
    else:
      per_doc_count_dict = None
    if self.max_tokens is None:
      data = [0, 0]
    else:
      data = [None, 0]
    return _CategoryEncodingAccumulator(data, per_doc_count_dict)


def sparse_bincount(inputs, out_depth, binary_output, count_weights=None):
  """Apply binary or count encoding to an input and return a sparse tensor."""
  result = tf.sparse.bincount(
      inputs,
      weights=count_weights,
      minlength=out_depth,
      maxlength=out_depth,
      axis=-1,
      binary_output=binary_output)
  result = tf.cast(result, K.floatx())
  batch_size = tf.compat.v1.shape(result)[0]
  result = tf.SparseTensor(
      indices=result.indices,
      values=result.values,
      dense_shape=[batch_size, out_depth])
  return result


def dense_bincount(inputs, out_depth, binary_output, count_weights=None):
  """Apply binary or count encoding to an input."""
  result = tf.math.bincount(
      inputs,
      weights=count_weights,
      minlength=out_depth,
      maxlength=out_depth,
      dtype=K.floatx(),
      axis=-1,
      binary_output=binary_output)
  result.set_shape(tf.TensorShape((None, out_depth)))
  return result
