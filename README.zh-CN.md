# FNIRSI DMC-600 蓝牙万用表记录器

[English](README.md)

Windows 电脑可以独立通过 BLE 读取 DMC-600，并保存测量 CSV。真机抓包已确认 14 种功能码，覆盖交直流电压和电流、电阻、通断、二极管、电容、频率、温度、NCV、LIVE、浪涌和交直流同测。记录器还识别 HOLD 和开路超量限。各档位的验证范围见 [协议规格](docs/PROTOCOL.md)。

## 下载 Windows EXE

从 [GitHub Releases](https://github.com/zhk-vb/DMC600-Reverse/releases/latest) 下载 Windows x64 EXE，双击后选择 English 或简体中文。无需安装 Python。点击「播放演示」即可查看内置样本。

## 从源码安装

需要 Windows、Python 3.10+（含 Tk）和支持 BLE 的蓝牙适配器。在项目目录运行：

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

以下命令中的 `python` 指安装了依赖的 Python。如果使用上面的虚拟环境，可用 `.venv\Scripts\python` 直接运行。

## Windows 实时仪表盘

仪表开机并释放手机连接后，在项目目录运行：

```powershell
python src/meter_dashboard.py
```

默认启动中文版。使用下列命令启动英文界面，其他参数和文件格式通用。

```powershell
python src/meter_dashboard.py --language en
```

启动时自动扫描，只有一台 DMC-600 时自动连接。发现多台时，从设备下拉菜单按名称和蓝牙 ID 选择，再点「连接」。「扫描设备」可以更新列表。使用 `--manual` 可跳过启动扫描。窗口显示读数、档位、单位、量程、HOLD、Min/Max 和最近 30 秒的 BLE 读数趋势。同测档分开显示 W、V 和 A。上报速率按最近 5 秒收到的测量帧计算，包含设备重复上报的帧。Min/Max 从连接或切档开始统计，单位变化时重置对应通道，开路和无效值跳过。

「曲线取点间隔」可在 20–3000 ms 内调整，默认 50 ms，实时和回放均适用。电脑端按 BLE 测量帧的时间戳取点，实际取点速度受上报速率限制。Min/Max、实时报表读数、上报速率及 CSV 保存均使用完整数据。官方 APP 的范围和默认值见固件笔记中的曲线设置记录。

「坐标范围」可设置 0–3600 秒内的正数横轴跨度，以及纵轴上下限。固定纵轴按各通道的显示单位应用。默认横轴最近 30 秒，纵轴自动。

「蓝牙查询间隔」默认 500 ms，也可在实时连接时切换到 200 ms。200 ms 更频繁地读取仪表的当前值，可能收到更多重复帧。

「保存最近 60 秒」将按钮按下前的缓冲区保存到文件，采集不足 60 秒时保存已有数据。「开始实时保存」将之后收到的帧连续写入，手动停止保存、断开或关闭窗口时结束。两个入口都支持 CSV 和 ASCII。CSV 可用「打开记录回放」按原时间间隔播放。

选择「ASCII / MDA」默认保存 `.ascii`，内容使用 ETAS `ETASAsciiItemFile` 单时间轴布局，含采样数、通道类型和单位。保存对话框也可选 `.dxl`，使用 ETAS 多时间轴布局。两者均由仪表盘直接写入。用户已在 MDA 中实测确认 `.ascii` 导入可用。旧版软件导出的 `.asc` 采用 DXL 布局，按 `.dxl` 使用。参见 [ETAS MDA 支持的文件格式](https://docs.etas.com/mda/docs/V8.7.6/EN/Content/Topics/2_FileExplorer/FileEx_CON_SupportedFileFormats.htm)。

文本导出保留 BLE 接收时间，电压、电流和功率归一到 V、A、W。同测档分别输出三路。缺失值在 `.ascii` 写为 `NaN`，在 `.dxl` 留空。状态通道中 0 为有效、1 为超量限、2 为无效。实时导出提供 `DMC600_UTC_s`（电脑时钟 Unix 秒），回放仅保留相对时间。和发动机数据对齐时以共同事件校准，BLE 接收时间包含传输延迟。

实时连接时，控制栏可请求切换档位、HOLD 和 REL/ZERO。界面等待仪表状态确认，超时不自动重发。HOLD 命令会翻转仪表当前状态。

无需连接仪表即可回放随仓库附带的模拟数据：

```powershell
python src/meter_dashboard.py --replay samples/demo.csv
```

连接后断线，仪表盘会重连同一台仪表，并在同一记录文件接着写。断线期间的空档保留，控制命令需要重新发起。

「仪表设置」可调整亮度、表端语言和自动关机时长。音量当前显示上报值。

待完善项包括表端原始波形、电压电流档的同步信号频率、同测档远程 AC/DC 切换，以及音量写入。独立频率档的测量值仍正常显示。

## 命令行记录器

让万用表开机，在手机 FNIRSI+ 里断开它的连接，然后运行：

```powershell
python src/bt_scan.py 10
python src/meter_record.py 60
```

默认自动连接唯一一台 DMC-600，并保存到 `meter_log.csv`。多台同时开机时请指定扫描得到的设备 ID。可指定设备地址、输出路径和原始 BLE 抓包：

```powershell
python src/meter_record.py 60 readings.csv --address AA:BB:CC:DD:EE:FF --raw notifications.csv
python tools/verify.py
```

`--raw` 写入新的 CSV 文件，每行一条原始通知。测量 CSV 的前五列沿用时间、读数、单位和量程，后面记录功能码、HOLD、超量限和同测档的附加读数。收到设备的 HOLD 和测量设置上报后，CSV 才填充相应状态。

## 项目资料

- [APK 静态解包与功能清单](docs/STATIC_ANALYSIS.md)
- [协议规格](docs/PROTOCOL.md)
- [V1.0.19 固件分析](docs/FIRMWARE_V1.0.19.md)
- [模拟回放数据](samples/demo.csv)

运行 `python tools/verify.py` 检查 CRC、分包解码和 14 个档位。模拟文件由程序生成，供无设备预览和回归测试。

## 许可证

项目代码按 [MIT](LICENSE) 发布。独立项目，与 FNIRSI 或 ETAS 无隶属关系。
