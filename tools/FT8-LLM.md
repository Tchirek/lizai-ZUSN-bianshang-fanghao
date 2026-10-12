# FT8 自由文本的 LLM 回复

`scheduler/ft8_llm.py`。调度参数加 `--ft8-llm` 后启用，需要同时有 `--ft8-tx --mode live`。代码里默认关闭，本站的 `scheduler/scheduler.env` 开着它。CQ、呼号、网格、报告和 RR73/73 始终由状态机处理，不经过 LLM。

## 何时回复

一次通联完成后的四分钟内，只对同一接收时隙奇偶、音频频率相差不超过 50 Hz 的自由文本调用 Responses API。

- 同一条消息只回一次。
- 请求在后台进行，8 秒超时；回复过期或生成失败就跳过。
- FT8 自由文本不带发送者呼号，这种归属判断只是启发式，频点拥挤时可能认错对方。

回复最多 13 个 ASCII 字符，发送前还要经 WSJT-X 编码器确认属于自由文本（类型 0.0），不会拆成多帧。例：`HOW R U?` → `FINE TNX`。

聊天记在 `auto.jsonl`（事件 `llm_receive`、`llm_transmit`、`llm_error`），不算新的 ADIF 通联。

## 模型看到什么

- 双方呼号。
- 这次通联的全过程：每一帧（状态机发的、操作员手动发的、对方发的）及其 UTC 时间和信号报告，还有对方的网格、收发的报告。
- 之后最多四轮自由文本聊天。
- 本站可以说的事实 `FT8_LLM_FACTS`，以及被问联系方式时的固定回复 `FT8_LLM_CONTACT`。没设就不说。

密钥、精确坐标、飞机数据都不进提示。API 调用带 `store=false`。

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

兼容 OpenAI Responses API 的服务都能用，按所选账户计费。

参考：[OpenAI Responses API 文本生成](https://developers.openai.com/api/docs/guides/text)、[DeepSeek Responses API](https://api-docs.deepseek.com/api/create-response/)、[FT4/FT8 协议规范](https://wsjt.sourceforge.io/FT4_FT8_QEX.pdf)（类型 0.0、13 字符自由文本）。
