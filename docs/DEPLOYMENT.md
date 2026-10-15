# 部署与运行手册

## 1 确认目标设备

读取 `/etc/os-release`、`/etc/nv_tegra_release`，记录 `python3 --version`、`cmake --version`、`g++ --version`。本工程针对原 TX2 Ubuntu16.04/Python3.5/OpenCV3.3.1 基线；没有进行系统升级。不要为项目直接替换 `/usr/bin/python`。

C++ 编译依赖需要 g++、cmake、make、OpenCV 开发头文件与库、OpenMP/pthread。旧 TX2 已具备这些依赖。新设备先核对包管理器与系统状态，再按目标系统安装，不能把 Windows 的 requirements-pc-lock.txt 安装到板上。

## 2 构建与路径

工程应整体复制，模型在 `models/yolo26n`，应用可执行文件统一输出到 `build`。相对配置路径以工程根目录为基准，配置文件本身的路径按调用时工作目录解释。

```bash
python3 scripts/verify_models.py
python3 scripts/fetch_ncnn.py
bash scripts/build_tx2.sh
python3 scripts/test_local.py
```

也可使用 `bash scripts/build_tx2.sh --ncnn-root ~/tx2-yolo26-ncnn` 复用原来的 NCNN 头文件和静态库，路径中需要有 `ncnn-master` 和 `ncnn-build`。如果替换了依赖来源，应使用新的构建目录或核对 CMakeCache，不能把不同架构库混用。

固定 NCNN 提交为 `6c52e59dfb610cbd77946bea19099aaf96f8dcc1`。构建脚本只对旧 CMake 不认识的一处 `GREATER_EQUAL 23` 做等价语法替换；网络算子实现不变。第一次构建用 `make -j2`，需要几分钟，具体取决于散热和系统负载。

下载脚本支持已有原始归档的 SHA256 核对，在线下载使用固定提交 URL 并记录下载后的哈希。在线 ZIP 与历史 master 命名 ZIP 不是相同压缩字节，不能对两者套用同一个归档哈希。

## 3 先测接口

相机接匹配电源和网线，完成激活，与 TX2 同一局域网。设备只读探测：

```bash
python3 tools/ptz/probe_camera.py --url http://相机IP --user 实际用户名
```

输出 XML 可能含设备标识，只在本地保留。手动云台控制进入交互终端：

```bash
python3 tools/ptz/ptz_control.py --url http://相机IP --user 实际用户名
```

先输入 `status`、`stop`，再在现场观察下输入 `pan 20 500` 或 `tilt 20 500`。命令可能真的转动相机。`snapshot` 保存照片，`quit` 退出并请求停止。速度值不是角速度，接口原始位置不随意换算。

## 4 启动追踪

复制配置文件，修改 `camera`、`user`、`rotation`、`port`。不支持 password 字段，带用户名密码的 URL 也会被拒绝。

```bash
cp config/tracker.example.json config/tracker.local.json
bash scripts/run_tracker.sh --config config/tracker.local.json
```

浏览器打开 TX2 的 8767 端口。校准时静止背景应覆盖大部分画面，站定等待。每轴最多三次有时限正向动作及对应反向动作。校准约 10–35 秒；位移不足或背景不一致时会失败，固定场景后重新校准。

点击开始后，初次确认需要两帧高分人体。关闭/隐藏控制页面时会请求停止，3 秒没有心跳也停用跟随。用一个控制页面操作；一个页面关闭后，其他可见页面仍可能维持心跳。

## 5 日常停止与排障

前台按 Ctrl+C 退出。后台模式在启动命令追加 `--daemon`，先输入口令后才进入后台。PID 写入 `var/tracker/server.pid`。用 `ps -p PID -o pid,args` 确认是本工程后再 `kill -TERM PID`，避免误停其他进程。不要按进程名批量杀死所有 Python 进程。

v0.1.1 遇到 STOP 未确认会保留告警并禁止继续校准/追踪。恢复相机连接后再次点击“停止追踪”，取得接口确认后才允许开始。推理输入阻塞最多等待三秒；推理协议超时或半包会关闭检测进程，此时需要重启服务，不能仅重复点击校准。

| 现象 | 检查方向 |
| --- | --- |
| 401 或 403 | 账号口令或权限，停止自动尝试，核实后再启动 |
| 404 或不支持接口 | 本机固件、通道和 ISAPI 能力，勿套用近似型号 |
| 无 JPEG 或采集超时 | 网络连通、相机通道、端口与负载 |
| NCNN 加载失败 | param/bin 是否配套、哈希、编译版本、CPU 架构 |
| 校准位移不足 | 纹理、底座固定、镜头限位、运动是否生效 |
| 追踪停止但检测仍继续 | 这是 observe 模式，查看停止原因再决定是否重启 |
| 旧页面显示异常 | 刷新页面取得新服务令牌，确认连接的是当前端口 |

不要把未知故障自动处理成改密码、恢复出厂或刷机。保留日志与环境版本，先复现最小接口。

## 6 电脑侧重新导出模型

已附 NCNN 模型，可跳过导出直接构建运行。确需重新导出时，使用 Python3.12 的隔离环境。`requirements-pc-lock.txt` 来自历史转换环境，CPU版 PyTorch wheel 需要其官方索引；先安装 torch==2.7.1、torchvision==0.22.1 对应CPU轮子，再安装固定依赖。

```text
python -m venv .venv
激活这个虚拟环境
python -m pip install torch==2.7.1 torchvision==0.22.1 --index-url https://download.pytorch.org/whl/cpu
python -m pip install -r tools/model_export/requirements-pc-lock.txt
python tools/model_export/export_model.py
python tools/model_export/validate_pc.py
```

脚本在自身目录产生导出物；需要联网下载官方 yolo26n.pt，或提前放入同名原权重。核对 `validation/conversion-check.json` 等实际输出后，再将生成的 param/bin/classes 作为**另一模型版本**接入，并更新来源及哈希。不要悄悄覆盖当前已验证模型再沿用旧报告。

`compare_tx2.py` 保留历史两样本精确比对流程，需另行准备脚本中注明的 TX2 结果与公共样本像素；这些图片为避免混入现场照片不随工程包分发。该脚本不在默认本地测试入口执行。
