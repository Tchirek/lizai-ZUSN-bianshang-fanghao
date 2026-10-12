# FT8 自由文本的 LLM 回复

`scheduler/ft8_llm.py`。调度参数加 `--ft8-llm` 后启用，需要同时有 `--ft8-tx --mode live`。代码里默认关闭，本站的 `scheduler/scheduler.env` 开着它。CQ、呼号、网格、报告和 RR73/73 始终由状态机处理，不经过 LLM。

## 何时回复

一次通联完成后的四分钟内，只对同一接收时隙奇偶、音频频率相差不超过 50 Hz 的自由文本调用 Responses API。

- 同一条消息只回一次。
- 请求在后台进行，8 秒超时；回复过期或生成失败就跳过。
- FT8 自由文本不带发送者呼号，这种归属判断只是启发式，频点拥挤时可能认错对方。

回复最多 13 个 ASCII 字符，发送前还要经 WSJT-X 编码器确认属于自由文本（类型 0.0），不会拆成多帧。例：`HOW R U?` → `FINE TNX`。

聊天记在 `auto.jsonl`（事件 `llm_receive`、`llm_transmit`（带 `kind`）、`llm_open`、`llm_quiet`、`llm_notes`、`llm_error`、`sked`、`unsked`），不算新的 ADIF 通联。

## 由程序定的回答

模型每次以 JSON 作答：对方想要什么（`kind`）、它的回复（`reply`），约时间时还有换算成 UTC 的时间（`utc`）。下面几类请求，发出去的话由程序定，不用模型写的：

- **个人的事**（`personal`）：约见面（eyeball、喝咖啡、上门），要电话、QQ、微信、邮箱、地址，问操作员的姓名、年龄、工作、QTH、家庭，一律回 `FT8_LLM_CONTACT`。这个值由程序直接发出，不进提示；没设时发模型的婉拒。
- **改用话音**（`voice`）：要转 FM、SSB、上话筒或去某个话音频率，回 `FT8 ONLY SRY`，再问回 `SRY FT8 ONLY`。这是实情：写频线没有音频通路，本站只能发 FT8（见 [FT8.md](FT8.md)）。
- **约时间再通联**（`sked`）：对方提出或同意一个时间，回一句确认，时间一律写成 UTC：当天 `OK 1300Z CU`，次日 `OK TMW 1300Z`，更远 `1300Z 15 OCT`。确认发出后登记为约定（见下）。
  - 时间或时区说不清，模型先问，如 `Z OR LOCAL?`。
  - 今天已过的钟点算次日。
  - 不到两分钟后、超过七天或读不出的时间，回 `TIME IN UTC?`。
  - 那一天（UTC）已有四个约定，回 `SRY NOT THEN`，不登记。
- **取消约定**（`unsked`）：删掉对方的约定，回模型的话；模型的话发不出时回 `OK NP 73`。

## 约定

登记在 `~/influ/ft8/skeds.json`，每个电台一个，新时间替换旧的。

- 从约定时间前 5 分钟到后 15 分钟，调度器像对待进行中的通联一样守在 2 米（见 [TRAFFIC-POLICY.md](TRAFFIC-POLICY.md)）：每个能收的时隙都收，没有对象时每两分钟 CQ 一次。
- 这期间对方的呼叫和 CQ 优先应答。和对方完成通联后，约定提前结束。
- FT8 网页列出未结束的约定，并在当天的时间条上标出守听时段。

## 模型看到什么

- 双方呼号，现在的 UTC 和本地时间。
- 这次通联的全过程：每一帧（状态机发的、操作员手动发的、对方发的）及其 UTC 时间和信号报告，还有对方的网格、收发的报告。
- 之后最多四轮自由文本聊天。
- 以前和对方的通联与关于对方的要点（见下）。
- 本站可以说的事实 `FT8_LLM_FACTS`。没设就不说。

密钥、联系方式、精确坐标、飞机数据都不进提示。API 调用带 `store=false`。

## 记得通联过的电台

- 每次完成的通联都记进 `~/influ/ft8/memory.json`：时间、双方网格、报告、双方每一帧，以及之后的自由文本聊天。这个文件不在时，从 `auto.jsonl` 重建。
- 一段聊天结束（通联后的四分钟过去）后，模型把它和旧要点一起整理成关于对方的要点，最多 8 条（姓名、地点、设备、约定之类）：旧的合并、更新或删掉，不凭空添加。
- 再次和同一电台完成通联时，要点和最近 5 次通联放进提示。模型可以先发一句简短的寒暄（不超过 13 个字符，觉得没什么可说就不发），对方提起以前的通联也能接上。
- 回复或寒暄在路上、或聊天还在继续时，算作这次通联的一部分：调度器照常为它开接收窗口、放行发射，最长到通联结束后四分钟。

## 配置

`~/influ/private/llm.env`，权限 600，由用户服务读取，不进仓库：

```
OPENAI_API_KEY=…
OPENAI_MODEL=deepseek-flash
OPENAI_BASE_URL=https://api.deepseek.com
OPENAI_REASONING_EFFORT=none
FT8_LLM_FACTS=RIG? UV-K5; RX? RTL-SDR V4; …
FT8_LLM_CONTACT=…
```

`FT8_LLM_CONTACT` 是站主自己的联系渠道（如 TG），按 FT8 自由文本写：最多 13 个字符，只用 A–Z、0–9、空格和 `+ - . / ?`。

兼容 OpenAI Responses API 的服务都能用，按所选账户计费。

参考：[OpenAI Responses API 文本生成](https://developers.openai.com/api/docs/guides/text)、[DeepSeek Responses API](https://api-docs.deepseek.com/api/create-response/)、[FT4/FT8 协议规范](https://wsjt.sourceforge.io/FT4_FT8_QEX.pdf)（类型 0.0、13 字符自由文本）。
