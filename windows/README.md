# Windows 测试端 v0.4.12

接收电脑连接 JK808 和 FTDI USB NMC 线；测试主机使用固定 U 盘中的 HWiNFO32，BurnInTest 已安装在系统盘。
直接使用者请下载 Releases 中的完整包，其中提供运行库、FTDI 驱动和 VC++ 依赖。

首次使用运行「配置环境.cmd」，选择本机角色。填写 config.json 中的项目、批次、可访问的 NTP、本地视频文件和在线视频网址；盘信息自动识别。
两端分别运行「开始_测试主机.cmd」「开始_接收电脑.cmd」。

正式测试顺序：在线视频 → 待机 → 本地视频 → BIT。每项开始前等待 120 秒。
待机 1500 秒、本地/在线视频各 1200 秒、BIT 900 秒。失败后接收端选择 D 继续未完成流程。
详细操作见「操作指导.html」。

源码开发使用 Python 3.12，依赖见 requirements.txt。源码目录不包含 runtime、drivers、prerequisites，CMD 的便携运行环境请取自同版本完整包。
运行离线测试前执行：python -m unittest discover -s tests -v。测试不连接真实硬件。
