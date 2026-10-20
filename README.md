# CANQT

CANQT 是一款面向 Windows 的 CAN/CAN FD 日志回放与波形分析工具。它可以导入 CANoe BLF 日志和 DBC 数据库，按通道完成报文解码，并以可缩放波形查看信号变化。

## 主要功能

- 管理多个 DBC，并为 BLF 通道选择对应的 DBC。
- 解析 CAN/CAN FD BLF 日志，未绑定通道可直接跳过。
- 按报文、信号、单位、通道和报文 ID 浏览解码结果。
- 搜索、选择、隐藏、排序和定位信号。
- 多信号共用时间轴，支持秒和毫秒级缩放。
- 蓝色、黄色双游标以及时间差、信号差值显示。
- 保存常用观测组，快速恢复一组信号。
- 保存和打开 CANQT 工程，保留资源、绑定和视图配置。
- 保存最近使用的 DBC、BLF 和通道绑定；日志由用户手动解析。

## 源码运行

需要 Python 3.12 或更高版本。

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python canqt.py
```

也可以双击 `run.bat` 启动源码版本。

## 构建 Windows 版本

```powershell
python -m pip install -r requirements-build.txt
python build_exe.py
```

构建结果位于 `dist\CANQT`。发布时必须分发整个 `CANQT` 文件夹，不能只复制 `CANQT.exe`。

## 测试

```powershell
python -m unittest discover -s tests -v
```

## 数据与隐私

本仓库不包含任何 DBC、BLF、MF4、MDF、A2L、ELF、CNA、ARXML 或用户工程数据。CANQT 的本地用户配置保存在 Windows 用户数据目录，不会自动上传。

## 当前限制

- 目前只解析 CAN/CAN FD 数据帧。
- 不解析 LIN、FlexRay、以太网或诊断会话。
- DBC 必须与日志通道及报文定义正确匹配。
