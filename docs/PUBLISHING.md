# 从干净发布目录发布到GitHub

仓库现已公开。更新当前工程时使用下方“后续更新”，保留已有提交与标签，不重复创建仓库或覆盖历史。main 包含场景审核开发版，v0.5.0-demo 是历史运动演示；上传源码不等于新的实机验收。新增文件必须通过显式公开清单和凭据/私有数据检查。

本指南保留 v0.5.0-demo 的首次发布步骤，供新建自己的仓库时参考。已有仓库继续使用下方更新流程；公开文件范围、源码许可和模型来源说明保持随版本提供。

## 1 选择正确目录

使用专门生成的`jetson-ptz-inspection`发布目录，或者解压GitHub发布包后进入同名文件夹。顶层应直接看到`README.md`、`LICENSE`、`src`、`models`、`scripts`、`docs`和隐藏目录`.github`。

**不要在整个毕设工作区执行git add。** 工作区内还含私有运行记录和其他资料。也不要把一个ZIP上传后就当作完成源码发布，GitHub Actions需要实际的目录和文件。

源码和文档按LF保存；已验证模型的配套文件保留原始字节并禁用Git换行转换，避免Windows提交后清单或模型哈希失效。`.gitattributes`已设置对应规则。

## 2 准备Git、Python和GitHub账号

本机若已有Git可直接使用。检查：

```powershell
git --version
python --version
```

建议电脑侧使用Python3.11或3.12。如果只有`py`启动器，将下文的`python`换成`py -3`。没有Python时按[Python官方Windows说明](https://docs.python.org/3/using/windows.html)安装；检查不需要额外pip依赖。Git通过[Git官网](https://git-scm.com/downloads)安装。

注册或登录GitHub。HTTPS推送优先使用Git凭据管理器弹出的浏览器登录；若终端只接受密码提示，GitHub账号登录密码不能用于Git推送，应按[GitHub认证说明](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/about-authentication-to-github)配置令牌或SSH。不要将令牌发进聊天或写入远程URL。

## 3 本地检查并建立独立仓库

在Windows资源管理器打开干净发布目录，在地址栏输入`powershell`打开该目录的终端，先运行：

```powershell
python scripts/test_local.py
python scripts/check_release.py --verify-manifest
```

本地测试、哈希及发布检查均成功后，初始化这个目录自己的仓库：

```powershell
git init -b main
git rev-parse --show-toplevel
```

确认输出是当前`jetson-ptz-inspection`目录，而非上一级毕设目录。`main`是首次公开发布的默认分支。

若尚未配置Git作者信息，仅为当前仓库设置。把下面两项换成你自己的署名和邮箱；可以使用GitHub设置中的隐私邮箱：

```powershell
git config user.name "你的署名"
git config user.email "你的提交邮箱"
```

接着提交：

```powershell
git add .
git diff --cached --stat
git status --short
python scripts/check_release.py --verify-manifest
git commit -m "Release v0.5.0-demo: Jetson PTZ inspection prototype"
```

暂存列表应只有公开工程文件，没有`var`、`runs`、`.local.json`、`.env`、人物照片或口令文件。发布检查会拒绝清单外的已跟踪文件，但模式扫描不能代替人工核对。

## 4 在网页创建空仓库

登录GitHub，点击新建仓库。推荐填写：

| 项目 | 建议值 |
| --- | --- |
| Repository name | `jetson-ptz-inspection` |
| Description | `Experimental visual inspection pipeline on Jetson TX2 with YOLO, RTSP and Hikvision PTZ control.` |
| Visibility | 希望立即开源时选Public；仍需核对权属时可先选Private |
| Add README | 不选，已有本地版本 |
| Add .gitignore | 不选，已有本地版本 |
| Choose a license | 不在网页重复添加，本地已有AGPL-3.0 |

创建后复制HTTPS仓库地址。用实际GitHub用户名替换下面的`YOUR_GITHUB_USERNAME`：

```powershell
git remote add origin https://github.com/YOUR_GITHUB_USERNAME/jetson-ptz-inspection.git
git remote -v
git push -u origin main
```

这里的push会将已提交文件真正上传。若出现浏览器授权窗口，确认登录的是你希望发布的账号。命令流程参考[GitHub官方导入说明](https://docs.github.com/en/migrations/importing-source-code/using-the-command-line-to-import-source-code/adding-locally-hosted-code-to-github)。

## 5 检查仓库首页和Actions

推送后刷新仓库页面，核对：

- README能正常显示架构图、能力边界和文档链接，许可证识别为AGPL-3.0。
- `.github/workflows/ci.yml`存在，Actions中的`Offline checks`开始运行Linux和Windows两项任务。
- 等两项任务成功后再标记首版发布。CI不会编译ARM64检测器、访问相机或代替实机复验。
- 如果公开仓库的Actions被禁用，在仓库设置中按GitHub提示启用。工作流不需要相机密码或其他仓库Secrets。

自动检查失败时打开失败步骤，先复现本地命令。不要为了显示绿色而删除检查。当前工作流提供的是配置；发布前没有在你的远程仓库实际运行，结果以首次Actions运行为准。

可在仓库About中补充关键词：`jetson-tx2`、`ptz-camera`、`computer-vision`、`ncnn`、`yolo`、`visual-servoing`。这些描述不表示NVIDIA、海康或Ultralytics官方认可本项目。

## 6 创建首个预发布版本

Actions成功后，在仓库Releases中新建版本：

- Tag：`v0.5.0-demo`，目标选择已检查的main提交。
- Title：`v0.5.0-demo — stationary-target inspection prototype`。
- 勾选Pre-release，表明仍是实验Demo。
- 版本正文可直接复制[首版发布说明](RELEASE_NOTES_v0.5.0-demo.md)。

GitHub会自动提供与标签提交对应的源码压缩包。额外附件可上传经过检查的GitHub发布包；演示视频可后补，当前没有公开人物照片或视频。不上传旧工作区ZIP或原始实验日志。

## 7 后续更新

修改后刷新清单，再运行检查：

```powershell
python scripts/update_manifest.py
python scripts/test_local.py
# 示例为只修改首页；按实际修改逐项列出已审核文件
git add -- README.md FILE_MANIFEST.json
git diff --cached --stat
python scripts/check_release.py --verify-manifest
git commit -m "Describe the concrete change"
git push origin main
```

新增公开文件时先用`python scripts/update_manifest.py --add 路径`显式登记，并在最终清单审计前暂存已审核的新文件。源码改动、模拟测试、机械测试和发布整理分别描述，不沿用旧实验当作新验证。新版本使用新标签，保留历史失败和已发布版本。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| 仓库里只有一个ZIP，没有源码目录 | 推送解压后的文件树；不要用ZIP替代源码 |
| 首次push提示远程已有提交 | 新仓库应为空；若网页生成了README等，先检查远程差异，不使用强制覆盖命令 |
| Permission denied或403 | 核对账号、仓库URL及推送权限，按官方认证流程处理 |
| .github没有上传 | 使用Git命令提交整个干净目录，不靠资源管理器只选择可见文件 |
| 清单哈希不符 | 确认LF换行及改动来源，更新清单后重新检查 |
| Python找不到或弹出商店 | 安装正式Python或使用可用的py启动器，不把未执行检查当作通过 |
| 发现真实凭据已进入历史 | 先更换凭据，再按GitHub敏感数据移除文档处理；删除当前文件不足以撤回旧副本 |
