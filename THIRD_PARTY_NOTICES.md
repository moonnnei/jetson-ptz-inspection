# 第三方来源与发布状态

本工程已按AGPL-3.0准备开源发布：项目原创代码和文档的授权见根目录LICENSE；第三方模型、源码及依赖继续保留各自许可，不因共同打包而改成同一种上游许可证。对应工程源码、模型来源、导出适配脚本、配置及验证工具随公开文件树提供。发布者仍需确认自己拥有公开学校或合作项目资料的权利。

| 内容 | 固定来源或版本 | 说明 |
| --- | --- | --- |
| YOLO26n原模型与导出依赖 | Ultralytics8.4.0，官方yolo26n.pt | 官方提供AGPL-3.0与企业许可选项；转换不消除原有许可义务 |
| NCNN源码 | commit 6c52e59dfb610cbd77946bea19099aaf96f8dcc1 | BSD3-Clause及其内部第三方组件条款 |
| OpenCV | 原设备3.3.1 | 使用设备已有安装，不随本包重分发库 |
| PyTorch与PNNX等PC工具 | requirements-pc-lock.txt列明 | 仅电脑侧可选导出环境，不随包提供虚拟环境 |

已经附上 `third_party/licenses/Ultralytics-AGPL-3.0.txt` 和 `NCNN-BSD-3-Clause.txt`。本包的转换模型由官方YOLO26n COCO权重派生，导出脚本和适配代码同时提供，源模型地址见下。没有把该模型重新声明为MIT或BSD许可。

- Ultralytics源码与模型：[源码v8.4.0](https://github.com/ultralytics/ultralytics/tree/v8.4.0)，[官方权重](https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo26n.pt)，[许可说明](https://www.ultralytics.com/license)。
- NCNN：[固定提交](https://github.com/Tencent/ncnn/tree/6c52e59dfb610cbd77946bea19099aaf96f8dcc1)，[许可文本](https://github.com/Tencent/ncnn/blob/6c52e59dfb610cbd77946bea19099aaf96f8dcc1/LICENSE.txt)。
- OpenCV：[3.3.1源码与许可证](https://github.com/opencv/opencv/tree/3.3.1)。
- 海康：[云台协议参考](https://open.hikvision.com/hardware/v2/08%E5%8D%8F%E8%AE%AE%E9%80%8F%E4%BC%A0/%E4%BA%91%E5%8F%B0%E5%92%8C%E8%B7%9F%E9%9A%8F%E5%AE%9A%E4%BD%8D.html)。仅链接接口说明，不分发厂家SDK或固件。

测试报告中样本名称bus/dog指历史公共样本，不是摄像头现场图像。本包不附图片数据集。

## 发布归属与许可边界

- 原创集成代码：Jetson PTZ Inspection contributors，2026；按根目录AGPL-3.0提供，贡献者可在Git提交中使用自己的真实署名或公开署名。未代填个人姓名、学校或合作单位为权利人。
- 本地开发和发布整理没有代替发布者创建远程仓库。公开状态以实际GitHub仓库为准。
- legacy目录仅包含本工程早期集成脚本，不附Darknet源码、共享库或旧模型；它不是当前NCNN主线的可独立运行安装包。
- 不声明拥有Ultralytics模型架构、预训练权重或NCNN/OpenCV的原创权。项目名称及厂商名称仅用于说明兼容性和来源。
- 完整对应源码的范围及具体再分发义务按适用许可核对，不以“只上传部分脚本”或“转换格式”规避模型许可。
