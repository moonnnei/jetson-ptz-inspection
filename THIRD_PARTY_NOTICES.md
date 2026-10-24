# 第三方来源与发布状态

本工程已按AGPL-3.0准备开源发布：项目原创代码和文档的授权见根目录LICENSE；第三方模型、源码及依赖继续保留各自许可，不因共同打包而改成同一种上游许可证。对应工程源码、模型来源、导出适配脚本、配置及验证工具随公开文件树提供。发布者仍需确认自己拥有公开学校或合作项目资料的权利。

| 内容 | 固定来源或版本 | 说明 |
| --- | --- | --- |
| YOLO26n原模型与导出依赖 | Ultralytics8.4.0，官方yolo26n.pt | 官方提供AGPL-3.0与企业许可选项；转换不消除原有许可义务 |
| NCNN源码 | commit 6c52e59dfb610cbd77946bea19099aaf96f8dcc1 | BSD3-Clause及其内部第三方组件条款 |
| OpenCV / NumPy | TX2 现有 OpenCV 3.3.1；PC 可选 OpenCV 4.12.0.88 / NumPy 2.2.6 | 使用设备已有安装或用户安装的 PC 依赖，不随本包重分发库；PC 版本见 tools/scene_mapping/requirements.txt |
| PyTorch与PNNX等PC工具 | requirements-pc-lock.txt列明 | 仅电脑侧可选导出环境，不随包提供虚拟环境 |
| YOLO11n 仪表模型 | 本地 Pointer-10K 单类训练检查点；Ultralytics 8.4.0 | 公开推理与导出适配源码，不分发训练集或该本地权重；数据、架构与权重的许可分别核对 |
| Real-ESRGAN 独立 PC 对照 | `realesr-general-x4v3`，上游提交 a4abfb2979a7bbff3f69f58f58ae324608821e27 | 上游 BSD-3-Clause；结构、权重和许可证只保存在私有实验中，未打包或接入巡检主流程 |
| OpenSeadragon | 6.1.1，官方 npm 包 | BSD-3-Clause；浏览器缩放和平移组件随本工程本地分发，见 `src/inspection_console/vendor/` 的许可证和哈希清单 |
| Annotorious OpenSeadragon | `@annotorious/openseadragon` 3.9.5，官方 npm 包 | BSD-3-Clause；矩形绘制与选择组件，本地分发源码构建产物、样式及原始许可证 |
| Pixi CSP 兼容模块 | `@pixi/unsafe-eval` 7.4.3，官方 npm 包 | MIT；把动态 uniform 同步替换为普通函数，应用于 Annotorious 内嵌的同版本 Pixi 实例；见重现脚本和许可证 |

已经附上 `third_party/licenses/Ultralytics-AGPL-3.0.txt` 和 `NCNN-BSD-3-Clause.txt`。本包的转换模型由官方YOLO26n COCO权重派生，导出脚本和适配代码同时提供，源模型地址见下。没有把该模型重新声明为MIT或BSD许可。

- Ultralytics源码与模型：[源码v8.4.0](https://github.com/ultralytics/ultralytics/tree/v8.4.0)，[官方权重](https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo26n.pt)，[许可说明](https://www.ultralytics.com/license)。
- NCNN：[固定提交](https://github.com/Tencent/ncnn/tree/6c52e59dfb610cbd77946bea19099aaf96f8dcc1)，[许可文本](https://github.com/Tencent/ncnn/blob/6c52e59dfb610cbd77946bea19099aaf96f8dcc1/LICENSE.txt)。
- OpenCV：[3.3.1源码与许可证](https://github.com/opencv/opencv/tree/3.3.1)。
- 海康：[云台协议参考](https://open.hikvision.com/hardware/v2/08%E5%8D%8F%E8%AE%AE%E9%80%8F%E4%BC%A0/%E4%BA%91%E5%8F%B0%E5%92%8C%E8%B7%9F%E9%9A%8F%E5%AE%9A%E4%BD%8D.html)。仅链接接口说明，不分发厂家SDK或固件。

测试报告中样本名称bus/dog指历史公共样本，不是摄像头现场图像。本包不附图片数据集。

Real-ESRGAN 的实验配置、权重哈希与原始来源链接见[超分对照](docs/SUPER_RESOLUTION_ASSESSMENT.md)。项目未分发该上游源码或权重，不能把一次引用评估写成已整合其训练框架。当前仪表运行程序源码见 `src/meter_runtime/`，与 YOLO26n 通用演示的输出契约不同。

网页工作台的本地第三方包来源、固定版本、npm 完整性值与逐文件 SHA-256 见 [vendor/MANIFEST.json](src/inspection_console/vendor/MANIFEST.json)。对应许可证为 [OpenSeadragon](src/inspection_console/vendor/openseadragon-LICENSE.txt) 和 [Annotorious](src/inspection_console/vendor/annotorious-LICENSE.txt)。运行时不向 CDN 请求脚本。

固定的第三方构建产物按清单保留其字节和上游格式，使用哈希、API与CSP检查验证；自有源码的空白格式检查不重写这些产物。

本工程对 Annotorious 的固定浏览器构建产物作了 CSP 兼容修订，修改已经在 vendor 清单明确标记；原始构建哈希保留。[重现脚本](tools/web_ui/build_csp_vendor.py)下载并验证固定 npm 包，把 [Pixi 官方兼容模块](https://www.npmjs.com/package/@pixi/unsafe-eval)应用于组件内嵌实例。补丁以普通函数替代运行期代码生成，不放宽网页 CSP；[MIT 许可证](src/inspection_console/vendor/pixi-unsafe-eval-LICENSE.txt)单独保留。没有用另一个独立全局 Pixi 实例代替实际使用的内嵌实例。

[Frigate](https://docs.frigate.video/usage/live/) 的点击回中、[ZoneMinder](https://github.com/ZoneMinder/zoneminder/blob/master/scripts/ZoneMinder/lib/ZoneMinder/Control/HikVision.pm) 的海康控制接口作为交互和协议参考，未复制其控制源码。当前控制使用本工程已有 ISAPI 适配器；没有将 ONVIF 归一化坐标当成 ISAPI 角度。go2rtc 本轮只调研，未接入。

## 发布归属与许可边界

- 原创集成代码：Jetson PTZ Inspection contributors，2026；按根目录AGPL-3.0提供，贡献者可在Git提交中使用自己的真实署名或公开署名。未代填个人姓名、学校或合作单位为权利人。
- 公开源码仓库为项目首页所链接的 GitHub 仓库，现场数据与训练权重不在公开清单内；历史演示标签与当前开发版分别保留。
- legacy目录仅包含本工程早期集成脚本，不附Darknet源码、共享库或旧模型；它不是当前NCNN主线的可独立运行安装包。
- 不声明拥有Ultralytics模型架构、预训练权重或NCNN/OpenCV的原创权。项目名称及厂商名称仅用于说明兼容性和来源。
- 完整对应源码的范围及具体再分发义务按适用许可核对，不以“只上传部分脚本”或“转换格式”规避模型许可。
