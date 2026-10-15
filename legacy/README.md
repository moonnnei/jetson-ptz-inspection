# 历史 CSI 与 YOLOv3 tiny 实验

这两个脚本保留早期实验过程，未接入本次人物云台追踪主线，也不由 scripts/test_local.py 运行。

- `camera_preview.py`：Python3.5标准库，调用旧L4T的nvcamerasrc管线，提供板载CSI相机JPEG预览，历史端口8765。
- `yolo_live.py`：Python2.7、OpenCV3.3.1、NumPy、pjreddie Darknet、CUDA9/cuDNN7，使用YOLOv3-tiny COCO权重，历史端口8766。默认依赖同目录 `python/darknet.py`、`libdarknet.so`、cfg、类别和weights，不能在这个legacy目录直接当独立应用运行。

原设备历史路径分别为 `~/tx2-camera-preview`、`~/tx2-yolo-detector`。如需要复查早期实验，应回到原环境按原说明运行；本工程没有重新打包Darknet依赖和旧模型，避免把它们与NCNN主线混用。

历史结果：2026-09-17完成单图与CSI实时检测，短时间采样约8.36处理帧/秒，存在限帧逻辑。这不是YOLO26n的性能，也不是海康相机的追踪帧率。

当前服务是否在运行需要实查，历史网页端口不意味着现在仍有服务监听。公开发布这些脚本时也应核对Darknet及模型来源许可。
