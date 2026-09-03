"""Streamlit home page: live playback status, transport, and per-playlist tracks.

Run with::

    streamlit run web/app.py --server.address 0.0.0.0 --server.port 8501
"""
from __future__ import annotations

import os

import streamlit as st

from shared import schemas
from web import client
from web.sidebar import render_sidebar


st.set_page_config(
    page_title="pi-sounds 控制台",
    page_icon="🔊",
    layout="wide",
)


# ---------------------------------------------------------------------------
# Auto-refresh (every second)
# ---------------------------------------------------------------------------

try:
    from streamlit_autorefresh import st_autorefresh
    st_autorefresh(interval=1000, key="home_autorefresh")
except ImportError:
    st.warning("streamlit-autorefresh 未安装，页面不会自动刷新。")


# ---------------------------------------------------------------------------
# Sidebar first (so the rest of the page can read sb_pl_picker from session_state)
# ---------------------------------------------------------------------------

status = client.read_status()
render_sidebar(status)


# ---------------------------------------------------------------------------
# Read state
# ---------------------------------------------------------------------------

player = status.get("player", {}) or {}
queue = status.get("queue", {}) or {}
last_commands = status.get("last_commands", []) or []
errors = status.get("errors", []) or []

state = player.get("state", "idle")
playing_pid = player.get("playlist_id")
playing_idx = int(player.get("index", -1))
playlist_name = player.get("playlist_name") or "(未加载)"
track = player.get("track") or {}
track_title = track.get("title") or "(无曲目)"
track_path = track.get("path") or ""
position_sec = float(player.get("position_sec") or 0.0)
duration_sec = float(player.get("duration_sec") or 0.0)
volume = int(player.get("volume") or 0)
mode = player.get("play_mode") or "sequence"


# Resolve which playlist the sidebar is currently showing
sb_label = st.session_state.get("sb_pl_picker", "— 选择 —")
sb_map: dict[str, str | None] = st.session_state.get("sb_pl_label_to_pid", {})
selected_pid = sb_map.get(sb_label)

selected_pl = client.load_playlist(selected_pid) if selected_pid else None
selected_items = (selected_pl.get("items") or []) if selected_pl else []
selected_name = (selected_pl.get("name") if selected_pl else None) or selected_pid or ""


# ---------------------------------------------------------------------------
# Header: now-playing card
# ---------------------------------------------------------------------------

st.markdown(f"## 🎵 {track_title}")
if track_path:
    st.caption(f"`{track_path}`")
col_meta1, col_meta2, col_meta3 = st.columns(3)
with col_meta1:
    st.metric("状态", client.state_label(state))
with col_meta2:
    st.metric("正在播放", playlist_name)
with col_meta3:
    st.metric("当前音量", f"{volume}%")

st.progress(
    min(1.0, position_sec / duration_sec) if duration_sec > 0 else 0.0,
    text=f"{client.fmt_mmss(position_sec)} / {client.fmt_mmss(duration_sec)}",
)


# ---------------------------------------------------------------------------
# Transport controls
# ---------------------------------------------------------------------------

st.divider()
c1, c2, c3, c4 = st.columns(4)
with c1:
    if st.button("⏮ 上一首", use_container_width=True, key="home_prev"):
        client.send_prev()
with c2:
    play_label = "⏸ 暂停" if state == "playing" else "▶ 播放"
    if st.button(play_label, use_container_width=True, type="primary", key="home_toggle"):
        client.send_toggle_pause()
with c3:
    if st.button("⏭ 下一首", use_container_width=True, key="home_next"):
        client.send_next()
with c4:
    if st.button("⏹ 停止", use_container_width=True, key="home_stop"):
        client.send_stop()


# ---------------------------------------------------------------------------
# Mode selector
# ---------------------------------------------------------------------------

st.divider()
new_mode = st.selectbox(
    "播放模式",
    options=list(schemas.PLAY_MODES),
    index=list(schemas.PLAY_MODES).index(mode) if mode in schemas.PLAY_MODES else 1,
    format_func=lambda m: {
        "single": "单曲（播一次后停止）",
        "sequence": "顺序播放",
        "shuffle": "随机播放",
        "repeat_one": "单曲循环",
        "repeat_all": "列表循环",
    }.get(m, m),
    key="home_mode",
)
if new_mode != mode:
    client.send_set_mode(new_mode)


# ---------------------------------------------------------------------------
# Playlist tracks (the right side of the user's request)
# ---------------------------------------------------------------------------

st.divider()
if selected_pid is None:
    st.info("👈 在左侧侧边栏选择一个播放列表，曲目会显示在这里。")
else:
    header_left, header_right = st.columns([3, 2])
    with header_left:
        st.markdown(f"### 📃 {selected_name} · 曲目（共 {len(selected_items)} 首）")
    with header_right:
        is_current = (selected_pid == playing_pid)
        if is_current:
            st.caption(f"✓ 当前正在播放此列表")
        else:
            st.caption("此列表与正在播放的不同 — 点击 ▶ 跳到这首切到此列表")

    if not selected_items:
        st.caption("此播放列表没有任何曲目。去 **播放列表** 页面添加。")
    else:
        _render_track_list(selected_pid, selected_items, playing_pid, playing_idx)


def _render_track_list(
    playlist_id: str,
    items: list[dict],
    playing_pid: str | None,
    playing_idx: int,
) -> None:
    """Render one row per track. The currently playing track (if any) is
    highlighted and rows have a per-track play button."""
    for idx, item in enumerate(items):
        missing = bool(item.get("missing"))
        is_current = (idx == playing_idx) and (playlist_id == playing_pid)
        title = item.get("title") or os.path.basename(item.get("path", ""))

        c1, c2, c3, c4 = st.columns([0.5, 4, 2.5, 1])
        with c1:
            marker = "▶" if is_current else f"{idx + 1}"
            st.markdown(f"**{marker}**")
        with c2:
            badge = " · ❌ 缺失" if missing else ""
            st.markdown(f"{title}{badge}")
        with c3:
            st.caption(f"`{item.get('path', '')}`")
        with c4:
            label = "正在播" if is_current else "▶ 跳到"
            if st.button(
                label,
                key=f"home_jump_{playlist_id}_{idx}",
                use_container_width=True,
                disabled=is_current,
            ):
                client.send_play(playlist_id=playlist_id, index=idx)
                st.toast(f"跳到第 {idx + 1} 首：**{title}**")


# ---------------------------------------------------------------------------
# Footer: queue + errors + recent commands
# ---------------------------------------------------------------------------

st.divider()
tab_queue, tab_logs, tab_err = st.tabs(["队列", "最近命令", "错误"])

with tab_queue:
    length = queue.get("length", 0)
    order = queue.get("order", []) or []
    st.metric("曲目总数", length)
    if order:
        st.caption("播放顺序：")
        st.code(", ".join(str(i) for i in order))

with tab_logs:
    if not last_commands:
        st.caption("还没有命令记录。")
    else:
        for cmd in last_commands[:10]:
            ok_icon = "✅" if cmd.get("ok") else "❌"
            ts = cmd.get("ts", "")
            st.text(
                f"{ok_icon} [{ts}] {cmd.get('action')}  id={cmd.get('id')}  "
                f"{cmd.get('msg', '')}"
            )

with tab_err:
    if not errors:
        st.caption("没有错误。")
    else:
        for e in errors[:10]:
            level = e.get("level", "info")
            icon = "⚠️" if level == "warning" else "❌" if level == "error" else "ℹ️"
            st.text(f"{icon} [{e.get('ts')}] {e.get('msg')}")