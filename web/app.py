"""Streamlit home page: live playback status and transport.

Run with::

    streamlit run web/app.py --server.address 0.0.0.0 --server.port 8501

The Streamlit auto-reruns the whole script on every interaction; we use a
short cache TTL and a top-level autorefresh hook to keep this page live
without manual reload.
"""
from __future__ import annotations

import streamlit as st

from shared import schemas
from web import client


st.set_page_config(
    page_title="pi-sounds 控制台",
    page_icon="🔊",
    layout="wide",
    initial_sidebar_state="expanded",
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
# Read state
# ---------------------------------------------------------------------------

status = client.read_status()
player = status.get("player", {}) or {}
daemon = status.get("daemon", {}) or {}
queue = status.get("queue", {}) or {}
last_commands = status.get("last_commands", []) or []
errors = status.get("errors", []) or []

state = player.get("state", "idle")
playlist_id = player.get("playlist_id")
playlist_name = player.get("playlist_name") or "(未加载)"
track = player.get("track") or {}
track_title = track.get("title") or "(无曲目)"
track_path = track.get("path") or ""
position_sec = float(player.get("position_sec") or 0.0)
duration_sec = float(player.get("duration_sec") or 0.0)
volume = int(player.get("volume") or 0)
mode = player.get("play_mode") or "sequence"

responsive = client.daemon_is_responsive(status)
mpv_alive = bool(daemon.get("mpv_alive"))
healthy = bool(daemon.get("healthy"))
mpv_restarts = int(daemon.get("mpv_restarts") or 0)


# ---------------------------------------------------------------------------
# Sidebar: navigation + daemon health
# ---------------------------------------------------------------------------

with st.sidebar:
    st.title("🔊 pi-sounds")
    st.caption("局域网音频播放控制台")
    st.divider()
    st.markdown("### 导航")
    st.page_link("app.py", label="播放控制", icon="🎵")
    st.page_link("pages/1_Playlists.py", label="播放列表", icon="📋")
    st.page_link("pages/2_Schedules.py", label="定时任务", icon="⏰")
    st.page_link("pages/3_Settings.py", label="设置", icon="⚙️")
    st.divider()
    st.markdown("### 守护进程")
    if not responsive:
        st.error("❌ 守护进程离线（status.json 超时未更新）")
    elif not healthy:
        st.warning("⚠️ 守护进程报告异常")
    elif not mpv_alive:
        st.warning("⚠️ mpv 未运行，看门狗将尝试重启")
    else:
        st.success("✅ 一切正常")
    st.caption(f"mpv 重启次数：**{mpv_restarts}**")
    st.caption(f"PID：**{daemon.get('pid')}**")
    st.caption(f"启动于：**{daemon.get('started_at') or '—'}**")
    st.divider()
    if st.button("Ping 守护进程", use_container_width=True):
        client.send_ping()
        st.toast("已发送 ping")


# ---------------------------------------------------------------------------
# Header: current track card
# ---------------------------------------------------------------------------

st.markdown(f"## 🎵 {track_title}")
if track_path:
    st.caption(f"`{track_path}`")
col_meta1, col_meta2, col_meta3 = st.columns(3)
with col_meta1:
    st.metric("状态", client.state_label(state))
with col_meta2:
    st.metric("播放列表", playlist_name or "(未加载)")
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
c1, c2, c3, c4, c5 = st.columns([1, 1, 1, 1, 2])

with c1:
    if st.button("⏮ 上一首", use_container_width=True):
        client.send_prev()
with c2:
    play_label = "⏸ 暂停" if state == "playing" else "▶ 播放"
    if st.button(play_label, use_container_width=True, type="primary"):
        client.send_toggle_pause()
with c3:
    if st.button("⏭ 下一首", use_container_width=True):
        client.send_next()
with c4:
    if st.button("⏹ 停止", use_container_width=True):
        client.send_stop()
with c5:
    new_volume = st.slider(
        "音量", min_value=0, max_value=100, value=volume, step=1,
        key=f"volume_slider_{volume}",  # resets key when daemon changes volume
        label_visibility="collapsed",
    )
    if new_volume != volume:
        client.send_set_volume(new_volume)


# ---------------------------------------------------------------------------
# Mode selector + playlist quick-switch
# ---------------------------------------------------------------------------

st.divider()
col_mode, col_pl = st.columns([1, 2])
with col_mode:
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
    )
    if new_mode != mode:
        client.send_set_mode(new_mode)

with col_pl:
    playlists = client.list_playlists()
    if not playlists:
        st.info("还没有播放列表，去 **播放列表** 页面新建一个吧。")
    else:
        # Build labels
        labels = ["— 选择 —"]
        pid_by_label: dict[str, str | None] = {"— 选择 —": None}
        for pid in playlists:
            pl = client.load_playlist(pid)
            if pl is None:
                continue
            name = pl.get("name") or pid
            label = f"{name} ({len(pl.get('items') or [])} 首)"
            labels.append(label)
            pid_by_label[label] = pid
        current_label = "— 选择 —"
        for label, pid in pid_by_label.items():
            if pid == playlist_id:
                current_label = label
                break
        chosen = st.selectbox(
            "切换播放列表",
            options=labels,
            index=labels.index(current_label) if current_label in labels else 0,
        )
        if st.button("▶ 播放所选", use_container_width=True):
            chosen_pid = pid_by_label.get(chosen)
            if chosen_pid:
                client.send_play(playlist_id=chosen_pid)


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