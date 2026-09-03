# pi-sounds

局域网音频播放控制台：守护进程 + Streamlit Web UI + 定时任务。
支持播放 mp3 / wav / flac / ogg / m4a / aac / opus。

- **播放**：单曲 / 顺序 / 随机 / 单曲循环 / 列表循环
- **管理界面**：Streamlit 局域网访问（默认 `http://<树莓派IP>:8501`）
- **存储**：JSON 文件 + 文件锁（`fcntl.flock`），运行时热加载
- **定时任务**：标准 5 字段 cron 表达式
- **播放器**：mpv（subprocess + IPC socket，崩溃自动重启）

> ⚠️ 默认无认证。**只在可信局域网内暴露**，不要做公网端口转发。

---

## 架构

```
┌────────────────┐      commands.json         ┌─────────────────┐
│  Streamlit UI  │  ───────────────────────►   │   守护进程      │
│  (web/*.py)    │      status.json            │   daemon/*      │
│                │  ◄───────────────────────   │                 │
└────────────────┘                             │  ┌──────────┐   │
                                               │  │ mpv 子进 │   │
                                               │  │  程 + IPC │   │
                                               │  └──────────┘   │
                                               └─────────────────┘
                                                       │
                                                       ▼
                                              ~/sounds/*.mp3
```

两个独立进程通过 `data/` 下的 JSON 文件 + `fcntl.flock` 互锁通信。
守护进程负责：mpv 控制 / 队列调度 / cron 触发 / 状态写回。
Streamlit 只读写 JSON 并通过 `commands.json` 发命令。

---

## 部署（Ubuntu / 树莓派）

### 1. 系统依赖

```bash
sudo apt update
sudo apt install -y mpv ffmpeg python3-venv python3-pip alsa-utils
sudo usermod -aG audio pi      # 需要重新登录
```

### 2. 验证 USB 音箱

```bash
aplay -l                      # 记下 card / device 号
mpv --audio-device=help | grep alsa
speaker-test -D plughw:1,0 -c 2 -t wav   # 应该能听到白噪声
```

### 3. 克隆与虚拟环境

```bash
cd /home/pi
git clone <repo-url> pi-sounds
cd pi-sounds
python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements.txt
```

### 4. 初始化数据目录

```bash
mkdir -p /home/pi/sounds       # 放音频文件
PYTHONPATH=. .venv/bin/python scripts/init_data.py --audio-dir /home/pi/sounds
```

这一步会创建：
- `~/.local/share/pi-sounds/data/config.json`
- `~/.local/share/pi-sounds/data/schedules.json`
- `~/.local/share/pi-sounds/data/commands.json`
- `~/.local/share/pi-sounds/data/playlists/`

### 5. 前台试跑（两个终端）

```bash
# 终端 1：守护进程
PYTHONPATH=. .venv/bin/python -m daemon.main

# 终端 2：Streamlit
PYTHONPATH=. .venv/bin/streamlit run web/app.py --server.address 0.0.0.0
```

打开浏览器访问 `http://<树莓派IP>:8501` 即可。

### 6. 注册为 systemd 服务

```bash
sudo cp systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pi-sounds-daemon pi-sounds-web
sudo systemctl status pi-sounds-daemon
journalctl -u pi-sounds-daemon -f
```

### 7. 防火墙（如启用 ufw）

```bash
sudo ufw allow from 192.168.0.0/16 to any port 8501 proto tcp
```

---

## 配置文件

`data/config.json` 的所有字段：

```json
{
  "version": 1,
  "audio_dir": "/home/pi/sounds",
  "audio_device": "auto",
  "volume": 60,
  "play_mode": "sequence",
  "extensions": [".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".opus"],
  "startup": { "autoplay": false, "playlist_id": null },
  "status_interval_sec": 1.0,
  "command_poll_sec": 0.3
}
```

修改音频目录或设备后**需要重启守护进程**才生效。

---

## CLI 调试工具

```bash
# 发送命令（写到 commands.json，守护进程会消费）
python scripts/send_command.py ping
python scripts/send_command.py play --playlist morning
python scripts/send_command.py play path=/tmp/song.mp3 title="自定义标题"
python scripts/send_command.py set_volume 80
python scripts/send_command.py set_mode shuffle
python scripts/send_command.py next
python scripts/send_command.py stop

# 仅打印命令而不发送
python scripts/send_command.py ping --show
```

---

## 进程间通信协议

**commands.json** 队列（Streamlit → daemon）：

```json
{
  "version": 1,
  "seq": 42,
  "commands": [
    { "id": "c_abc123", "ts": "2026-09-03T08:00:00+08:00",
      "action": "play", "args": { "playlist_id": "morning", "index": 0 } }
  ]
}
```

支持的动作（`action`）：

| action | args | 语义 |
|---|---|---|
| `play` | `playlist_id?` + `index?` 或 `path` + `title?` | 切换并播放 |
| `pause` / `resume` / `toggle_pause` | — | 播放控制 |
| `stop` / `next` / `prev` | — | 传输控制 |
| `set_volume` | `volume:0-100` | 音量（持久化） |
| `set_mode` | `mode ∈ {single,sequence,shuffle,repeat_one,repeat_all}` | 播放模式 |
| `set_playlist` | `playlist_id` + `autoplay?` + `index?` | 仅切换队列 |
| `seek` | `seconds` + `mode?` | 跳转 |
| `reload_config` / `rescan_library` / `ping` | — | 维护 |

**status.json**（daemon → Streamlit）：

```json
{
  "version": 1,
  "ts": "...",
  "daemon": { "pid", "started_at", "healthy", "mpv_alive", "mpv_restarts" },
  "player": { "state", "playlist_id", "playlist_name", "index",
              "track": { "path", "title" }, "position_sec",
              "duration_sec", "volume", "play_mode" },
  "queue": { "length", "order" },
  "schedules": { "next": { "id", "name", "at" } },
  "errors": [],
  "last_commands": []
}
```

---

## 故障排查

| 现象 | 检查 |
|---|---|
| 守护进程启动失败 | `journalctl -u pi-sounds-daemon -n 50 -f` |
| mpv 一直重启 | 看门狗 60s 内最多重启 5 次；查 `/var/log/syslog` 中的 USB 设备错误 |
| 状态长时间不更新 | UI 顶部"守护进程离线"会变红；`cat data/status.json` 检查 `ts` |
| 播放列表看不到文件 | 设置页 → "重新扫描并标记缺失"；检查 `audio_dir` 路径与权限 |
| cron 表达式不触发 | 设置页下方预览会显示接下来 5 次触发时间；格式：分 时 日 月 周 |
| UI 不能访问 | `systemctl status pi-sounds-web`；`ufw status`；`streamlit` 端口是否被占 |
| 没有声音 | `aplay -l` 看 USB 卡；`mpv --audio-device=help` 选正确设备填到 `config.json` |

---

## 开发

- `daemon/` — 守护进程
- `web/` — Streamlit UI（`pages/` 是 multipage 子页）
- `shared/` — 守护进程和 UI 共用代码（paths / locking / schemas）
- `scripts/` — 一次性脚本（init / CLI 调试）

不写单元测试（按用户偏好）。本地 Windows + 真机 Ubuntu 双环境开发：
dev 环境的 Python 没有 Unix socket，mpv IPC 在 Windows 上不可用；
最终验证在树莓派上进行。

---

## 已知限制 / 后续可扩展

- 单音频输出（USB 音箱）；多设备同步或蓝牙未支持
- TTS 语音播报未集成
- GPIO 触发（门铃/感应器）未集成
- 文件监控自动加入播放列表未实现
- 元数据读取（封面 / ID3）未实现
- 紧急通知打断普通播放的优先级机制预留了 `if_busy` 字段但只在 cron 触发时使用

---

## License

仅供个人 / 内部使用。