"""Shared sidebar rendered on every page.

Provides:
* Navigation links
* Daemon health indicator + Ping button
* **Real-time volume slider** (sends ``set_volume`` on every drag tick)
* **Playlist picker** with on-change auto-play + per-playlist quick-play list

Call ``render_sidebar()`` at the top of every page script so the user
gets the same controls everywhere.
"""
from __future__ import annotations

from typing import Any

import streamlit as st

from web import client


def render_sidebar(status: dict[str, Any] | None = None) -> None:
    if status is None:
        status = client.read_status()

    player = status.get("player", {}) or {}
    daemon = status.get("daemon", {}) or {}

    responsive = client.daemon_is_responsive(status)
    mpv_alive = bool(daemon.get("mpv_alive"))
    healthy = bool(daemon.get("healthy"))
    mpv_restarts = int(daemon.get("mpv_restarts") or 0)

    with st.sidebar:
        st.title("🔊 pi-sounds")
        st.caption("局域网音频播放控制台")
        st.divider()

        # ---- navigation ----
        st.markdown("### 导航")
        st.page_link("app.py", label="播放控制", icon="🎵")
        st.page_link("pages/1_Playlists.py", label="播放列表", icon="📋")
        st.page_link("pages/2_Schedules.py", label="定时任务", icon="⏰")
        st.page_link("pages/3_Settings.py", label="设置", icon="⚙️")

        st.divider()

        # ---- daemon health ----
        st.markdown("### 守护进程")
        if not responsive:
            st.error("❌ 守护进程离线")
        elif not healthy:
            st.warning("⚠️ 报告异常")
        elif not mpv_alive:
            st.warning("⚠️ mpv 未运行")
        else:
            st.success("✅ 一切正常")
        st.caption(f"mpv 重启：**{mpv_restarts}**  PID：**{daemon.get('pid') or '—'}**")
        if st.button("Ping 守护进程", use_container_width=True, key="sb_ping"):
            client.send_ping()
            st.toast("已发送 ping")

        st.divider()

        # ---- real-time volume ----
        st.markdown("### 🎚 音量")
        current_vol = int(player.get("volume") or 0)
        if "sidebar_volume" not in st.session_state:
            st.session_state["sidebar_volume"] = current_vol
        st.session_state["sidebar_volume"] = current_vol

        new_vol = st.slider(
            "音量 (0-100)",
            min_value=0, max_value=100,
            value=st.session_state["sidebar_volume"],
            step=1,
            key="sidebar_volume_slider",
            on_change=_on_volume_change,
            label_visibility="collapsed",
        )
        st.session_state["sidebar_volume"] = new_vol
        st.caption(f"当前：**{new_vol}%**")

        st.divider()

        # ---- playlist picker (auto-plays on selection change) ----
        st.markdown("### ▶ 切换播放列表")
        st.caption("选择即播放，右侧会展示曲目。")
        playlists = client.list_playlists()
        if not playlists:
            st.caption("还没有播放列表")
        else:
            labels, label_to_pid = _build_playlist_labels(playlists)
            # Store mapping in session_state so on_change callback can resolve pid.
            st.session_state["sb_pl_label_to_pid"] = label_to_pid
            current_label = _current_label_for(
                st.session_state.get("sb_pl_picker"),
                label_to_pid,
                player.get("playlist_id"),
            )
            chosen = st.selectbox(
                "选择播放列表",
                options=labels,
                index=labels.index(current_label) if current_label in labels else 0,
                key="sb_pl_picker",
                on_change=_on_playlist_change,
                label_visibility="collapsed",
            )
            pid = label_to_pid.get(chosen)
            if pid:
                pl = client.load_playlist(pid) or {}
                st.caption(f"曲目：{len(pl.get('items') or [])} 首")
            # Explicit re-play from start
            if st.button(
                "↻ 从头播放",
                type="secondary",
                use_container_width=True,
                disabled=not pid,
                key="sb_pl_replay",
            ):
                client.send_play(playlist_id=pid, index=0)
                st.toast(f"已从头播放：**{pid}**")


def _on_volume_change() -> None:
    """Send a set_volume command whenever the sidebar slider moves."""
    new_vol = int(st.session_state.get("sidebar_volume_slider", 0))
    client.send_set_volume(new_vol)


def _on_playlist_change() -> None:
    """Auto-play the newly-selected playlist when the sidebar dropdown changes."""
    label = st.session_state.get("sb_pl_picker")
    mapping = st.session_state.get("sb_pl_label_to_pid", {})
    pid = mapping.get(label)
    if pid:
        client.send_play(playlist_id=pid)


def _build_playlist_labels(playlist_ids: list[str]) -> tuple[list[str], dict[str, str | None]]:
    labels = ["— 选择 —"]
    label_to_pid: dict[str, str | None] = {"— 选择 —": None}
    for pid in playlist_ids:
        pl = client.load_playlist(pid)
        if pl is None:
            continue
        name = pl.get("name") or pid
        count = len(pl.get("items") or [])
        label = f"{name}（{count} 首）"
        labels.append(label)
        label_to_pid[label] = pid
    return labels, label_to_pid


def _current_label_for(
    selected_label: str | None,
    label_to_pid: dict[str, str | None],
    playing_pid: str | None,
) -> str:
    """If the sidebar label is empty/missing, default to the currently
    playing playlist so the dropdown doesn't appear to reset every reload."""
    if selected_label and selected_label in label_to_pid:
        return selected_label
    if playing_pid:
        for label, pid in label_to_pid.items():
            if pid == playing_pid:
                return label
    return "— 选择 —"


__all__ = ["render_sidebar"]