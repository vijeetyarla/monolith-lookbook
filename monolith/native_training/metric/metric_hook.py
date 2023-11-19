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

# Metrics codes are ported from Lagrange Lite: lagrange_lite/tensorflow/train.py
#coding:utf-8
import json
import numpy as np
import os
import tensorflow as tf
import time
from typing import Any, Tuple, Callable
from queue import Queue, Empty
from threading import Thread, RLock

from absl import logging, flags
from datetime import datetime
from tensorflow.python.profiler.internal import _pywrap_traceme
from tensorflow.python.training import basic_session_run_hooks
from tensorflow.python.training import session_run_hook
from tensorflow.python.training import training_util

from monolith.native_training.alert import alert_manager
from monolith.native_training.alert import alert_pb2
from monolith.native_training.metric import cli
from monolith.native_training import utils
from monolith.native_training.metric.kafka_utils import KProducer
from monolith.native_training.metric.exit_hook import exit_hook


FLAGS = flags.FLAGS


class ThroughputMetricHook(tf.estimator.SessionRunHook):
  """ Log accumulated steps and time elapsed per step. """

  def __init__(self,
               model_name,
               start_time_secs,
               cluster_type="stable",
               run_every_n_secs=30):

    self._model_name = model_name
    self._start_time_secs = start_time_secs
    self._cluster_type = cluster_type
    self._run_every_n_secs = run_every_n_secs
    self._is_first_step = True
    self._mcli = cli.get_cli(utils.get_metric_prefix())
    am = alert_manager.get_default_alert_manager()
    if am:
      proto = alert_pb2.AlertProto()
      proto.training_alert.prefix = utils.get_metric_prefix()
      am.add_rules(proto)

  def begin(self):
    self._global_step_tensor = tf.compat.v1.train.get_global_step()

  def before_run(self, run_context):
    if self._is_first_step is True:
      self._emit_step = run_context.session.run(self._global_step_tensor)
      self._emit_time = int(time.time())
      if self._start_time_secs is not None:
        tags = {
            "model_name": self._model_name,
            "cluster_type": self._cluster_type
        }
        run_start_elapsed_time = self._emit_time - self._start_time_secs
        logging.info("Run start took {}s.".format(run_start_elapsed_time))
        self._mcli.emit_timer("run_start_elapsed_time.all",
                              run_start_elapsed_time, tags)
      self._is_first_step = False
    return session_run_hook.SessionRunArgs({
        "global_step": self._global_step_tensor,
    })

  def after_run(self, run_context, run_values):
    end_time = int(time.time())
    elapsed_time = end_time - self._emit_time
    if elapsed_time >= self._run_every_n_secs:
      global_step = run_values.results["global_step"]
      step_inerval = global_step - self._emit_step
      tags = {
          "model_name": self._model_name,
          "cluster_type": self._cluster_type
      }
      self._mcli.emit_counter("run_steps.all", step_inerval, tags)
      self._mcli.emit_timer("run_steps_elapsed_time.all",
                            elapsed_time / step_inerval, tags)
      self._emit_step = global_step
      self._emit_time = end_time


class StepLossMetricHook(tf.estimator.SessionRunHook):
  """ Log loss of each step. """

  def __init__(self, loss_tensor):
    self._loss_tensor = loss_tensor
    self._mcli = cli.get_cli(utils.get_metric_prefix())

  def before_run(self, run_context):
    return tf.estimator.SessionRunArgs(self._loss_tensor)

  def after_run(self, run_context, run_value):
    self._mcli.emit_store("step_loss", run_value.results)


class CustomMetricHook(tf.estimator.SessionRunHook):
  """ Log group of customed metircs for a batch. """

  def __init__(self, metric_tensors):
    for name in metric_tensors:
      tensor = metric_tensors[name]
      if len(tensor.shape.dims) > 0:
        raise ValueError("The metric tensor should be a scalar!")
      if tensor.dtype.base_dtype not in (tf.float32, tf.int32):
        raise ValueError(
            "The dtype of a metric tensor should be either tf.float or tf.int32!"
        )
    if len(metric_tensors) == 0:
      raise ValueError("At least one metric tensor should be offered!")
    self._metric_tensors = metric_tensors
    self._mcli = cli.get_cli(utils.get_metric_prefix())

  def before_run(self, run_context):
    return tf.estimator.SessionRunArgs(self._metric_tensors)

  def after_run(self, run_context, run_value):
    metric_values = run_value.results
    for name in metric_values:
      self._mcli.emit_store(name, float(metric_values[name]))


class Tf2ProfilerHook(tf.estimator.SessionRunHook):
  """ Using TF2 profiler in esitmator """

  def __init__(self,
               logdir: str,
               init_step_range: Tuple[int, int],
               save_steps: int = None,
               save_secs: int = None,
               options: tf.profiler.experimental.ProfilerOptions = None):
    """Only one of save_steps and save_secs should be provided."""
    self._logdir = logdir
    self._options = options
    self._start_step, self._end_step = init_step_range
    if self._start_step is not None and (self._end_step is None or self._end_step <= self._start_step):
      raise ValueError("End step invalid, start_step: {}, end_step: {}".format(self._start_step, self._end_step))
    self._default_delta = 10
    self._delta = self._end_step - self._start_step if self._end_step is not None else self._default_delta
    if save_steps is not None and save_steps <= self._delta:
      raise ValueError("Save steps must be greater than delta steps(default: {})".format(self._default_delta))
    self._timer = tf.estimator.SecondOrStepTimer(every_steps=save_steps,
                                                 every_secs=save_secs)
    self._current_step = 0
    self._trace_me = None

    self._profiling = False

  def begin(self):
    try:
      # if enable_sync_training, there is no tf.distribute.Server
      # we need start profiler server
      if FLAGS.enable_sync_training:
        tf.profiler.experimental.server.start(6666)
    except:
      logging.warning("cannot start profiler server at 6666")

  def before_run(self, run_context):
    # fix step-time graph, related issue: https://github.com/tensorflow/profiler/issues/282
    # TODO(huangruiteng): remove this after updating tensorflow
    if self._profiling:
      self._trace_me = _pywrap_traceme.TraceMe("TraceContext", graph_type="train", step_num=self._current_step)
    return tf.estimator.SessionRunArgs(fetches=None)

  def after_run(self, run_context, run_values: tf.estimator.SessionRunValues):
    self._current_step += 1
    if self._profiling:
      self._trace_me.Stop()
    if self._start_step is None:
      self._start_step = self._current_step + 500
      self._end_step = self._start_step + self._default_delta
    if self._current_step < self._start_step:
      return
    if self._current_step >= self._end_step:
      self._stop_profiling()
    if self._timer.should_trigger_for_step(self._current_step):
      self._start_profiling()
      self._timer.update_last_triggered_step(self._current_step)
      self._start_step = self._current_step
      self._end_step = self._start_step + self._delta

  def end(self, sess):
    if self._profiling:
      self._stop_profiling()

  def _start_profiling(self):
    try:
      tf.profiler.experimental.start(self._logdir, self._options)
      self._profiling = True
    except tf.errors.AlreadyExistsError:
      # Two cases:
      # 1. User profiles by themselves.
      # 2. When profiling by save_secs, it's still profiling after save_secs.
      # OK to ignore here.
      self._profiling = True

  def _stop_profiling(self):
    try:
      if self._profiling:
        self._profiling = False
        tf.profiler.experimental.stop()
    except tf.errors.UnavailableError:
      # Maybe user terminates profiling
      self._profiling = False


class ByteCCLTelemetryHook(tf.estimator.SessionRunHook):
  """Log telemetry information at regular intervals"""

  def __init__(self, interval: int):
    """Log telemetry information at regular intervals"""
    self._interval = interval
    self._last_step = 0
    logging.info(f"Created ByteCCL telemetry hook, interval={interval}")

  def begin(self):
    self._global_step_tensor = training_util._get_or_create_global_step_read()
    if self._global_step_tensor is None:
      raise RuntimeError(
          "Global step should be created to use ByteCCLTelemetryHook")

  def before_run(self, run_context):
    return tf.estimator.SessionRunArgs(self._global_step_tensor)

  def after_run(self, run_context, run_values: tf.estimator.SessionRunValues):
    current_step = run_values.results
    if current_step > self._last_step + self._interval:
      self._log_telemetry()
      self._last_step = current_step

  def end(self, sess):
    pass

  def _log_telemetry(self):
    import byteps.tensorflow as bps
    if bps.rank() == 0:
      telemetry = bps.get_telemetry()
      # sample a few operations and show them
      samples = []
      num_allreduce_ops = 0
      for name, mean, stdev, count in telemetry:
        name = str(name)
        is_alltoall = 'alltoall' in name.lower()
        if is_alltoall or ('PushPull' in name and num_allreduce_ops < 3):
          num_allreduce_ops += 1
          entry = f'name: {name} mean(ms): {mean:.2f} stdev(ms): {stdev:.2f} count: {count}'
          samples.append(entry)
      if samples:
        logging.info(f'Communication telemetry: {samples} ...')


class NVProfilerHook(Tf2ProfilerHook):

  def __init__(self,
               init_step_range: Tuple[int, int],
               save_steps: int = None,
               save_secs: int = None,
               options: tf.profiler.experimental.ProfilerOptions = None):
    super().__init__(None, init_step_range, save_steps, save_secs)
    import ctypes
    self._libcudart = ctypes.cdll.LoadLibrary("libcudart.so")  # linux

  def _start_profiling(self):
    # http://docs.nvidia.com/cuda/cuda-runtime-api/group__CUDART__PROFILER.html,
    self._libcudart.cudaProfilerStart()
    self._profiling = True

  def _stop_profiling(self):
    if self._profiling:
      self._profiling = False
      self._libcudart.cudaProfilerStop()


class KafkaMetricHook(tf.estimator.SessionRunHook):
  """ Log group of customed metircs for a batch. """
  __instance = None

  def __new__(cls, *args, **kwargs):
    if cls.__instance is None:
      cls.__instance = super().__new__(cls)
      cls.__instance._kproducer = None
      cls.__instance._init_kafka()

    return cls.__instance

  @classmethod
  def _init_kafka(cls):
    brokers = os.getenv('KAFKA_BROKER_LIST', None)
    topic = os.getenv('KAFKA_TOPIC_NAME', None)
    if brokers is None or topic is None:
      logging.info(
          'KafkaMetricHook init kafka failed, brokers: {}, topic: {}'.format(
              brokers, topic))
      return

    cls.__instance._kproducer = KProducer(brokers, topic)
    logging.info(
        'KafkaMetricHook init kafka success, brokers: {}, topic: {}'.format(
            brokers, topic))

  def __init__(self, deep_insight_op=None):
    if deep_insight_op is None:
      collection = tf.compat.v1.get_collection(key='deep_insight_op')
      if collection:
        if isinstance(collection, (list, tuple)):
          deep_insight_op = collection[0]
        else:
          deep_insight_op = collection
    self._metric_tensors = {'deep_insight_op': deep_insight_op}

  def before_run(self, run_context):
    return tf.estimator.SessionRunArgs(self._metric_tensors)

  def after_run(self, run_context, run_value):
    if self._kproducer:
      metric_values = run_value.results
      msgs = metric_values.get('deep_insight_op')
      if msgs is not None and len(msgs) > 0:
        self._kproducer.send(msgs)

  def end(self, session):
    if self._kproducer:
      self._kproducer.close()
      logging.info('KafkaMetricHook end, flush msg, success: {}, failed: {}'.\
        format(self._kproducer.success(), self._kproducer.failed()))
      self._kproducer = None


def default_parse_fn(obj: Any) -> Any:
  if obj is not None:
    if isinstance(obj, (str, bytes)):
      return json.loads(obj)
  return obj


def default_layout_fn(obj, indent=None) -> str:
  if isinstance(obj, str):
    return obj
  else:
    try:
      return json.dumps(obj, indent=indent)
    except:
      return repr(obj)


def vepfs_layout_fn(obj) -> str:
  req_time = obj.get('__REQ_TIME__') or obj.get('req_time')
  gid = obj.get('__FEED_ID__') or obj.get('feedid') or obj.get('gid') or 'gid'
  uid = obj.get('__UID__') or obj.get('userid') or obj.get('uid') or 'uid'
  predict_scores = json.dumps(obj['predict']) if 'predict' in obj else None
  labels = json.dumps(obj['label']) if 'label' in obj else None
  return f"{req_time};{gid};{uid};{predict_scores};{labels}"


def vepfs_key_fn(obj, worker_id: int, base_name: str) -> str:
  model_name = obj.get('model_name') or 'model_name'
  date = obj.get('__REQ_TIME__') or obj.get('req_time')
  return os.path.join(base_name, model_name, date, f'worker_{worker_id}')


class WriteOnlyFileAndStat(object):

  def __init__(self,
               key: str,
               layout_fn: Callable[[Any], str] = None,
               batch_size: int = 1024,
               partition_size: int = None,
               file_ext: str = 'txt'):
    self.current_partition: int = 0
    self.current_offset: int = 0
    self.last_update_time: float = time.time()
    self.buffer: List[Any] = []

    self.batch_size = batch_size
    self.partition_size = partition_size or int(1e6)
    self.layout_fn = layout_fn or default_layout_fn
    self.file_ext = file_ext

    assert key is not None
    self.key = key
    self.stream = None
    self._lock = RLock()

  def write(self, obj):
    if len(self.buffer) >= self.batch_size:
      self.flush()

    with self._lock:
      if obj is not None:
        self.buffer.append(self.layout_fn(obj))
        self.current_offset += 1
        self.last_update_time = time.time()

  def write_many(self, objs):
    if objs:
      for obj in objs:
        self.write(obj)

  def flush(self, check: bool = True):
    with self._lock:
      if self.stream is None:
        if not tf.io.gfile.exists(path=self.key):
          tf.io.gfile.makedirs(path=self.key)
        part_name = os.path.join(
            self.key, f'part_{self.current_partition:06d}.{self.file_ext}')
        self.stream = tf.io.gfile.GFile(part_name, 'w+')

      if self.stream is not None:
        if self.buffer:
          self.stream.write('\n'.join(self.buffer))
          self.stream.write('\n')
          self.buffer = []
        self.stream.flush()

      if check and self.current_offset >= self.partition_size:
        self.current_partition += 1
        self.current_offset = 0
        self.stream.close()
        part_name = os.path.join(
            self.key, f'part_{self.current_partition:06d}.{self.file_ext}')
        self.stream = tf.io.gfile.GFile(part_name, 'w+')

  def close(self):
    with self._lock:
      self.flush(check=False)
      if self.stream is not None:
        self.stream.close()
        self.stream = None

  def is_available(self):
    return (time.time() - self.last_update_time) < 24 * 60 * 60


class FileMetricHook(tf.estimator.SessionRunHook):
  """ Log group of customed metircs for a batch. """
  __instance = None

  def __new__(cls, *args, **kwargs):
    if cls.__instance is None:
      cls.__instance = super().__new__(cls)
    return cls.__instance

  def __init__(self,
               deep_insight_op=None,
               *,
               worker_id: int = None,
               parse_fn: Callable[[Any], Any] = None,
               key_fn: Callable[[Any, int, str], str] = None,
               layout_fn: Callable[[Any], str] = None,
               batch_size: int = 1024,
               partition_size: int = None,
               base_name: str = '/vepfs/jaguar_deepinsight_results',
               file_ext: str = 'txt'):
    if deep_insight_op is None:
      collection = tf.compat.v1.get_collection(key='deep_insight_op')
      if collection:
        if isinstance(collection, (list, tuple)):
          deep_insight_op = collection[0]
        else:
          deep_insight_op = collection
      else:
        deep_insight_op = None

    self._worker_id = worker_id
    self._key_fn = key_fn
    self._layout_fn = layout_fn or default_layout_fn
    self._parse_fn = parse_fn or default_parse_fn
    self._batch_size = batch_size
    self._partition_size = partition_size
    self._base_name = base_name
    self._file_ext = file_ext

    self._queue: Queue = Queue()
    self._files: Dict[str, WriteOnlyFileAndStat] = {}
    self._stopped = False
    self._metric_tensors = {'deep_insight_op': deep_insight_op}
    self._thread = None

  def before_run(self, run_context):
    return tf.estimator.SessionRunArgs(self._metric_tensors)

  def after_run(self, run_context, run_value):
    if self._thread is None:
      self._thread = Thread(target=self._send)
      self._thread.start()

    metric_values = run_value.results
    msgs = metric_values.get('deep_insight_op')
    if msgs is not None:
      if isinstance(msgs, (list, tuple, np.ndarray)):
        for msg in msgs:
          if msg:
            self._queue.put(msg)
      else:
        self._queue.put(msgs)

  def end(self, session):
    logging.info('end FileMetricHook: empty the queue ...')
    while not self._queue.empty():
      time.sleep(1)
    logging.info('end FileMetricHook: queue is empty, begin to stop thread ...')
    self._stopped = True
    if self._thread is not None:
      self._thread.join()
      self._thread = None
    logging.info(
        'end FileMetricHook: thread stopped, begin to close open file ...')
    for fs in self._files.values():
      fs.close()
    logging.info('end FileMetricHook: all done! ')

  def _send(self):
    last_check_time = time.time()
    while not self._stopped:
      try:
        item = self._queue.get(timeout=1)
        item = self._parse_fn(item)
      except Empty as e:
        continue

      key = self._key_fn(item, self._worker_id, self._base_name)
      if key not in self._files:
        file_and_stat = WriteOnlyFileAndStat(
            key,
            layout_fn=self._layout_fn,
            batch_size=self._batch_size,
            partition_size=self._partition_size,
            file_ext=self._file_ext)
        self._files[key] = file_and_stat
      else:
        file_and_stat = self._files[key]
      file_and_stat.write(item)

      if time.time() - last_check_time > 600:
        to_remove = set()
        for key, fs in self._files.items():
          if not fs.is_available():
            fs.close()
            to_remove.add(key)

        for key in to_remove:
          del self._files[key]
        last_check_time = time.time()
