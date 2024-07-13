# -*- coding: utf-8 -*-
"""
TencentBlueKing is pleased to support the open source community by making 蓝鲸智云-节点管理(BlueKing-BK-NODEMAN) available.
Copyright (C) 2017-2022 THL A29 Limited, a Tencent company. All rights reserved.
Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
You may obtain a copy of the License at https://opensource.org/licenses/MIT
Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
specific language governing permissions and limitations under the License.
"""
from __future__ import absolute_import, unicode_literals

import logging
import time
from collections import OrderedDict, defaultdict
from copy import deepcopy
from functools import wraps
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from django.db.models import Value
from django.utils.translation import ugettext as _
from memory_profiler import profile

from apps.backend.celery import app
from apps.backend.components.collections.base import ActivityType
from apps.backend.subscription import handler, tools
from apps.backend.subscription.constants import TASK_HOST_LIMIT
from apps.backend.subscription.errors import (
    SubscriptionInstanceCountMismatchError,
    SubscriptionInstanceEmpty,
)
from apps.backend.subscription.steps import StepFactory, agent
from apps.core.gray.tools import GrayTools
from apps.node_man import constants, models
from apps.node_man import tools as node_man_tools
from apps.node_man.handlers.cmdb import CmdbHandler
from apps.prometheus import metrics
from apps.utils import md5, translation
from pipeline import builder
from pipeline.builder import Data, NodeOutput, ServiceActivity, Var
from pipeline.parser import PipelineParser
from pipeline.service import task_service

logger = logging.getLogger("app")


def handle_auto_trigger_dirty_task_data(task: models.SubscriptionTask, err: Exception) -> bool:
    if not task.id:
        return False
    cnt, __ = models.SubscriptionTask.objects.filter(id=task.id).delete()
    if not isinstance(err, SubscriptionInstanceEmpty):
        models.SubscriptionInstanceRecord.objects.filter(task_id=task.id).delete()
    return cnt == 1


def handle_manual_dirty_task_data(task: models.SubscriptionTask, err: Exception):
    # 任务创建失败is_ready置为False，防止create_task_transaction置为True但实际任务创建失败的情况
    # 例如：can not find celery workers - pipeline run 报错
    task.is_ready = False
    task.err_msg = str(err)
    task.save(update_fields=["err_msg", "is_ready"])
    if not isinstance(err, SubscriptionInstanceEmpty):
        models.SubscriptionInstanceRecord.objects.filter(task_id=task.id).delete()


def mark_acts_tail_and_head(activities: List[ServiceActivity]) -> None:
    """标记头尾节点，用于在开始和结束时实现一些钩子"""
    if len(activities) == 1:
        activities[0].component.inputs.act_type = Var(type=Var.PLAIN, value=ActivityType.HEAD_TAIL)
        return
    activities[0].component.inputs.act_type = Var(type=Var.PLAIN, value=ActivityType.HEAD)
    activities[-1].component.inputs.act_type = Var(type=Var.PLAIN, value=ActivityType.TAIL)


def build_instances_task(
    subscription_instances: List[models.SubscriptionInstanceRecord],
    meta: Dict[str, Any],
    step_actions: Dict[str, str],
    subscription: models.Subscription,
    global_pipeline_data: Data,
) -> ServiceActivity:
    """
    对同类step_actions任务进行任务编排
    :param subscription_instances: 订阅实例列表
    :param meta: 流程元数据
    :param step_actions: {"basereport": "MAIN_INSTALL_PLUGIN"}
    :param subscription: 订阅对象
    :param global_pipeline_data: 全局pipeline公共变量
    :return:
    """
    # 首先获取当前订阅对应步骤的工厂类
    step_id_manager_map = OrderedDict()
    for step in subscription.steps:
        step_id_manager_map[step.step_id] = StepFactory.get_step_manager(step)
    step_map = {step.step_id: step for step in subscription.steps}

    step_index = 0
    step_id_record_step_map = {}
    for step_id in step_id_manager_map:
        if step_id not in step_actions:
            continue
        step_id_record_step_map[step_id] = {
            "id": step_id,
            "type": step_map[step_id].type,
            "index": step_index,
            "pipeline_id": "",
            "action": step_actions[step_id],
            "extra_info": {},
        }
        step_index += 1

    # 将 step_actions 信息注入 meta
    inject_meta: Dict[str, Any] = {**meta, "STEPS": list(step_id_record_step_map.values())}

    # 对流程步骤进行编排
    current_activities = []
    subscription_instance_ids = [sub_inst.id for sub_inst in subscription_instances]
    for step_id in step_id_manager_map:
        if step_id not in step_actions:
            continue

        step_manager = step_id_manager_map[step_id]
        action_name = step_actions[step_manager.step_id]
        action_manager = step_manager.create_action(action_name, subscription_instances)

        activities, __ = action_manager.generate_activities(
            subscription_instances,
            global_pipeline_data=global_pipeline_data,
            meta=inject_meta,
            current_activities=current_activities,
        )

        # 记录每个 step 的起始 id 及步骤名称
        step_id_record_step_map[step_id].update(
            pipeline_id=activities[0].id,
            node_name=_("[{step_id}] {action_description}").format(
                step_id=step_id, action_description=action_manager.ACTION_DESCRIPTION
            ),
        )

        current_activities.extend(activities)

        # 在 Update 之前写指标，便于后续统计因死锁等情况引发的任务丢失率
        metrics.app_task_engine_sub_inst_step_statuses_total.labels(
            step_id=step_id,
            step_type=step_id_record_step_map[step_id]["type"],
            step_num=len(inject_meta["STEPS"]),
            step_index=step_id_record_step_map[step_id]["index"],
            gse_version=meta.get("GSE_VERSION") or "unknown",
            action=step_id_record_step_map[step_id]["action"],
            code="CreatePipeline",
            status=constants.JobStatusType.PENDING,
        ).inc(amount=len(subscription_instance_ids))

    mark_acts_tail_and_head(current_activities)

    logger.info(
        "[sub_lifecycle<sub(%s), task(%s)>][build_instances_task] inject_meta -> %s, step_id_record_step_map -> %s",
        subscription.id,
        subscription_instances[0].task_id,
        inject_meta,
        step_id_record_step_map,
    )

    # 每个原子引用上个原子成功输出的 succeeded_subscription_instance_ids
    for index, act in enumerate(current_activities):
        if index == 0:
            # 第一个activity 需传入初始的 subscription_instance_ids
            act.component.inputs.subscription_instance_ids = Var(type=Var.PLAIN, value=subscription_instance_ids)
            continue

        source_act_id = current_activities[index - 1].id
        succeeded_ids_output_name = f"${{succeeded_subscription_instance_ids_{act.id}}}"
        act.component.inputs.succeeded_subscription_instance_ids = Var(type=Var.SPLICE, value=succeeded_ids_output_name)
        global_pipeline_data.inputs[succeeded_ids_output_name] = NodeOutput(
            type=Var.SPLICE, source_act=source_act_id, source_key="succeeded_subscription_instance_ids"
        )

    instance_start = current_activities[0]
    current_node = deepcopy(instance_start)
    # 用extend串联这些activities
    for act in current_activities:
        if act:
            current_node = current_node.extend(act)

    models.SubscriptionInstanceRecord.objects.filter(id__in=subscription_instance_ids).update(
        pipeline_id=instance_start.id, steps=list(step_id_record_step_map.values())
    )
    return instance_start


@profile
def create_pipeline(
    subscription: models.Subscription,
    instances_action: Dict[str, Dict[str, str]],
    subscription_instances: List[models.SubscriptionInstanceRecord],
) -> Dict[str, Any]:
    """
      批量执行实例的步骤的动作
      :param subscription: Subscription
      :param instances_action: {
          "instance_id_xxx": {
              "step_id_x": "INSTALL",
              "step_id_y": "UNINSTALL,
          }
      }
      :param subscription_instances
    构造形如以下的pipeline，根据 ${TASK_HOST_LIMIT} 来决定单条流水线的执行机器数量
                         StartEvent
                             |
                      ParallelGateway
                             |
          ---------------------------------------
          |                  |                  |
    500_host_init      500_host_init         .......
          |                  |                  |
    install_plugin     install_plugin        .......
          |                  |                  |
    render_config      render_config         .......
          |                  |                  |
       .......            .......            .......
          |                  |                  |
          ---------------------------------------
                             |
                       ConvergeGateway
                             |
                          EndEvent
    """

    md5__metadata: Dict[str, Dict[str, Any]] = {}
    sub_insts_gby_metadata_md5: Dict[str, List[models.SubscriptionInstanceRecord]] = defaultdict(list)
    subscription_instance_map: Dict[str, models.SubscriptionInstanceRecord] = {
        subscription_instance.instance_id: subscription_instance for subscription_instance in subscription_instances
    }
    for instance_id, step_actions in instances_action.items():
        if instance_id not in subscription_instance_map:
            continue

        sub_inst = subscription_instance_map[instance_id]
        # metadata 包含：meta-任务元数据、step_actions-操作步骤及类型
        metadata = {"meta": sub_inst.instance_info["meta"], "step_actions": step_actions}

        # 聚合同 metadata 的任务
        metadata_md5 = md5.count_md5(metadata)
        md5__metadata[metadata_md5] = metadata
        sub_insts_gby_metadata_md5[metadata_md5].append(sub_inst)

    global_pipeline_data = Data()
    sub_process_ids: List[str] = []
    start_event = builder.EmptyStartEvent()
    sub_processes: List[ServiceActivity] = []
    subpid__actions_map: Dict[str, Union[int, Dict[str, str]]] = {}
    task_host_limit = models.GlobalSettings.get_config(
        models.GlobalSettings.KeyEnum.TASK_HOST_LIMIT.value, default=TASK_HOST_LIMIT
    )
    for metadata_md5, sub_insts in sub_insts_gby_metadata_md5.items():
        start = 0
        metadata = md5__metadata[metadata_md5]
        while start < len(sub_insts):
            sub_inst_chunk: List[models.SubscriptionInstanceRecord] = sub_insts[start : start + task_host_limit]
            activities_start_event: ServiceActivity = build_instances_task(
                sub_inst_chunk,
                metadata["meta"],
                metadata["step_actions"],
                subscription,
                global_pipeline_data,
            )
            sub_processes.append(activities_start_event)
            sub_process_ids.append(activities_start_event.id)
            subpid__actions_map[activities_start_event.id] = {
                "count": len(sub_inst_chunk),
                "step_actions": metadata["step_actions"],
            }

            start = start + task_host_limit

    end_event = builder.EmptyEndEvent()
    parallel_gw = builder.ParallelGateway()
    converge_gw = builder.ConvergeGateway()
    start_event.extend(parallel_gw).connect(*sub_processes).to(parallel_gw).converge(converge_gw).extend(end_event)

    # 构造pipeline树
    tree = builder.build_tree(start_event, data=global_pipeline_data)
    models.PipelineTree.objects.create(id=tree["id"], tree=tree)
    pipeline_id = PipelineParser(pipeline_tree=tree).parse().id

    return {"id": pipeline_id, "subpid__actions_map": subpid__actions_map}


def create_task_transaction(create_task_func):
    """创建任务事务装饰器，用于创建时发生异常时记录错误或抛出异常回滚"""

    @wraps(create_task_func)
    def wrapper(subscription: models.Subscription, subscription_task: models.SubscriptionTask, *args, **kwargs):
        logger.info(
            "[sub_lifecycle<sub(%s), task(%s)>][create_task_transaction] enter, sub -> %s, task -> %s",
            subscription.id,
            subscription_task.id,
            subscription,
            subscription_task,
        )
        try:
            func_return = create_task_func(subscription, subscription_task, *args, **kwargs)
        except Exception as err:
            logger.exception(
                "[sub_lifecycle<sub(%s), task(%s)>][create_task_transaction] failed",
                subscription.id,
                subscription_task.id,
            )
            if subscription_task.is_auto_trigger or kwargs.get("preview_only"):
                # 自动触发的发生异常或者仅预览的情况，记录日志后直接删除此任务即可
                handle_auto_trigger_dirty_task_data(subscription_task, err)
                # 抛出异常用于前端展示或事务回滚
                raise err
            handle_manual_dirty_task_data(subscription_task, err)
        else:
            if kwargs.get("preview_only"):
                # 仅预览，不执行动作
                return func_return

            # 设置任务就绪
            subscription_task.is_ready = True
            logger.info(
                "[sub_lifecycle<sub(%s), task(%s)>][create_task_transaction] task ready, actions -> %s",
                subscription.id,
                subscription_task.id,
                subscription_task.actions,
            )
            subscription_task.save(update_fields=["is_ready"])

            task_throttle(subscription, subscription_task, func_return["subpid__actions_map"])
            run_subscription_task(subscription_task)

            return func_return

    return wrapper


def task_throttle(
    subscription: models.Subscription,
    subscription_task: models.SubscriptionTask,
    subpid__actions_map: Dict[str, Union[int, Dict[str, str]]],
):
    # TODO 申请配额
    for sub_process_id, actions in subpid__actions_map.items():
        logger.info(
            "[sub_lifecycle<sub(%s), task(%s)>][task_throttle] sub_process_id -> %s, actions -> %s",
            subscription.id,
            subscription_task.id,
            sub_process_id,
            actions,
        )
        task_service.pause_activity(sub_process_id)

    return


def run_subscription_task_and_create_instance_transaction(func):
    """创建任务事务装饰器，用于创建时发生异常时记录错误或抛出异常回滚"""

    @wraps(func)
    def wrapper(subscription: models.Subscription, subscription_task: models.SubscriptionTask, *args, **kwargs):
        logger.info(
            "[sub_lifecycle<sub(%s), task(%s)>][run_subscription_task_and_create_instance_transaction] "
            "enter, sub -> %s, task -> %s",
            subscription.id,
            subscription_task.id,
            subscription,
            subscription_task,
        )
        try:
            func_result = func(subscription, subscription_task, *args, **kwargs)
        except Exception as err:
            logger.exception(
                "[sub_lifecycle<sub(%s), task(%s)>][run_subscription_task_and_create_instance_transaction] failed",
                subscription.id,
                subscription_task.id,
            )
            if subscription_task.is_auto_trigger or kwargs.get("preview_only"):
                # 自动触发的发生异常或者仅预览的情况
                handle_auto_trigger_dirty_task_data(subscription_task, err)
                # 抛出异常用于前端展示或事务回滚
                raise err
            handle_manual_dirty_task_data(subscription_task, err)
            return {}
        else:
            return func_result

    return wrapper


def create_sub_insts(
    subscription: models.Subscription,
    subscription_task: models.SubscriptionTask,
    instance_actions: Dict[str, Dict[str, str]],
    instances: Dict[str, Dict[str, Union[Dict, Any]]],
) -> Tuple[List[Dict[str, Any]], Dict[str, models.SubscriptionInstanceRecord]]:
    def _fetch_host_ids() -> Set[int]:
        return {
            instance_info["host"]["bk_host_id"]
            for instance_info in instances.values()
            if instance_info["host"].get("bk_host_id")
        }

    # func cache
    host_ids: Optional[Set[int]] = None
    topo_order: Optional[List[str]] = None
    plugin__host_id__bk_obj_sub_map: Dict[str, Dict[int, List[Dict]]] = {}

    # 前置错误需要跳过的主机，不创建订阅任务实例
    error_hosts: List[Dict[str, Any]] = []
    to_be_injected_instances: Dict[str, Dict[str, Union[Dict, Any]]] = {}
    to_be_created_records_map: Dict[str, models.SubscriptionInstanceRecord] = {}

    for instance_id, step_action in instance_actions.items():
        if instance_id not in instances:
            # instance_id不在instances中，则说明该实例可能已经不在该业务中，因此无法操作，故不处理。
            continue

        # 新装AGENT或PROXY会保存安装信息，需要清理
        need_clean = step_action.get("agent") in [
            agent.InstallAgent.ACTION_NAME,
            agent.InstallAgent2.ACTION_NAME,
            agent.InstallProxy.ACTION_NAME,
            agent.InstallProxy2.ACTION_NAME,
        ]
        record = models.SubscriptionInstanceRecord(
            task_id=subscription_task.id,
            subscription_id=subscription.id,
            instance_id=instance_id,
            instance_info=instances[instance_id],
            steps=[],
            is_latest=True,
            need_clean=need_clean,
        )
        to_be_injected_instances[instance_id] = record.instance_info

        # agent 任务无需检查抑制情况
        if "agent" in step_action:
            to_be_created_records_map[instance_id] = record
            continue

        is_suppressed = False
        for step_id, action in step_action.items():
            # 提前判断是否需要进行抑制检查，不需要的情况下直接返回，减少无效数据的查询
            if not subscription.need_suppression_check(action):
                continue

            host_ids = host_ids if host_ids else _fetch_host_ids()
            topo_order = topo_order if topo_order else CmdbHandler.get_topo_order()

            if step_id in plugin__host_id__bk_obj_sub_map:
                host_id__bk_obj_sub_map = plugin__host_id__bk_obj_sub_map.get(step_id)
            else:
                host_id__bk_obj_sub_map = models.Subscription.get_host_id__bk_obj_sub_map(host_ids, step_id)
                plugin__host_id__bk_obj_sub_map[step_id] = host_id__bk_obj_sub_map

            host_info = record.instance_info["host"]
            # 检查订阅策略间的抑制关系
            result = subscription.check_is_suppressed(
                action=action,
                cmdb_host_info=host_info,
                topo_order=topo_order,
                host_id__bk_obj_sub_map=host_id__bk_obj_sub_map,
            )
            is_suppressed = result["is_suppressed"]
            if is_suppressed:
                # 策略被抑制，跳过部署，记为已忽略
                suppressed_by_id: int = result["suppressed_by"]["subscription_id"]
                suppressed_by_name: str = result["suppressed_by"]["name"]
                error_hosts.append(
                    {
                        "ip": host_info.get("bk_host_innerip") or host_info.get("bk_host_innerip_v6"),
                        "inner_ip": host_info.get("bk_host_innerip"),
                        "inner_ipv6": host_info.get("bk_host_innerip_v6"),
                        "bk_host_id": host_info.get("bk_host_id"),
                        "bk_biz_id": host_info.get("bk_biz_id"),
                        "bk_cloud_id": host_info.get("bk_cloud_id"),
                        "suppressed_by_id": suppressed_by_id,
                        "suppressed_by_name": suppressed_by_name,
                        "os_type": node_man_tools.HostV2Tools.get_os_type(host_info),
                        "status": constants.JobStatusType.IGNORED,
                        "msg": _(
                            "当前{category_alias}（{bk_obj_name} 级）"
                            "已被优先级更高的{suppressed_by_category_alias}【{suppressed_by_name}(ID: {suppressed_by_id})】"
                            "（{suppressed_by_obj_name} 级）抑制"
                        ).format(
                            category_alias=models.Subscription.CATEGORY_ALIAS_MAP[result["category"]],
                            bk_obj_name=constants.CmdbObjectId.OBJ_ID_ALIAS_MAP.get(
                                result["sub_inst_bk_obj_id"],
                                constants.CmdbObjectId.OBJ_ID_ALIAS_MAP[constants.CmdbObjectId.CUSTOM],
                            ),
                            suppressed_by_category_alias=models.Subscription.CATEGORY_ALIAS_MAP[
                                result["suppressed_by"]["category"]
                            ],
                            suppressed_by_name=suppressed_by_name,
                            suppressed_by_id=suppressed_by_id,
                            suppressed_by_obj_name=constants.CmdbObjectId.OBJ_ID_ALIAS_MAP.get(
                                result["suppressed_by"]["bk_obj_id"],
                                constants.CmdbObjectId.OBJ_ID_ALIAS_MAP[constants.CmdbObjectId.CUSTOM],
                            ),
                        ),
                    }
                )
                break

        if is_suppressed:
            continue

        to_be_created_records_map[instance_id] = record

    if to_be_injected_instances:
        # 兜底注入 Meta，此处注入是覆盖面最全的（包含历史被移除实例）
        GrayTools().inject_meta_to_instances(to_be_injected_instances)
    logger.info(
        "[sub_lifecycle<sub(%s), task(%s)>][create_sub_insts] inject meta to instances[num=%s] successfully",
        subscription.id,
        subscription_task.id,
        len(to_be_injected_instances),
    )

    return error_hosts, to_be_created_records_map


def fill_id_to_sub_insts(
    task_id: int, subscription_id: int, subscription_instances: List[models.SubscriptionInstanceRecord]
):
    inst_id_map: [str, int] = dict(
        models.SubscriptionInstanceRecord.objects.filter(task_id=task_id, is_latest=Value(1)).values_list(
            "instance_id", "id"
        )
    )
    if len(inst_id_map) != len(subscription_instances):
        logger.error(
            "[sub_lifecycle<sub(%s), task(%s)>][create_task] subscription instances count mismatch, "
            "expect -> %s, actual -> %s",
            subscription_id,
            task_id,
            len(subscription_instances),
            len(inst_id_map),
        )
        raise SubscriptionInstanceCountMismatchError

    for sub_inst in subscription_instances:
        sub_inst.id = inst_id_map[sub_inst.instance_id]

    metrics.app_task_engine_sub_inst_statuses_total.labels(status=constants.JobStatusType.PENDING).inc(
        len(subscription_instances)
    )


@app.task(queue="backend", ignore_result=True)
@translation.RespectsLanguage()
@profile
@create_task_transaction
@profile
def create_task(
    subscription: models.Subscription,
    subscription_task: models.SubscriptionTask,
    instances: Dict[str, Dict[str, Union[Dict, Any]]],
    instance_actions: Dict[str, Dict[str, str]],
    preview_only: bool = False,
) -> Dict[str, Any]:
    """
    创建执行任务
    :param preview_only: 是否仅预览
    :param subscription: Subscription
    :param subscription_task: SubscriptionTask
    :param instances: dict
    :param instance_actions: {
        "instance_id_xxx": {
            "step_id_x": "INSTALL",
            "step_id_y": "UNINSTALL,
        }
    }
    :return: SubscriptionTask
    """

    def _handle_empty():
        if subscription_task.is_auto_trigger:
            # 如果是自动触发，且没有任何实例，那么直接抛出异常，回滚数据库
            raise SubscriptionInstanceEmpty()

        # 非自动触发的直接退出即可
        logger.warning(
            "[sub_lifecycle<sub(%s), task(%s)>][create_task] no instances to execute",
            subscription.id,
            subscription_task.id,
        )
        return {}

    error_hosts, to_be_created_records_map = create_sub_insts(
        subscription, subscription_task, instance_actions, instances
    )

    if preview_only:
        # 仅做预览，不执行下面的代码逻辑，直接返回计算后的结果
        return {
            "to_be_created_records_map": to_be_created_records_map,
            "error_hosts": error_hosts,
        }

    # 这里反写到 job 表里，与SaaS逻辑耦合了，需解耦
    if error_hosts:
        models.Job.objects.filter(subscription_id=subscription.id, task_id_list__contains=subscription_task.id).update(
            error_hosts=error_hosts
        )

    if not to_be_created_records_map:
        return _handle_empty()

    # TODO [偶发死锁] 将最新属性置为False并批量创建订阅实例
    models.SubscriptionInstanceRecord.objects.filter(
        subscription_id=subscription.id, instance_id__in=list(instance_actions.keys())
    ).update(is_latest=False)

    # TODO [偶发死锁]
    batch_size = models.GlobalSettings.get_config("BATCH_SIZE", default=100)
    subscription_instances: List[models.SubscriptionInstanceRecord] = list(to_be_created_records_map.values())
    models.SubscriptionInstanceRecord.objects.bulk_create(subscription_instances, batch_size=batch_size)
    fill_id_to_sub_insts(subscription_task.id, subscription.id, subscription_instances)

    create_pipeline_result = create_pipeline(subscription, instance_actions, subscription_instances)
    # 保存 pipeline id
    subscription_task.pipeline_id = create_pipeline_result["id"]
    # 保存 instance_actions，用于重试场景
    subscription_task.actions = instance_actions
    subscription_task.save(update_fields=["actions", "pipeline_id"])
    logger.info(
        "[sub_lifecycle<sub(%s), task(%s)>][create_task] instance_actions -> %s",
        subscription.id,
        subscription_task.id,
        instance_actions,
    )

    return {"subpid__actions_map": create_pipeline_result["subpid__actions_map"]}


@app.task(queue="backend", ignore_result=True)
@translation.RespectsLanguage()
@run_subscription_task_and_create_instance_transaction
def run_subscription_task_and_create_instance(
    subscription: models.Subscription,
    subscription_task: models.SubscriptionTask,
    scope: Optional[Dict] = None,
    actions: Optional[Dict] = None,
    preview_only: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    自动检查实例及配置的变更，执行相应动作
    :param preview_only: 是否仅预览，若为true则不做任何保存或执行动作
    :param subscription: Subscription
    :param subscription_task: SubscriptionTask
    :param scope
    {
        "bk_biz_id": 2,
        "nodes": [],
        "node_type": "INSTANCE",
        "object_type": "HOST"
    }
    :param actions {
        "step_id_xxx": "INSTALL"
    }
    """
    # 如果不传范围，则使用订阅全部范围
    if not scope:
        scope = subscription.scope
    else:
        scope["object_type"] = subscription.object_type
        scope["bk_biz_id"] = subscription.bk_biz_id

    # 获取订阅范围内全部实例
    steps = subscription.steps
    tolerance_time: int = (59, 0)[subscription.is_need_realtime()]
    instances = tools.get_instances_by_scope_with_checker(
        scope, steps, source="run_subscription_task_and_create_instance", tolerance_time=tolerance_time
    )
    logger.info(
        "[sub_lifecycle<sub(%s), task(%s)>][run_subscription_task_and_create_instance] "
        "get_instances_by_scope_with_checker -> %s",
        subscription.id,
        subscription_task.id,
        len(instances),
    )

    # 创建步骤管理器实例
    step_managers = {step.step_id: StepFactory.get_step_manager(step) for step in subscription.steps}
    # 删除无用subscription缓存，否则执行延时任务时传入可能引起pickle异常
    if hasattr(subscription, "_steps"):
        delattr(subscription, "_steps")

    logger.info(
        "[sub_lifecycle<sub(%s), task(%s)>][run_subscription_task_and_create_instance] step_managers -> %s",
        subscription.id,
        subscription_task.id,
        step_managers,
    )

    if actions is not None:
        # 指定了动作，不需要计算，直接执行即可
        instance_actions = {instance_id: actions for instance_id in instances}
        create_task(subscription, subscription_task, instances, instance_actions)
        return

    # 预注入 Meta，用于变更计算（仅覆盖当前订阅范围，移除场景通过 create_task 兜底注入）
    GrayTools().inject_meta_to_instances(instances)
    logger.info(
        "[sub_lifecycle<sub(%s), task(%s)>][run_subscription_task_and_create_instance] "
        "pre-inject meta to instances[num=%s] successfully",
        subscription.id,
        subscription_task.id,
        len(instances),
    )

    # 按步骤顺序计算实例变更所需的动作
    instance_actions = defaultdict(dict)
    instance_migrate_reasons = defaultdict(dict)
    is_unknown_migrate_type_exists: bool = False
    for step in step_managers.values():
        # 计算变更的动作
        migrate_results = step.make_instances_migrate_actions(
            instances, auto_trigger=subscription_task.is_auto_trigger, preview_only=preview_only
        )
        # 归类变更动作
        # eg: {"host|instance|host|1": "MAIN_INSTALL_PLUGIN"}
        instance_id_action_map: Dict[str, str] = migrate_results["instance_actions"]
        for instance_id, action in instance_id_action_map.items():
            instance_actions[instance_id][step.step_id] = action
            metrics.app_task_instances_migrate_actions_total.labels(step_id=step.step_id, action=action).inc()

        # 归类变更原因
        # eg:
        # {
        #   "host|instance|host|1": {
        #     "processbeat": {
        #       "target_version": "1.17.66",
        #       "current_version": "1.17.66",
        #       "migrate_type": "NOT_CHANGE"
        #     }
        #   }
        # }
        instance_id_action_reason_map: Dict[str, Dict] = migrate_results["migrate_reasons"]
        for instance_id, migrate_reason in instance_id_action_reason_map.items():
            instance_migrate_reasons[instance_id][step.step_id] = migrate_reason
            if "migrate_type" not in migrate_reason:
                is_unknown_migrate_type_exists = True
            metrics.app_task_instances_migrate_reasons_total.labels(
                step_id=step.step_id, reason=migrate_reason.get("migrate_type") or "UNKNOWN"
            ).inc()

    logger.info(
        "[sub_lifecycle<sub(%s), task(%s)>][run_subscription_task_and_create_instance] make_instances"
        "_migrate_actions: instance_actions -> %s, migrate_reasons -> %s, unknown_migrate_type_exists -> %s",
        subscription.id,
        subscription_task.id,
        instance_actions,
        instance_migrate_reasons,
        ("no", "unknown_migrate_type_exists")[is_unknown_migrate_type_exists],
    )

    # 查询被从范围内移除的实例
    instance_not_in_scope = [instance_id for instance_id in instance_actions if instance_id not in instances]
    if instance_not_in_scope:
        deleted_id_not_in_scope = []
        for instance_id in instance_not_in_scope:
            if subscription.object_type == models.Subscription.ObjectType.HOST:
                host_key = tools.parse_node_id(instance_id)["id"]
                deleted_id_not_in_scope.append(tools.parse_host_key(host_key))
            else:
                service_instance_id = tools.parse_node_id(instance_id)["id"]
                deleted_id_not_in_scope.append({"id": service_instance_id})

        logger.info(
            "[sub_lifecycle<sub(%s), task(%s)>][run_subscription_task_and_create_instance] "
            "try to find deleted instances -> %s",
            subscription.id,
            subscription_task.id,
            deleted_id_not_in_scope,
        )
        deleted_instance_info = tools.get_instances_by_scope_with_checker(
            {
                "bk_biz_id": subscription.bk_biz_id,
                "object_type": subscription.object_type,
                "node_type": models.Subscription.NodeType.INSTANCE,
                "nodes": deleted_id_not_in_scope,
            },
            steps,
            source="find_deleted_instances",
        )

        # 如果被删掉的实例在 CMDB 找不到，那么就使用最近一次的 InstanceRecord 的快照数据
        not_exist_instance_id = set(instance_not_in_scope) - set(deleted_instance_info)
        latest_instance_ids = set()
        if not_exist_instance_id:
            records = list(
                models.SubscriptionInstanceRecord.objects.filter(
                    subscription_id=subscription.id, instance_id__in=not_exist_instance_id, is_latest=Value(1)
                )
            )
            for record in records:
                deleted_instance_info[record.instance_id] = record.instance_info
                latest_instance_ids.add(record.instance_id)

            logger.info(
                "[sub_lifecycle<sub(%s), task(%s)>][run_subscription_task_and_create_instance] "
                "deleted instances not exist in cc, find from db -> %s, find num -> %s",
                subscription.id,
                subscription_task.id,
                not_exist_instance_id,
                len(records),
            )
        not_latest_instance_ids: Set[str] = not_exist_instance_id - latest_instance_ids
        exist_db_instance_id_set = set()
        if not_latest_instance_ids:
            sub_inst_record_qs = models.SubscriptionInstanceRecord.objects.filter(
                subscription_id=subscription.id, instance_id__in=not_latest_instance_ids, is_latest=Value(0)
            )
            max_instance_record_ids: List[int] = handler.SubscriptionTools.fetch_latest_record_ids_in_same_inst_id(
                sub_inst_record_qs
            )
            instance_records = models.SubscriptionInstanceRecord.objects.filter(id__in=max_instance_record_ids).values(
                "instance_id", "instance_info"
            )
            for instance_record in instance_records:
                deleted_instance_info[instance_record["instance_id"]] = instance_record["instance_info"]
                exist_db_instance_id_set.add(instance_record["instance_id"])

            not_exist_db_instance_id_set: Set[str] = not_latest_instance_ids - exist_db_instance_id_set
            logger.info(
                "[sub_lifecycle<sub(%s), task(%s)>][run_subscription_task_and_create_instance] "
                "deleted instances not latest, find latest record from db -> %s, find num -> %s, "
                "can't find latest record from db -> %s, num -> %s",
                subscription.id,
                subscription_task.id,
                not_latest_instance_ids,
                len(instance_records),
                not_exist_db_instance_id_set,
                len(not_exist_db_instance_id_set),
            )

        instances.update(deleted_instance_info)

    create_task_result = create_task(
        subscription, subscription_task, instances, instance_actions, preview_only=preview_only
    )

    if preview_only:
        return {
            "to_be_created_records_map": create_task_result.get("to_be_created_records_map") or {},
            "error_hosts": create_task_result.get("error_hosts") or [],
            "instance_actions": instance_actions,
            "instance_migrate_reasons": instance_migrate_reasons,
            "instance_id__inst_info_map": instances,
        }


@translation.RespectsLanguage()
def run_subscription_task(subscription_task: models.SubscriptionTask):
    logger.info(
        "[sub_lifecycle<sub(%s), task(%s)>][run_subscription_task] enter",
        subscription_task.subscription_id,
        subscription_task.id,
    )

    def _log_args() -> Tuple[int, int, str]:
        return subscription_task.subscription_id, subscription_task.id, subscription_task.pipeline_id

    from apps.node_man.models import PipelineTree

    try:
        pipeline = PipelineTree.objects.get(id=subscription_task.pipeline_id)
    except PipelineTree.DoesNotExist:
        logger.error("[sub_lifecycle<sub(%s), task(%s)>][run_subscription_task] pipeline not found -> %s", *_log_args())
        return

    try:
        pipeline.run(1)
    except Exception:
        logger.exception(
            "[sub_lifecycle<sub(%s), task(%s)>][run_subscription_task] failed to run pipeline -> %s", *_log_args()
        )
    else:
        logger.info("[sub_lifecycle<sub(%s), task(%s)>][run_subscription_task] run pipeline -> %s", *_log_args())
    finally:
        del pipeline


@app.task(queue="backend", ignore_result=True)
def retry_node(node_id: str):
    task_service.retry_activity(node_id)


@app.task(queue="backend", ignore_result=True)
def set_record_status(instance_record_ids: List[str], status: str, delay_seconds: float):
    # 不允许长时间占用资源
    time.sleep(delay_seconds if delay_seconds < 2 else 2)
    models.SubscriptionInstanceRecord.objects.filter(id__in=instance_record_ids).update(status=status)


@app.task(queue="backend_additional_task", ignore_result=True)
def update_subscription_instances_chunk(subscription_ids: List[int]):
    """
    分片更新订阅状态
    """
    subscriptions = models.Subscription.objects.filter(id__in=subscription_ids, enable=True)
    for subscription in subscriptions:
        if tools.check_subscription_is_disabled(
            subscription_identity=f"subscription -> [{subscription.id}]",
            scope=subscription.scope,
            steps=subscription.steps,
        ):
            logger.info("[update_subscription_instances] skipped for subscription disabled")
            continue
        logger.info(f"[update_subscription_instances] start: {subscription}")
        try:
            if subscription.is_running():
                logger.info("[update_subscription_instances] skipped: subscription is running")
                continue

            # 创建订阅任务记录
            subscription_task = models.SubscriptionTask.objects.create(
                subscription_id=subscription.id,
                scope=subscription.scope,
                actions={},
                is_auto_trigger=True,
            )
            run_subscription_task_and_create_instance(subscription, subscription_task)
            logger.info(f"[update_subscription_instances] succeed: subscription_task -> {subscription_task}")
        except SubscriptionInstanceEmpty:
            logger.info("[update_subscription_instances] skipped: no change, do nothing")
        except Exception as e:
            logger.exception(f"[update_subscription_instances] failed: subscription -> {subscription}, error -> {e}")
