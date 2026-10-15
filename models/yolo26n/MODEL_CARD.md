# YOLO26n 转换模型说明

本目录是2026-09-24实机验证用的转换产物，不是新的训练模型。

| 属性 | 值 |
| --- | --- |
| 原权重 | Ultralytics官方yolo26n.pt，COCO80类 |
| 导出版本 | Ultralytics8.4.0，PyTorch2.7.1+cpu |
| 工具 | PNNX20260526，NCNN1.0.20260526 |
| 输入 | RGB，batch1，320×320，FP32，除255 |
| 几何变换 | 等比缩放，居中补边114，输出框需要逆变换 |
| 输出 | 1×2100×84，已解码xyxy+80类分数 |
| 图层名 | in0 / out0 |
| 改动 | 保留one-to-one网络与权重，末端TopK/Gather转到主机 |
| 板端后端 | NCNN CPU；原单图基线四线程，当前巡检配置两线程；无Vulkan/CUDA/TensorRT |
| 用途 | 通用检测及人物视觉控制演示，不支持仪表读数 |

单图程序使用全局top300类别候选与0.25阈值；追踪程序只读person类，不执行通用top300筛选，以更严格目标确认规则控制云台。

`SHA256.json` 对classes/param/bin逐一校验。固定权重文件为9,661,336字节。随包提供相应导出适配代码及参考校验脚本。导出的自动元数据没有完整描述自定义布局，应以export-info.json和本文件为准；不要照搬默认 `yolo predict model=NCNN目录` 命令。

验证只覆盖历史bus/dog两张公共图片与现场短时人体回中，不代表COCO整体精度或安全关键性能。强反光、遮挡、非典型姿态或杂物可能引起错误。参数更换后需重新校验。

来源：https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo26n.pt 。许可与第三方条款见工程根目录THIRD_PARTY_NOTICES.md。
