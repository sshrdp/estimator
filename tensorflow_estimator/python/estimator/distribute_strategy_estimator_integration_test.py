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
from absl.testing import parameterized
import numpy as np
import tensorflow as tf
from tensorflow.python.data.ops import dataset_ops
from tensorflow.python.distribute import combinations
from tensorflow.python.distribute import strategy_combinations
from tensorflow.python.feature_column import feature_column_lib as feature_column
from tensorflow.python.framework import ops
from tensorflow.python.platform import gfile
from tensorflow.python.platform import test
from tensorflow.python.summary.writer import writer_cache
from tensorflow.python.training import basic_session_run_hooks
from tensorflow.python.training import training_util
from tensorflow_estimator.python.estimator import estimator as estimator_lib
from tensorflow_estimator.python.estimator import model_fn as model_fn_lib
from tensorflow_estimator.python.estimator import run_config
from tensorflow_estimator.python.estimator import training
from tensorflow_estimator.python.estimator.canned import dnn_linear_combined
from tensorflow_estimator.python.estimator.canned import prediction_keys
from tensorflow_estimator.python.estimator.export import export_lib as export
from tensorflow_estimator.python.estimator.inputs import numpy_io


class DNNLinearCombinedClassifierIntegrationTest(test.TestCase,
                                                 parameterized.TestCase):

  def setUp(self):
    self._model_dir = tempfile.mkdtemp()

  def dataset_input_fn(self, x, y, batch_size, shuffle):

    def input_fn():
      dataset = dataset_ops.Dataset.from_tensor_slices((x, y))
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
  def test_estimator_with_strategy_hooks(self, distribution,
                                         use_train_and_evaluate):
    config = run_config.RunConfig(eval_distribute=distribution)

    def _input_map_fn(tensor):
      return {'feature': tensor}, tensor

    def input_fn():
      return dataset_ops.Dataset.from_tensors(
          [1.]).repeat(10).batch(5).map(_input_map_fn)

    def model_fn(features, labels, mode):
      del features, labels
      global_step = training_util.get_global_step()
      if mode == model_fn_lib.ModeKeys.TRAIN:
        train_hook1 = basic_session_run_hooks.StepCounterHook(
            every_n_steps=1, output_dir=self.get_temp_dir())
        train_hook2 = tf.compat.v1.test.mock.MagicMock(
            wraps=tf.compat.v1.train.SessionRunHook(),
            spec=tf.compat.v1.train.SessionRunHook)
        return model_fn_lib.EstimatorSpec(
            mode,
            loss=tf.constant(1.),
            train_op=global_step.assign_add(1),
            training_hooks=[train_hook1, train_hook2])
      if mode == model_fn_lib.ModeKeys.EVAL:
        eval_hook1 = basic_session_run_hooks.StepCounterHook(
            every_n_steps=1, output_dir=self.get_temp_dir())
        eval_hook2 = tf.compat.v1.test.mock.MagicMock(
            wraps=tf.compat.v1.train.SessionRunHook(),
            spec=tf.compat.v1.train.SessionRunHook)
        return model_fn_lib.EstimatorSpec(
            mode=mode,
            loss=tf.constant(1.),
            evaluation_hooks=[eval_hook1, eval_hook2])
    num_steps = 10
    estimator = estimator_lib.EstimatorV2(
        model_fn=model_fn, model_dir=self.get_temp_dir(), config=config)
    if use_train_and_evaluate:
      training.train_and_evaluate(
          estimator, training.TrainSpec(input_fn, max_steps=num_steps),
          training.EvalSpec(input_fn))
    else:
      estimator.train(input_fn, steps=num_steps)
      estimator.evaluate(input_fn, steps=num_steps)

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
        feature_column.numeric_column('x', shape=(input_dimension,))
    ]
    dnn_feature_columns = [
        feature_column.numeric_column('x', shape=(input_dimension,))
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

    self.assertEqual(num_steps, scores[ops.GraphKeys.GLOBAL_STEP])
    self.assertIn('loss', scores)

    predictions = np.array([
        x[prediction_keys.PredictionKeys.PREDICTIONS]
        for x in estimator.predict(predict_input_fn)
    ])
    self.assertAllEqual((batch_size, label_dimension), predictions.shape)

    feature_spec = feature_column.make_parse_example_spec(feature_columns)
    serving_input_receiver_fn = export.build_parsing_serving_input_receiver_fn(
        feature_spec)
    export_dir = estimator.export_saved_model(tempfile.mkdtemp(),
                                              serving_input_receiver_fn)
    self.assertTrue(gfile.Exists(export_dir))

  def tearDown(self):
    if self._model_dir:
      writer_cache.FileWriterCache.clear()
      shutil.rmtree(self._model_dir)


if __name__ == '__main__':
  test.main()
