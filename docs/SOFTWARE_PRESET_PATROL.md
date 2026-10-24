# 软件预置位与周期采图

固定机位完成建档后，按保存的云台姿态访问仪表，用新图确认身份并留档。首次建档的搜索、构图和位置验证与后续巡检分别执行。

## 一轮如何运行

1. 校验当前场景、启用的仪表及独立回访证据，读取相机当前姿态。
2. 用有界水平/俯仰距离的最近邻方法排序。该方法是启发式，不保证全局最短；当前代价不含变焦时间。
3. 转为广角，按不超过 20° 的分段绝对位置移动到下一张表，恢复保存倍率。
4. 位置稳定后分别发起两次新的主分辨率抓图请求，与已建档的局部参考核对并做基本图像质量检查；最多追加一张，要求两次通过观测。
5. 保存通过的照片、位置、时间、哈希、身份结果和中心残差。未通过的照片标为诊断图。
6. 整轮正常结束返回起点并确认 STOP。周期任务遇到不确定或失败会暂停。

单轮时限按表数计算：`max(180, min(600, 60 + 45 × 表数))` 秒，四表为 240 秒。同一相机由共享运动锁串行使用。传输异常、取消、看门狗与 STOP 未确认均阻止正常成功结果。

## 视频与留档图像

网页实时监看复用 RTSP 最新帧；检测预览来自并行的 NCNN 推理服务。留档使用相机的 2560×1440 高清抓图接口，等待云台停稳后发出新请求，不使用网页预览缓存作为巡检照片。

时间记录是主机接收时间，相机曝光时间没有被接口提供。预览帧率、网络推理耗时、抓图耗时与整轮巡检时间分别统计。

## 单轮与周期

- “立即采集一轮”对应 `POST /api/patrol/run-once`，完成后停止，不开启周期。
- 周期由操作者保存间隔并启用，采用上一轮结束后的固定延迟，不会因任务超时叠加多轮。
- 可选最多运行轮数，由设备端在达到上限后自动停止；批次计数、耗时及质量统计见[有限轮次巡检](BOUNDED_PERIODIC.md)。
- 页面关闭后由 TX2 服务端继续执行已启用的周期；重启会暂停并保留中断记录。
- 操作者暂停同时取消当前执行和后续建档排队，恢复需明确操作。

2026-10-24 在新初始化清单上完成三轮有限周期实测，结束后周期关闭；业务间隔尚待使用者配置。有限周期结果、模糊补拍和真实服务重启见[本轮复验](INITIALIZATION_VALIDATION_2026-10-24.md)，长期运行尚未验收。

## 证据保存

预置位包含位置、参考图、框、来源记录和两次新图核对。关键证据另外固定在仪表档案中，避免建档任务退出最近 80 项任务列表后失去回访能力。完整任务与审计保存在 SQLite 历史表中。

源图或目标标注发生变化时重新核对绑定；增加新目标时，像素来源未改变的旧预置位可保留。自动重复目标保留原始记录，关闭别名采集，不给同一张表重复计数。

## 此前四表基线实测（2026-10-23）

| 验证 | 结果 |
| --- | --- |
| 新预置位后的首次四表回访 | 4/4 表、8 次新图核对通过；外部完整流程约 91 秒 |
| 连续三轮 | 12/12 次逐表采集、24 次新图核对通过；整组约 275 秒 |
| 自动候选注册与去重后的四表回访 | 4/4 表、8 次新图核对通过；外部完整流程约 95 秒 |
| 总计 | 五轮、20/20 次逐表采集、40 次新图核对通过 |

“外部完整流程”包括服务请求和结果轮询，核心控制器耗时另存记录。这是同一固定四表场景的有限验证；三表仍受俯仰下限影响而偏心，部分盘面有遮挡，读数尚未接入。

## 部署当前适配器

TX2 控制端使用 Python 3.5 标准库，图像子进程使用 Python 2.7/OpenCV 3.3.1；YOLO11n 的单类 ROI 复检使用 NCNN CPU FP32。构建源码见 [`src/meter_runtime`](../src/meter_runtime/)，PC 导出见 [`export_meter.py`](../tools/model_export/export_meter.py)。实际训练权重与现场数据库由使用者提供。

示例目录：`PTZ_PROJECT` 是源码，`PTZ_CONSOLE/var/inspection-console` 是数据库及图片，`PTZ_MODEL_RUNTIME` 包含 `build/meter_detector` 和 `models/meter640/model.ncnn.param/bin`。相机、用户名和允许访问的主机地址按现场配置。

```bash
export PTZ_PROJECT=/path/to/jetson-ptz-inspection
export PTZ_CONSOLE=/path/to/inspection-runtime
export PTZ_MODEL_RUNTIME=/path/to/meter-runtime

python3 -c 'import getpass,json; print(json.dumps({"password":getpass.getpass("Camera password: ")}))' | \
python3 "$PTZ_PROJECT/src/inspection_console/server.py" \
  --data "$PTZ_CONSOLE/var/inspection-console" \
  --host 0.0.0.0 --allowed-host 192.0.2.4 --port 8775 \
  --first-capture-camera http://192.0.2.64 \
  --first-capture-user YOUR_CAMERA_USER \
  --first-capture-console-root "$PTZ_CONSOLE" \
  --first-capture-project-root "$PTZ_PROJECT" \
  --first-capture-password-stdin --software-patrol --auto-annotations \
  --meter-detector-root "$PTZ_MODEL_RUNTIME"
```

已有兼容实时预览服务时追加 `--preview-port 8774`。未配置预览仍可执行高清采图。密码只通过标准输入进入进程内存，不写入参数或配置文件。`--first-capture-*` 保留为兼容参数名称；当前适配器由后面的功能开关选择。

### 构建单类推理程序

以下方式兼容实验 TX2 的 CMake 3.5。`NCNN_ROOT` 为包含 `ncnn-master` 和 `ncnn-build` 的既有兼容 NCNN 目录：

```bash
mkdir -p /path/to/meter-runtime/build
cd /path/to/meter-runtime/build
cmake /path/to/jetson-ptz-inspection/src/meter_runtime -DNCNN_ROOT=/path/to/ncnn-runtime
make -j2
```

当前 C++ 检测器要求单类 `meter`、固定 640 输入、输出 `1×5×8400` 对应的 NCNN 张量。提供的 C++ 源码与本轮运行程序的来源源码一致；新的 CMake 入口去除了个人绝对路径。视频硬解与模型推理后端分别配置。

### 迁移和回退

保留相应版本的源码、模型哈希、完整数据库及 `images/`、实验记录和运行条件。用 SQLite 一致性导出或在线备份取得数据库，恢复到新目录校验后再启动；不要用普通文件复制覆盖正在运行的数据库。

相同安装条件下迁移仍先做一轮新图检查。换相机、移动安装或移动仪表后旧机位不直接视为有效。回退源码优先保留现行数据，启动审核模式检查清单；历史图像和失败记录不因回退删除。
