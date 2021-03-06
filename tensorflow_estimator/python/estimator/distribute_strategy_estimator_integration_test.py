# Copyright 2016 The TensorFlow Authors. All Rights Reserved.
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
"""Tests that show that DistributionStrategy works with canned Estimator."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import shutil
import tempfile
import tensorflow as tf
from absl.testing import parameterized
import numpy as np
from tensorflow.python.data.ops import dataset_ops
from tensorflow.python.distribute import combinations
from tensorflow.python.distribute import strategy_combinations
from tensorflow.python.feature_column import feature_column_lib as feature_column
from tensorflow.python.framework import ops
from tensorflow.python.platform import gfile
from tensorflow.python.platform import test
from tensorflow.python.summary.writer import writer_cache
from tensorflow_estimator.python.estimator import run_config
from tensorflow_estimator.python.estimator import training
from tensorflow_estimator.python.estimator.canned import dnn_linear_combined
from tensorflow_estimator.python.estimator.canned import prediction_keys
from tensorflow_estimator.python.estimator.export import export_lib as export
from tensorflow_estimator.python.estimator.inputs import numpy_io


class DNNLinearCombinedClassifierIntegrationTest(tf.test.TestCase,
                                                 parameterized.TestCase):

  def setUp(self):
    self._model_dir = tempfile.mkdtemp()

  def dataset_input_fn(self, x, y, batch_size, shuffle):

    def input_fn():
      dataset = tf.compat.v1.data.Dataset.from_tensor_slices((x, y))
      if shuffle:
        dataset = dataset.shuffle(batch_size)
      dataset = dataset.repeat(10).batch(batch_size)
      return dataset

    return input_fn

  @combinations.generate(
      combinations.combine(
          mode=['graph'],
          distribution=[
              strategy_combinations.one_device_strategy,
              strategy_combinations.mirrored_strategy_with_gpu_and_cpu,
              strategy_combinations.mirrored_strategy_with_two_gpus
          ],
          use_train_and_evaluate=[True, False]))
  def test_complete_flow_with_mode(self, distribution, use_train_and_evaluate):
    label_dimension = 2
    input_dimension = label_dimension
    batch_size = 10
    data = np.linspace(0., 2., batch_size * label_dimension, dtype=np.float32)
    data = data.reshape(batch_size, label_dimension)
    train_input_fn = self.dataset_input_fn(
        x={'x': data},
        y=data,
        batch_size=batch_size // distribution.num_replicas_in_sync,
        shuffle=True)
    eval_input_fn = self.dataset_input_fn(
        x={'x': data},
        y=data,
        batch_size=batch_size // distribution.num_replicas_in_sync,
        shuffle=False)
    predict_input_fn = numpy_io.numpy_input_fn(
        x={'x': data}, batch_size=batch_size, shuffle=False)

    linear_feature_columns = [
        tf.feature_column.numeric_column('x', shape=(input_dimension,))
    ]
    dnn_feature_columns = [
        tf.feature_column.numeric_column('x', shape=(input_dimension,))
    ]
    feature_columns = linear_feature_columns + dnn_feature_columns
    estimator = dnn_linear_combined.DNNLinearCombinedRegressor(
        linear_feature_columns=linear_feature_columns,
        dnn_hidden_units=(2, 2),
        dnn_feature_columns=dnn_feature_columns,
        label_dimension=label_dimension,
        model_dir=self._model_dir,
        # TODO(isaprykin): Work around the colocate_with error.
        dnn_optimizer='Adagrad',
        linear_optimizer='Adagrad',
        config=run_config.RunConfig(
            train_distribute=distribution, eval_distribute=distribution))

    num_steps = 10
    if use_train_and_evaluate:
      scores, _ = training.train_and_evaluate(
          estimator,
          training.TrainSpec(train_input_fn, max_steps=num_steps),
          training.EvalSpec(eval_input_fn))
    else:
      estimator.train(train_input_fn, steps=num_steps)
      scores = estimator.evaluate(eval_input_fn)

    self.assertEqual(num_steps, scores[tf.compat.v1.GraphKeys.GLOBAL_STEP])
    self.assertIn('loss', scores)

    predictions = np.array([
        x[prediction_keys.PredictionKeys.PREDICTIONS]
        for x in estimator.predict(predict_input_fn)
    ])
    self.assertAllEqual((batch_size, label_dimension), predictions.shape)

    feature_spec = tf.compat.v1.feature_column.make_parse_example_spec(feature_columns)
    serving_input_receiver_fn = export.build_parsing_serving_input_receiver_fn(
        feature_spec)
    export_dir = estimator.export_saved_model(tempfile.mkdtemp(),
                                              serving_input_receiver_fn)
    self.assertTrue(tf.compat.v1.gfile.Exists(export_dir))

  def tearDown(self):
    if self._model_dir:
      tf.compat.v1.summary.FileWriterCache.clear()
      shutil.rmtree(self._model_dir)


if __name__ == '__main__':
  tf.test.main()
