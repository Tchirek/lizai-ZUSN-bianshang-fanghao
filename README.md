<p align="center"><img src="hamclock/influ-hamclock.svg" width="128" alt=""></p>

# 立在 ZUSN 边上放号

<p align="center">
  <img src="ai-slop-67.png" alt="Vibe Trending — #67 AI-slop Of The Day" width="250" height="55">
</p>

莫斯科号无法同时使用通信和雷达设备，rtl-sdr v4无法同时接收adsb和民用频段。

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="fanghao-dark.png">
    <img src="fanghao.png" width="300" alt="無數的電波正在空中怒湧，啊啊！好幅壯麗的蛤木（ham）的圖景喲！小小的UV-K5提起他全身的功率來要把西南接通。啊啊！我眼前來了的密密的航跡喲！啊啊！不斷的交媾，不斷的呼號，不斷的革命喲！啊啊！波喲！波喲！波的地圖，波的頻譜，波的遙測，波的通聯，波的律呂喲！">
  </picture>
</p>

这是一个业余电台站的软件，跑在一台 Debian 笔记本（ThinkPad X230）上，项目代号 influ。

**试用：<https://influ-demo.pages.dev>**（展示版，从几处山顶之一出发，见下文“展示网页”。）

> **In English.** Software for an amateur radio station on an old ThinkPad X230 running Debian. Its one RTL-SDR stays on 1090 MHz feeding ADS-B (FlightAware, Flightradar24) without dropping the feed or MLAT; a scheduler with a model of aircraft arrivals takes it to the 2 m band for seconds at a time to hear FT8 and watch the band, and a Quansheng UV-K5 sends FT8. A patched HamClock is the station clock: a native ADS-B scope pane, no world map of its own, and in its place a zoomable terrain map of the radio's line-of-sight coverage with APRS, FT8 and WSPR activity, search (pinyin aware), on the built-in screen and in a web page. Packaged for Debian 13: see the Releases page. `demo/` is a showcase of the same screen for Cloudflare Pages, run from one of a few summits with open data only: try it at <https://influ-demo.pages.dev>.

- **接收**：站里只有一个 RTL-SDR V4，平时守在 1090 MHz，给 FlightAware 和 Flightradar24 喂 ADS-B。调度器在不影响喂数的空当，把它切到 2 米波段听十几秒，收 FT8、看频段活动。收到的 FT8 每五分钟上报 PSK Reporter（`tools/pskreport.py`，与 WSJT-X 同一格式），本站因此列在它的监听站里；什么也没收到时也隔半小时报一次站名与 6 位网格。
- **发射**：一台 UV-K5，用 CEC 固件经写频线发 FT8。
- **显示**：机器自带屏幕上跑一个本地化的 HamClock。ADS-B 雷达屏是补丁给 HamClock 加的原生窗格，和其他窗格一样在菜单里选，点一下可在 10–100 英里间换量程；HamClock 经补丁不再有世界地图：不生成、不下载、不绘制，标记、路径、比例尺、新闻横幅和借地图显示的消息一概不画，地图框只留一片黑；覆盖图由显示程序画在那里，本周 FT8 统计叠在左栏下方。在覆盖图上点任意一处，弹出该处的可达性，HamClock 的天气窗格同时换成那里的天气（DX 设在该处），关掉弹窗才换回本站天气。左上角呼号框可换成一幅图：`tools/callbox.py 文字` 把一行字（阿拉伯文等 HamClock 字库写不出的也行，由 libraqm 排版）以 Amiri 字体（`hamclock/vendor/amiri`，OFL）白字压在红黑斜切的底上，存进 HamClock 目录，补丁过的 HamClock 便以它代替呼号；删掉那个文件即复原。

![X230 屏幕上的 HamClock：中间是可缩放的 UV-K5 视距覆盖图，叠着 APRS 台站、FT8/WSPR 路径和 Maidenhead 网格；左边 DX 集群和本周 FT8 统计，上面 X 射线、ADS-B 雷达屏和天气](hamclock/screenshot.png)

## 组成

| 目录 | 内容 |
|---|---|
| `sdrmux/` | 独占 SDR 的复用器：1090 MHz 的样本不断流地喂给 dump1090，切走时补零，馈送和 MLAT 不掉线；切出时录下 IQ，事后解 FT8、找 FM 活动 |
| `scheduler/` | 每 15 秒决定 SDR 能不能离开 1090 MHz；FT8 自动通联的状态机、网页审批和可选的 LLM 自由文本回复 |
| `collector/` | 只读记录：Beast 帧、aircraft.json、接收统计、云端参考航迹、天气 |
| `analysis/` | 模型与报告：到达风险模型、训练时段统计、覆盖模型、虚拟切换回测、实际窗口对账、增益 A/B 诊断 |
| `hamclock/` | HamClock 原生全屏窗口：可缩放的 UV-K5 视距覆盖图、业余活动图层、本周 FT8 统计 |
| `tools/` | FT8 网页、PSK Reporter 上报、UV-K5 读写与刷机、HamClock 补丁、ADS-B 雷达屏、DX 集群过滤 |
| `deploy/` | systemd 用户服务与定时器，以及唯一需要 root 的 `influ-admin` |
| `demo/` | 纯展示的网页：不连站上，挂在 Cloudflare Pages，从几处山顶之一出发，数据全取公开来源 |

说明文档：

- [调度策略](tools/TRAFFIC-POLICY.md)
- [FT8 自动通联](tools/FT8.md)
- [FT8 网页](tools/FT8-WEB.md)
- [LLM 回复](tools/FT8-LLM.md)

## 部署

- 仓库内容放在站上的 `~/influ`，Python 依赖装进 `~/influ/.venv`。
- `deploy/` 里的单元都按用户服务启用。
- `influ-admin` 由站主以 root 安装到 `/usr/local/sbin`，用法见它的文件头。

站点的位置、呼号和网格只放在 `~/influ/station.json`（模板是 `deploy/station.example.json`），不进仓库。发往公共接口（adsb.lol、adsb.fi、Open-Meteo）的位置只取到 0.01°。密钥和登录口令放在 `~/influ/private/`，同样不进仓库。站点位置每半小时经 APRS-IS 发一次信标（`tools/aprs_beacon.py`，aprs.fi 上可见），只报到整分（APRS 位置模糊 2 级，约 1.8 公里）；登录要本呼号的 APRS-IS passcode，写进 `~/influ/private/aprs.env` 的 `APRS_PASSCODE=` 才会发，没有就不发。

### Debian 软件包

GitHub 的 Releases 页有给 Debian 13（trixie，amd64）打好的 `influ_<版本>_amd64.deb`：代码装在 `/usr/share/influ`，打好补丁的 HamClock 引擎在 `/usr/lib/influ/hamclock-web`，用户服务单元在 `/usr/lib/systemd/user`，`influ-admin` 在 `/usr/sbin`；Python 依赖全用 Debian 自带的包，不建 venv。站点自己的东西仍放 `~/influ`，软件包不碰。由本仓库打包：`tools/build_deb.sh 版本号 hamclock可执行文件 [输出目录]`。

```sh
sudo apt install ./influ_0.1.0_amd64.deb
mkdir -p ~/influ && cp /usr/share/influ/deploy/station.example.json ~/influ/station.json   # 填上本站的呼号、网格和经纬度
python3 /usr/share/influ/hamclock/build_rangemap.py                                       # 视距覆盖图，要联网取地形，较久
python3 /usr/share/influ/hamclock/build_tiles.py dem admin airports campuses military render
systemctl --user enable --now hamclock.service hamclock-display.service
```

收发与调度那几项（`influ-scheduler`、`influ-collector` 等）依赖 RTL-SDR、dump1090 和电台硬件，照上文与各说明文档按需启用。

### 可选的 HamClock 网页

网页地图由 OpenLayers 在浏览器里绘制，直接读取站上的地形瓦片、图层数据和可达性 API。每个页面的地图中心、缩放和地点弹窗独立，本机窗口也不受网页浏览影响；电台与 HamClock 本身的设置仍作用于同一座站。拖拽、缩放和键盘、手柄平移在本地立即响应，地图交互不再等待服务器绘图和传帧。

按需启用，默认不开启：

```sh
mkdir -p ~/.config/systemd/user
cp ~/influ/deploy/hamclock-http.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now hamclock-http.service
```

服务监听所有网卡的 IPv4 和 IPv6 地址，内网访问 `http://站机地址:8075/`。滚轮缩放，拖动平移，点击地点查询可达性；触屏支持双指平移和捏合，方向键平移，`+` / `=` / `PgUp` 放大、`-` / `PgDn` 缩小（按一下放大或缩小一档，按住不放也只一档；按住 Shift 时才连续缩放，本机窗口相同），`Esc` 关闭弹窗；打开网页即可用键盘，不必先点地图。点地点时天气窗格同样换成彼处天气，关掉弹窗换回；几个页面和本机窗口都开着弹窗时，以最后打开的为准。按一下鼠标中键（X230 小红点的中键）进入触屏模式：指针隐去，手指在触控板上一滑，地图便顺着手指跟过去，双指捏合与滚轮照旧缩放，再按一下中键回到平常（网页借浏览器的指针锁定，`Esc` 也可退出）；原先中键给 HamClock 的备用点击改由右键或 `Ctrl+左键`。手柄（网页用浏览器所在设备的手柄，本机窗口用 pygame.joystick）：左摇杆或十字键平移，右 / 左肩键放大 / 缩小，网页 A 查询地图中心地点，B 关闭弹窗；键盘焦点在地图上时 `Enter` 也查询中心地点。时钟、天气和雷达窗继续使用无损增量帧，地图不传屏幕截图。网页可在本机窗口关闭时独立运行，没有启动加载文字或转圈遮罩。

OpenLayers 10.11.0 的 JS、CSS 和许可证随仓库提供；网页不依赖外部 CDN 或外部地图服务。瓦片来自 `~/influ/model/tiles/`，复用原生窗口的投影与缓存。可达性计算复用 `map_panel.path_profile`，按完整 DEM 分辨率计算判定，图表仅缩减传输采样点。静态图层按视野获取，活动台站与航迹定期更新，图例只数视野内的台站，与本机窗口一致。夜色和晨昏线用本机窗口的同一套算法，按页面当前视野和屏幕像素在站上算出，每分钟更新：夜色垫在河流、路径之下，金线压在它们之上，斜体 SUNSET / SUNRISE 沿线标注，放大多少倍都不糊。

本机窗口与网页均可按 `S` 或 `/` 搜索地图已有的地点、机场和电台呼号：弹出一个 `>_` 输入框，输入名称的一部分即可，不区分英文大小写，汉字与拼音互通：`遂宁` 与 `Suining` 找到的相同，拼音须从音节开头对起（地名的拼音由 pypinyin 按词转写，`重庆` 读作 chongqing；需 `~/influ/.venv/bin/pip install pypinyin`，缺了它只按字面找）；也可像搜索引擎那样写：几个词须同时命中，加引号的 `"学院"` 只按原文找，`-能源` 排除，`city:遂宁`（或 `in:`，写拼音亦可）只取该省、市、区县境内，`type:airport` 只取一类（place、region、campus、military、office、regulator、embassy、airport、station，写开头几个字母即可），如 `city:Suining "学院" -能源`；省、市、区县本身也能搜到，境界取自 `build_tiles.py admin` 生成的 `admin_areas.json`；`↑` / `↓` 或 `Tab` / `Shift+Tab` 选择（鼠标悬停时临时高亮所指一项，按键则从高亮的一项接着走），`Enter` 或点击结果定位并查询可达性，结果右侧是与地图标记同色的经纬度。`Esc` 关闭；长按退格连续删字，删空后停住，空框再按一次退格才关闭。搜索框开着时地图照常拖动、缩放、点选地点，键盘仍归搜索框。高亮的那一项（键盘所选或鼠标所指，只此一处）在地图上同时标出：地点套一个同色方框，区域与校园等面状地物铺上缓缓流动的同色斜纹（面太小时也套方框）；它不在视野里时地图不会自己挪过去。搜索覆盖整张本地地图，不受当前缩放或视野限制，最多取 200 项，一页 8 项，输入行右端是 `9-16/37` 这样的页码；`PgDn` / `PgUp` 或 `Ctrl+F` / `Ctrl+B` 翻页（如 less、vi），在框上滚轮或双指滑动也翻页，`Tab` 和方向键走到页末自动翻过去；只查站内已有数据，不请求外部搜索服务。搜索框不用字体文件，字是一个个点亮的：英文直接读 HamClock 编译所用的 Courier 点阵表（`hamclock/vendor/hamclock-font/`，原样），按它自己画字的办法点在 1600×960 的原生像素上，随时钟画面一起缩放，因此与 DX 集群栏的呼号是同一副字；中文取站机 X11 自带的 1988 年 ISAS 16 点阵宋体（Debian `xfonts-base` 的 `gb16st.pcf.gz`），照 UCDOS 的老办法右移一点再印一遍成粗体。网页只向站上取所用之字的点阵（`/map/dots`，一个汉字 34 字节），在画布上逐点画出。

地图上可摆 MIL-STD-2525 / APP-6 陆军标号（`hamclock/milsym.py`，按标准的 200×200 作图空间以几何画出，不用图片）：按住 `F1`–`F4`（友、敌、中立、不明）再按数字 `1`–`9`、`0`（步兵、装甲、机械化步兵、野战炮兵、侦察、工兵、防空、通信、卫生、陆航）拾取一种，`Enter` 或空格在鼠标所指处放下，可连放，长按也只放一个；单击已放的标号即拿起（略透明，随鼠标走），再单击放下，按住拖动也可直接挪；拿在手里时按 `Delete` 或退格扔掉，`Ctrl+Z` 撤销（扔掉的也能捡回），`Esc` 放回或收手。本机窗口与各网页共用一块板（`~/influ/model/symbols.json`），一处摆放，处处可见。

关闭：`systemctl --user disable --now hamclock-http.service`。也可仅监听本机：`~/influ/.venv/bin/python ~/influ/hamclock/hcweb.py`，默认地址为 `127.0.0.1:8075`。

### 展示网页（Cloudflare Pages）

`demo/` 是这块屏幕的展示版：不连站上，也不暴露本站的位置。每次打开网页，电台从几处山顶（`demo/sites.json`：峨眉山金顶、香港太平山顶、东京高尾山、苏黎世于特利山、旧金山湾暗黑山）中随机挑一处出发，与上次打开的不同，进来后就停在那里不再变；`?site=emei` 等可指定。HamClock 的各窗格由网页按 HamClock 自己的画法逐像素画出（`demo/public/tft.js`：它的 Courier 与 Germano 点阵字表、RGB565 配色、画线画圆的算法），时钟是该山顶当地时间；X 射线、太阳风、DST、Kp 取自 NOAA SWPC，天气取自 Open-Meteo，DX 集群取自 HamQTH；ADS-B 雷达屏照 `tools/hamclock-adsb.cpp` 画，航迹取自 adsb.lol / adsb.fi 的开放数据，军机（readsb 的 `dbFlags` 军用位）一概剔除；雷达屏的底图（公路、铁路、河流、水面、城镇、民用机场跑道）由网页直接读 OpenFreeMap 的 OSM 矢量瓦片，军用机场不取。地图照本机网页的样子：地形由浏览器从 Terrarium 高程瓦片按 `build_tiles.py` 的着色法在 Web Worker 里逐像素画出，金黄色是该山顶的视距可达区——每处山顶各自预先算好（`demo/tools/build_sites.py reach`，即 `build_rangemap.py` 的视距算法，天线立在 30 米高程里的真峰顶上，海面按海面算）；FT8/FT4 由 PSK Reporter 的 MQTT 经 WebSocket 直送网页，WSPR 取自 WSPR.live，APRS 由后端经 APRS-IS 收听；夜色与晨昏线、Maidenhead 网格、距离圈、点地查可达性（地形剖面、菲涅尔区、各频率各高度）、`S` / `/` 搜索（汉字拼音互通，引号、`-`、`type:`，`city:` 取该城 25 千米内）、键盘、手柄与中键触屏模式都与本机网页一致。城市、河流和民用机场取自 Natural Earth；军事区、军用机场与其跑道一概不收。

后端只有两个 Pages Functions：`/api/adsb`（开放 ADS-B 接口不发 CORS 头，代取并缓存 5 秒）和 `/api/aprs`（APRS-IS 是 TCP 流，后端以只读身份收听几秒、在边缘缓存里攒一小时）。其余都由浏览器直取公开来源。

试用站 <https://influ-demo.pages.dev> 由 `cd demo && sh build.sh && npx wrangler pages deploy dist --project-name influ-demo` 直接上传。也可在 Cloudflare 控制台 Workers & Pages 创建 Pages 项目并连接本 GitHub 仓库，根目录填 `demo`，构建命令 `sh build.sh`，输出目录 `dist`；`demo/functions/` 自动成为后端。开放 ADS-B 接口对 Cloudflare 的共享出口限流很紧，雷达屏的航迹可能时有时无。本地预览：`cd demo && sh build.sh && npx wrangler pages dev dist`。后端的单元测试：`cd demo && node --test`。山顶数据重建：`python demo/tools/build_sites.py reach map`，然后在装有 pypinyin、`xfonts-base` 和 HamClock 源码的机器上 `build_sites.py spell glyphs --hamclock-src HamClock源码目录`。

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

本项目的原创代码采用 [GNU Affero General Public License v3.0](LICENSE)（AGPL-3.0-only）。第三方代码与数据遵循各自的许可证。

- 地图数据 © [OpenStreetMap](https://www.openstreetmap.org/copyright) 贡献者，ODbL，经 Overpass API 获取。
- OSM 里没有的无线电管理机构，地址取自各级政府网站和政务服务网，出处逐条写在 `hamclock/radio_regulators.json`。
- 政府机关：`hamclock/government.json`，取自 OSM 与高德、百度、腾讯地图的 POI 和地理编码，已换算为 WGS-84；监管场所只有 OSM 标注的那些（地图商的检索不返回）。
- 使领馆：四川省、重庆市外事办公室公布的名单，出处逐条写在 `hamclock/embassies.json`。
- 高程：AWS Terrain Tiles（Terrarium 格式），来源见 [Tilezen 的说明](https://github.com/tilezen/joerd/blob/master/docs/attribution.md)。
- [HamClock](https://www.clearskyinstitute.com/ham/HamClock/)（Clear Sky Institute，WB0OEW）。其天气（OpenWeatherMap）、DX 远征和比赛日历数据经 HamClock 的服务获取；本站补丁去掉了屏幕上的来源字样，数据与致谢归原提供者。
- 业余活动：[PSK Reporter](https://pskreporter.info/)、APRS-IS、[WSPR.live](https://wspr.live/)。
- 参考航迹：[adsb.lol](https://www.adsb.lol/)（ODbL）、[adsb.fi](https://adsb.fi/)。
- 天气：[Open-Meteo](https://open-meteo.com/)（CC BY 4.0）。
- 展示网页另用：[Natural Earth](https://www.naturalearthdata.com/)（公有领域）的城市、河流和机场，[NOAA SWPC](https://www.swpc.noaa.gov/) 的空间天气，[HamQTH](https://www.hamqth.com/) 的 DX 集群，NASA [SDO](https://sdo.gsfc.nasa.gov/) 的太阳图像，[OpenFreeMap](https://openfreemap.org/) 的 OSM 矢量瓦片（地图数据 © OpenStreetMap 贡献者，ODbL）。
