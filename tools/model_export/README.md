# PC 模型工具

这些工具在独立的现代 PC 环境运行，不在 TX2 系统 Python 安装 PyTorch。现有通用模型导出脚本保持原样；当前单类仪表模型新增如下入口。

## 导出自己的仪表模型

`meter_checkpoint.py` 要求可信训练产物的 SHA256，使用显式审核的 PyTorch 加载类型和 `weights_only=True`。它只接受单类 `meter` 的 YOLO11n Detect，不能据此安全地加载任意来源的检查点。

```bash
python tools/model_export/export_meter.py \
  --checkpoint /path/to/trusted/best.pt \
  --trusted-sha256 YOUR_REVIEWED_SHA256 \
  --output /path/to/new-export
```

固定 640、batch 1、FP32，保存 NCNN 模型、参考输入/输出和导出环境。转换后仍需验证 NCNN 数值和真实图像，不能只以导出命令退出成功作为部署验收。当前源码沿用原验证路径，本轮只验证检查点前向，没有再次导出；PC 基础依赖见 [requirements-pc-lock.txt](requirements-pc-lock.txt)。

## 对比原图、普通放大与超分

`compare_roi_enhancement.py` 不下载模型、不连接相机。准备自己标注的 ROI，每块至多一个完整仪表；JSON 的 `image` 相对清单，`bbox` 是 ROI 内像素框，负样本用 `null`：

```json
{"cases": [
  {"id": "scene1-meter1", "group": "field", "image": "meter.png", "bbox": [20, 20, 160, 160]},
  {"id": "scene1-background", "group": "negative", "image": "background.png", "bbox": null}
]}
```

从 [Real-ESRGAN 固定提交](https://github.com/xinntao/Real-ESRGAN/blob/a4abfb2979a7bbff3f69f58f58ae324608821e27/realesrgan/archs/srvgg_arch.py)保存未经修改的结构文件，并保留其 [BSD-3-Clause 许可证](https://github.com/xinntao/Real-ESRGAN/blob/a4abfb2979a7bbff3f69f58f58ae324608821e27/LICENSE)。从[官方发布](https://github.com/xinntao/Real-ESRGAN/releases/tag/v0.2.5.0)取得 `realesr-general-x4v3.pth`。脚本要求与本轮固定哈希一致的文件，不打包上游源码或权重。

```bash
python tools/model_export/compare_roi_enhancement.py \
  --manifest /path/to/roi-cases.json \
  --checkpoint /path/to/trusted/best.pt \
  --trusted-sha256 YOUR_REVIEWED_SHA256 \
  --sr-source /path/to/srvgg_arch.py \
  --sr-weights /path/to/realesr-general-x4v3.pth \
  --output /path/to/new-comparison --repeats 3
```

原图、4 倍双三次、4 倍超分进入同一个 640 检测器，固定 0.25 置信度、0.45 NMS、0.5 匹配 IoU。PC CPU 四线程，预热后重复三次，记录增强和检测耗时；不计初始化、文件 IO 或相机移动。单 ROI 限 250000 像素以约束 CPU 实验内存。

不要把同一张图的缩小版本作为新的独立场景。先比较新增正确检出、误检与成本，再决定是否集成。当前小样本结果见[超分对照](../../docs/SUPER_RESOLUTION_ASSESSMENT.md)，未接入巡检主流程。
