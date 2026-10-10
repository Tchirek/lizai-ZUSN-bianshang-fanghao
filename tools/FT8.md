FT8 自动通联使用现有 CEC 0.3V 固件、普通写频线和 RTL-SDR V4；无需额外声卡。

手台保持 **FT8 / Ready**（正常画面长按 MENU）。按 EXIT 退出后，软件的模式核对会停止后续发射。固件在发送一个完整的 79 符号帧时阻塞，串口无法中途取消这一帧；最长约 13 秒，完成后自动恢复接收。不要把手台放回 SDR 天线旁。

X230 上的 `influ-scheduler` 用户服务通过已有的 `dialout` 用户组启动（无需重启用户登录），增加 `--ft8-tx`，负责完整流程：

- 原有风险模型批准接收窗口后，监听 144.174 MHz，解码 CQ 或对本台的呼叫。
- 自动应答、交换报告、发送 RR73 / 73；无目标时，每两分钟最多一次 CQ，且必须先有一个获准的接收窗口。
- 解码没赶上下一个发射时隙时，等下一个同奇偶时隙。每条消息最多发三次，每次通联最多等待四分钟。
- 已完成的双向交换写入 `~/influ/ft8/qsos.adi`；全过程写入 `auto.jsonl`。未完成的尝试不算 QSO。
- 接收继续遵守日、小时风险和盲区预算。日总风险上限仍为 0.1 架、单窗口 0.015 架，小时上限调整为 0.05 架，以容纳一轮通联所需的三个接收窗口。开始通联前确认至少还有两个接收窗口的预算。发射时 V4 留在 1090 MHz；条件变化或预算不足时暂停接收，可能无法完成一次通联。

启停（Windows PowerShell）：

```powershell
ssh x230@192.168.10.167 "systemctl --user stop influ-scheduler"
ssh x230@192.168.10.167 "systemctl --user start influ-scheduler"
ssh x230@192.168.10.167 "journalctl --user -u influ-scheduler -n 30 --no-pager"
```

单帧发射工具是 `tools/k5ft8.py send`。重刷固件、更换手台后，先重新做 `tools/k5ft8_calibrate.py`，验证八个音调和真实回收解码；失败时不能启用自动发射。校准会开一个约 14 秒的 SDR 接收窗口，其风险须计入当日预算。

2026-10-10 真机校准：`CQ BG8ASN OM20` 完整解码，时间误差 0.0 秒，八音调间隔 6.13–6.40 Hz；1200 Hz 的命令偏移实测约 1127 Hz。校准数据和录音保存在 X230 的 `~/influ/radio/`，不提交公开仓库。

来源：[CEC 固件协议和有限时长发送实现](https://github.com/phdlee/uvk5cec/blob/f17c0a5f71f8caa864fffda9a483e90e602ae38b/cecdigital.c)，[帧格式](https://github.com/phdlee/uvk5cec/blob/f17c0a5f71f8caa864fffda9a483e90e602ae38b/ceccommon.c)。

普通写频线没有语音音频通路；本实现完成的是 FT8 数字通联。
