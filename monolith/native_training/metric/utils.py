# Copyright 2022 ByteDance and/or its affiliates.
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

import datetime
import logging
from typing import Dict, List

import tensorflow as tf

from monolith.native_training.metric import deep_insight_ops


def write_deep_insight(features: Dict[str, tf.Tensor],
                       sample_ratio: float,
                       model_name: str,
                       labels: tf.Tensor = None,
                       preds: tf.Tensor = None,
                       target: str = None,
                       targets: List[str] = None,
                       labels_list: List[tf.Tensor] = None,
                       preds_list: List[tf.Tensor] = None,
                       sample_rates_list: List[tf.Tensor] = None,
                       extra_fields_keys: List[str] = [],
                       enable_deep_insight_metrics=True,
                       enable_kafka_metrics=False,
                       dump_filename=None) -> tf.Tensor:
  """ Writes the data into deepinsight
  Requires 'uid', 'req_time', and 'sample_rate' in features. 
  sample_ratio is deepinsight sample ratio, set value like 0.01.
  
  If targets is non-empty, MonolithWriteDeepInsightV2 will be used, enabling:
  - Multi-target sent as one message;
  - Dump extra fields.
  When using MonolithWriteDeepInsightV2, labels/preds/sample_rates should be
  shape (num_targets, batch_size). sample_rates is optional.
  Extra fields specified in extra_fields_keys must be present in features, and
  must have batch_size numbers of values.
  """
  if 'req_time' not in features:
    logging.info("Disabling deep_insight because req_time is absent")
    return tf.no_op()

  is_fake = enable_kafka_metrics or (dump_filename is not None and len(dump_filename) > 0)
  deep_insight_client = deep_insight_ops.deep_insight_client(
      enable_deep_insight_metrics, is_fake, dump_filename=dump_filename)
  req_times = tf.reshape(features["req_time"], [-1])

  if not targets:
    uids = tf.reshape(features["uid"], [-1])
    sample_rates = tf.reshape(features["sample_rate"], [-1])
    deep_insight_op = deep_insight_ops.write_deep_insight(
        deep_insight_client_tensor=deep_insight_client,
        uids=uids,
        req_times=req_times,
        labels=labels,
        preds=preds,
        sample_rates=sample_rates,
        model_name=model_name,
        target=target,
        sample_ratio=sample_ratio,
        return_msgs=is_fake)
  else:
    labels = tf.stack([label if label.shape.rank == 1 else  tf.reshape(label, (-1,))
                       for label in labels_list if label is not None])
    preds = tf.stack([pred if pred.shape.rank == 1 else  tf.reshape(pred, (-1,))
                      for pred in preds_list if pred is not None])
    if not sample_rates_list:
      sample_rates_list = [tf.reshape(features["sample_rate"], [-1])
                          ] * len(targets)
    elif isinstance(sample_rates_list, (tuple, list)):
      sample_rates_list = [sample_rate if sample_rate.shape.rank == 1 else  tf.reshape(sample_rate, (-1,))
                           for sample_rate in sample_rates_list if sample_rate is not None]
    else:
      raise Exception("sample_rates_list error!")
    sample_rates = tf.stack(sample_rates_list)
    if "uid" not in extra_fields_keys:
      extra_fields_keys.append("uid")
    extra_fields_values = []
    for key in extra_fields_keys:
      extra_fields_values.append(tf.reshape(features[key], [-1]))
    deep_insight_op = deep_insight_ops.write_deep_insight_v2(
        deep_insight_client_tensor=deep_insight_client,
        req_times=req_times,
        labels=labels,
        preds=preds,
        sample_rates=sample_rates,
        model_name=model_name,
        sample_ratio=sample_ratio,
        extra_fields_values=extra_fields_values,
        extra_fields_keys=extra_fields_keys,
        targets=targets,
        return_msgs=is_fake)
  return deep_insight_op
