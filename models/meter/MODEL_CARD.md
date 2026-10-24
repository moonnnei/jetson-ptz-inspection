# 冻结版仪表模型契约（权重自备）

关联版本：`freeze-2026-10-24`。本文件登记运行模型的接口和内容哈希，仪表权重、训练数据及检查点不随公开工程或Release附件分发。

## 模型与接口

- 架构：Ultralytics YOLO11n Detect，本地训练的单类 `{0: "meter"}`；不是新的检测网络算法。
- 推理配置：固定batch 1、640输入、NCNN CPU FP32。图像为RGB、除255、等比缩放、居中补边114；默认置信度0.25、NMS IoU 0.45。
- 原框架输出：`1×5×8400`。NCNN输出按本版本的 [meter_detector.cpp](../../src/meter_runtime/meter_detector.cpp)解析，不能套用随附YOLO26n的通用检测解码器。
- 输出是目标框和分数，不包含指针方向、刻度解析或仪表读数。盘面形状、远景尺寸、反光、倾斜和遮挡都会影响结果。

## 冻结文件标识

| 私有文件 | SHA-256 |
| --- | --- |
| 训练检查点 `best.pt` | `e6bf75f5aa415b86d62e06497b9adfabefd815a7699df3ff24a22de5fef688f8` |
| 640 NCNN参数 `model.ncnn.param` | `52abfb4609200dcf46d7a08f6ef96a828103ac6401aa9c7cc6ee0f3420114afb` |
| 640 NCNN权重 `model.ncnn.bin` | `7dddc18a45ddfe60d5ce22f9d7356d40a2085b9cbb40045e9b46ee7dfeb125dc` |

NCNN两份文件来自实际运行目录的只读快照。检查点另行保存在本地恢复档案；冻结操作不重新训练、导出或覆盖在线模型。哈希相同只证明文件一致，不证明检测精度或新场景可用性。

导出元数据记录Ultralytics 8.4.0；转换工具见[导出说明](../../tools/model_export/README.md)。本版公开C++源码和构建入口已在此前TX2实验中构建，并做非空ROI输出对照；本次发布只运行离线回归，不把它说成新的ARM64编译或相机实验。

实际部署见[软件预置位巡检](../../docs/SOFTWARE_PRESET_PATROL.md)。重新训练、重新导出或换用其他模型时，应另建版本、登记新哈希，并分别验证源框架、NCNN输出、坐标还原和现场效果。Ultralytics及数据集保留其各自许可；模型转换不改变其许可。
