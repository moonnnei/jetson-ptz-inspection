# RTSP 视频采集版：TX2 实机验证

日期：2026-10-13。版本：v0.2.0。相机：DS-2DC2402IW-DE3 S6。

## 已实现的链路

```text
海康相机 H.264 主码流 2560×1440 / 25fps
  → RTSP TCP / 1554
  → 独立 Python GI / GStreamer 进程
  → omxh264dec 硬件解码 / nvvidconv 缩放至 1280×720
  → videorate 限制 JPEG 输出至 10fps
  → 后台持续接收，只保存一个最新帧
  → YOLO26n / NCNN CPU FP32（原模型与检测器不变）
  → 目标位置反馈 / ISAPI 有时限 PTZ 动作与 STOP
  → 浏览器 HTTP 轮询最新检测 JPEG 与状态
```

相机到 TX2 已采用持续 RTSP 采集，不再逐张调用相机 `/picture` 接口。
JPEG 是解码后送给现有 C++ 检测器的内部图像格式；它不代表相机仍使用 HTTP 抓拍。
浏览器仍刷新检测后的 JPEG，未改成 WebRTC、HLS 或浏览器视频码流。
连续解码帧率、模型处理帧率和网页刷新频率是不同指标。

## 实测数据

| 检查 | 结果 |
|---|---|
| PC 与 TX2 标准库检查 | 各 69 项测试通过；模型 SHA256 校验通过 |
| 独立硬件解码拉流 | 103 帧 / 10.147 秒，接收约 10.05 fps |
| 独立拉流首帧等待 | 0.645 秒 |
| RTSP 接入 YOLO | 连续检测 30 帧，耗时 8.629 秒，约 3.48 fps |
| 等待新帧＋检测处理 | 中位数 275.0 ms；最大值 416.4 ms |
| 检测器统计的预处理＋推理 | 中位数 103.3 ms |
| JPEG 接收完成至处理完成 | 中位数 180.4 ms；最大值 270.5 ms |
| 检测消费者暂停两秒 | 帧序号前跳 21，恢复读取时最新 JPEG 收帧龄 61.3 ms |
| 终止本 App 自有解码进程 | 检测到故障并重新连接，恢复用时 1.816 秒 |
| 网页服务 | http://192.168.1.4:8769/，观察模式，控制未启用 |
| 本轮非零运动指令 | 0；只有认证/状态读取与零速度 STOP |

这些时间基于 TX2 单调时钟的 JPEG 接收、等待与处理过程。
尚未测量从相机曝光到浏览器显示的端到端延迟；不能把收帧龄当作实际现场延迟。
本轮画面中的人数以检测结果为准，未将静态场景检测等同于动态人物跟踪复验。
解码进程终止属于故障注入，不等同于物理拔网线测试。

## 取帧与控制边界

采集进程持续运行，只缓存最新 JPEG，慢消费者主动跳过中间帧。
主服务要求序号递增，并拒绝在本轮取帧开始之前收到的本地缓存帧。
推理后检查 JPEG 接收完成时间至处理完成时间不超过 1.5 秒。
运动前再次检查解码连接状态、帧龄、控制代数和网页心跳。
这能防止应用本地排队和重用旧缓存，但无法证明相机编码器或网络内部没有延迟。

采集失败时保持 STOP 并禁用跟随；解码器至多重新启动三次。
重连或图像尺寸变化使旧目标及标定失效，流恢复只回到观察模式。
切换输入码流、旋转、变焦或安装位置之后，需要重新校准。
现有固定速度、500ms 脉冲控制没有改成 PID。

## 运行与回退

需要板端 Python 3.5、python3-gi、GStreamer、rtspsrc、H.264 解码器、jpegenc 等组件。
本台 TX2 的 GStreamer 为 1.8.3，硬件分支使用 omxh264dec 与 nvvidconv；新版 Jetson 需要另行适配。
配置模板是 `config/tracker.rtsp.example.json`，RTSP 端口应读取实际设备配置，不能默认照抄 554。

```bash
python3 src/person_tracker/server.py --config config/tracker.rtsp.example.json --user admin
```

密码在终端交互输入，仅经内存和子进程 stdin 传递，不写入 JSON 配置、环境或命令行。
RTSP 模式可选 hardware/software 解码；调整输出尺寸时保持画面比例。
需要回退时，加 `--frame-source snapshot`，恢复原 HTTP JPEG 采集。
默认配置仍是 snapshot，RTSP 模式通过配置或命令行显式启用。

本轮部署在独立目录 `/home/nvidia/tx2-ptz-rtsp-20261013/jetson-ptz-inspection`，旧工程目录保留。
服务启动后处于观察模式。本轮没有自动校准或启用人物追踪，没有改写相机码流配置。

## 本轮发现并修复的问题

初次集成的 OMX→nvvidconv→CPU videoscale 管道出现 not-negotiated。
将输出宽高直接交给 nvvidconv 后的 caps、去掉硬件分支的 CPU videoscale 后仍未通过。
最终改用不含凭据的 Gst.parse_launch 固定管道，对齐已通过的独立 probe：显式限定 H264 RTP、将单帧队列放在 videorate 前，再对 source 设置连接属性。
修正后完成持续解码、YOLO 对接、慢消费者跳帧与解码进程重连的实机验证。
首次失败、修复及最终验证都保留在本地运行记录中，最终结论依据通过的实机测试。

## 验证证据与后续边界

原始统计位于本地 `runs/rtsp-20261013/` 的 probe-summary.json、live-summary.json、board-tests.txt、status-latest.json。
实机图像留在本地运行目录，不收入公共工程包。
当前已完成持续 RTSP 采集与已有 YOLO 的对接；仍需重新复验方向校准、人员回中和动态跟随。
完整仪表检测、逐表扫描、对焦、读数及上传业务仍待后续对接，不能把视频接入通过视为完整毕设验收。

参考：[GStreamer rtspsrc](https://gstreamer.freedesktop.org/documentation/rtsp/rtspsrc.html)、[NVIDIA L4T R28.3 GStreamer 指南](https://developer.download.nvidia.com/embedded/L4T/r28_Release_v3.0/Docs/Jetson_TX1_and_TX2_Accelerated_GStreamer_User_Guide.pdf)。
