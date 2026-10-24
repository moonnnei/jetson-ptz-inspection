# 模型目录

[项目首页](../README.md) · [部署](../docs/DEPLOYMENT.md) · [第三方许可](../THIRD_PARTY_NOTICES.md)

本目录提供公开工程已使用的固定转换模型。模型来自Ultralytics官方通用检测权重，工程工作是导出适配、旧TX2运行和输出到云台控制的对接。

## 已包含模型

| 项目 | 配置 |
| --- | --- |
| 目录 | [yolo26n/](yolo26n/MODEL_CARD.md) |
| 用途 | COCO 80类通用物体检测；巡检和跟随控制只使用person类 |
| 格式与执行后端 | NCNN，FP32，CPU |
| 输入 | RGB，batch 1，320×320，除255；等比缩放、居中补边114 |
| 输出 | `1×2100×84`，已解码xyxy与80类分数 |
| 图层名 | `in0` / `out0` |
| 导出处理 | 保留one-to-one网络，将末端TopK/Gather移至主机 |
| 详细依据 | [MODEL_CARD.md](yolo26n/MODEL_CARD.md)、[export-info.json](yolo26n/export-info.json) |

这里随附的 YOLO26n 不检测仪表类别，也不输出仪表数值。当前真实仪表主线另用本地训练的单类 YOLO11n，权重未随仓库提供。输入输出布局经过工程适配，不能只按文件扩展名套用其他 YOLO 或 NCNN 示例的解码逻辑。

## 当前仪表模型接口（权重自备）

本次冻结的模型接口与检查点／NCNN文件哈希见[仪表模型卡](meter/MODEL_CARD.md)。这些文件已进入私有恢复档案，公开工程不分发仪表权重。

- 类别 `{0: "meter"}`，YOLO11n Detect，固定 batch 1、640 输入、FP32。
- 原框架输出 `1×5×8400`，原生 NCNN 检测器按其转换张量解析；不能直接换用 YOLO26n 的解码器。
- 预处理为 RGB、除 255、等比缩放和居中补边 114；置信度 0.25、NMS IoU 0.45。
- 源码：[meter_detector.cpp](../src/meter_runtime/meter_detector.cpp)；电脑导出：[export_meter.py](../tools/model_export/export_meter.py)；可信检查点加载：[meter_checkpoint.py](../tools/model_export/meter_checkpoint.py)。
- 先验证本地检查点来源及哈希；当前保留已运行的转换模型，冻结时不重新导出。CMake独立入口已在此前TX2阶段构建并对照非空ROI输出，见[初始化复验](../docs/INITIALIZATION_VALIDATION_2026-10-24.md)；该编译记录与本次发布离线检查分别保存。

运行目录和构建方式见[预置位巡检部署](../docs/SOFTWARE_PRESET_PATROL.md)。普通目标检测不等于指针/刻度解析或仪表读数。

## 文件用途

| 文件 | 内容 |
| --- | --- |
| [model.ncnn.param](yolo26n/model.ncnn.param) | 网络结构与图层描述 |
| [model.ncnn.bin](yolo26n/model.ncnn.bin) | 转换后的权重 |
| [classes.txt](yolo26n/classes.txt) | 类别顺序 |
| [SHA256.json](yolo26n/SHA256.json) | 固定模型与类别文件的哈希 |
| [MODEL_CARD.md](yolo26n/MODEL_CARD.md) | 来源、预处理、输出契约、验证范围和局限 |

从仓库根目录核对模型文件：

```bash
python scripts/verify_models.py
```

校验成功说明文件与登记版本一致，不代表检测精度、推理速度或实机巡检已经通过验收。重新导出的哈希可能变化，不能直接用旧哈希替换新结果。

## 更换模型时需要同步什么

1. 核对权重来源和公开许可，用新的版本目录保存产物，保留原模型。
2. 记录导出环境、输入尺寸、类别顺序、预处理方式、张量布局及数值精度。
3. 对照源框架结果检查转换前后的一致性，包含正样本、无目标图像及容易误检的背景。
4. 修改检测器的预处理、输出解析和类别选择，确保框能正确映射回相机图像。
5. 分别测量板端推理、采集处理、显示和整轮任务耗时，再进行静止目标的实机验收。

电脑侧导出与验证工具位于`tools/model_export/`；当前板端无需安装现代PyTorch或Ultralytics。仪表检测模型还需要实际仪表数据验证；读数算法是另一个接口，不能用检测分数替代读数或读数置信度。

第三方权重保留其原许可，不能因为转换为NCNN就重新声明为MIT或BSD。新增公开文件需要按[贡献说明](../CONTRIBUTING.md)更新发布清单。
