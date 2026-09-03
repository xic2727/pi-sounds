"""Settings page: configure audio directory / device / defaults and inspect
raw JSON files (for debugging)."""
from __future__ import annotations

import json

import streamlit as st

from shared import paths
from shared import schemas
from web import client
from web.sidebar import render_sidebar


st.set_page_config(page_title="设置 · pi-sounds", page_icon="⚙️", layout="wide")

# Shared sidebar (nav, health, real-time volume, manual playlist trigger)
render_sidebar()

st.title("⚙️ 设置")

cfg = client.load_config()
playlists = client.list_playlists()

# ---------------------------------------------------------------------------
# Form: edit + save config
# ---------------------------------------------------------------------------

st.markdown("### 全局配置")

with st.form("settings_form"):
    col1, col2 = st.columns(2)
    with col1:
        new_audio_dir = st.text_input(
            "音频目录（绝对路径）",
            value=cfg.get("audio_dir", ""),
            help="递归扫描此目录下的音频文件。修改后需重启守护进程。",
        )
        new_volume = st.slider(
            "默认音量",
            min_value=0, max_value=100,
            value=int(cfg.get("volume", 60)),
            step=1,
        )
        new_device = st.text_input(
            "音频输出设备",
            value=cfg.get("audio_device", "auto"),
            help="`auto` 让 mpv 自动选择；具体名称见 `mpv --audio-device=help`",
        )
    with col2:
        new_mode = st.selectbox(
            "默认播放模式",
            options=list(schemas.PLAY_MODES),
            index=list(schemas.PLAY_MODES).index(cfg.get("play_mode", "sequence"))
            if cfg.get("play_mode") in schemas.PLAY_MODES else 1,
            format_func=lambda m: {
                "single": "单曲", "sequence": "顺序", "shuffle": "随机",
                "repeat_one": "单曲循环", "repeat_all": "列表循环",
            }.get(m, m),
        )
        new_ext_text = st.text_input(
            "支持的文件扩展名（逗号分隔）",
            value=", ".join(cfg.get("extensions", schemas.DEFAULT_EXTENSIONS)),
        )
        autoplay = st.checkbox(
            "开机自动播放",
            value=bool(cfg.get("startup", {}).get("autoplay", False)),
            help="守护进程启动时自动播放指定的播放列表",
        )
        autoplay_pl = st.selectbox(
            "自动播放的播放列表",
            options=[None] + playlists,
            index=(playlists.index(cfg.get("startup", {}).get("playlist_id")) + 1)
            if cfg.get("startup", {}).get("playlist_id") in playlists else 0,
            disabled=not autoplay,
            format_func=lambda p: "(不指定)" if p is None else p,
        )

    submitted = st.form_submit_button("💾 保存配置", type="primary")
    if submitted:
        exts = [
            e.strip().lower() if e.strip().startswith(".")
            else f".{e.strip().lower()}"
            for e in new_ext_text.split(",")
            if e.strip()
        ]
        new_cfg = dict(cfg)
        new_cfg["audio_dir"] = new_audio_dir
        new_cfg["audio_device"] = new_device
        new_cfg["volume"] = int(new_volume)
        new_cfg["play_mode"] = new_mode
        new_cfg["extensions"] = exts
        new_cfg["startup"] = {
            "autoplay": bool(autoplay),
            "playlist_id": autoplay_pl,
        }
        try:
            client.save_config(new_cfg)
            st.success("配置已保存。修改音频目录后需要重启守护进程才能生效。")
        except Exception as e:
            st.error(f"保存失败：{e}")


# ---------------------------------------------------------------------------
# Library rescan
# ---------------------------------------------------------------------------

st.divider()
st.markdown("### 音频库")
files = client.list_audio_files()
st.caption(f"音频目录里共 **{len(files)}** 个文件。")
if files:
    sample = files[:20]
    st.dataframe(
        {
            "path": [f["path"] for f in sample],
            "title": [f["title"] for f in sample],
            "size (KB)": [round(f["size"] / 1024, 1) for f in sample],
        },
        use_container_width=True,
        hide_index=True,
    )
    if len(files) > 20:
        st.caption(f"...还有 {len(files) - 20} 个未显示")

col_a, col_b = st.columns([1, 4])
with col_a:
    if st.button("🔄 重新扫描并标记缺失", use_container_width=True):
        client.rescan_library()
        st.success("已发送 rescan_library 命令；几秒后刷新页面查看结果")
with col_b:
    st.caption("扫描会标记所有播放列表里缺失的曲目（不影响磁盘文件）。")


# ---------------------------------------------------------------------------
# Raw JSON inspector
# ---------------------------------------------------------------------------

st.divider()
st.markdown("### 原始 JSON（调试用）")

tab_cfg, tab_sch, tab_cmd, tab_st = st.tabs([
    "config.json", "schedules.json", "commands.json", "status.json"
])
with tab_cfg:
    st.json(cfg)
with tab_sch:
    st.json(client.load_schedules())
with tab_cmd:
    st.json(client.load_commands())
with tab_st:
    st.json(client.read_status())


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

st.divider()
st.markdown("### 文件路径")
st.code(
    f"data dir:      {paths.data_dir()}\n"
    f"audio dir:     {cfg.get('audio_dir')}\n"
    f"playlists dir: {paths.playlists_dir()}\n"
    f"runtime dir:   {paths.runtime_dir()}\n"
    f"mpv socket:    {paths.mpv_socket_path()}",
    language="text",
)