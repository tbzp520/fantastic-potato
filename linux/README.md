# Linux 测试端 v2.2.4

本目录的控制程序运行在 Windows 接收电脑，通过 SSH 控制 Linux 测试主机。
直接使用者请下载 Releases 中的完整包；完整包含离线 wheelhouse。

源码运行前，把 config.example.json 复制为 config.json，安装 Python 3.12 和 requirements.lock 中的依赖，再运行「开始测试.cmd」。
读写测试对指定盘执行 TRIM，会清除测试盘数据。请只连接专用测试盘并在 A 部署时核对型号、序列号、项目和批次。

A：首次部署或换盘更新。B→1：L1.2；B→2：L0、写、读连跑；B→3：四项任选一项单独测试。
L1.2 完成后提示人工关机、调整 BIOS。详细步骤见「使用说明.md」。
config.json 和配置备份可能含明文 SSH 密码，已列入 .gitignore。
