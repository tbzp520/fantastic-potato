# JK808 SSD 温度自动化测试

采集 JK808 表面温度和 SSD SMART，执行 Windows / Linux 测试、对齐原始记录、绘图并汇总到 Excel。

## 下载与更新

| 程序 | 当前版本 | 发布页 | 运行位置 |
| --- | --- | --- | --- |
| Windows | 0.4.22 登录后全屏修复版 | [Windows 发布页](https://github.com/tbzp520/fantastic-potato/releases/tag/windows-v0.4.22) | Windows 测试主机、接收电脑各一份 |
| Linux | 2.3.4 | [Linux 发布页](https://github.com/tbzp520/fantastic-potato/releases/tag/linux-v2.3.4) | Windows 接收电脑，通过 SSH 控制一台或两台 Linux |

直接使用请下载附件 `JK808_..._full.zip` 及其 `.zip.sha256`。GitHub 自动生成的 **Source code (zip)** 只有源码，不含离线运行环境。Linux 另提供 `_update.zip`，不覆盖配置、模板及 wheelhouse。

等测试、回传结束后退出旧程序，再完整解压。保留每台电脑自己的 `config.json`；Linux 双机还需保留自己的 `双机配置.json`。不要复制旧 `.venv`。公开源码和附件中的示例配置不带现场 SSH、盘身份、仪器绑定、网卡标识、内网 NTP 或视频路径。

## Windows

接收电脑连接 JK808，并通过 FTDI USB NMC 虚拟串口连接测试主机。测试主机使用固定 U 盘中的 HWiNFO32、系统盘已安装的 BurnInTest 和本机常用 Edge。

整套顺序为 **在线视频 → 待机 → 本地视频 → BIT**。新建整套首项在线视频免开始前等待；其他项目、单项、联调及续测继续使用各自的配置等待值。

| 项目 | 正式时长 | SMART 原始间隔 | 绘图和统计 |
| --- | --- | --- | --- |
| 在线视频 | 20 分钟 | 5 秒 | 连续 600 秒，每 30 秒一点，共 20 点 |
| 待机 | 25 分钟 | 60 秒 | 连续 10 点 |
| 本地视频 | 20 分钟 | 5 秒 | 连续 600 秒，每 30 秒一点，共 20 点 |
| BIT | 15 分钟 | 5 秒 | 连续 600 秒，每 5 秒一点，共 120 点 |

扫码登录时保持普通 Edge 窗口，等待不限时、不计正式测试时间。确认登录后才设置画质、播放、最大化及视频全屏；至少 1080P / 30 帧并通过就绪检查后才正式计时。

每次正式测试从测试主机读取机型、CPU、系统、BIOS 和系统盘，接收电脑只接收主机快照。换机自动定位已有视频、BIT 和网卡；无法唯一确认时提示处理。B 可直接正式测试，D 继续原电脑和原 SSD 的未完成项目。同批次平台必须一致。

详细步骤：[Windows 使用说明](windows/README.md)、[操作指导](windows/操作指导.html)。

## Linux

保留单机流程，并支持一台 Windows 接收电脑、一台八通道 JK808 同时控制两台 Linux。两台使用独立 SSH、盘身份、探针分配和结果目录，可分别启动、取消及续测。

A 部署或换盘时核对身份；B 选择 L1.2、L0→写→读或单项。L1.2 后由测试人员手动关机调整 BIOS。读写正式记录和 fio 均为 900 秒，SMART 每 5 秒采集，最后 600 秒每 30 秒选一点，共 20 点；L0、L1.2 沿用各自规则。

**Linux 读写项包含 TRIM，会清除指定测试盘数据。部署时必须核对未挂载的专用测试盘，按实际许可启用写盘授权。**

详细步骤：[Linux 使用说明](linux/README.md)、[单机与双机指南](linux/使用说明.html)。

## 数据与图表

JK808 每秒记录。SMART、JK808 全量原始记录与正式段、绘图选点分别显示，提供中文 CSV 和可直接查看的 HTML；缺失会说明，不补造。图统一为 3000×1800 PNG，Excel 使用同一张图；六路温度全部实线、不描点、不平滑、不修改原值。CPU 温度指标表示 SSD 主控，不是电脑 CPU。

公开空模板保留原三个容量工作表及版式，没有现场数据或图片。程序按需追加 128G、256G，支持 128G、256G、512G、1T、2T 汇总。

## 源码与发布验证

- `windows/`：主机控制、接收调度、串口通信、环境准备及报表。
- `linux/`：单机/双机 SSH 流程、JK808 采集、报表和人工复算。
- `tools/`：公开配置、语法、启动哈希校验及发布打包。
- `docs/`：版本说明。

Git 不保存 runtime、驱动、离线 wheels、实际配置和现场结果。源码使用 Python 3.12 x64；依赖见各目录 requirements 文件。离线测试和模拟通过不代表现场双机已验收，真实环境需按操作指导检查。第三方许可见各目录 `THIRD_PARTY_NOTICE.txt`。

```powershell
python tools/verify_source.py --check-files
python tools/build_release.py --windows-dependencies D:\licensed-dependencies --linux-wheelhouse D:\linux-wheelhouse --output D:\release-output
```

打包只从明确指定的依赖目录读取运行库，不下载安装软件。生成三个 ZIP、各自的 SHA256 及 `release-manifest.json`；`FILES.sha256` 记录每个包内文件的哈希。输出目录必须在源码工作树之外。
