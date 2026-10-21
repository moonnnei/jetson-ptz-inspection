# PC 场景建图与候选发现

[项目首页](../../README.md) · [审核流程](../../docs/REVIEW_CONSOLE.md) · [环视记录](../../docs/RING_SCAN_2026-10-21.md) · [全景复审](../../docs/PANORAMA_REVIEW_2026-10-21.md)

这些工具只处理本地图片，不控制相机。推荐独立 PC Python 3.11/3.12 环境，勿安装到旧 TX2 系统 Python。

```bash
python -m venv .venv
# 激活环境：Linux/macOS 用 source .venv/bin/activate；Windows 用 .venv\Scripts\activate
python -m pip install -r tools/scene_mapping/requirements.txt
python -m unittest discover -s tools/scene_mapping -p 'test_*.py' -v
```

依赖文件固定本地验证过的 NumPy/OpenCV 版本，不是所有平台的完整锁定环境。若需运行 `detect_views.py` / `detect_panorama.py`，另装 `ultralytics==8.4.0` 及适合 PC 的 PyTorch，并提供可信本地单类 `meter/gauge/dial` 模型。程序不下载表盘模型，仓库不提供训练集或训练权重。仅运行几何检查不需要 Ultralytics。

## 入口

| 文件 | 用途 |
| --- | --- |
| `build_map.py` | 少量重叠视图的局部平面配准及来源清单 |
| `build_ring_map.py` | 环视来源索引、相邻检查、拼接尝试及角度辅助投影回退 |
| `detect_views.py` | 高清来源图和局部候选上的仪表检测 |
| `detect_panorama.py` | 全景整体/重叠分块检测、跨接缝合并、来源外观核对与复检 |
| `check_revisit.py` | 已确认仪表的参考图复核与拒绝检查，不执行运动 |
| `shape_candidates.py` | 非学习形状候选，供实验比较，不能保证是表盘 |

## 原图到审核清单

所有命令在仓库根目录执行。输入为有限扫描导出的 `manifest.json` 与对应图片，输出目录必须尚未存在。

```bash
python tools/scene_mapping/build_map.py var/input/manifest.json --output var/mapped
python tools/scene_mapping/detect_views.py var/mapped/manifest.json --weights var/private/best.pt --output var/discovered
python scripts/import_review.py var/discovered/manifest.json
```

原图确实倒置时才给建图入口添加 `--rotate180`；原图与检测框同步处理。大范围环视使用 `build_ring_map.py`，具体步骤及失败回退见环视文档。全景候选的新增导入使用 `scripts/import_panorama_candidates.py`，输入格式见全景复审文档。

来源图哈希、尺寸、方向或拍摄姿态不匹配时拒绝导入。图像索引、局部总览和角度投影预览分别记录，不能当成完整无缝全景或 PTZ 标定。外观相关值和检测分数都不等于人工确认；误检、漏检和拒绝结果须保留。
