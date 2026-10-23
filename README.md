# JK808 SSD 温度自动化测试

采集 JK808 表面温度与 SSD SMART 温度，自动执行 Windows / Linux 测试、对齐记录、绘图并汇总到 Excel。

## 下载与启动

| 程序 | 当前版本 | 完整包下载 | 启动位置 |
| --- | --- | --- | --- |
| Windows | 0.4.12 | [Windows 发布页](https://github.com/tbzp520/fantastic-potato/releases/tag/windows-v0.4.12) | Windows 测试主机和接收电脑各一份 |
| Linux | 2.2.4 | [Linux 发布页](https://github.com/tbzp520/fantastic-potato/releases/tag/linux-v2.2.4) | Windows 接收电脑，通过 SSH 控制 Linux |

请下载发布页附件中的 `JK808_..._full.zip`。GitHub 自动生成的 `Source code (zip)` 只包含源码，没有完整的离线运行环境。
附件另有 SHA256 校验文件。完整解压后再运行 CMD，不要直接在压缩包里运行。

更新已有环境时，保留每台电脑自己的 `config.json`。公开发布包不预填现场盘序列号、网卡标识、内网服务器和视频路径。
首次使用 Windows 包需填写可访问的 NTP、本地视频文件及在线视频网址；Linux 包需填写 SSH 主机、项目与批次等现场信息。

## Windows 流程

接收电脑连接 JK808，并通过 FTDI USB NMC 虚拟串口连接测试主机。测试主机使用固定 U 盘中的 HWiNFO32，调用系统盘已有的播放器和 BurnInTest。

整套顺序：**在线视频 → 待机 → 本地视频 → BIT**。每项开始前等待 2 分钟。

| 项目 | 正式时长 | SMART 记录间隔 | 连续选点数 | 测试主机网络 |
| --- | --- | --- | --- | --- |
| 在线视频 | 20 分钟 | 60 秒 | 12 | 联网 |
| 待机（桌面开着） | 25 分钟 | 60 秒 | 10 | 断网 |
| 本地视频 | 20 分钟 | 60 秒 | 12 | 断网 |
| BIT | 15 分钟 | 30 秒 | 20 | 断网 |

JK808 每秒记录一次。自动识别当前系统盘，按容量命名结果；B 可直接正式测试，D 可继续失败的流程。
在线视频页面首次失败后最多刷新重试 5 次；等待扫码不限时，画质要求至少 1080P / 30 帧。

操作入口和详细说明：[Windows 使用说明](windows/README.md)。

## Linux 流程

A 首次部署或换盘后更新盘信息，核对项目、批次及盘片归属。
B→1 测 L1.2；B→2 依次测 L0、写、读；B→3 在四项中任选一项单独测试。
L1.2 完成后由测试人员手动关机并调整 BIOS，再继续其他测试。

读写正式记录和 fio 均为 900 秒。曲线截到最后一个有效 SMART 点；插入 Excel 的图片随单元格调整位置和大小。
同项目同批次的 512G、1T、2T 结果汇总到同一本表。

**Linux 读写项包含 TRIM，会清除指定测试盘数据。部署时必须核对专用测试盘。**
Linux 配置按现场需求支持明文保存 SSH 密码，勿提交实际配置和配置备份。

操作入口和详细说明：[Linux 使用说明](linux/README.md)。

## 源码与验证

- `windows/`：测试主机控制、接收端调度、串口通信、环境配置、绘图与报表。
- `linux/`：SSH 测试流程、JK808 采集、报表和人工复算代码。
- `tools/`：源码校验和发布打包工具。
- `docs/`：发布说明。

运行时、驱动、离线 wheels 通过 Release 附件提供，不存入 Git 历史。
仓库提供示例配置和空白温度模板；测试使用临时目录和模拟设备，不包含现场结果包。
源码开发使用 Python 3.12；依赖见各目录的 requirements 文件。Linux 测试额外需要 `openpyxl` 作只读校验。

完整包针对 HWiNFO32 8.34-5870、BurnInTest 9.0 build 1003 及既定设备适配。
离线验证不等于现场双机验证；更换软件版本或测试平台后，应先做一次联调。
第三方软件和依赖的许可说明见各目录的 `THIRD_PARTY_NOTICE.txt`。

在仓库根目录运行 `python tools/verify_source.py`，可检查 Python 语法和 Windows 启动文件清单，不连接测试设备。发布包构建命令可通过 `python tools/build_release.py --help` 查看；构建时需另行提供相应的离线依赖目录。
