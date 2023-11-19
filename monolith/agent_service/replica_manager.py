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

from absl import logging
from dataclasses import dataclass
from dataclasses_json import dataclass_json
from kazoo.client import KazooState
from kazoo.protocol.states import WatchedEvent, EventType, ZnodeStat
from kazoo.exceptions import NodeExistsError, NoNodeError
import os, re
import socket
import sys
import threading
import time
import traceback
from typing import List, Dict, Union, Optional, Tuple

from tensorflow.core.protobuf.error_codes_pb2 import Code as ErrorCode

from monolith.agent_service.tfs_monitor import TFSMonitor
from monolith.agent_service.utils import AgentConfig, ModelState, TFSServerType, DeployType, ZKPath
from monolith.agent_service.agent_service_pb2 import ServerType
from monolith.agent_service.data_def import ReplicaMeta
from monolith.agent_service.backends import SyncBackend
from monolith.native_training.model_export import export_state_utils
from monolith.native_training.net_utils import AddressFamily
from monolith.native_training.zk_utils import MonolithKazooClient, is_ipv6_only
from monolith.native_training.metric import cli
from monolith.native_training.runtime.parameter_sync.parameter_sync_pb2 import ClientConfig
DEFAULT_USE_ARCHON = False
class ReplicaWatcher(object):

  def __init__(self,
               zk_client: MonolithKazooClient,
               config: AgentConfig,
               use_archon: bool = False,
               zk_watch_address_family: str = AddressFamily.IPV4):
    self._zk = zk_client
    # the info of this replica
    self._conf: AgentConfig = config

    self._use_archon = use_archon
    if zk_watch_address_family == AddressFamily.IPV4 and is_ipv6_only():
      logging.warning("zk_watch_address_family change to IPV6")
      self._zk_watch_address_family = AddressFamily.IPV6
    else:
      self._zk_watch_address_family = zk_watch_address_family

    self.path_prefix = os.path.join('/', config.bzid, 'service',
                                    config.base_name)

    # /bzid/service/model_name/server_type:task -> replica -> (addr, stat)
    self._lock = threading.Lock()
    self.replicas: Dict[str, Dict[str, ReplicaMeta]] = {}
    self._has_stop = False
    self._should_poll = True
    self._thread = None

  @property
  def zk(self):
    return self._zk

  def watch_data(self):
    if self._conf.dc_aware:
      self.zk.ChildrenWatch(path=self.path_prefix,
                            func=self._get_idc_cluster_children_watch(
                                self.path_prefix))
    else:
      self.zk.ChildrenWatch(path=self.path_prefix,
                            func=self._get_task_children_watch(
                                self.path_prefix))
    self._thread = threading.Thread(target=self._poll, daemon=True)
    self._has_stop = False
    self._thread.start()

  def stop(self):
    try:
      self._has_stop = True
      if self._thread is not None:
        try:
          self._thread.join()
          self._thread = None
        except:
          self._thread = None
    finally:
      with self._lock:
        self.replicas.clear()

  def _get_idc_cluster_children_watch(self, path_prefix: str):
    _idc_cluster = set()

    def idc_cluster_children_watch(children: List[str]):
      if children is not None:
        for idc_cluster in children:
          if idc_cluster not in _idc_cluster:
            # idc_cluster -> idc:cluster
            _idc_cluster.add(idc_cluster)
            ic_path = os.path.join(path_prefix, idc_cluster)
            self.zk.ChildrenWatch(path=ic_path,
                                  func=self._get_task_children_watch(ic_path))

    return idc_cluster_children_watch

  def _get_task_children_watch(self, path_prefix: str):
    _tasks = set()

    def task_children_watch(children: List[str]):
      if children is not None:
        for task in children:
          if task not in _tasks:
            # task -> entry/ps/dense:idx
            _tasks.add(task)
            task_path = os.path.join(path_prefix, task)
            self.zk.ChildrenWatch(
                path=task_path,
                func=self._get_replica_children_watch(task_path))

    return task_children_watch

  def _get_replica_children_watch(self, task_path: str):
    _replicas = set()

    def replica_children_watch(children: List[str]):
      if children is not None:
        for replica in children:
          if replica not in _replicas:
            _replicas.add(replica)
            replica_path = os.path.join(task_path, replica)
            self.zk.DataWatch(path=replica_path,
                              func=self._get_data_watch(replica_path))

    return replica_children_watch

  def _get_data_watch(self, path):

    def data_watch(data: bytes, state: ZnodeStat, event: WatchedEvent):
      task_path = os.path.dirname(path)
      rnode = str(int(os.path.basename(path)))
      if data is None or len(data) == 0:
        with self._lock:
          if task_path in self.replicas:
            if rnode in self.replicas[task_path]:
              meta = self.replicas[task_path][rnode]
              meta.stat = ModelState.UNKNOWN
            else:
              return
          else:
            return
      else:
        meta = ReplicaMeta.deserialize(data)

      with self._lock:
        if event is None or event.type == EventType.CREATED:
          # in the first call, event is None
          if task_path in self.replicas:
            self.replicas[task_path][rnode] = meta
          else:
            self.replicas[task_path] = {rnode: meta}
        elif event.type == EventType.DELETED:
          if task_path in self.replicas.keys(
          ) and rnode in self.replicas[task_path].keys():
            del self.replicas[task_path][rnode]
          if task_path in self.replicas.keys() and len(
              self.replicas[task_path]) == 0:
            del self.replicas[task_path]
        elif event.type == EventType.CHANGED:
          self.replicas[task_path][rnode] = meta
        elif event.type == EventType.NONE:
          meta.stat = ModelState.UNKNOWN
          self.replicas[task_path][rnode] = meta
        else:
          assert event.type == EventType.CHILD

    return data_watch

  def _poll(self):
    while not self._has_stop:
      time.sleep(60)
      if not self._should_poll:
        continue
      try:
        tasks = []
        if self._conf.dc_aware:
          idc_clusters = self.zk.get_children(self.path_prefix)
          if idc_clusters:
            for ic in idc_clusters:
              ic_path = os.path.join(self.path_prefix, ic)
              ts = self.zk.get_children(ic_path)
              if ts:
                tasks.extend([f'{ic}/{t}' for t in ts])
        else:
          ts = self.zk.get_children(self.path_prefix)
          if ts:
            tasks.extend(ts)

        replicas_tmp = {}
        for task in tasks:
          task_path = os.path.join(self.path_prefix, task)
          replicas = self.zk.get_children(task_path)
          replicas_tmp[task_path] = {}
          if replicas:
            for replica in replicas:
              replica_path = os.path.join(task_path, replica)
              value, _ = self.zk.get(replica_path)
              if value is not None:
                meta = ReplicaMeta.from_json(str(value, encoding='utf-8'))
                replicas_tmp[task_path][str(int(replica))] = meta

        with self._lock:
          old_paths, new_paths = {}, {}
          for task, replicas in self.replicas.items():
            for replica in replicas:
              key = os.path.join(task, replica)
              old_paths[key] = self.replicas[task][replica]
          for task, replicas in replicas_tmp.items():
            for replica in replicas:
              key = os.path.join(task, replica)
              new_paths[key] = replicas_tmp[task][replica]
          to_removed_replicas = set(old_paths) - set(new_paths)
          if self._conf.deploy_type == DeployType.MIXED or self._conf.deploy_type == DeployType.PS:
            server_type = TFSServerType.PS
            port_grpc, port_archon = self._conf.tfs_port_grpc, self._conf.tfs_ps_archon_port
          elif self._conf.deploy_type == DeployType.DENSE:
            server_type = TFSServerType.DENSE
            port_grpc, port_archon = self._conf.tfs_dense_port, self._conf.tfs_dense_archon_port
          else:
            server_type = None

          need_register_replicas: Dict[str, ReplicaMeta] = {}
          if server_type and to_removed_replicas:
            for i in self._conf.get_server_schedule_iter(server_type):
              for replica in to_removed_replicas:
                if f'/{server_type}:{i}/{self._conf.replica_id}' in replica:
                  meta: ReplicaMeta = old_paths[replica]
                  meta.address = f"{meta.address.split(':')[0]}:{port_grpc}"
                  meta.address_ipv6 = f"{meta.address_ipv6.split(':')[0]}:{port_grpc}"
                  meta.archon_address = f"{meta.archon_address.split(':')[0]}:{port_archon}"
                  meta.archon_address_ipv6 = f"{meta.archon_address_ipv6.split(':')[0]}:{port_archon}"
                  need_register_replicas[replica] = meta

          # update self.replicas
          self.replicas = replicas_tmp

          # register
          while need_register_replicas:
            zk_path, meta = need_register_replicas.popitem()
            replica_meta_bytes = bytes(meta.to_json(), encoding='utf-8')
            try:
              self.zk.retry(self.zk.create,
                            path=zk_path,
                            value=replica_meta_bytes,
                            ephemeral=True,
                            makepath=True,
                            sequence=False)
            except NodeExistsError:
              logging.info(f'{zk_path} has already exists')
      except Exception as e:
        exc_type, exc_value, exc_traceback_obj = sys.exc_info()
        logging.log_every_n_seconds(logging.ERROR, f"exc_type: {exc_type}",
                                    10 * 60)

  def get_all_replicas(self,
                       server_type: ServerType,
                       idc: str = None,
                       cluster: str = None) -> Dict[str, List[str]]:
    st = ServerType.Name(server_type).lower()

    result = {}
    with self._lock:
      for path, replicas in self.replicas.items():
        zk_path = ZKPath(path)
        dc_flag = zk_path.ship_in(idc, cluster) if self._conf.dc_aware else True
        if zk_path.server_type == st and dc_flag:
          key = os.path.join(
              zk_path.location,
              zk_path.task) if self._conf.dc_aware else zk_path.task
          addrs = [
              pm.get_address(use_archon=self._use_archon,
                             address_family=self._zk_watch_address_family)
              for pm in replicas.values()
              if pm and pm.stat == ModelState.AVAILABLE
          ]
          if key in result:
            result[key].extend(addrs)
          else:
            result[key] = addrs

    if (server_type == ServerType.PS and
        len(result) == 0) or (server_type == ServerType.DENSE and
                              self._conf.dense_alone and len(result) == 0):
      logging.error(f'empty replicas {self.path_prefix}-{st}')
      logging.info('all replicas is ' + str(self.replicas))
    return result

  def get_replicas(self,
                   server_type: ServerType,
                   task: int,
                   idc: str = None,
                   cluster: str = None) -> List[str]:
    st = ServerType.Name(server_type).lower()
    with self._lock:
      addrs = []
      for path, replicas in self.replicas.items():
        zk_path = ZKPath(path)
        dc_flag = zk_path.ship_in(idc, cluster) if self._conf.dc_aware else True
        if zk_path.server_type == st and int(zk_path.index) == task and dc_flag:
          if replicas:
            addrs.extend([
                meta.get_address(use_archon=self._use_archon,
                                 address_family=self._zk_watch_address_family)
                for meta in replicas.values()
                if meta.stat == ModelState.AVAILABLE
            ])
      return addrs

  def get_replica(self,
                  server_type: ServerType,
                  task: int,
                  replica: int,
                  idc: str = None,
                  cluster: str = None) -> Optional[Union[List[str], str]]:
    st = ServerType.Name(server_type).lower()
    result = []
    with self._lock:
      for path, replicas in self.replicas.items():
        zk_path = ZKPath(path)
        dc_flag = zk_path.ship_in(idc, cluster) if self._conf.dc_aware else True
        if zk_path.server_type == st and int(zk_path.index) == task and dc_flag:
          for replica_id, meta in replicas.items():
            if int(replica_id) == replica:
              if meta is not None and meta.stat == ModelState.AVAILABLE:
                result.append(
                    meta.get_address(
                        use_archon=self._use_archon,
                        address_family=self._zk_watch_address_family))

    if result:
      if len(result) == 1:
        return result[0]
      else:
        return result
    else:
      return None

  def get_replicas_with_extra_info(self,
                                   server_type: ServerType,
                                   task: int,
                                   idc: str = None,
                                   cluster: str = None) -> List[str]:
    st = ServerType.Name(server_type).lower()
    with self._lock:
      addr_dict = {}
      for path, replicas in self.replicas.items():
        zk_path = ZKPath(path)
        dc_flag = zk_path.ship_in(idc, cluster) if self._conf.dc_aware else True
        if zk_path.server_type == st and int(zk_path.index) == task and dc_flag:
          if replicas:
            addr_dict.update({
                meta.get_address(use_archon=self._use_archon,
                                 address_family=self._zk_watch_address_family):
                ClientConfig.TargetExtraInfo(idc=zk_path.idc,
                                             cluster=zk_path.cluster,
                                             replica_id=int(replica_id))
                for replica_id, meta in replicas.items()
                if meta.stat == ModelState.AVAILABLE
            })
      return addr_dict

  def to_sync_wrapper(self) -> SyncBackend:
    return SyncBackendWrapper(self)


class ReplicaUpdater(object):

  def __init__(self, zk_client: MonolithKazooClient, config: AgentConfig):
    self._zk = zk_client
    self._conf: AgentConfig = config
    self.path_prefix = config.path_prefix

    self.model_monitor = TFSMonitor(config)

    self.meta = {}
    self._has_stop = False
    self._should_reregister = False
    self._should_update = True
    self._thread = None
    self._reregister_thread = None
    self._watch_update_thread = None

    self._entry_last_update_version = None
    self._metrics_cli = None
    self._metrics_cli_global = None
    self._tagkv = {'status': 'OK'}
    self._model_latest_version = {}
    self._model_last_update_ts = {}
    if self._conf.use_metrics:
      try:
        self.init_metrics()
      except:
        logging.error('init metrics error')
        exc_type, exc_value, exc_traceback_obj = sys.exc_info()
        logging.error(f"exc_type: {exc_type}")
        logging.error(f"exc_value: {exc_value}")
        traceback.print_tb(exc_traceback_obj, limit=10)
    else:
      logging.info("conf.use_metrics is false")

  def init_metrics(self):
    if "MONOLITH_METRIC_PREFIX" in os.environ: # In MLP Env
      prefix = os.environ.get("MONOLITH_METRIC_PREFIX")
    elif "TCE_PSM" in os.environ: # In  Byterec Env
      prefix = os.environ.get("TCE_PSM")
    else:
      prefix = "data.monolith_serving." + self._conf.base_name
    self._metrics_cli = cli.get_cli(prefix=prefix)
    self._metrics_cli_global = cli.get_cli(prefix="data.monolith_serving.global")
    logging.info(f"after init_metrics, prefix is {prefix}")

  @property
  def zk(self):
    return self._zk

  @property
  def model_names(self):
    names = []
    if self._conf.deploy_type == DeployType.MIXED or self._conf.deploy_type == DeployType.PS:
      for task_id in range(self._conf.num_ps):
        if task_id % self._conf.num_shard == self._conf.shard_id:
          names.append(f'{TFSServerType.PS}_{task_id}')

    if self._conf.deploy_type == DeployType.MIXED or self._conf.deploy_type == DeployType.ENTRY:
      names.append(TFSServerType.ENTRY)

    if self._conf.dense_alone and (self._conf.deploy_type == DeployType.MIXED or
                                   self._conf.deploy_type == DeployType.DENSE):
      names.append(f'{TFSServerType.DENSE}_0')

    return names

  @property
  def entry_path(self):
    return os.path.join(self.path_prefix, f'{TFSServerType.ENTRY}:0',
                        str(self._conf.replica_id))

  def ps_path(self, task_id: int):
    return os.path.join(self.path_prefix, f'{TFSServerType.PS}:{task_id}',
                        str(self._conf.replica_id))

  def dense_path(self):
    return os.path.join(self.path_prefix, f'{TFSServerType.DENSE}:0',
                        str(self._conf.replica_id))

  def _do_register(self, replica_path: str, grpc_port: int, archon_port: int):
    try:
      host = os.environ.get("MY_HOST_IP",
                            socket.gethostbyname(socket.gethostname()))
    except:
      host = '0.0.0.0'
    try:
      defalut_host_ipv6 = socket.getaddrinfo(socket.gethostname(), None,
                                             socket.AF_INET6)[0][4][0]
    except:
      defalut_host_ipv6 = '::'
    host_ipv6 = os.environ.get("MY_HOST_IPV6")
    if not host_ipv6:
      host_ipv6 = defalut_host_ipv6
    host_ipv6 = '[{}]'.format(host_ipv6)
    replica_meta = ReplicaMeta(address=f'{host}:{grpc_port}',
                               address_ipv6=f'{host_ipv6}:{grpc_port}',
                               stat=ModelState.UNKNOWN,
                               archon_address=f'{host}:{archon_port}',
                               archon_address_ipv6=f'{host_ipv6}:{archon_port}')
    self.meta[replica_path] = replica_meta
    replica_meta_bytes = bytes(replica_meta.to_json(), encoding='utf-8')
    node_stat = self.zk.exists(replica_path)
    if not node_stat:
      try:
        sequence = True if TFSServerType.ENTRY in replica_path and self._conf.replica_id == -1 else False
        real_path = self.zk.retry(self.zk.create,
                                  path=replica_path,
                                  value=replica_meta_bytes,
                                  ephemeral=True,
                                  makepath=True,
                                  sequence=sequence)
        if self._conf.replica_id == -1:
          self._conf.replica_id = int(os.path.basename(real_path))
          del self.meta[replica_path]
          self.meta[real_path] = replica_meta
      except NodeExistsError:
        logging.info(f'{replica_path} has already exists')
        self.zk.retry(self.zk.set, path=replica_path, value=replica_meta_bytes)
    else:
      value, _ = self.zk.get(replica_path)
      if value != replica_meta_bytes:
        self.zk.retry(self.zk.set, path=replica_path, value=replica_meta_bytes)

  def register(self):
    if self._conf.deploy_type == DeployType.MIXED or self._conf.deploy_type == DeployType.ENTRY:
      if self._conf.replica_id == -1:
        replica_path = f'{self.path_prefix}/{TFSServerType.ENTRY}:0/0'
      else:
        replica_path = f'{self.path_prefix}/{TFSServerType.ENTRY}:0/{self._conf.replica_id:011d}'
      self._do_register(replica_path, self._conf.tfs_entry_port,
                        self._conf.tfs_entry_archon_port)

    if self._conf.deploy_type == DeployType.MIXED or self._conf.deploy_type == DeployType.PS:
      for task_id in range(self._conf.num_ps):
        if task_id % self._conf.num_shard == self._conf.shard_id:
          self._do_register(self.ps_path(task_id), self._conf.tfs_ps_port,
                            self._conf.tfs_ps_archon_port)

    if self._conf.dense_alone and (self._conf.deploy_type == DeployType.MIXED or
                                   self._conf.deploy_type == DeployType.DENSE):
      self._do_register(self.dense_path(), self._conf.tfs_dense_port,
                        self._conf.tfs_dense_archon_port)

  def _do_update(self, name: str):
    if name.startswith(TFSServerType.ENTRY):
      replica_path = f'{self.path_prefix}/{TFSServerType.ENTRY}:0/{self._conf.replica_id:011d}'
    elif name.startswith(TFSServerType.PS):
      replica_path = self.ps_path(int(name.split("_")[1]))
    else:
      replica_path = self.dense_path()

    try:
      model_status = self.model_monitor.get_model_status(name)
    except Exception as e:
      replica_meta = self.meta[replica_path]
      if replica_meta.stat != ModelState.UNKNOWN:
        replica_meta.stat = ModelState.UNKNOWN
        replica_meta_bytes = bytes(replica_meta.to_json(), encoding='utf-8')
        try:
          self.zk.retry(self.zk.set,
                        path=replica_path,
                        value=replica_meta_bytes)
        except NoNodeError:
          self.zk.retry(self.zk.create,
                        path=replica_path,
                        value=replica_meta_bytes,
                        ephemeral=True,
                        makepath=True)
      return

    if model_status is not None and len(model_status) > 0:
      model_version_status = None
      if len(model_status) > 1:
        model_status.sort(key=lambda mvs: mvs.version, reverse=True)
        for m_status in model_status:
          if m_status.state == ModelState.AVAILABLE:
            model_version_status = m_status
            break
      if model_version_status is None:
        # check model version status
        model_version_status = model_status[0]

      status = model_version_status.status
      if status.error_code != ErrorCode.OK:
        raise Exception(status.error_message)

      # update state if changed
      stat = model_version_status.state
      replica_meta = self.meta[replica_path]
      if replica_meta.stat != stat:
        replica_meta.stat = stat
        replica_meta_bytes = bytes(replica_meta.to_json(), encoding='utf-8')
        try:
          self.zk.retry(self.zk.set,
                        path=replica_path,
                        value=replica_meta_bytes)
        except NoNodeError:
          self.zk.retry(self.zk.create,
                        path=replica_path,
                        value=replica_meta_bytes,
                        ephemeral=True,
                        makepath=True)

  def _updater(self):
    while not self._has_stop:
      curr_name = None
      time.sleep(1)
      if not self._should_update:
        continue
      try:
        for name in self.model_names:
          curr_name = name
          self._do_update(name)
      except Exception as e:
        exc_type, exc_value, exc_traceback_obj = sys.exc_info()
        logging.error(f"exc_type: {exc_type}")
        logging.error(f"exc_value: {exc_value}")
        traceback.print_tb(exc_traceback_obj, limit=10)
        logging.error(f"{e}, when model {curr_name} update")
      except (SystemExit, KeyboardInterrupt, GeneratorExit) as e:
        self._has_stop = True
        logging.error(f"{e}, when model {curr_name} update")

  def _get_latest_version_in_fs(self, name):
    exported_models_dir = os.path.join(self._conf.base_path, name)
    state = export_state_utils.get_export_saver_listener_state(
        exported_models_dir)
    if state.entries:
      return state.entries[-1].export_dir
    else:
      return None

  def _check_version(self):
    if not self._metrics_cli:
      return

    # metrics_v2
    for name in self.model_names:
      model_status = self.model_monitor.get_model_status(name)
      req_ts = int(time.time())
      if model_status is None or len(model_status) == 0:
        continue
      model_status.sort(key=lambda x: x.version, reverse=True)
      latest_model_status = model_status[0]

      latest_version = latest_model_status.version
      if latest_model_status.status.error_code != ErrorCode.OK:
        raise Exception(latest_model_status.status.error_message)

      tags = {
          "model_name": name,
          # It's safe when clueter or idc is None
          "idc": f"{self._conf.idc}:{self._conf.cluster}",
          "replica_id": str(self._conf.replica_id),
          "shard_id": str(self._conf.shard_id),
          "base_name": self._conf.base_name
      }
      self._metrics_cli.emit_store("serving_model.latest_version",
                                   latest_version, tags)
      if name in self._model_last_update_ts:
        interval = req_ts - self._model_last_update_ts[name]
        self._metrics_cli_global.emit_store("serving_model.since_last_update",
                                            interval, tags)
      if name not in self._model_latest_version or self._model_latest_version[
          name] < latest_version:
        self._metrics_cli.emit_store("serving_model.update_ts", req_ts, tags)
        self._model_latest_version[name] = latest_version
        self._model_last_update_ts[name] = req_ts
      self._metrics_cli.flush()

    return

  def _watch_update(self):
    if not self._metrics_cli:
      return

    while not self._has_stop:
      time.sleep(60)
      try:
        self._check_version()
      except:
        exc_type, exc_value, exc_traceback_obj = sys.exc_info()
        logging.error(f"exc_type: {exc_type}")
        logging.log_every_n_seconds(logging.WARNING, traceback.format_exc(),
                                    600)

  def _reregister(self):
    while not self._has_stop:
      time.sleep(10)
      if self._should_reregister:
        self.register()
        self._should_update = True
        # self._should_reregister = False

  def start(self):
    self.model_monitor.start()
    self._has_stop = False
    if self._thread is None:
      self._thread = threading.Thread(target=self._updater, daemon=True)
      self._thread.start()
    if self._reregister_thread is None:
      self._reregister_thread = threading.Thread(target=self._reregister,
                                                 daemon=True)
      self._reregister_thread.start()
    if self._watch_update_thread is None and self._metrics_cli is not None:
      self._watch_update_thread = threading.Thread(target=self._watch_update,
                                                   daemon=True)
      self._watch_update_thread.start()

  def stop(self):
    try:
      self._has_stop = True
      if self._thread is not None:
        try:
          self._thread.join()
        finally:
          self._thread = None
      if self._reregister_thread is not None:
        try:
          self._reregister_thread.join()
        finally:
          self._reregister_thread = None
      if self._watch_update_thread is not None:
        try:
          self._watch_update_thread.join()
        finally:
          self._watch_update_thread = None
    finally:
      self.model_monitor.stop()
      self.meta.clear()


class ZKListener(object):

  def __init__(self, watcher: ReplicaWatcher, updater: ReplicaUpdater):
    self._watcher: ReplicaWatcher = watcher
    self._updater: ReplicaUpdater = updater
    self._has_lost = False

  def __call__(self, state: KazooState) -> bool:
    if state == KazooState.LOST:
      # The connection has been confirmed dead
      logging.warning(
          "Any ephemeral nodes will need to be recreated upon re-establishing a connection."
      )
      self._has_lost = True
      self._watcher._should_poll = False
      self._updater._should_update = False
    elif state == KazooState.SUSPENDED:
      # Handle being disconnected from Zookeeper
      return False
    else:
      # Handle being connected/reconnected to Zookeeper
      if self._has_lost:
        logging.info(
            "connected/reconnected after lost, restart updater and watcher")
        self._updater._should_reregister = True
        time.sleep(5)  # wait for updater reregister
        self._watcher._should_poll = True
        self._has_lost = False

    return False


class ReplicaManager:

  def __init__(self, zk_client: MonolithKazooClient, config: AgentConfig):
    self._watcher = ReplicaWatcher(zk_client, config, DEFAULT_USE_ARCHON)
    self._updater = ReplicaUpdater(zk_client, config)
    self._conf = config

    listener = ZKListener(self._watcher, self._updater)
    zk_client.add_listener(listener)

  @property
  def watcher(self):
    return self._watcher

  @property
  def updater(self):
    return self._updater

  def start(self):
    self._updater.register()
    self._watcher.watch_data()
    self._updater.start()

  def stop(self):
    self._updater.stop()
    self._watcher.stop()

  def get_all_replicas(self,
                       server_type: ServerType,
                       idc: str = None,
                       cluster: str = None) -> Dict[str, List[str]]:
    return self._watcher.get_all_replicas(server_type, idc, cluster)

  def get_replicas(self,
                   server_type: ServerType,
                   task: int,
                   idc: str = None,
                   cluster: str = None) -> List[str]:
    return self._watcher.get_replicas(server_type, task, idc, cluster)

  def get_replica(self,
                  server_type: ServerType,
                  task: int,
                  replica: int,
                  idc: str = None,
                  cluster: str = None) -> Optional[Union[List[str], str]]:
    return self._watcher.get_replica(server_type, task, replica, idc, cluster)

  def is_ps_set_started(self):
    for i in range(self._conf.num_ps):
      replicas = self._watcher.get_replicas(ServerType.PS, i, self._conf.idc,
                                            self._conf.cluster)
      if replicas is None or len(replicas) == 0:
        return False
    logging.info(
        f"get_all_replicas: {self._watcher.get_all_replicas(ServerType.PS)}")
    return True

  def is_dense_set_started(self):
    replicas = self._watcher.get_replicas(ServerType.DENSE, 0)
    if replicas is None or len(replicas) == 0:
      return False
    logging.info(f"get_replicas: {replicas}")
    return True


class SyncBackendWrapper(SyncBackend):

  def __init__(self, watcher: ReplicaWatcher):
    super(SyncBackendWrapper, self).__init__()
    self._watcher = watcher
    self._model_name = None

  def subscribe_model(self, model_name: str):
    self._model_name = model_name

  def get_sync_targets(self, sub_graph: str) -> Tuple[str, Dict]:
    ps, i = sub_graph.split("_")[:2]
    assert ps == "ps"
    return sub_graph, self._watcher.get_replicas_with_extra_info(
        ServerType.PS, int(i))

  def start(self):
    self._watcher.watch_data()

  def stop(self):
    self._watcher.stop()
