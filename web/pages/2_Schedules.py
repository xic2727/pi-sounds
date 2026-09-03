"""Schedules management page: cron-based timed playlist triggers.

Layout:
* Sidebar: list of schedules + "新建" button.
* Main: editable table of schedules, cron preview ("next 5 fires"),
  enable/disable toggles, delete buttons.

Writes go through ``client.save_schedules`` (single shared-lock write).
The daemon hot-reloads schedules.json when its mtime changes, so changes
take effect on the next 5-second tick without restart.
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd
import streamlit as st

from daemon.scheduler import validate_cron, next_fire
from shared import schemas
from web import client


st.set_page_config(page_title="定时任务 · pi-sounds", page_icon="⏰", layout="wide")


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

schedules_doc = client.load_schedules()
schedules = schedules_doc.get("schedules") or []

with st.sidebar:
    st.markdown("### 定时任务")
    st.caption(f"当前共 **{len(schedules)}** 个任务")
    if st.button("🔄 刷新", use_container_width=True):
        st.rerun()
    st.divider()
    st.caption(
        "cron 格式：`分 时 日 月 周`\n\n"
        "例：`0 7 * * *` 每天 7:00\n"
        "`*/15 * * * *` 每 15 分钟\n"
        "`30 8 * * 1-5` 工作日 8:30"
    )


# ---------------------------------------------------------------------------
# New schedule form
# ---------------------------------------------------------------------------

st.title("⏰ 定时任务")

st.markdown("### 新建定时任务")
playlists = client.list_playlists()
playlist_options = ["— 选择播放列表 —"] + playlists

with st.form("new_schedule_form", clear_on_submit=True):
    cols = st.columns([2, 2, 2])
    with cols[0]:
        new_name = st.text_input("名称", value="", placeholder="例：每天早 7 点")
        new_cron = st.text_input("cron 表达式", value="0 7 * * *")
    with cols[1]:
        new_playlist = st.selectbox(
            "触发时播放",
            options=playlist_options,
            index=0,
        )
        new_enabled = st.checkbox("启用", value=True)
    with cols[2]:
        new_mode = st.selectbox(
            "覆盖播放模式（可选）",
            options=[None] + list(schemas.PLAY_MODES),
            index=0,
            format_func=lambda m: "（不覆盖）" if m is None else m,
        )
        new_volume = st.number_input(
            "覆盖音量 0-100（可选）",
            min_value=0, max_value=100, value=0,
            help="填 0 表示不覆盖",
        )
        new_priority = st.number_input("优先级", min_value=0, max_value=10, value=0)
        new_if_busy = st.selectbox("忙碌时", options=["preempt", "skip"], index=0)

    # Cron validation feedback
    ok, msg = validate_cron(new_cron)
    if not ok:
        st.error(f"cron 无效：{msg}")
    else:
        nexts = []
        cur = datetime.now()
        for _ in range(5):
            nxt = next_fire(new_cron, cur)
            if nxt is None:
                break
            nexts.append(nxt.strftime("%Y-%m-%d %H:%M:%S"))
            cur = nxt
        st.caption("接下来 5 次：" + ("，".join(nexts) if nexts else "无"))

    ok_submit = st.form_submit_button("➕ 添加", type="primary")
    if ok_submit:
        if not new_name.strip():
            st.error("请填写名称")
        elif new_playlist == "— 选择播放列表 —":
            st.error("请选择目标播放列表")
        elif not ok:
            st.error("cron 表达式无效")
        else:
            sch = schemas.make_schedule(
                name=new_name.strip(),
                cron=new_cron.strip(),
                playlist_id=new_playlist,
                enabled=bool(new_enabled),
                volume=int(new_volume) if int(new_volume) > 0 else None,
                play_mode=new_mode,
                priority=int(new_priority),
                if_busy=new_if_busy,
            )
            schedules.append(sch)
            try:
                client.save_schedules({"version": 1, "schedules": schedules})
                st.success(f"已添加：{sch['id']}")
                st.rerun()
            except Exception as e:
                st.error(f"添加失败：{e}")


# ---------------------------------------------------------------------------
# Existing schedules table
# ---------------------------------------------------------------------------

st.divider()
st.markdown(f"### 已有任务（{len(schedules)}）")

if not schedules:
    st.info("还没有定时任务，使用上方表单创建一个。")
    st.stop()


# Build editable dataframe
rows = []
for s in schedules:
    rows.append({
        "id": s.get("id", ""),
        "name": s.get("name", ""),
        "cron": s.get("cron", ""),
        "playlist_id": s.get("playlist_id", ""),
        "enabled": bool(s.get("enabled", True)),
        "volume": int(s.get("volume") or 0),
        "play_mode": s.get("play_mode") or "",
        "priority": int(s.get("priority", 0)),
        "if_busy": s.get("if_busy", "preempt"),
        "last_run": s.get("last_run") or "",
        "last_result": s.get("last_result") or "",
    })
df = pd.DataFrame(rows)


edited = st.data_editor(
    df,
    column_config={
        "id": st.column_config.TextColumn("ID", disabled=True, width="small"),
        "enabled": st.column_config.CheckboxColumn("启用", width="small"),
        "priority": st.column_config.NumberColumn("优先级", min_value=0, max_value=10, step=1, width="small"),
        "volume": st.column_config.NumberColumn("音量", min_value=0, max_value=100, step=1, width="small"),
        "if_busy": st.column_config.SelectboxColumn(
            "忙碌时", options=["preempt", "skip"], width="small"
        ),
        "play_mode": st.column_config.SelectboxColumn(
            "模式", options=[""] + list(schemas.PLAY_MODES), width="small"
        ),
        "last_run": st.column_config.TextColumn("上次触发", disabled=True, width="medium"),
        "last_result": st.column_config.TextColumn("结果", disabled=True, width="small"),
    },
    column_order=[
        "name", "cron", "playlist_id", "enabled", "if_busy",
        "priority", "play_mode", "volume", "last_run", "last_result", "id",
    ],
    hide_index=True,
    use_container_width=True,
    key="schedules_editor",
)


# ---- cron preview + save button per row ----
st.markdown("**cron 校验与预览**")
invalid_count = 0
for i, row in edited.iterrows():
    cols = st.columns([3, 1, 1, 1])
    with cols[0]:
        ok, msg = validate_cron(row["cron"])
        if not ok:
            st.error(f"`{row['name']}` 的 cron `{row['cron']}` 无效：{msg}")
            invalid_count += 1
        else:
            cur = datetime.now()
            nexts = []
            for _ in range(3):
                nxt = next_fire(row["cron"], cur)
                if nxt is None:
                    break
                nexts.append(nxt.strftime("%m-%d %H:%M:%S"))
                cur = nxt
            st.caption(f"`{row['name']}` · 接下来：{' / '.join(nexts) if nexts else '无'}")
    with cols[1]:
        if st.button("🗑 删除", key=f"del_sched_{row['id']}"):
            new_schedules = [s for s in schedules if s.get("id") != row["id"]]
            try:
                client.save_schedules({"version": 1, "schedules": new_schedules})
                st.success("已删除")
                st.rerun()
            except Exception as e:
                st.error(f"删除失败：{e}")

# ---- save edited ----
st.divider()
c1, c2 = st.columns([3, 1])
with c1:
    st.caption("修改后点 **保存改动** 写入磁盘，守护进程会在 5 秒内热加载。")
with c2:
    if st.button("💾 保存改动", type="primary", use_container_width=True):
        new_schedules = []
        for _, row in edited.iterrows():
            # Find original to preserve created_at / etc
            orig = next((s for s in schedules if s.get("id") == row["id"]), {})
            new_schedules.append({
                **orig,
                "id": row["id"],
                "name": row["name"],
                "cron": row["cron"],
                "playlist_id": row["playlist_id"],
                "enabled": bool(row["enabled"]),
                "if_busy": row["if_busy"],
                "priority": int(row["priority"]),
                "play_mode": row["play_mode"] or None,
                "volume": int(row["volume"]) if int(row["volume"]) > 0 else None,
                "last_run": row["last_run"],
                "last_result": row["last_result"],
            })
        try:
            client.save_schedules({"version": 1, "schedules": new_schedules})
            st.success("已保存")
        except Exception as e:
            st.error(f"保存失败：{e}")