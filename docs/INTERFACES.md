# 技术接口与数据约定

本页对应 v0.4.1。最新标定、速度与实际人员跟随范围见 [2026-10-14 跟随复验](FOLLOW_2026-10-14.md)。PC 与板端159项测试通过。v0.4.0 的协议与模拟调度实验见 [2026-10-13 修订记录](TRACKING_PRIORITY_2026-10-13.md)，历史性能见 [分核优化记录](OPTIMIZATION_2026-10-13.md)。软件测试、人体实测和机械安全验收分别评价。

## 已实现的网页 API

这些是 TX2 自建服务的路径，不是海康设备 API。GET 未设用户认证，仅适合可信实验室网络。

| 方法 | 路径 | 行为 |
| --- | --- | --- |
| GET | `/` | 返回网页和本次服务控制令牌 |
| GET | `/api/status` | 当前状态，不含图像 |
| GET | `/api/frame` | 当前状态加按需 Base64 JPEG 与绑定该图片的 preview 元数据 |
| GET | `/api/live.mjpeg` | 独立输出最新 RTSP 源 JPEG 的连续 MJPEG；无旋转、无检测框 |
| POST | `/api/calibrate` | 异步方向校准，可能运动 |
| POST | `/api/start` | 已校准且帧新鲜时启用追踪 |
| POST | `/api/stop` | 停用、废弃旧动作并请求 STOP |
| POST | `/api/heartbeat` | 更新服务级三秒控制租期 |
| POST | `/api/pulse-limit` | 停止后设置100..500毫秒追踪脉冲上限，JSON为 `{"duration_ms":500}` |
| POST | `/api/motion-speed` | 确认停止后设置20..40的整型速度参数，JSON为 `{"speed":30}`；旧标定失效，必须重做校准 |

POST 使用 `X-Control-Token`，有 Origin 时必须与当前 http Host 匹配；令牌不是密码或账号系统。成功返回 `{"ok":true}`，错误含 error，状态不合适返回409，令牌错误403。两个设置接口要求上述精确JSON字段，其他接口使用空请求体。

`/api/live.mjpeg` 返回 `multipart/x-mixed-replace; boundary=jetson-live`，每帧包含 `Content-Type: image/jpeg`、`Content-Length`、`X-Source-Sequence` 和原 JPEG 字节。没有 RTSP 源时返回409，同时观看客户端超过两个时返回429并带 `Retry-After: 1`。每客户端独立调用 `capture.read(after_sequence=自己的序号, timeout=1)`，不修改 App 检测序号、不维护待播队列；每次整帧 socket 写入设一秒总超时。QUIT、读取失败或慢客户端超时结束该连接。它不做图像编码、旋转、检测或控制，网页根据 rotation 用 CSS 旋转，示例默认180°。实时画面无检测框，不能将另一序号的检测框叠加到它上面。

网页默认实时模式，请求 MJPEG 与 `/api/status`；切到检测模式才轮询 `/api/frame`。当 `preview_interval>0` 时，每次 frame 请求延续两秒预览需求，需求有效且距离上次预览达到设定间隔才生成 JPEG。首次请求可能还没有图片，客户端应等待后续请求。响应顶层是最新检测状态，`jpeg` 必须配合 `preview` 内的 persons、target、frames 和 source_sequence 绘制，不能使用顶层较新的框。`preview_frame_age` 是该预览源帧本地收齐到读取接口的时间（秒），不是曝光到显示延迟。Base64 预先随 JPEG 计算并缓存，状态锁内不重复编码。

v0.1.1 中，`/api/stop` 在停用本地控制后请求设备 STOP；设备请求失败返回409，告警保留到后续 STOP 确认成功，采集新帧不会清除。STOP 失败还会使已有控制代次失效，阻止并发开始请求继续驱动。接口确认不代表已测得机械停止。

状态包含 mode、message、error、enabled、calibrating、calibrated、persons、target、frames、frame_age、width、height、shift 和 shift_response，以及以下运行配置和计时字段。首次尚无图像时部分字段不存在或为空，前端必须容忍；无目标时 target 为 null。

| 字段 | 含义 |
| --- | --- |
| `frame_source`、`frame_policy` | snapshot/rtsp 输入；latest/fresh 取帧策略 |
| `control_mode` | fixed/adaptive 脉冲策略；均不是 PID |
| `motion_mode` | serial 同步动作／responsive 独立追踪动作线程 |
| `tracking_pulse_ms` | responsive 普通追踪动作上限；追踪示例200 ms，校准不使用此上限 |
| `motion_speed` | 校准和追踪共同使用的速度参数；软件范围20..40，当前跟随示例30，不代表角速度 |
| `max_control_age`（启动配置） | 动作提交和实际请求前允许的源帧本地最大年龄；追踪示例0.5秒 |
| `motion_busy`、`motion_phase` | 是否有未结束动作，以及 idle／moving／stopping／settling 阶段 |
| `preview_interval` | 检测预览生成最小间隔；0代表每次 capture 都生成 |
| `detector_threads`、`opencv_threads` | NCNN 和 OpenCV 的线程配置 |
| `detector_cpus`、`rtsp_cpus` | 检测子进程、RTSP 解码／编码子进程的 Linux CPU 亲和性；null 表示不限定 |
| `source_sequence` | RTSP 本地接收帧序号，消费时要求递增，不是相机曝光帧号 |
| `video_source` | RTSP 收帧数、序号、重启计数、状态、最后收帧龄和脱敏错误 |
| `flow_enabled` | 此帧是否计算校准光流；普通检测 false、校准 true |
| `flow_reference_held` | 此次光流是否与固定参考图像比较；校准脉冲后的六次采样为true |
| `decode_ms` | JPEG 解码与图像旋转 |
| `preprocess_ms` | 缩放、补边、BGR→RGB 和归一化 |
| `network_ms` | NCNN 输入／提取网络结果的计算时间 |
| `inference_ms` | 预处理＋网络计算；不含 JPEG 解码、光流和预览编码 |
| `flow_ms` | 光流阶段；跳过时仍有极小的计时与分支开销 |
| `postprocess_ms` | 人体分数筛选、坐标还原及人员 JSON 构造 |
| `preview_ms` | 预览缩放和 JPEG 编码 |
| `preview_generated` | 此次协议响应是否包含预览 JPEG；false 时 JPEG 长度为0 |
| `control_result_ms` | C++ 从 JPEG 解码开始到人体筛选与人员 JSON 构造完成，不含预览编码 |
| `total_process_ms` | C++ 从 JPEG 解码开始到可选预览阶段结束；不含 IPC 等待和输出写入 |
| `pipeline_ms` | Python capture 开始到处理结果与预览接收完成；包含本轮取帧等待、IPC 与 C++ 处理，不含后续云台动作和网页显示 |
| `decoded_frame_age_ms` | 本地 JPEG 收齐到处理完成的时间；snapshot 输入以请求开始为起点 |
| `result_to_command_ms` | 最近一次响应式动作使用的检测结果完成到开始发送运动请求的本地间隔 |
| `command_http_ms` | 最近一次运动请求发送到接口响应成功的时间；不等于机械启动时间 |
| `last_pulse_duration_ms` | 最近一次发送的设备端动作时限 |

分阶段计时单位为毫秒，使用板端单调时钟。`frame_age` 是最近一次完成处理至状态读取的时间（秒），不能代替源帧龄。上述指标均不是相机曝光到网页显示的端到端延迟。

人体结构示意：

```json
{"confidence":0.82,"bbox":[0.30,0.20,0.60,0.80]}
```

bbox 为旋转后图像的归一化 xyxy，0 到1。检测器只返回 COCO person 类别；网页人体关联并不识别具体身份。

## 已实现的采集和调度配置

`config/tracker.responsive.example.json` 的本台 TX2 配置为：NCNN 两线程、`detector_cpus="1,2"`（已核对为 Denver）、OpenCV 单线程，`rtsp_cpus="0,3,4,5"`（A57），RTSP 输出 1280×720、20 fps 上限，latest 取帧、adaptive 控制、responsive 动作调度、`tracking_pulse_ms=200`、`max_control_age=0.5`、`preview_interval=0.35`，主循环完成一轮后等待0.02秒，示例网页端口8771。网页状态每轮刷新完成后等待100 ms。输出上限和等待间隔都不代表固定检测／显示帧率。`tracker.optimized.example.json` 保留历史 v0.3.0 配置，端口8770；相应旧版归档保留。

`detector_cpus` 和 `rtsp_cpus` 分别通过 `taskset -c` 作用于各自子进程，主服务不被一同绑核；子进程创建的线程继承相应可用 CPU 集合。需要 Linux 的 taskset，CPU 编号仅适用于已核对的本台设备。命令行默认仍保留 snapshot、NCNN 四线程、未绑核、fixed、serial 与 `preview_interval=0`，可显式选择其他配置。

RTSP 后台只保留一张最新 JPEG。`latest` 使用最新且序号大于已消费序号的帧，通常无需等待 capture 开始后才到达的新帧；每次脉冲执行 STOP 成功并等待 0.12 秒后更新屏障，后续读取要求帧的本地收齐时间不早于该屏障。`fresh` 和方向校准始终要求本次 capture 开始后收齐的帧。两种模式都保留帧龄、序号递增、重连和尺寸变化检查；重连／尺寸变化使标定失效，回到观察模式。

这些规则防止消费本地过期缓存，但不能证明相机曝光发生在 STOP 或 capture 开始之后。提高 RTSP 输出上限本身不保证推理变快；本台的同一活进程亲和性对照显示采集／编码争用 Denver 可拖慢检测，因此当前配置将其与检测器分核。RTSP 保留已验证的 TCP、100 ms jitter 配置、单帧 leaky queue、普通 videorate 和 `sync=false`。本轮曾尝试 `videorate drop-only=true`，板端 OMX 流报 `not-negotiated` 后退出，该参数已撤回；最终版本没有改变这些采集参数，也没有据此报告曝光延迟改善。

启动时请求零速度 STOP，随后只观察检测，不发送非零运动。校准与追踪需显式触发。fixed 策略返回速度±20、500 ms；adaptive 根据死区外误差与标定矩阵返回100～500 ms、速度±20。serial 在检测线程同步执行普通动作；responsive 将其送入唯一动作线程，并将普通动作时长限制到 `tracking_pulse_ms`（示例200 ms），不缓存下一动作。检测可在普通运动等待期间继续运行；新反馈发现目标丢失、回中、方向反转、控制轴变化或帧过期时取消旧动作并发送 STOP，停用／控制代次改变也使旧动作失效。STOP 与120 ms停稳等待完成前不启动替代动作。

responsive 在提交及持命令锁实际发送前检查源帧本地年龄≤`max_control_age`（示例0.5秒）且源帧不早于上次停稳后的屏障。屏障与帧龄只使用本地单调时钟，没有取得曝光时间。HTTP 命令仍用锁串行，取消需等待在途请求退出；追踪服务 HTTP 超时设为1.5秒，因此不能保证取消后立即送达 STOP，更不能保证机械立即停止。动作出错或 STOP 未确认时停用追踪并保留错误。

校准保留同一操作锁下的同步500 ms脉冲、0.85秒运动等待及光流反馈；动作线程尚未结束时拒绝校准。普通动作等待自身时长＋0.05秒后请求 STOP，再等待0.12秒，以上不含 HTTP 请求耗时，不是固定闭环周期。adaptive 和 responsive 均不是 PID。v0.4.1已补做本台设备的回中及走动复验，用户仍反馈明显滞后；实际范围见本次报告，不能据此认定所有速度、距离和场景通过验收。

## 已实现的 Python 与 C++ 进程协议

单一操作线程顺序调用，一次请求对应一次响应；当前不做并行请求复用。

```text
Python -> C++ stdin
  uint32 请求头 + JPEG原图
  请求头 bit31=0：保留光流（兼容旧客户端）
  请求头 bit31=1：跳过光流
  请求头 bit30=0：生成预览JPEG（兼容旧客户端）
  请求头 bit30=1：跳过预览JPEG生成
  请求头 bit29=0：光流参考随请求推进（兼容旧客户端）
  请求头 bit29=1：保持固定光流参考；必须同时启用光流
  JPEG字节数 = 请求头 & 0x1fffffff
C++ -> Python stdout
  uint32 JSON字节数 + UTF-8 JSON
  uint32 预览JPEG字节数 + JPEG预览
  只有请求明确设置bit30=1时，预览JPEG字节数才允许为0
```

整数协议按 TX2 当前小端环境使用；Python显式按小端解析，C++当前按本机uint32写入。移植到大端平台必须修改序列化，不能直接照搬。读取和写入处理部分完成情况；模型每进程加载一次，不输出调试文字到stdout污染协议。解码失败、模型输出维度不符、管道退出或读取超时均进入异常处理。

Python `Engine.infer(jpeg, include_flow=True, include_preview=True, hold_reference=False)` 的默认值保留旧客户端行为；App 普通 capture 显式关闭光流，只有校准时启用。bit29保持脉冲前参考，六次后续图像都直接与该参考比较，不累加逐帧中位数；响应必须包含 `flow_reference_held=true`。没有参考的首次持有请求会建立参考，返回零位移。普通光流请求推进参考，禁用光流则清除参考；同时请求禁用光流和持有参考会被拒绝。bit30关闭预览时JPEG长度为0且 `preview_generated=false`。标志不匹配视为协议异常，关闭子进程并要求重启；新服务和新检测器必须成对部署。

v0.4.1的校准为每次脉冲单独建立参考，再读取六次图像。每轴最多三次500毫秒正向脉冲及相同次数反向动作；图像位移小于0.005的采样脉冲不进入有效响应平均值。图像位移接近零不能证明电机没有移动，原始位置反馈仍单独记录。所有采样的一致性下限0.6、矩阵列长度下限0.005及行列式下限0.000025保留。校准失败保留错误，正常采集不会清除；速度改变清除旧矩阵，启动追踪必须重新校准。

`tracker.follow.example.json` 是2026-10-14复验配置：检测器CPU0、3，RTSP工作进程CPU4、5，速度30、脉冲上限500毫秒；20/200毫秒的旧示例仍保留。启动参数 `--motion-speed` 的默认值20保持旧行为。此核心分配来自当前设备状态的实测，不保证其他状态或设备更快。速度40在本次场景下未通过校准一致性检查，不作为当前跟随配置。

检测器命令行为 `detector PARAM BIN ROTATION [NCNN_THREADS [OPENCV_THREADS]]`；省略线程参数时保留四线程／OpenCV 原默认行为。App 当前显式传入线程配置，且在加载网络之前设置 NCNN 线程数。网络 JSON 包含前述阶段计时、`control_result_ms`、`flow_enabled` 和 `preview_generated`。控制结果在预览阶段之前计算，当前响应整体仍在可选预览阶段结束后输出；它不是提前推送结果的另一条管道。检测协议仍为 JPEG 输入与可选 JPEG 预览；浏览器 MJPEG 由独立源 JPEG 路径提供。

输入管道采用非阻塞写并共享三秒写入期限；读取每个协议块也有三秒期限。推理通信发生异常后，在释放采集/校准共用锁前关闭子进程，防止另一流程消费上一请求的残留输出；重启服务后重建管道。

NCNN网络输入名 `in0`、输出名 `out0`，网络输出在 C++ 为二维 [2100,84]。每行 xyxy+80类分数；预处理固定RGB、320×320、补114、除255。这个布局由专用导出适配器约定，其他YOLO导出不能直接替换。

## 已实现的海康 ISAPI

`Camera` 使用标准库 Digest 认证和拒绝重定向的 opener。`status()`查询 `/ISAPI/PTZCtrl/channels/1/status`；`move(...,duration=500)` PUT momentary；`stop()` PUT continuous，所有速度置0。HTTP及XML状态都检查。抓拍为 `/ISAPI/Streaming/channels/101/picture`。

用户侧 request 成功不保证相机机械转动或物理停止，需看图像/位置。v0.2.0 首次 RTSP 视频解码证据见 `REVERIFY_2026-10-13_RTSP.md`；本台设备实际 RTSP 端口为1554，其他设备应读取实际配置。v0.3.0 性能验证见 `OPTIMIZATION_2026-10-13.md`，v0.4.0 追踪调度及实时画面变更见 `TRACKING_PRIORITY_2026-10-13.md`，不能把旧版机械验证扩展至新版短脉冲。原抓拍路径仍保留。ONVIF与厂家SDK未作为当前控制路径。

## 后续仪表接口设计

以下是设计契约，不是已实现 API。

```text
detect(frame, frame_id) -> 原图xyxy与类别分数和版本
read_meter(crop, metadata) -> value/unit/valid/reason/confidence?
run_inspection(panel_config) -> task_id及每目标状态
save_observation(task,target,frame,reading,evidence) -> record_id
enqueue_upload(record_id) -> 本地上传队列状态
```

必须约定是否由模型内部完成NMS、坐标采用原图还是输入图、颜色顺序、补边/归一化、小数点与单位处理、最小可读像素，以及模型版本。读数无效返回value=null；无置信度返回confidence=null。任务、检测、裁剪和读数绑定同一frame_id。

仪表身份由面板布局、标识或配准决定，不能直接复用人体最近框关联作为永久编号。每轮平台停稳后重新扫描，读完一表返回概览并重新匹配下一表。
