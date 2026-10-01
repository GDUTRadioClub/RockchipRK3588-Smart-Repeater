# ELF2 智能中继网页控制中心

运行在 ELF2/RK3588 上，代码目录：`/www`，Flask + SQLite + 原生 JS。

## 1. 当前功能

- 登录 / 退出；初始管理员由部署初始化生成，登录页不显示任何默认凭据，首次登录后请立即修改密码
- 用户管理：添加用户、删除用户、重置密码、修改本人密码
- 总览：
  - 电池电压（SARADC_VIN4）
  - 光伏电压（SARADC_VIN6）
  - CPU / 内存 / 温度 / Load / 运行时间
- 电压校准：每个通道支持零点 `zero_raw`（ADC 原始值）与倍率 `multiplier`（**V/引脚电压**）
  - **换算链路**：`引脚电压 = (raw − 零点) × 0.43945 mV`（RK3588 SARADC 12bit，0~1.8V，LSB=1.8/4096）；`实际电压 = 引脚电压 × 倍率`
  - **倍率 = 1/分压比 = (R上 + R下)/R下**（与 ADC LSB 无关，便于按分压板直接填写）
  - CH1 电池 `R14 100Ω + R15 91kΩ / R16 10kΩ`：分压比 `10k/101.1k = 0.098912` → 倍率 **10.11**，满量程 **18.198V**，分辨率 **4.443 mV/LSB**
  - CH2 光伏 `100Ω + 160kΩ / 10kΩ`：分压比 `10k/170.1k = 0.058789` → 倍率 **17.01**，满量程 **30.618V**，分辨率 **7.475 mV/LSB**
  - 采集通道可配置（设置/校准页或 `/api/voltage/calibrate` 的 `<key>_adc_channel`，0~7）：
    默认 电池 = `VIN4`（`in_voltage4_raw`，P1_36）、光伏 = `VIN6`（`in_voltage6_raw`）；
    若按分压板设计把 CH2 接到 P1_38，则把光伏通道改成 **VIN5** 即可（无需改代码）
  - 口径迁移：首次启动新版会自动把旧的「V/raw」倍率重置为设计值（`adc_mult_unit=vin` 标记），之后不再覆盖用户校准值；界面提供「填入设计值」按钮
- 系统全局音量：控制 NAU88C22 Headphone / Speaker 输出，支持 0~100% 调节与全局静音
- LLM 对话：
  - 本地模型：`http://127.0.0.1:8001/v1`，OpenAI 兼容
  - 外部 API：任意 OpenAI 兼容 Base URL / Key / Model
  - 支持流式 SSE 输出
- 网页对讲：
  - 浏览器麦克风录音（需要安全上下文/HTTPS/localhost；局域网 HTTP 可能被浏览器限制）
  - WAV 文件上传测试
  - 录音分段保存到 `/www/recordings/`
  - 自动播放到 `plughw:CARD=rockchipnau8822,DEV=0`（3.5mm AUX）
  - 播放测试音按钮
  - 录音记录列表与重放
- 开发板 3.5mm 耳机输入监听测试：
  - 服务端 `arecord` 采集 NAU88C22 输入
  - 输入源选择：板载 MIC / 3.5mm 耳机 MIC
  - 监听声道选择：左（3.5mm）/ 右（板载）/ 混合
  - 采集增益：PGA、ADC、+20dB Boost、L2/R2、Aux Boost
  - 实时电平/RMS/Peak/dBFS 显示
  - 实时 PCM 流到网页客户端并播放
  - 支持开始/停止采集、开始/停止网页监听
- TTS 语音朗读：
  - 本地 Piper：默认音色 `zh_CN-huayan-medium`
  - 外部 API：支持多个 OpenAI 兼容 `/v1/audio/speech`
  - 支持 LLM 流式增量朗读：按句/段边生成边合成边播放
  - LLM 回复后自动朗读到 3.5mm AUX，**朗读全程 PTT 使能**（一条回复只发一次载波）
  - 英文音色 / ICAO 开关对普通朗读与流式朗读一致生效（`en_voice` / `icao` / `icao_voice`）
  - 流式会话空闲看门狗（默认 20 s，`RELAY_TTS_STREAM_IDLE` 可调）：
    无播放且无排队片段超过阈值 → 自动停会话并释放 PTT
  - 网页测试朗读、音色选择、音色包上传、训练数据上传
- PTT 控制：
  - GPIO3_A1（Linux 全局 GPIO 97）在 TTS/对讲音频播放期间自动拉高
  - 流式朗读：**整个回复期间保持 PTT**，播放队列排空 / `stream/stop` / 看门狗触发后释放
  - 音频结束后延迟约 0.8 s 拉低，桥接流式 TTS 分片间隙
  - 播放超时保护：aplay 按「音频时长 + 15 s」限时结束；`stream/stop` 后 2 s 未退出则 SIGKILL
  - 支持 `RELAY_PTT_GPIO`、`RELAY_PTT_ACTIVE_HIGH` 环境变量调整
  - 状态查看：`GET /api/ptt/status`
- 摄像头（UVC / V4L2）：
  - 实时 MJPEG 预览
  - 循环录像（可设置分段时长、循环容量上限、存储容量上限、最大文件数）
  - 单独录像并保存
  - 摄像头页顶部提供“录像回放 / 分片管理”快速入口，并保留紧凑的录像控制条
  - 现代化录像管理：Hikvision 风格 24 小时时间轴、拖动定位回放、分片续播、
    倍速、上一分片/下一分片、当前分片进度拖动、截图与下载
  - 分片管理表：类型筛选、文件名搜索、排序、分页、批量下载、批量删除、CSV 清单导出
  - 存储图形化：磁盘使用率、循环容量上限、存储容量上限三个环形仪表 + 占用进度条
  - 自动清理策略：最旧 `loop_*.mp4` → 最旧 `snapshot_*.jpg`，保护 `manual_*.mp4`
  - OSD 文字与时间叠加（录像/推流烧录，预览客户端叠加）
  - RTMP 推流
- 气象（RS485 / Modbus RTU）：
  - 实时风力大小显示（m/s，自动轮询刷新）
  - 原始寄存器值、最近更新时间与采集状态显示
  - 数据记录：按日期查询、按日保存 CSV
  - 当日时间轴曲线，可切换 1/5/10/30/60 分钟采样间隔
  - 当日最大风力、最大时间、平均、最小、数据点数
  - 历史数据在线查询与 CSV 导出（可选时间长度 1/3/7/30/90 天，采样频率 1/5/10/30/60 分钟）
  - 轮询间隔、串口、从站、功能码、寄存器、倍率设置（位于“设置 / 校准”页）
  - 翻斗式雨量计共用同一 RS485 总线，默认从站 23、寄存器 0x0000、倍率 0.1 mm/raw
  - 实时降水量、今日累计、最近 1 小时降水、采样点数显示
  - 每小时降水量统计页面：按日期查看 24 小时柱状图与表格，支持导出小时 CSV
  - 降水量按日保存为 `rain_YYYY-MM-DD.csv`，并同步 SQLite `rain_readings`
  - 顶栏显示北京时间日期与时间

## 2. 部署

```bash
sudo mkdir -p /www
sudo chown -R elf:elf /www
# 将文件复制到 /www
sudo cp /www/relay-web.service /etc/systemd/system/
# PTT GPIO3_A1 初始化：安装 root 脚本 + oneshot 服务
sudo cp /www/ptt_gpio_prepare.sh /usr/local/sbin/elf2-ptt-gpio.sh
sudo chmod +x /usr/local/sbin/elf2-ptt-gpio.sh
sudo cp /www/elf2-ptt-gpio.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable elf2-ptt-gpio
sudo systemctl start elf2-ptt-gpio
sudo systemctl enable relay-web
sudo systemctl restart relay-web
```

访问：

```text
http://192.168.101.215:8080
```

初始管理员凭据由部署方在首次部署时配置，登录页不显示默认账号或密码；首次登录后请立即修改密码。

## 3. 目录结构

```text
/www
├── app.py                  # Flask 后端
├── relay.db                # SQLite 数据库（首次启动生成）
├── recordings/             # 网页对讲录音 WAV 分段
├── instance/secret.key     # Flask 会话签名密钥
├── templates/
│   ├── base.html
│   ├── login.html
│   └── dashboard.html
├── static/
│   ├── css/app.css
│   └── js/app.js
└── relay-web.service
```

## 4. 安全说明

- 密码使用 Werkzeug PBKDF2 哈希，不保存明文。
- Session 使用 Flask 签名 Cookie；密钥保存在 `instance/secret.key`（0600）。
- 所有写操作要求 `X-CSRF-Token`。
- 用户管理、设置、校准接口仅管理员可用。
- SQLite 开启 WAL + NORMAL 同步，适合单板低开销运行。
- 当前 Flask 内置开发服务器适合内网联调；公网暴露前建议使用 gunicorn/waitress + Nginx/Caddy TLS + API 网关。

## 5. 网页对讲说明

- 浏览器麦克风采集使用 `getUserMedia`，Chrome 要求安全上下文：
  - `https://...` 或
  - `http://localhost` / `http://127.0.0.1`
- 如果通过 `http://192.168.101.215:8080` 访问，浏览器可能不提供 `navigator.mediaDevices`，此时：
  - 使用“上传 WAV 测试”验证 AUX 输出；
  - 或后续配置 HTTPS/FRP 反向代理；
  - 或在 Chrome 启动参数中把该地址加入 `--unsafely-treat-insecure-origin-as-secure`。
- 录音文件保存为 WAV，播放设备为 `plughw:CARD=rockchipnau8822,DEV=0`。

## 6. 常用 API

| API | 方法 | 说明 |
|---|---|---|
| `/api/status` | GET | 电压、CPU、内存、温度 |
| `/api/voltage/calibrate` | POST | 保存电压零点/倍率（倍率单位 V/引脚电压，0~200） |
| `/api/audio/volume` | GET/POST | 系统全局音量与静音控制 |
| `/api/chat` | POST | LLM 对话（本地/外部，支持 stream） |
| `/api/chat/providers` | GET | 当前提供商配置 |
| `/api/tts/providers` | GET | 本地 Piper 音色列表与当前配置 |
| `/api/tts/voices` | GET | 列出 /opt/ai/voices 下的音色包 |
| `/api/tts/voice/<id>` | DELETE | 删除音色包（管理员 + CSRF，内置 huayan 受保护） |
| `/api/tts/speak` | POST | 合成并播放到 AUX；支持 `voice`/`en_voice`/`icao`（中英混读 + ICAO） |
| `/api/tts/stream/start` | POST | 启动流式朗读会话 |
| `/api/tts/stream/chunk` | POST | 提交流式文本片段（增量合成播放） |
| `/api/tts/stream/end` | POST | 结束流式朗读会话 |
| `/api/tts/stream/stop` | POST | 停止流式朗读（播放中立即停播并释放 PTT） |
| `/api/tts/stream/status` | GET | 流式会话状态（排队片段、PTT 占用、`last_error`、空闲秒数） |
| `/api/ptt/diag` | GET | PTT 全链路自检信息（软件状态 + sysfs 实测 value/direction） |
| `/api/ptt/manual` | POST | 手动发射（设置/校准页「按住发射」）`{"hold":true|false}`，管理员 |
| `/api/llm/stats` | GET | LLM 生成速率统计（最近 N 次 + 今日汇总）；`DELETE` 清空（管理员） |
| `/api/agent/tools` | GET | 技能/工具清单、启用状态、提示词实时变量 |
| `/api/agent/chat` | POST | 类 Agent 对话（SSE：`iter/delta/tool_start/tool_result/usage/usage_total`） |
| `/api/intercom/upload` | POST | 上传 WAV 并**发射**（默认拉 PTT；`dry=1` 只送 AUX 不发射） |
| `/api/intercom/recordings` | GET | 录音记录 |
| `/api/intercom/play/<id>` | POST | 重放录音并**发射**（`dry=1` 只本地放音） |
| `/api/intercom/test-tone` | POST | 播放测试音并**发射**（`dry=1` 只本地放音） |
| `/api/mic/settings` | GET/POST | 麦克风输入源、监听声道、PGA/ADC/Boost 增益 |
| `/api/mic/capture/start` | POST | 开始采集开发板 3.5mm 麦克风输入 |
| `/api/mic/capture/stop` | POST | 停止采集 |
| `/api/mic/level` | GET | 实时电平 / RMS / Peak / dBFS / L-RMS / R-RMS |
| `/api/mic/stream` | GET | 实时 PCM（audio/L16, 16kHz, mono）流 |
| `/api/users` | GET/POST | 用户列表/添加 |
| `/api/users/<id>` | DELETE | 删除用户 |
| `/api/users/<id>/password` | POST | 重置密码 |
| `/api/settings` | GET/POST | LLM/页面设置 |
| `/api/camera/status` | GET | 摄像头状态、设置、录像列表、存储图形数据（含 `loop_running` / `loop_autostart` / `loop_manual_stop`） |
| `/api/camera/stream` | GET | MJPEG 实时预览 |
| `/api/camera/settings` | GET/POST | 摄像头 / OSD / 录像 / RTMP 设置（含 `storage_max_mb`） |
| `/api/camera/snapshot` | POST | 抓拍一张 JPEG |
| `/api/camera/record/manual/start` | POST | 开始单独录像 |
| `/api/camera/record/manual/stop` | POST | 停止并保存单独录像 |
| `/api/camera/loop/start` | POST | 开始循环录像（同时清除「手动停止」标记，恢复自愈） |
| `/api/camera/loop/stop` | POST | 停止循环录像（本次运行内不再自动拉起，重启恢复自动） |
| `/api/camera/rtmp/start` | POST | 开始 RTMP 推流 |
| `/api/camera/rtmp/stop` | POST | 停止 RTMP 推流 |
| `/api/camera/recordings` | GET | 录像分片列表（含时长/起止时间），支持 `limit` |
| `/api/camera/segments` | GET | 分片管理：`date/type/q/sort/page/page_size` 分页查询 |
| `/api/camera/timeline` | GET | 按日期返回时间轴分片与统计 |
| `/api/camera/storage` | GET | 磁盘/循环容量/存储上限占用数据 |
| `/api/camera/recordings/cleanup` | POST | 按策略立即清理最旧循环分片/快照 |
| `/api/camera/recordings/batch_delete` | POST | 批量删除分片（JSON: `{"files":[...]}`） |
| `/api/camera/recordings/<file>` | GET/DELETE | 播放（`?download=1` 下载）/ 删除录像文件 |
| `/api/weather/realtime` | GET | 实时风力、原始值、采集状态 |
| `/api/weather/history?date=YYYY-MM-DD` | GET | 指定日期原始/按分钟聚合数据 |
| `/api/weather/history_range?days=N&interval=N` | GET | 最近 N 天按采样频率聚合数据 |
| `/api/weather/stats?date=YYYY-MM-DD` | GET | 当日最大/最小/平均/计数 |
| `/api/weather/export.csv?days=N&interval=N` | GET | 导出历史聚合数据 CSV |
| `/api/weather/settings` | GET/POST | 风力/降水量/温湿度 Modbus/串口/轮询设置 |
| `/api/weather/th` | GET | **温湿度实时值 + 当日统计**（从站 03，功能码 04） |
| `/api/weather/th/history` | GET | 温湿度历史点，按日期 |
| `/api/weather/th/read` | POST | 立即读取一次温湿度（调试用，未接线返回 Modbus 超时） |
| `/api/busy/status` | GET | **BUSY 接收状态**：原始电平、触发态/时长、电平沿计数、自激标志 |
| `/api/busy/diag` | GET | BUSY 链路自检（原始电平 / 事件 / 排查提示） |
| `/api/busy/polarity` | POST | 设置 BUSY 有效极性 `{"active_low":true|false}`，立即生效，管理员 |
| `/api/rain/realtime` | GET | 实时降水量、今日累计、最近 1 小时 |
| `/api/rain/hourly?date=YYYY-MM-DD` | GET | 指定日期 24 小时降水量 |
| `/api/rain/history?date=YYYY-MM-DD` | GET | 指定日期累计降水量原始记录 |
| `/api/rain/export.csv?date=YYYY-MM-DD` | GET | 导出小时降水量 CSV |
| `/api/weather` | GET | 兼容旧接口 |
| `/api/ptt/status` | GET | 查看 GPIO3_A1 PTT 电平、引用计数、错误 |

---

## 2026-09-15 更新（可信端口 / 本地 TTS / 实时对讲 / 上传修复）

### 1. 访问地址（nginx 反向代理，Flask 仍在 127.0.0.1:8080）
| 入口 | 说明 |
|---|---|
| `http://<板卡IP>/` | 301 跳转到 HTTPS（另有 `/healthz` 探活） |
| `https://<板卡IP>/` | **推荐入口**：自签证书，浏览器首次需“继续前往” |
| `https://elf2-desktop/` / `https://elf2.local/` | 同上（证书 SAN 已包含这两个域名） |
| `http://<板卡IP>:8080/` | 直连 Flask 兜底入口；**HTTP 下浏览器不给麦克风权限** |

- 证书：`/www/certs/elf2.{crt,key}`（自签，10 年，SAN 含板卡 IP 与主机名）
- 配置：`/etc/nginx/sites-available/relay`（仓库内 `relay-nginx.conf`）
  - `client_max_body_size 512m`、`proxy_buffering off`（摄像头/麦克风流不缓冲）、
    超时 3600s、WebSocket/HTTP2 头；已 `systemctl enable nginx`
- **为什么要 HTTPS**：`getUserMedia`（网页麦克风）只在安全上下文可用，HTTP 局域网地址会被浏览器禁用。

### 2. TTS：仅本地 Piper（外部 OpenAI 兼容 TTS 已下线）
- `POST /api/tts/speak` 只使用板端 `/opt/ai/piper` + `/opt/ai/voices/<voice>`，
  请求里带 `provider`/`model` 也会被忽略；
- `/api/tts/providers` 只返回本地音色；设置页不再有“外部 TTS API”表单；
- 语音对话页新增 **「朗读到网页」** 按钮与 `<audio>` 播放器（`/recordings/<file>`）；
  「测试朗读（板端 AUX）」仍走 3.5mm 输出 + 自动 PTT。

### 3. 实时对讲（网页麦克风 → 板端 AUX + 自动 PTT）
- 新端点：`POST /api/intercom/push?token=<id>[&end=1]`、`GET /api/intercom/push/status`、
  `POST /api/intercom/push/stop`
- 前端「按住说话」：16kHz 单声道 PCM，每 ~250ms 一块；首块即拉起 PTT，
  松开/指针离开/手动停止时发送 `end=1`；**5 秒无数据看门狗自动停流并释放 PTT**（上限 120s）
- 板端以 `aplay -D plughw:CARD=rockchipnau8822,DEV=0 -t raw -f S16_LE -r 16000 -c 1` 持续播放
- 实测：`push→200 / PTT high=true / end→200 / PTT high=false` ✓

### 4. 上传修复（原“Fail to Fetch”根因）
- **根因**：`app.config MAX_CONTENT_LENGTH = 32MB`（录音上限）套用到全局，
  56MB 的 Rosmontis 音色包 → Werkzeug 在解析 multipart 时中断连接 → 浏览器报 `Failed to fetch`。
- 现改为：`MAX_UPLOAD_BYTES = 512MB`（可用环境变量 `RELAY_MAX_UPLOAD_MB` 调整），
  录音仍单独限制 32MB；并新增 **413 JSON 处理器**，不再只给“连接被重置”。
- 上传改为 **XHR + 进度条**（音色包/训练集各一条），失败时给出明确原因。
- **CSRF 兼容性修复**：`_csrf_protect()` 原先对非 JSON 请求体直接访问 `request.json`
  会抛 400，导致所有 multipart / octet-stream 的 POST（含音色包上传、实时对讲 PCM）
  在无 `X-CSRF-Token` 头时异常；现改为 `_csrf_from_request()`（先看头部，再看 JSON/form，且不抛异常），
  前端 XHR/fetch 均带 `X-CSRF-Token`。

### 5. 音色包兼容性（板端 piper 为 2023 版 C++）
- 板端 piper 要求 `phoneme_id_map` 的键必须是**单个 Unicode 码点**；
  新版 piper 训练出的配置常含 `aɪ / aʊ / eɪ / oʊ / ɔɪ` 等多码点键 →
  piper 直接崩溃：`"aɪ" is not a single codepoint`。
- 现 `tts_service.save_voice_zip()` 会在上传时**自动剔除多码点键**（原文件备份为
  `model.onnx.json.bak_multicodepoint`），返回值里给出 `phoneme_fix` 列表。
- 已部署音色：`/opt/ai/voices/Rosmontis_v2`（本地导出，修复后 RTF≈0.136）；
  试听样本见 `音频训练/输出/板端实测样本/`（`ros_*.wav` 与 huayan 对照）。

### 6. 英文音色 Rosmontis_en 与中英混读（2026-09-15）
- 训练数据：PRTS wiki「迷迭香/语音记录」英文台词（38 条）配
  `PseudoMon/arknights-audio` 英文配音（`voice_en/char_391_rosmon/`，35 条 / 578 s，
  22.05 kHz 单声道）；以官方 `en_US-lessac-medium` 为基座微调 290 epoch。
- 导出后剔除 5 个多码点键（`aɪ / aʊ / ɔɪ / eɪ / oʊ`）→ 161 键，板端 piper 加载正常，
  实测 RTF≈0.12（推理 0.43 s / 音频 3.44 s）。
- 音色包：`音频训练/输出/Rosmontis_en_voice_pack.zip`，板端路径 `/opt/ai/voices/Rosmontis_en/`。
- 中英混读：`tts_service.split_by_language()` 按语言分段 → 中文段用 `tts_local_voice`、
  英文段用 `tts_en_voice`（留空自动挑 `language` 以 `en` 开头的音色）→
  `concat_wavs()` 段间插 120 ms 静音后拼接。

### 7. ICAO 字母解释法朗读
- `POST /api/tts/speak` 新增 `en_voice`、`icao` 参数；设置页新增「英文片段音色」
  「ICAO 字母解释法」两项（设置键 `tts_en_voice` / `tts_icao`，默认开启）。
- `expand_icao()` 展开呼号与单字母串：
  `BG7XYZ → Bravo Golf Seven X-ray Yankee Zulu`、`ELF2 → Echo Lima Foxtrot Two`、
  `W A R → Whiskey Alpha Romeo`、`N23E113 → November Two Three Echo One One Three`；
  `aviation_digits=True` 时数字用航空读法（Two Tree / Fower）。
- 板端实测分段：`呼号 BG7XYZ，这里是 ELF2 中继站。` →
  `呼号 Bravo Golf Seven X-ray Yankee Zulu，这里是 Echo Lima Foxtrot Two 中继站。`
- 音色包管理：设置页新增「音色包管理」表格，除内置 `zh_CN-huayan-medium` 外均可删除。
- 试听样本：`音频训练/输出/板端实测样本/` 下
  `en_icao.wav`、`en_call.wav`、`en_radio_check.wav`、`zh_call_icao_on.wav`、`bilingual_mixed.wav`。

### 8. 其它
- `ProxyFix` 已启用（`X-Forwarded-Proto/For/Host`），日志里能看到真实客户端 IP；
- 静态资源版本号已改为 `20260915c`，`base.html` 中同时给 `app.css`/`app.js` 带版本参数，
  避免浏览器缓存旧界面。

## 2026-09-16 修复：LLM 流式朗读（自动朗读到 3.5mm AUX + PTT 使能）

**现象**：LLM 回复后勾选「自动朗读」，普通（非流式）朗读正常，
但流式朗读**完全无声**：3.5mm AUX 无输出、PTT 也一直不抬起。

**根因**：`_tts_stream_synth_worker()` 运行在后台线程里，却用 `get_setting()` / `bool_setting()`
读取英文音色、ICAO 开关 —— 这两个函数走 `flask.g`（`get_db()`），
线程内没有应用上下文 → 抛 `RuntimeError: Working outside of application context`；
异常被 `except Exception as e: sess['last_error'] = str(e)` 吞掉，
于是每个分片都合成失败、既没有音频也没有 PTT 动作（日志里看不到任何报错）。

**修复**：
1. `get_setting()` 在无应用上下文时回退到直连数据库（`_setting_direct`），后台线程不再崩；
2. 合成线程显式使用 `_setting_direct`，失败时打印 `[TTS-STREAM] 合成失败：…` 并写入 `last_error`，
   `POST /api/tts/stream/end` 会返回 `last_error`，前端弹错误提示（不再静默）；
3. 播放线程改为**会话级 PTT**：第一段音频开始时 `_ptt_retain()`，整条回复结束（队列排空）才释放，
   分片间隙不再松开 PTT（原来只靠 0.8 s 延时桥接，长句合成间隔会掉发）；
4. 新增空闲看门狗（`RELAY_TTS_STREAM_IDLE`，默认 20 s）：正在播放或仍有排队片段时不判空闲，
   真空闲超时才停会话并释放 PTT，避免异常退出后一直占用信道；
5. 播放限时 + `stop` 硬杀（2 s 未退出 SIGKILL），aplay 卡死不会把 PTT 卡住；
6. 前端聊天页提交分片时带上 `en_voice` / `icao` / `icao_voice`，与 `speakText()` 行为一致。

**验证**（板端 `192.168.101.215`，轮询 `/sys/class/gpio/gpio97/value`）：
- 两段流式：`GPIO97 0→1 @2.5 s`，跨分片间隙保持高，最后一段播完 +0.8 s 拉低；
- 单段 101 字（≈21 s 音频）：连续发射 21.5 s，看门狗未误停，播完自动释放；
- 播放中 `POST /api/tts/stream/stop`：1.07 s 内 aplay 消失、PTT 归零、会话移除；
- 端到端（本地 RKLLM `qwen2.5-1.5b` 流式回复 → 分句 → 流式 TTS）：LLM 4.7 s 出完，
  PTT `@8.2 s` 抬起，连续压发 22.5 s 后自动释放，`last_error` 为空。

**排查命令**：
```bash
journalctl -u relay-web -f | grep TTS-STREAM          # 合成失败/超时/看门狗日志
curl -s localhost:8080/api/tts/stream/status | python3 -m json.tool
cat /sys/class/gpio/gpio97/value                      # PTT 实时电平
```

### 2026-09-16 补充：播放错误 / PTT 极短时间内反复触发

**现象**：自动朗读过程中出现播放错误，PTT 在极短时间内被反复触发（继电器连续 key）。

**根因 1（主因）**：流式朗读自己启的 `aplay` 没有登记到 `CURRENT_PLAY_PROC`，
而「测试朗读（板端 AUX）」/ 录音回放走 `_play_file_locked()`。两条路径的播放窗口一旦重叠，
第二路 `aplay` 会因声卡被占用**直接失败**（听感即「播放错误」/断音）；
失败进程瞬间退出，下一片段又立刻重新 raise/release，PTT 就被短时间反复触发。

**根因 2**：eth0 物理层抖动。内核日志 `rk_gmac-dwmac eth0: Link is Down/Up`
（20:21、20:45 各一次），抖动期间浏览器 `POST /api/tts/stream/chunk` 失败，
而前端原来「失败即静默丢弃（只 console.warn）」→ 丢字、断句。

**修复**：
1. 流式分片播放改为**与「测试朗读（板端 AUX）」完全同一条发射路径**：
   `_ptt_retain()` → `_play_file_locked()`（同一把 `PLAY_LOCK`、同一 `CURRENT_PLAY_PROC` 抢占）
   → 播完 `_ptt_release()`；唯一区别是 PTT 连续性（整条回复由会话级 PTT 保持，分片之间不松手）。
2. 新增 `_stop_proc()`：TERM → 1 s → KILL；任何 `aplay` 卡死都不会长期占住声卡与 PTT。
3. 新增 `PTT_MIN_HOLD`（默认 1.0 s，`RELAY_PTT_MIN_HOLD=0` 可关闭）：最短压发时间，
   从机制上抑制毫秒级/亚秒级反复 key。
4. 流式会话按 `client_id`（浏览器 `sessionStorage` 里的标签页 id）隔离：
   别的标签页/其它设备开始朗读不再掐断当前回复；`stream/stop` 不带 `client_id` 仍为**全停**（应急）。
5. 前端片段投递加退避重试（4 次）+ 会话被回收时自动重建会话并重投，
   链路抖动不再丢字。

**实测**（headless Chrome 驱动真实前端，采样 `/sys/class/gpio/gpio97/value`）：
- 板端 `ps` 采样并发 `aplay`：`max_concurrent_aplay = 1`，时间线 `[[0.0,0],[8.8,1],[31.0,0]]`；
- 自动朗读播放中连点两次「测试朗读（板端 AUX）」：PTT `4.29 s ↑ → 27.48 s ↓`，全程只抬一次；
- 单句回复自动朗读：PTT `4.15 s ↑ → 13.72 s ↓`，状态「已完成」，无 4xx；
- 会话隔离：两个 `client_id` 并存 2 个会话，A 重启只替换 A，跨客户端投片返回 409。

**备注**：eth0 已按需求强制 100M/Full 且关闭自协商（`ethtool eth0`：`Speed 100Mb/s, Duplex Full, Auto-negotiation off`），
但 20:21 / 20:45 仍出现两次物理层 Link Down/Up —— 建议检查网线与交换机端口，
这类抖动同样会表现为「播放中断」。

### 2026-09-16 新增：设置/校准 → 「PTT 发射自检（硬件排查 · 不接音频）」

页面位置：**设置 / 校准** 标签页 → 电压校准卡片下方的 **PTT 发射自检** 卡片。

- **按住发射（PTT 高）**：按住期间 `GPIO3_A1`（`gpiochip3 line1` → Linux 全局 **GPIO 97**）输出高电平，
  经隔离/驱动送控制板 PTT；**松开即松开 PTT**。
- **强制松开**：一键释放。
- 安全机制：前端每 1 s 续一次心跳，**3 s 无心跳自动松开**（页面关掉/断网不会一直压着信道），
  单次最长 **30 s**（`RELAY_PTT_HEARTBEAT` / `RELAY_PTT_MANUAL_MAX` 可调）。
- 实时诊断行：软件状态（高/低 + 引用计数）、**引脚实测 `value`/`direction`**、GPIO 号与极性、
  手动发射状态、sysfs 节点、写入错误。
- **PTT 事件追踪**（卡片下方直接显示最近 8 条）：每次拉高/拉低都记 `时间 + ↑/↓ + 动作 + 原因 + 调用者函数:行号 + hold 计数`，
  可一眼区分「软件反复拉低」还是「硬件电平抖动」；`GET /api/ptt/diag` 的 `events` 字段是同一份数据（保留最近 60 条）。
- **抗误停**：若浏览器发起 `pointercancel`/`lostpointercapture`（指针被系统抢走）而指针其实仍按着，
  前端 **200ms 内自动恢复发射**，避免「按住却间歇性发射」；`pointerup`/`mouseup`/窗口失焦/强制松开才真正停发。
- 心跳超时默认 **8s**（`RELAY_PTT_HEARTBEAT`），前端每 **0.7s** 续一次心跳 —— 网络抖动不再中途松 PTT。
- 板端接口：`POST /api/ptt/manual {"hold":true|false}`（管理员）、`GET /api/ptt/diag`（登录即可）。
- 日志：journal 打印 `[PTT] 手动发射开始（GPIO 97 拉高）` / `结束（user-release|heartbeat-timeout|max-hold）`。

**硬件排查顺序**（配合万用表，逐级定位断点）：

| 步骤 | 测点 | 正常表现 | 不正常说明 |
|---|---|---|---|
| ① | 本页「引脚实测」 | 按住时 `value = 1` | 软件/GPIO 层问题：`cat /sys/class/gpio/gpio97/value`，查 GPIO 号、占用、direction |
| ② | RK3588 → 隔离板：P26 pin1 对 GND | 按住 0 V ↔ **3.3 V** | 排针/排线/焊点/串阻问题 |
| ③ | 隔离板输出侧 → 控制板 PTT 输入 | 按住出现**有效**电平 | 光耦/驱动管、限流电阻、VDD2 供电、两侧是否共地 |
| ④ | **有效极性** | 与控制板要求一致 | ⚠️ GM3188 的 PTT 为**低有效**（拉 GND 发射）；若控制板要低有效而输出是高有效，需在隔离板上反相 |
| ⑤ | 导线短接控制板 PTT 端子模拟发射 | 电台发射 | 不发射 → 控制板/电台侧问题（控制板供电、PTT 线序、电台 PTT 定义） |

**实测**（headless Chrome 驱动真实前端）：初始 `value = 0 / direction = out`；
按住 3 s → `high = true, value = 1, hold_count = 1`；松开 → `value = 0`；
不发心跳时 1.5 s 仍为高、5.5 s 已被看门狗强制松开（journal `手动发射心跳超时`）。

### 2026-09-16 补充：网页实时对讲「PTT 反复触发、说不了话」

**现象**：网页对讲里按住「按住说话」，PTT 被反复触发（继电器连响），无法正常讲一句话；
实测日志看到每次按住只有 5~10 个 250ms PCM 块就被中断，用户反复重试 4 次。

**根因 1（前端，主因）**：`#btn-push-talk` 同时挂了
`pointerdown → startPushTalk()` 和 **`pointerleave → stopPushTalk('指针离开')`**。
按住时鼠标只要抖一下、或移出按钮一点点，就立刻停发并松开 PTT；用户再按又拉高 ——
听感/观感就是「PTT 反复触发、说不了话」。

**根因 2（板端）**：`/api/intercom/push` 的重启分支是
`_intercom_push_stop()`（松 PTT）+ `_ptt_retain()`（拉 PTT）。
aplay 若中途意外退出（设备被抢占/出错），每来一块数据就重启一次；
虽然 0.8 s 释放延时通常能掩盖，但重启耗时一旦超过 0.8 s，继电器就会真的松-合一次，
连续重启即表现为反复触发。

**根因 3**：「本机监听」默认勾选，扬声器回放麦克风极易啸叫回授，听感也是「说不了话」。

**修复**：
1. 前端改为 `setPointerCapture()` 锁指针：按住期间指针事件不再乱跑；
   去掉 `pointerleave`，只在 `pointerup` / `pointercancel` / `lostpointercapture` 停止；
   按钮加 `touch-action:none; user-select:none`。
2. 板端 aplay 意外退出时**只换播放进程、不松 PTT**（`_intercom_push_stop(release_ptt=False)`），
   同一次发言里 PTT 连续保持；并统计 `restarts`（`/api/intercom/push/status` 可见，
   journal 打印 `[INTERCOM] aplay 意外退出，第 N 次重启（PTT 保持不松）`）；
   写入失败也不再中断会话，只丢这一块并重启播放。
3. 「本机监听」默认关闭（需要时用户自行勾选）。

**实测**（headless Chrome + 假麦克风驱动真实前端；板端 `pkill aplay` 模拟设备被抢占）：
- 按住 14 s：PTT `1.12 s ↑ → 15.79 s ↓`，**全程只抬一次**，状态一直「对讲中…」；
- 中途杀掉 aplay：`/api/intercom/push/status` 返回 `restarts=1`、`ptt_high=true`，
  日志 `[INTERCOM] aplay 意外退出，第 1 次重启（PTT 保持不松）`，音频自动恢复；
- 松开后 PTT 归零、无残留 aplay。

## 2026-09-18 新增：端侧 LLM 提示词注入 · Agent 技能/工具 · 生成速率监测

### 1. 生成速率监测（实时 + 统计）

- LLM 页面速率条实时显示：**首字延迟（TTFT）· tok/s · 已生成 tokens**（每 350 ms 刷新）。
- 板端为每次生成记录一行 `llm_stats`（流式在转发 SSE 时顺带统计，非流式按总耗时折算），
  设置页「最近生成速率」表格 + 今日汇总（次数 / 平均与最快 tok/s / 平均首字 / 总 tokens）。
- token 估算：CJK/全角 1 token/字，其余约 4 字符/token（板端 RKLLM 的 `usage` 字段恒为 0，不能直接用）。
- 接口：`GET /api/llm/stats`，`DELETE /api/llm/stats`（管理员）。

### 2. 提示词注入

- 设置页可编辑系统提示词 + 开关 + 变量展开，变量：`{battery} {pv} {battery_raw} {pv_raw} {cpu_temp}
  {wind} {rain_today} {date} {time} {weekday} {site}`（页面实时显示当前取值）。
- **重要（2026-09-27 更正）**：板端 RKLLM 并非「忽略 `system` 角色」——真因是
  `rkllm-server` 的服务端包装只取**最后一条**消息，system 与多轮历史都被它丢掉了，
  而且每条请求还被硬塞一句写死的英文 system。`deploy/patch_rkllm_chat.py` 已修：
  支持真正的 system 槽位、完整多轮、规范 Qwen2.5 ChatML 模板。
  应用层因此对 `local` 也走标准 `system` 角色；若还在跑未打补丁的服务端，
  设 `RELAY_LOCAL_INLINE_SPEC=1` 可退回「并入用户消息」的旧行为。

### 3. Agent 技能 / 工具调用

- 8 个技能：`get_weather`（风速/气象）、`get_rain`（雨量）、`get_power`（电池/光伏电压）、
  `get_system`（CPU 温度/负载/内存/磁盘/时长）、`get_radio`（PTT/发射状态）、`get_camera`（摄像头/录像）、
  `get_time`、`speak`（TTS 语音播报，会占用 PTT —— 动作型技能）。
- 对话页可开关「Agent 技能调用」；对话流里会显示 `⚙ 调用技能 …` / `✔ 技能返回 …`。
- 后台设置页可勾选启用的技能（默认全开）、设置最大技能轮次（1~5，默认 3）。
- LLM 页「Agent 技能调用」关闭时退回普通对话（仍走提示词注入 + 速率统计）。

### 4. 板端模型三个坑（都已适配，改代码前务必先读）

| 现象 | 根因 | 现在的做法 |
|---|---|---|
| 工具调用指令完全不被执行 | 服务端**只取最后一条消息**，system 被丢掉（旧记的「RKLLM 忽略 system」是误判） | 打 `patch_rkllm_chat.py`，走真正的 system 槽位 |
| 一旦出现 `<tool_call>` 就返回**空串** | `<tool_call>` 是 Qwen **特殊 token**，被服务端当控制符丢弃 | 改用纯文本协议 `READ 工具名 {}`，解析器兼容缺标签/裸 JSON |
| 提示词超过约 400 字空输出 | **已作废**：那是 `max_context_len` 硬编码 512 造成的，`patch_rkllm_context.py` 提到 4096 后实测 4615 字符仍正常 | 保留「精简」习惯，但理由换成**延时**（≈4.7 ms/字符） |
| 提示词超限时**静默**返回空串（HTTP 200 + `content=''` + 0.3s） | 运行时长度检查直接拒，不产生错误码 | `patch_rkllm_chat.py` 提交前估算 token，超限返回 HTTP 400 + 明确说明 |
| **长提示词下行为约束不被遵守** | 近因效应：约束放在提示词**开头**会被无视 | 播报规范固定放在**问题之后**、提示词最后一段（实测放前面答「哈喽！…」无标记，放后面答「…，喵。」） |
| `max_tokens` 请求参数不起作用 | 服务端 `max_new_tokens` 写死 -1，且不读请求值 | `RKLLM_MAX_NEW_TOKENS=512` 全局封顶；请求值只用于超限余量估算 |
| 模型会自造工具名（`get_battery_voltage`） | 1.5B 模型不稳定 | 别名表 + 关键词归一（`get_battery_voltage`→`get_power` 等） |

### 5. 实测（板端 `192.168.101.215`；该次实测时旧地址为 192.168.101.206）

- 提问「电池电压和光伏电压是多少？CPU 温度多少？」→ 模型输出 `READ get_battery_voltage {} …`，
  归一为 `get_power`/`get_system`，执行后回答：
  「当前电池电压 12.9288 V，光伏电压 21.6255 V，CPU 温度 37.9 °C」；
- 速率：首字 ~0.9 s、**13~24 tok/s**、单次 60~100 tokens（Qwen2.5-1.5B RKNPU W8A8）；
- 提示词注入 + `{battery}` 变量：回答开头正确带出实时电压 12.98 V；
- 前端真机验证：技能芯片、速率条、设置页技能清单/统计表均正常，无 5xx。

---

## 端侧语音识别（ASR）/ On-device ASR

| 项 | 值 |
|---|---|
| 引擎 | sherpa-onnx 1.13.8 + SenseVoice int8（onnxruntime，CPU 4 线程） |
| 模型 | `/opt/ai/asr/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17`（155 MB，中英日韩粤） |
| 服务模块 | `/www/asr_service.py`（懒加载 / 串行解码 / ffmpeg 归一化成 16 kHz 单声道） |
| 实测性能 | 模型加载 2~3 s（仅首次）；识别 **RTF 0.045~0.07**（5~7 s 音频 → 170~310 ms） |
| 录音留档 | `/www/asr_recordings/asr_YYYYmmdd_HHMMSS.wav`；识别记录写 `asr_logs` 表 |
| 依赖 | `pip3 install --user sherpa-onnx`；`ffmpeg`（已在板端）；`numpy`（sherpa-onnx 依赖） |

### 接口

| 接口 | 方法 | 说明 |
|---|---|---|
| `/api/asr/status` | GET | 引擎状态（模型文件、是否已加载、最近一次识别） |
| `/api/asr/transcribe` | POST | 上传录音（multipart 字段 `audio`）或 `{"path": "/www/xxx.wav"}` → `{text, ms, seconds, rtf, filename}` |
| `/api/asr/recordings` | GET | 留档录音列表 + 最近 50 条识别记录 |

### 前端用法（LLM 对话页）

- **BUSY（按住说话）**：按住 → 网页麦克风采集 16 kHz PCM → 松开 → 打包 WAV 上传识别 →
  文本回填输入框；勾选「转写后自动发送」则直接发给端侧 LLM（可再接 TTS 朗读，形成语音闭环）。
- 识别耗时与 RTF 会显示在按键右侧；录音同时留档。
- 总览页「中继状态」的 **PTT / BUSY** 行由 `/api/ptt/status` 每 1.5 s 同步：
  GPIO3_A1 拉高 → 显示「PTT 使能（发射中）」；本地录音中 → BUSY 显示「本地录音中（语音输入）」。

### 调参

```bash
RELAY_ASR_MODEL=/opt/ai/asr/<其他模型目录>   # 换模型
RELAY_ASR_THREADS=4                          # 解码线程（A76 大核）
RELAY_ASR_LANG=auto|zh|en|yue|ja|ko          # 强制语言可略提精度
RELAY_ASR_ITN=1                              # 数字/标点规整（"十二点八" → "12.8"）
```
