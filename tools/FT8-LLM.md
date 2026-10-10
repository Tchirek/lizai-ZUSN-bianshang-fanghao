这是可选版本，默认关闭；现有 CQ、呼号、网格、报告及 RR73/73 全部继续由原状态机处理，不调用 LLM。

启用时，提供 `OPENAI_API_KEY` 和 `OPENAI_MODEL` 环境变量，在原调度参数后追加 `--ft8-llm`。兼容服务可设置 `OPENAI_BASE_URL`；DeepSeek Flash 使用 `https://api.deepseek.com` 和 `deepseek-flash`，可用 `OPENAI_REASONING_EFFORT=none` 关闭思考。原有 `--ft8-tx --mode live` 和校准要求仍然适用。密钥只放在站点的 `~/influ/private/llm.env`，权限 600，由用户服务读取，不提交 GitHub。

完成正常 QSO 后四分钟内，只对相同接收时隙奇偶、相近音频频率（±50 Hz）的自由文本调用 OpenAI Responses API。每个不同消息只应答一次；API 在后台运行，8 秒超时，回复过期或生成失败就跳过。FT8 自由文本没有发送者呼号，因此这种归属判断只是启发式，在拥挤频点可能误认对方。

回复只允许最多 13 个 ASCII 字符，发送前还须被 WSJT-X 编码器确认属于自由文本类型 0.0；不会拆成长篇多帧消息。例：`HOW R U?` → `FINE TNX`。自由聊天记录仍在 `auto.jsonl`，事件名为 `llm_receive`、`llm_transmit`、`llm_error`；不计作新的 ADIF 通联。

发给 API 的内容只有双方呼号、收到的短文本和最多四轮聊天历史。密钥、精确坐标、飞机数据不进入聊天提示。API 使用 `store=false`，实际调用按所选 API 账户计费。2026-10-10 已在 X230 启用 `deepseek-flash`，并通过一条不发射的真实 API 测试。

通联查看：`http://192.168.10.167:8073/`；收发文本：`/events`；ADIF 下载：`/qsos.adi`。这是只读的局域网 HTTP 服务。

接口依据：[OpenAI Responses API 文本生成](https://developers.openai.com/api/docs/guides/text)、[DeepSeek Responses API](https://api-docs.deepseek.com/api/create-response/)。协议依据：[FT4/FT8 协议规范，类型 0.0、13 字符自由文本](https://wsjt.sourceforge.io/FT4_FT8_QEX.pdf)。
