"""Playlists management page.

Lets the operator create / delete playlists, rename them, switch the
play mode, reorder items via drag-and-drop, add files from the audio
directory, and edit titles. Writes go through ``web.client.save_playlist``
which serialises under the shared lock so the daemon reads consistent
data.

This page disables autorefresh (the form would be interrupted by it).
"""
from __future__ import annotations

import streamlit as st

from shared import schemas
from web import client


st.set_page_config(page_title="播放列表 · pi-sounds", page_icon="📋", layout="wide")


# ---------------------------------------------------------------------------
# Sidebar: playlist picker + new
# ---------------------------------------------------------------------------

playlist_ids = client.list_playlists()


def _refresh_ids() -> list[str]:
    return client.list_playlists()


def _load(pid: str) -> dict | None:
    return client.load_playlist(pid)


with st.sidebar:
    st.markdown("### 播放列表")
    selected = st.selectbox(
        "选择",
        options=["— 新建 —"] + playlist_ids,
        index=0 if not playlist_ids else 1,
        key="playlist_picker",
    )
    if st.button("🔄 刷新列表", use_container_width=True):
        st.rerun()

    st.divider()
    st.caption("在右侧编辑后点 **保存** 生效。")


# ---------------------------------------------------------------------------
# Main area
# ---------------------------------------------------------------------------

st.title("📋 播放列表管理")

# If user just clicked "新建", show the new-playlist form
if selected == "— 新建 —":
    st.markdown("### 新建播放列表")
    with st.form("new_playlist_form", clear_on_submit=True):
        new_id = st.text_input(
            "ID（仅小写字母、数字、下划线、连字符，1-40 字符）",
            value="",
            placeholder="例如 morning",
        ).strip()
        new_name = st.text_input("显示名称", value="").strip()
        new_mode = st.selectbox(
            "默认播放模式",
            options=[None] + list(schemas.PLAY_MODES),
            index=1,
            format_func=lambda m: "（继承全局）" if m is None else {
                "single": "单曲（播一次后停止）",
                "sequence": "顺序播放",
                "shuffle": "随机播放",
                "repeat_one": "单曲循环",
                "repeat_all": "列表循环",
            }.get(m, m),
        )
        ok = st.form_submit_button("创建", use_container_width=True)
        if ok:
            if not schemas.validate_playlist_id(new_id):
                st.error("ID 格式不合法，请使用小写字母、数字、下划线或连字符（1-40 字符）。")
            elif _load(new_id) is not None:
                st.error(f"已存在同名 ID：{new_id}")
            elif not new_name:
                st.error("请填写显示名称。")
            else:
                pl = schemas.make_playlist(new_id, new_name, play_mode=new_mode)
                try:
                    client.save_playlist(pl)
                    st.success(f"已创建：{new_id}")
                    st.rerun()
                except Exception as e:
                    st.error(f"创建失败：{e}")
    st.stop()


# Existing playlist: edit it
playlist = _load(selected)
if playlist is None:
    st.error(f"播放列表 {selected!r} 不存在或读取失败。")
    st.stop()


# Session-state copy to allow edits without writing back until "保存"
key = f"pl_edit_{selected}"
if key not in st.session_state:
    st.session_state[key] = {
        "name": playlist.get("name") or selected,
        "play_mode": playlist.get("play_mode"),
        "items": [dict(it) for it in (playlist.get("items") or [])],
    }

draft = st.session_state[key]


# ---- name + mode row ----
col_meta1, col_meta2 = st.columns([3, 2])
with col_meta1:
    new_name = st.text_input("名称", value=draft["name"], key=f"{key}_name")
with col_meta2:
    new_mode = st.selectbox(
        "播放模式",
        options=[None] + list(schemas.PLAY_MODES),
        index=([None] + list(schemas.PLAY_MODES)).index(draft["play_mode"])
        if draft["play_mode"] in (None, *schemas.PLAY_MODES) else 0,
        format_func=lambda m: "（继承全局）" if m is None else {
            "single": "单曲（播一次后停止）",
            "sequence": "顺序播放",
            "shuffle": "随机播放",
            "repeat_one": "单曲循环",
            "repeat_all": "列表循环",
        }.get(m, m),
        key=f"{key}_mode",
    )

draft["name"] = new_name
draft["play_mode"] = new_mode


# ---- file browser ----
st.divider()
st.markdown("### 🎵 添加曲目")

audio_files = client.list_audio_files()
if not audio_files:
    st.info("音频目录里还没有文件，请先去 **设置** 页面检查音频目录配置。")
else:
    col_filter, col_select = st.columns([1, 3])
    with col_filter:
        filter_text = st.text_input("过滤", value="", placeholder="输入文件名关键字")
    candidates = [
        f for f in audio_files
        if not filter_text or filter_text.lower() in f["path"].lower()
    ]
    with col_select:
        paths_to_add = st.multiselect(
            "选择要加入播放列表的文件",
            options=[f["path"] for f in candidates],
            format_func=lambda p: next(
                (f"{c['title']}  ·  {c['path']}"
                 for c in candidates if c["path"] == p),
                p,
            ),
        )
    if st.button("➕ 加入列表末尾", disabled=not paths_to_add, type="primary"):
        existing_paths = {it["path"] for it in draft["items"]}
        added = 0
        for p in paths_to_add:
            if p in existing_paths:
                continue
            meta = next((c for c in candidates if c["path"] == p), None)
            title = meta["title"] if meta else p
            draft["items"].append({"path": p, "title": title, "missing": False})
            added += 1
        if added:
            st.success(f"已加入 {added} 首")
            st.rerun()


# ---- current items ----
st.divider()
st.markdown(f"### 曲目（共 {len(draft['items'])} 首）")

if not draft["items"]:
    st.caption("还没有曲目，请从上方添加。")
else:
    # Render each row with delete + rename
    for idx in reversed(range(len(draft["items"]))):
        item = draft["items"][idx]
        missing_badge = "❌ 缺失" if item.get("missing") else "✅"
        cols = st.columns([0.5, 5, 2, 0.8])
        with cols[0]:
            st.markdown(f"**{idx + 1}**")
        with cols[1]:
            new_title = st.text_input(
                "标题",
                value=item.get("title", ""),
                key=f"{key}_title_{idx}",
                label_visibility="collapsed",
            )
            item["title"] = new_title
            st.caption(f"`{item.get('path', '')}` {missing_badge}")
        with cols[2]:
            new_path = st.text_input(
                "路径",
                value=item.get("path", ""),
                key=f"{key}_path_{idx}",
                label_visibility="collapsed",
            )
            item["path"] = new_path
        with cols[3]:
            if st.button("🗑", key=f"{key}_del_{idx}"):
                draft["items"].pop(idx)
                st.rerun()


# ---- reorder via sortables (fallback to data_editor if missing) ----
st.divider()
st.markdown("### 排序（拖拽）")

try:
    from streamlit_sortables import sort_items

    if draft["items"]:
        labels = [f"{i+1:02d}. {it.get('title','')}  —  {it.get('path','')}"
                  for i, it in enumerate(draft["items"])]
        new_labels = sort_items(labels, direction="vertical")
        if new_labels and new_labels != labels:
            # Rebuild items in new order by matching labels back to items
            by_label = {labels[i]: draft["items"][i] for i in range(len(labels))}
            draft["items"] = [by_label[lab] for lab in new_labels]
            st.rerun()
except ImportError:
    st.caption("未安装 streamlit-sortables，跳过拖拽功能。可在 requirements.txt 启用。")
    # Fallback: numeric order column via data_editor
    if draft["items"]:
        import pandas as pd
        df = pd.DataFrame([
            {"order": i + 1, "title": it.get("title", ""), "path": it.get("path", "")}
            for i, it in enumerate(draft["items"])
        ])
        edited = st.data_editor(df, hide_index=True, use_container_width=True,
                                 key=f"{key}_order_editor")
        # Reorder items based on edited order column
        edited_sorted = edited.sort_values("order")
        draft["items"] = [
            {"path": row["path"], "title": row["title"], "missing": False}
            for _, row in edited_sorted.iterrows()
        ]


# ---- save / delete buttons ----
st.divider()
col_save, col_del, col_dup = st.columns([2, 2, 2])

with col_save:
    if st.button("💾 保存", type="primary", use_container_width=True):
        new_pl = schemas.make_playlist(
            selected,
            draft["name"],
            items=[{"path": it["path"], "title": it["title"], "missing": it.get("missing", False)}
                   for it in draft["items"]],
            play_mode=draft["play_mode"],
        )
        # Preserve original created_at
        new_pl["created_at"] = playlist.get("created_at") or new_pl["created_at"]
        try:
            client.save_playlist(new_pl)
            st.success(f"已保存 {selected}")
        except Exception as e:
            st.error(f"保存失败：{e}")

with col_del:
    confirm = st.checkbox("确认删除", key=f"{key}_confirm_del")
    if st.button("🗑 删除列表", disabled=not confirm, use_container_width=True):
        try:
            client.delete_playlist(selected)
            st.success(f"已删除 {selected}")
            st.session_state.pop(key, None)
            st.rerun()
        except Exception as e:
            st.error(f"删除失败：{e}")

with col_dup:
    new_dup_id = st.text_input("复制为 ID", value="",
                                key=f"{key}_dup_id",
                                placeholder="新 ID").strip()
    if st.button("📋 复制", disabled=not new_dup_id, use_container_width=True):
        if not schemas.validate_playlist_id(new_dup_id):
            st.error("ID 格式不合法")
        elif _load(new_dup_id) is not None:
            st.error(f"已存在：{new_dup_id}")
        else:
            dup = schemas.make_playlist(
                new_dup_id,
                f"{draft['name']}（副本）",
                items=[{"path": it["path"], "title": it["title"], "missing": it.get("missing", False)}
                       for it in draft["items"]],
                play_mode=draft["play_mode"],
            )
            try:
                client.save_playlist(dup)
                st.success(f"已复制为 {new_dup_id}")
            except Exception as e:
                st.error(f"复制失败：{e}")