# FT8 自动通联

用 UV-K5 的 CEC 0.3V 固件和普通写频线发 FT8，用 RTL-SDR V4 收，不需要声卡。写频线没有音频通路，所以只能做固件自己生成音调的 FT8，不能做话音。

## 手台

- 保持 **FT8 / Ready**（主界面长按 MENU）。按 EXIT 退出后，软件核对模式时发现不在 Ready，就不再发射。
- 固件发一个 79 符号帧时会阻塞，串口无法中途取消；一帧最长约 13 秒，发完自动回到接收。
- 手台天线和 SDR 天线要隔开：近距离发射会压垮接收机。

## 流程

`influ-scheduler` 用户服务加 `--ft8-tx` 后负责整个流程，以 `dialout` 组身份读写串口。

- 调度器批准一个接收窗口后，在 144.174 MHz 收一个时隙，解出 CQ 和呼叫本台的消息。
- 自动应答、交换报告、发 RR73 / 73。没有对象时最多每两分钟 CQ 一次，而且必须紧跟一个已批准的接收窗口。
- 解码赶不上下一个发射时隙，就等下一个同奇偶的时隙。每条消息最多发三次，一次通联最长四分钟。
- 完成双向交换才写进 `~/influ/ft8/qsos.adi`；全过程记在 `auto.jsonl`，没完成的不算 QSO。
- 发射时 V4 留在 1090 MHz。接收窗口按调度策略审批（见 [TRAFFIC-POLICY.md](TRAFFIC-POLICY.md)），通联开始后优先完成。开始一次通联前，先确认今天的切出秒数和本小时的窗口数还够再收两次。

服务日志：`journalctl --user -u influ-scheduler`。

## 单帧发射与校准

`tools/k5ft8.py send` 发一帧，默认发本台 CQ。

重刷固件或换手台后，先跑 `tools/k5ft8_calibrate.py`：发一个真实 CQ，用本地 SDR 收回来，核对八个音调和解码结果；不通过就不能开自动发射。校准要占一个约 14 秒的接收窗口。校准数据和录音留在站上的 `~/influ/radio/`。

2026-10-10 的校准：本台 CQ 完整解出，时间误差 0.0 秒，八音调间隔 6.13–6.40 Hz；1200 Hz 的命令偏移实测约 1127 Hz。

参考：CEC 固件的[有限时长发送](https://github.com/phdlee/uvk5cec/blob/f17c0a5f71f8caa864fffda9a483e90e602ae38b/cecdigital.c)与[帧格式](https://github.com/phdlee/uvk5cec/blob/f17c0a5f71f8caa864fffda9a483e90e602ae38b/ceccommon.c)。
