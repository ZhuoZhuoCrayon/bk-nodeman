### 功能描述

查询订阅运行状态

### 请求参数

{{ common_args_desc }}

#### 接口参数

| 字段               | 类型   | <div style="width: 50pt">必选</div> | 描述         |
| ---------------- | ---- | --------------------------------- | ---------- |
| subscription_id  | int  | 是                                 | 订阅ID       |
| show_task_detail | bool | 否                                 | 展示任务详细信息   |
| need_detail      | bool | 否                                 | 展示实例主机详细信息 |

### 请求参数示例

```json
{
    "bk_app_code": "esb_test",
    "bk_app_secret": "xxx",
    "bk_username": "admin",
    "bk_token": "xxx",
    "subscription_id_list": [
        1
      ],
      "show_task_detail": false,
      "need_detail": false
}
```

### 返回结果示例

```json
{
    "result": true,
    "code": 0,
    "message": "success",
    "data": [
        {
            "subscription_id": 1,
            "instances": [
                {
                    "instance_id": "host|instance|host|1",
                    "status": "SUCCESS",
                    "create_time": "2021-01-03 23:04:31+0800",
                    "host_statuses": [
                        {
                            "name": "bkmonitorbeat",
                            "status": "RUNNING",
                            "version": "1.10.67"
                        }
                    ],
                    "instance_info": {
                        "host": {
                            "bk_biz_id": 1,
                            "bk_host_id": 1,
                            "bk_biz_name": "GameMatrix",
                            "bk_cloud_id": 0,
                            "bk_host_name": "",
                            "bk_cloud_name": "云区域ID",
                            "bk_host_innerip": "127.0.0.1",
                            "bk_supplier_account": "tencent"
                        },
                        "service": {

                        }
                    },
                    "running_task": {
                        "id": 1,
                        "is_auto_trigger": true,

                    },
                    "last_task": {
                        "id": 1
                    }
                }
            ]
        }
    ]
}
```

### 返回结果参数说明

#### response

| 字段      | 类型     | 描述                         |
| ------- | ------ | -------------------------- |
| result  | bool   | 请求成功与否。true:请求成功；false请求失败 |
| code    | int    | 错误编码。 0表示success，>0表示失败错误  |
| message | string | 请求失败返回的错误信息                |
| data    | object | 请求返回的数据，见data定义            |

#### data

| 字段              | 类型     | 描述                    |
| --------------- | ------ | --------------------- |
| subscription_id | int    | 订阅ID                  |
| instances       | object | 主机实例信息列表，见instances定义 |

##### instances

| 字段            | 类型     | <div style="width: 50pt">必选</div> | 描述                             |
| ------------- | ------ | --------------------------------- | ------------------------------ |
| instance_id   | string | 是                                 | 实例ID                           |
| status        | string | 是                                 | 执行状态，见status 定义                |
| create_time   | string | 否                                 | 创建时间                           |
| host_statuses | object | 是                                 | 主机状态，见host_status定义            |
| instance_info | object | 是                                 |                                |
| running_task  | object | 是                                 | 正在运行中的订阅执行任务信息，见running_task定义 |
| last_task     | object | 是                                 | 最后一个订阅执行任务，见last_task定义        |

##### status

| 状态类型        | 类型     | 描述   |
| ----------- | ------ | ---- |
| PENDING     | string | 等待执行 |
| RUNNING     | string | 正在执行 |
| FAILED      | string | 执行失败 |
| SUCCESS     | string | 执行成功 |
| PART_FAILED | string | 部分失败 |
| TERMINATED  | string | 已终止  |
| REMOVED     | string | 已移除  |
| FILTERED    | string | 被过滤的 |
| IGNORED     | string | 已忽略  |

##### host_statuses

| 字段       | 类型     | <div style="width: 50pt">必选</div> | 描述                  |
| -------- | ------ | --------------------------------- | ------------------- |
| name     | string | 否                                 | 进程名称                |
| status   | string | 否                                 | 进程状态，见proc_status定义 |
| version  | string | 否                                 | 版本号                 |
| group_id | int    | 否                                 | 插件组ID               |

##### instance_info

当need_detail参数为True时，展示信息将包括但不限于以下字段

| 字段      | 类型     | <div style="width: 50pt">必选</div> | 描述                |
| ------- | ------ | --------------------------------- | ----------------- |
| host    | object | 否                                 | 主机信息，见host定义      |
| service | object | 否                                 | 服务实例信息，见service定义 |

##### host

| 字段                  | 类型     | <div style="width: 50pt">必选</div> | 描述         |
| ------------------- | ------ | --------------------------------- | ---------- |
| bk_biz_id           | int    | 否                                 | 蓝鲸业务ID     |
| bk_host_innerip_v6  | string | 否                                 | 主机IPV6内网地址 |
| bk_host_innerip     | string | 否                                 | 主机IPV4内网地址 |
| bk_cloud_id         | int    | 否                                 | 云区域ID      |
| bk_supplier_account | int    | 否                                 | 服务商ID      |
| bk_host_name        | string | 否                                 | 主机名        |
| bk_host_id          | int    | 否                                 | 主机ID       |
| bk_biz_name         | string | 否                                 | 业务名称       |
| bk_cloud_name       | string | 否                                 | 云区域名称      |

##### service

| 字段           | 类型     | <div style="width: 50pt">必选</div> | 描述     |
| ------------ | ------ | --------------------------------- | ------ |
| id           | int    | 否                                 | 服务实例ID |
| name         | string | 否                                 | 服务实例名称 |
| bk_module_id | int    | 否                                 | 模块ID   |
| bk_host_id   | int    | 否                                 | 主机ID   |

##### proc_status

| 状态类型          | 类型     | 描述   |
| ------------- | ------ | ---- |
| RUNNING       | string | 运行中  |
| UNKNOWN       | string | 未知状态 |
| TERMINATED    | string | 已终止  |
| NOT_INSTALLED | string | 未安装  |
| UNREGISTER    | string | 未注册  |
| REMOVED       | string | 已移除  |
| MANUAL_STOP   | string | 手动停止 |

##### running_task

| 字段              | 类型   | <div style="width: 50pt">必选</div> | 描述       |
| --------------- | ---- | --------------------------------- | -------- |
| id              | int  | 是                                 | 订阅执行任务ID |
| is_auto_trigger | bool | 是                                 | 是否为自动触发  |

##### last_task

| 字段  | 类型  | <div style="width: 50pt">必选</div> | 描述       |
| --- | --- | --------------------------------- | -------- |
| id  | int | 是                                 | 订阅执行任务ID |
