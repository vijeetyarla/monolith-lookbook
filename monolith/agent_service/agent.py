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

from absl import app, flags, logging
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from kazoo.client import KazooClient
import os
import copy
import subprocess
import signal
from subprocess import CalledProcessError
import threading
import time
from typing import List
from multiprocessing import Process

from monolith.agent_service.replica_manager import ReplicaManager
from monolith.agent_service.agent_service import AgentService
from monolith.agent_service.utils import AgentConfig, DeployType, check_port_open
from monolith.native_training.zk_utils import MonolithKazooClient
from monolith.native_training import env_utils
from monolith.agent_service.agent_v1 import AgentV1
from monolith.agent_service.agent_v3 import AgentV3
from monolith.agent_service.model_manager import ModelManager

FLAGS = flags.FLAGS
flags.DEFINE_string('tfs_log', '/var/log/tfs.std.log',
                    'The tfs log file path')
def run_agent(agent_config_path: str, tfs_log: str,
              use_mps: bool, replica_id: int,
              dense_service_index: int):
  if use_mps:
    os.environ["REPLICA_ID"] = str(replica_id)
    logging.info(f"[INFO] the corresponding replica_id {replica_id}")
    os.environ["DENSE_SERVICE_IDX"] = str(dense_service_index)
    tfs_log = "{}.mps{}".format(tfs_log, dense_service_index)

  config = AgentConfig.from_file(agent_config_path)
  conf_path = os.path.dirname(agent_config_path)
  if config.agent_version == 1:
    agent = AgentV1(config, conf_path, tfs_log)
  elif config.agent_version == 2:
    raise Exception('agent_version v2 is not support')
  elif config.agent_version == 3:
    agent = AgentV3(config, conf_path, tfs_log)
  else:
    raise Exception(f"agent_version error {config.agent_version}")

  # start model manager for rough sort model
  model_manager = ModelManager(config.rough_sort_model_name,
                               config.rough_sort_model_p2p_path,
                               config.rough_sort_model_local_path, True)
  ret = model_manager.start()
  if not ret:
    logging.error('model_manager start failed, kill self')
    os.kill(os.getpid(), signal.SIGKILL)

  agent.start()
  agent.wait_for_termination()


def main(_):
  try:
    env_utils.setup_hdfs_env()
  except Exception as e:
    logging.error('setup_hdfs_env fail {}!'.format(e))
  logging.info(f'environ is : {os.environ!r}')

  if FLAGS.conf is None:
    print(FLAGS.get_help())
    return

  config = AgentConfig.from_file(FLAGS.conf)

  if config.deploy_type == DeployType.DENSE and config.dense_service_num > 1:
    p_list = []
    for i in range(config.dense_service_num):
      cur_rid = config.replica_id * config.dense_service_num + i
      p = Process(target=run_agent, args=(FLAGS.conf, FLAGS.tfs_log, True, cur_rid, i))
      p.start()
      p_list.append(p)
    for p in p_list:
      p.join()
  else:
    run_agent(FLAGS.conf, FLAGS.tfs_log, False, config.replica_id, 0)

if __name__ == '__main__':
  app.run(main)
