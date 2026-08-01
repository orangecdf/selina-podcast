#!/usr/bin/env python3
"""Selina英语播客自动同步（每12小时运行）：
1. 拉取最新视频列表（过滤付费/充电专属）2. 下载新增音频 3. 响度归一化 -12.2 LUFS
4. 重排 ep001-ep015（最新=ep015）5. 生成 episodes.json + feed.xml 6. 推送到 GitHub
无新视频时静默退出（stdout 为空）；有更新时输出摘要（cron no_agent 会推送到聊天）。
"""
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

BASE = Path("/root/selina-podcast")
EP_DIR = BASE / "episodes"
COOKIES = Path("/root/.hermes/secrets/bili_cookies.txt")
PAGES_BASE = "https://orangecdf.github.io/selina-podcast"
AUTHOR = "Selina英语"
MID = 660252251
LIMIT = 30
BRANCH = "gh-pages"
TZ8 = timezone(timedelta(hours=8))
WBI = Path("/root/.hermes/skills/media/bilibili-podcast/scripts/bili_wbi.py")


def get_vlist():
    """B站空间视频列表（WBI 签名 API）"""
    spec = __import__("importlib.util").util.spec_from_file_location("bili_wbi", str(WBI))
    bw = __import__("importlib.util").util.module_from_spec(spec)
    spec.loader.exec_module(bw)
    cookie = bw.load_cookie_header(str(COOKIES))
    return bw.space_videos(MID, cookie, 50, 1)


def download_to(bvid: str, dest: Path, attempts=3) -> bool:
    for i in range(attempts):
        r = subprocess.run(
            ["yt-dlp", "--no-playlist", "-f", "ba/b", "-x", "--audio-format", "m4a",
             "--audio-quality", "0", "--no-warnings", "--retries", "5",
             "--cookies", str(COOKIES), "-o", str(dest),
             f"https://www.bilibili.com/video/{bvid}"],
            capture_output=True, text=True, timeout=3600)
        if dest.exists() and dest.stat().st_size > 100_000:
            return True
        if i < attempts - 1:
            time.sleep(5)
    return False


def normalize(f: Path) -> bool:
    """两遍 loudnorm 归一化到 -12.2 LUFS"""
    r1 = subprocess.run(
        ["ffmpeg", "-i", str(f), "-af", "loudnorm=I=-12.2:TP=-1.5:LRA=11:print_format=json",
         "-f", "null", "-"], capture_output=True, text=True, timeout=3600)
    m = re.search(r"\{.*\}", r1.stderr, re.S)
    if not m:
        return False
    meas = json.loads(m.group(0))
    af = (f"loudnorm=I=-12.2:TP=-1.5:LRA=11:"
          f"measured_I={meas['input_i']}:measured_TP={meas['input_tp']}:"
          f"measured_LRA={meas['input_lra']}:measured_thresh={meas['input_thresh']}:"
          f"offset={meas['target_offset']}:linear=true")
    tmp = f.with_suffix(".norm.m4a")
    r2 = subprocess.run(
        ["ffmpeg", "-y", "-i", str(f), "-af", af, "-c:a", "aac", "-b:a", "128k",
         "-ar", "44100", str(tmp)], capture_output=True, text=True, timeout=3600)
    if r2.returncode != 0 or not tmp.exists():
        return False
    tmp.replace(f)
    return True


def sec_to_hms(secs: int) -> str:
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def parse_duration(length: str) -> int:
    parts = length.split(":")
    secs = 0
    for p in parts:
        secs = secs * 60 + int(p)
    return secs


def write_feed(clean: list, new_map: dict):
    """生成 episodes.json + feed.xml（保持现有格式：ep001→ep015 顺序）"""
    items_meta = []
    feed_items = []
    for i, v in enumerate(clean):
        ep = new_map[v["bvid"]]
        title = v["title"]
        bvid = v["bvid"]
        audio_url = f"{PAGES_BASE}/episodes/{ep}.m4a"
        f = EP_DIR / f"{ep}.m4a"
        size = f.stat().st_size if f.exists() else 0
        dur = parse_duration(v["length"])
        pub = datetime.fromtimestamp(v["created"], tz=TZ8)
        items_meta.append({
            "title": title,
            "desc": title,
            "link": f"https://www.bilibili.com/video/{bvid}",
            "audio_url": audio_url,
            "duration": sec_to_hms(dur),
            "size": size,
            "pubdate": pub.isoformat(timespec="seconds"),
            "ep": ep,
        })
        feed_items.append(f"""    <item>
      <title>{title}</title>
      <description>{title}</description>
      <link>https://www.bilibili.com/video/{bvid}</link>
      <guid>{audio_url}</guid>
      <pubDate>{format_datetime(pub)}</pubDate>
      <enclosure url="{audio_url}" type="audio/mp4" length="{size}"/>
      <itunes:duration>{sec_to_hms(dur)}</itunes:duration>
      <itunes:episodeType>full</itunes:episodeType>
    </item>""")

    # episodes.json：ep001→ep015 升序
    items_meta.sort(key=lambda x: x["ep"])
    json.dump(items_meta, open(BASE / "episodes.json", "w"), ensure_ascii=False, indent=2)

    last_build = format_datetime(datetime.now(timezone.utc))
    rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{AUTHOR}</title>
    <link>https://github.com/orangecdf/selina-podcast</link>
    <description>{AUTHOR}的英文学习播客</description>
    <language>zh-cn</language>
    <lastBuildDate>{last_build}</lastBuildDate>
    <atom:link href="{PAGES_BASE}/feed.xml" rel="self" type="application/rss+xml"/>
    <itunes:author>{AUTHOR}</itunes:author>
    <itunes:category text="Education"/>
    <itunes:explicit>false</itunes:explicit>
    <itunes:image>{PAGES_BASE}/images/selina_face.jpg</itunes:image>
    <image>
      <url>{PAGES_BASE}/images/selina_face.jpg</url>
      <title>{AUTHOR}</title>
      <link>https://github.com/orangecdf/selina-podcast</link>
    </image>
{chr(10).join(feed_items)}
  </channel>
</rss>
"""
    (BASE / "feed.xml").write_text(rss, "utf-8")


def main():
    vlist = get_vlist()
    clean = [v for v in vlist if v.get("elec_arc_badge") != "充电专属" and v.get("is_pay") != 1][:LIMIT]
    # 新映射：clean[0]=最新 → ep015
    new_map = {v["bvid"]: f"ep{LIMIT - i:03d}" for i, v in enumerate(clean)}
    new_bvids = set(new_map.keys())

    # 读现有 episodes.json
    old_items = json.loads((BASE / "episodes.json").read_text("utf-8")) if (BASE / "episodes.json").exists() else []
    old_map = {it["link"].rsplit("/", 1)[-1]: it["ep"] for it in old_items}  # bvid -> ep
    old_bvids = set(old_map.keys())

    if old_bvids == new_bvids:
        return  # 无变化，完全静默

    # 需要下载的新视频（不在现有文件里的）
    existing_files = {f.stem: f for f in EP_DIR.glob("*.m4a")}
    to_download = [v for v in clean if v["bvid"] not in old_bvids or old_map[v["bvid"]] not in existing_files]

    # 1. 下载 + 归一化到临时目录
    tmp_dir = BASE / ".stage"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir()
    failed = []
    for v in to_download:
        dest = tmp_dir / f"{v['bvid']}.m4a"
        if not download_to(v["bvid"], dest):
            failed.append(v["title"])
            continue
        if not normalize(dest):
            failed.append(v["title"])
    if failed:
        shutil.rmtree(tmp_dir)
        print(f"❌ 播客同步失败，下载/处理失败 {len(failed)} 个: {', '.join(failed[:5])}")
        print("（可能是B站cookies过期，需重新导出）")
        sys.exit(1)

    # 2. 重排：每个新 ep 的文件源 = 新下载的 或 现有文件
    for bvid, ep in new_map.items():
        src = tmp_dir / f"{bvid}.m4a"
        if not src.exists():
            src = EP_DIR / f"{old_map[bvid]}.m4a"
        shutil.copy2(src, tmp_dir / f"{ep}.m4a")

    # 3. 原子替换 episodes 目录内容（只移动 ep 命名文件）
    for f in EP_DIR.glob("*.m4a"):
        f.unlink()
    for f in tmp_dir.glob("ep*.m4a"):
        shutil.move(str(f), str(EP_DIR / f.name))
    shutil.rmtree(tmp_dir)

    # 4. 生成 episodes.json + feed.xml
    write_feed(clean, new_map)

    # 5. 推送 GitHub（remote 已带 token）
    subprocess.run(["git", "-C", str(BASE), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(BASE), "commit", "-m", f"自动同步播客（新增{len(to_download)}期）"],
                   capture_output=True)
    r = subprocess.run(["git", "-C", str(BASE), "push", "origin", BRANCH], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"❌ GitHub推送失败: {r.stderr[-200:]}")
        sys.exit(1)

    # 输出摘要（推送到聊天）
    print(f"📻 {AUTHOR}播客已更新（新增 {len(to_download)} 期）\n")
    for v in to_download:
        ep = new_map[v["bvid"]]
        f = EP_DIR / f"{ep}.m4a"
        print(f"- {v['title']} ({f.stat().st_size/1024/1024:.1f}MB)")
    print(f"\nRSS: {PAGES_BASE}/feed.xml")


if __name__ == "__main__":
    main()
