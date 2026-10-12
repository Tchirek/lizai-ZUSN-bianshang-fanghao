# 立在 ZUSN 边上放号

莫斯科号无法同时使用通信和雷达设备，rtl-sdr v4无法同时接收adsb和民用频段。

这是一个业余电台站的软件，跑在一台 Debian 笔记本（ThinkPad X230）上，项目代号 influ。

- **接收**：站里只有一个 RTL-SDR V4，平时守在 1090 MHz，给 FlightAware 和 Flightradar24 喂 ADS-B。调度器在不影响喂数的空当，把它切到 2 米波段听十几秒，收 FT8、看频段活动。
- **发射**：一台 UV-K5，用 CEC 固件经写频线发 FT8。
- **显示**：机器自带屏幕上跑一个本地化的 HamClock。

## 组成

| 目录 | 内容 |
|---|---|
| `sdrmux/` | 独占 SDR 的复用器：1090 MHz 的样本不断流地喂给 dump1090，切走时补零，馈送和 MLAT 不掉线；切出时录下 IQ，事后解 FT8、找 FM 活动 |
| `scheduler/` | 每 15 秒决定 SDR 能不能离开 1090 MHz；FT8 自动通联的状态机、网页审批和可选的 LLM 自由文本回复 |
| `collector/` | 只读记录：Beast 帧、aircraft.json、接收统计、云端参考航迹、天气 |
| `analysis/` | 模型与报告：到达风险模型、训练时段统计、覆盖模型、虚拟切换回测、实际窗口对账、增益 A/B 诊断 |
| `hamclock/` | HamClock 原生全屏窗口：可缩放的 UV-K5 视距覆盖图、业余活动图层、本周 FT8 统计 |
| `tools/` | FT8 网页、UV-K5 读写与刷机、HamClock 补丁、ADS-B 雷达屏、DX 集群过滤 |
| `deploy/` | systemd 用户服务与定时器，以及唯一需要 root 的 `influ-admin` |

说明文档：

- [调度策略](tools/TRAFFIC-POLICY.md)
- [FT8 自动通联](tools/FT8.md)
- [FT8 网页](tools/FT8-WEB.md)
- [LLM 回复](tools/FT8-LLM.md)

## 部署

- 仓库内容放在站上的 `~/influ`，Python 依赖装进 `~/influ/.venv`。
- `deploy/` 里的单元都按用户服务启用。
- `influ-admin` 由站主以 root 安装到 `/usr/local/sbin`，用法见它的文件头。

站点的位置、呼号和网格只放在 `~/influ/station.json`（模板是 `deploy/station.example.json`），不进仓库。发往公共接口（adsb.lol、adsb.fi、Open-Meteo）的位置只取到 0.01°。密钥和登录口令放在 `~/influ/private/`，同样不进仓库。

## 1090 MHz 接收设置

手动增益 49.6 dB，不开 RTL2832 数字 AGC。依据是 `analysis/gain_ab.py report` 对随机分块 A/B 的诊断：

- 数字 AGC 带来的只有噪声：Mode A/C 噪声帧约多 47 倍，落在真实飞机地址上的假帧也更多。
- 真实接收（位置帧、经校验的飞机数）没测出差别。

## 发射

- 只发 FT8。手台必须处在 FT8/Ready，退出即停发。
- 自动发射前必须有一次通过的收发校准（`tools/k5ft8_calibrate.py`）。
- 手台天线要和 SDR 天线隔开，近距离发射会压垮接收机。
- FT8 网页可以加登录（`tools/ft8log_http.py passwd`）；不加的话，局域网里谁都能让手台发射。

## 数据来源与许可

- 地图数据 © [OpenStreetMap](https://www.openstreetmap.org/copyright) 贡献者，ODbL，经 Overpass API 获取。
- 高程：AWS Terrain Tiles（Terrarium 格式），来源见 [Tilezen 的说明](https://github.com/tilezen/joerd/blob/master/docs/attribution.md)。
- [HamClock](https://www.clearskyinstitute.com/ham/HamClock/)（Clear Sky Institute，WB0OEW）。其天气（OpenWeatherMap）、DX 远征和比赛日历数据经 HamClock 的服务获取；本站补丁去掉了屏幕上的来源字样，数据与致谢归原提供者。
- 业余活动：[PSK Reporter](https://pskreporter.info/)、APRS-IS、[WSPR.live](https://wspr.live/)。
- 参考航迹：[adsb.lol](https://www.adsb.lol/)（ODbL）、[adsb.fi](https://adsb.fi/)。
- 天气：[Open-Meteo](https://open-meteo.com/)（CC BY 4.0）。
