# -*- coding: utf-8 -*-
"""
TencentBlueKing is pleased to support the open source community by making 蓝鲸智云-节点管理(BlueKing-BK-NODEMAN) available.
Copyright (C) 2017-2021 THL A29 Limited, a Tencent company. All rights reserved.
Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
You may obtain a copy of the License at https://opensource.org/licenses/MIT
Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
specific language governing permissions and limitations under the License.
"""
import logging
import socket
from enum import Enum
from typing import Dict

from celery.schedules import crontab
from celery.task import periodic_task

from apps.node_man import models
from apps.utils import enum

logger = logging.getLogger("celery")


class GseK8sSvcName(enum.EnhanceEnum):
    BK_GSE_TASK = "bk-gse-task"
    BK_GSE_DATA = "bk-gse-data"
    BK_GSE_BT_SVR = "bk-gse-svr"

    @classmethod
    def _get_member__alias_map(cls) -> Dict[Enum, str]:
        return {
            cls.BK_GSE_TASK: "gse task server",
            cls.BK_GSE_DATA: "gse data server",
            cls.BK_GSE_BT_SVR: "gse bt file server",
        }


@periodic_task(
    run_every=crontab(hour="*", minute="*/5", day_of_week="*", day_of_month="*", month_of_year="*"),
    queue="backend",  # 这个是用来在代码调用中指定队列的，例如： update_subscription_instances.delay()
    options={"queue": "backend"},  # 这个是用来celery beat调度指定队列的
)
def sync_gse_svr_host():

    gse_k8s_svc_name__ap_field_map = {
        GseK8sSvcName.BK_GSE_TASK.value: "taskserver",
        GseK8sSvcName.BK_GSE_DATA.value: "dataserver",
        GseK8sSvcName.BK_GSE_BT_SVR.value: "btfileserver",
    }

    default_ap = models.AccessPoint.objects.filter(is_enabled=True, is_default=True).first()
    if not default_ap:
        logger.info("sync_gse_svr_host: default ap not exists")
        return

    logger.info(f"sync_gse_svr_host: get default ap -> {default_ap.name}")

    for gse_k8s_svc_name, ap_field in gse_k8s_svc_name__ap_field_map.items():
        try:
            ip = socket.gethostbyname(gse_k8s_svc_name)
        except Exception as e:
            logger.error(f"sync_gse_svr_host: gse_k8s_svc_name -> {gse_k8s_svc_name}, err_msg -> {e}")
            continue

        logger.info(f"sync_gse_svr_host: gse_k8s_svc_name -> {gse_k8s_svc_name}, ip -> {ip}")

        ap_field_value = getattr(default_ap, ap_field, {})
        ap_field_value.update({"inner_ip": ip})
        logger.info(f"sync_gse_svr_host: ap_field_value -> {ap_field_value}")
        setattr(default_ap, ap_field, ap_field_value)
