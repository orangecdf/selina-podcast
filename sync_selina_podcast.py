#!/usr/bin/env python3
"""Selina英语播客自动同步（每12小时运行）：
1. 拉取最新视频列表（过滤付费/充电专属）2. 有新视频时只下载最新一期音频
3. 响度归一化 -12.2 LUFS 4. 更新 episodes.json + feed.xml（音频 bvid 稳定命名，永不重命名）
5. 推送到 GitHub；被挤出的旧期从仓库移除
无新视频时静默退出（stdout 为空）；有更新时输出摘要（cron no_agent 会推送到聊天）。

设计要点（2026-08 用户确认）：
- 音频文件名 = audio/<bvid>.m4a（稳定，不随 ep 编号滚动）
- 本地不保留音频：.gitignore 忽略 audio/*.m4a，push 用 git add -f 强制添加新音频
- 检测到新视频只下载最新一期，旧音频不拉回本地
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

BASE = Path("/root/selina-podcast")
AUDIO_DIR = BASE / "audio"
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


def prepare_yt_dlp_cookies() -> tuple[Path, bool]:
    """Make the existing Netscape rows acceptable to yt-dlp.

    The exported file has valid seven-column cookie rows but lacks the
    Netscape header, which yt-dlp requires.  Keep the source secret unchanged
    and create a temporary normalized copy instead.
    """
    text = COOKIES.read_text(encoding="utf-8")
    if text.lstrip().startswith("# Netscape HTTP Cookie File"):
        return COOKIES, False
    fd, name = tempfile.mkstemp(prefix="bili-cookies-", suffix=".txt")
    path = Path(name)
    try:
        with open(fd, "w", encoding="utf-8") as f:
            f.write("# Netscape HTTP Cookie File\n")
            f.write(text)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    path.chmod(0o600)
    return path, True


def download_to(bvid: str, dest: Path, cookie_file: Path, attempts=3) -> bool:
    for i in range(attempts):
        r = subprocess.run(
            ["yt-dlp", "--no-playlist", "-f", "ba/b", "-x", "--audio-format", "m4a",
             "--audio-quality", "0", "--no-warnings", "--retries", "5",
             "--cookies", str(cookie_file), "-o", str(dest),
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
    """生成 episodes.json + feed.xml（音频 URL = audio/<bvid>.m4a，ep 编号仅作逻辑排序）"""
    items_meta = []
    feed_items = []
    for i, v in enumerate(clean):
        ep = new_map[v["bvid"]]
        title = v["title"]
        bvid = v["bvid"]
        audio_url = f"{PAGES_BASE}/audio/{bvid}.m4a"
        f = AUDIO_DIR / f"{bvid}.m4a"
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
    # 新映射：clean[0]=最新 → ep030
    new_map = {v["bvid"]: f"ep{LIMIT - i:03d}" for i, v in enumerate(clean)}
    new_bvids = set(new_map.keys())

    old_items = json.loads((BASE / "episodes.json").read_text("utf-8")) if (BASE / "episodes.json").exists() else []
    old_bvids = set(it["link"].rsplit("/", 1)[-1] for it in old_items)

    if old_bvids == new_bvids:
        return  # 无变化，完全静默

    # 只下载最新一期（新出现的 bvid）；旧音频不拉回本地
    brand_new = [v for v in clean if v["bvid"] not in old_bvids]
    evicted = old_bvids - new_bvids  # 被挤出 30 期的旧 bvid（从仓库移除）

    # 1. 下载新视频音频 + 归一化
    AUDIO_DIR.mkdir(exist_ok=True)
    failed = []
    cookie_file, temporary_cookie = prepare_yt_dlp_cookies()
    try:
        for v in brand_new:
            dest = AUDIO_DIR / f"{v['bvid']}.m4a"
            if not download_to(v["bvid"], dest, cookie_file):
                failed.append(v["title"])
                continue
            if not normalize(dest):
                failed.append(v["title"])
    finally:
        if temporary_cookie:
            cookie_file.unlink(missing_ok=True)
    if failed:
        print(f"❌ 播客同步失败，下载/处理失败 {len(failed)} 个: {', '.join(failed[:5])}")
        print("（可能是B站cookies过期，需重新导出）")
        sys.exit(1)

    # 2. 更新 episodes.json + feed.xml
    write_feed(clean, new_map)

    # 3. git：添加新音频（-f 强制，因 .gitignore 忽略音频）+ 元数据；移除被挤出的旧音频
    git = ["git", "-C", str(BASE)]
    for v in brand_new:
        subprocess.run(git + ["add", "-f", f"audio/{v['bvid']}.m4a"], check=True)
    for bvid in evicted:
        subprocess.run(git + ["rm", "--cached", "--ignore-unmatch", f"audio/{bvid}.m4a"],
                       capture_output=True)
    subprocess.run(git + ["add", "episodes.json", "feed.xml"], check=True)
    subprocess.run(git + ["commit", "-m", f"自动同步播客（新增{len(brand_new)}期）"], capture_output=True)
    r = subprocess.run(git + ["push", "origin", BRANCH], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"❌ GitHub推送失败: {r.stderr[-200:]}")
        sys.exit(1)

    # 4. 推送成功后删除本地音频（保持磁盘干净，GitHub 已有）
    for f in AUDIO_DIR.glob("*.m4a"):
        f.unlink()

    # 输出摘要（推送到聊天）
    print(f"📻 {AUTHOR}播客已更新（新增 {len(brand_new)} 期）\n")
    for v in brand_new:
        print(f"- {v['title']}")
    if evicted:
        print(f"\n（移除 {len(evicted)} 期旧节目，保持最新{LIMIT}期）")
    print(f"\nRSS: {PAGES_BASE}/feed.xml")


if __name__ == "__main__":
    main()
