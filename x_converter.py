# -*- coding: utf-8 -*-
"""
X 视频转换工具 (X Video Converter)
把 HEVC(H.265) 等格式的视频转换成 X / Twitter 可上传、Windows 可直接播放的格式:
  H.264 High Profile + AAC + yuv420p + 30fps + faststart

两种模式:
  完整版  - 不限时长, 整段转换 (适合 X Premium 账号 / 电脑播放)
  分段版  - 自动切成每段 <=139 秒 (适合 X 普通账号 140 秒上限), 长视频自动分多段

命令行用法 (无界面, 便于批处理/自检):
  XConverter.exe input.mp4 output.mp4 [--split]
内嵌 ffmpeg.exe (打包时通过 PyInstaller --add-binary 带入), 用户无需另装任何东西。
"""

import os
import re
import subprocess
import sys
import threading

SEGMENT_SECONDS = 139  # X 普通账号上限 140 秒, 留 1 秒余量

VF = (
    "fps=30,"
    "scale=w='min(1920,iw)':h='min(1200,ih)':force_original_aspect_ratio=decrease:"
    "in_range=auto:out_range=tv,"
    "scale=w='trunc(iw/2)*2':h='trunc(ih/2)*2',"
    "format=yuv420p"
)

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def find_ffmpeg():
    """定位 ffmpeg: 优先打包内嵌的, 其次程序同目录, 最后系统 PATH。"""
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(os.path.join(meipass, "ffmpeg.exe"))
        candidates.append(os.path.join(meipass, "ffmpeg"))
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
    else:
        exe_dir = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(exe_dir, "ffmpeg.exe"))
    candidates.append(os.path.join(exe_dir, "ffmpeg"))
    for c in candidates:
        if os.path.isfile(c):
            return c
    return "ffmpeg"


FFMPEG = find_ffmpeg()


def _app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


LOG_PATH = os.path.join(_app_dir(), "XConverter.log")


def _file_log(msg):
    """诊断日志: 所有 log_cb 内容都带时间戳追加到 EXE 旁的
    XConverter.log, 下载失败时把这个文件发来就能定位到哪一步。"""
    try:
        import datetime
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (ts, msg))
    except Exception:
        pass


def _tee_log(log_cb):
    """把 log_cb 再分一路写进诊断日志文件 (界面/命令行照常显示)。"""
    orig = log_cb or (lambda t: None)

    def tee(t):
        try:
            orig(t)
        except Exception:
            pass
        _file_log(t)

    return tee


def get_duration(path):
    """用 ffmpeg -i 解析时长(秒), 失败返回 0。"""
    try:
        p = subprocess.run(
            [FFMPEG, "-hide_banner", "-i", path],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW, timeout=60,
        )
        text = p.stderr.decode("utf-8", "ignore")
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:
        pass
    return 0.0


def build_cmd(src, dst, start=None, length=None):
    cmd = [FFMPEG, "-hide_banner", "-y"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", src]
    if length is not None:
        cmd += ["-t", f"{length:.3f}"]
    cmd += [
        "-vf", VF,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-profile:v", "high", "-level", "4.2",
        "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart",
        "-progress", "pipe:1", "-nostats",
        dst,
    ]
    return cmd


class Converter:
    """带进度回调与取消能力的转换器。progress_cb(frac, text), log_cb(text)。"""

    def __init__(self, progress_cb=None, log_cb=None):
        self.progress_cb = progress_cb or (lambda f, t: None)
        self.log_cb = _tee_log(log_cb)
        self._proc = None
        self._cancelled = False

    def cancel(self):
        self._cancelled = True
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:
                pass

    def _run_one(self, src, dst, base_frac, span_frac, start=None, length=None,
                 seg_total=None):
        """跑一段 ffmpeg, 把本段进度映射到 [base_frac, base_frac+span_frac]。"""
        total = seg_total
        if total is None:
            total = length if length is not None else get_duration(src)
            if start:
                full = get_duration(src)
                total = min(total, max(0.0, full - start)) if full else total
        cmd = build_cmd(src, dst, start=start, length=length)
        self.log_cb("开始转换: " + os.path.basename(dst))
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW, bufsize=1,
        )
        tail = []
        for raw in self._proc.stdout:
            if self._cancelled:
                break
            line = raw.decode("utf-8", "ignore").strip()
            tail.append(line)
            if line.startswith("out_time_ms=") and total:
                try:
                    done = int(line.split("=", 1)[1]) / 1_000_000.0
                    frac = base_frac + span_frac * min(1.0, done / total)
                    self.progress_cb(frac, f"{frac * 100:.0f}%")
                except ValueError:
                    pass
        self._proc.wait()
        if self._cancelled:
            raise RuntimeError("已取消")
        if self._proc.returncode != 0:
            err = "\n".join(tail[-15:])
            raise RuntimeError("ffmpeg 转换失败:\n" + err)
        self.progress_cb(base_frac + span_frac, "")

    def convert(self, src, mode):
        """mode: 'full' 完整版 | 'split' 每段<=139秒。返回输出文件列表。"""
        self._cancelled = False
        duration = get_duration(src)
        if duration <= 0:
            raise RuntimeError("读不到视频时长, 文件可能已损坏或格式不受支持")
        folder = os.path.dirname(os.path.abspath(src))
        stem = os.path.splitext(os.path.basename(src))[0]
        outputs = []
        if mode == "split" and duration > SEGMENT_SECONDS + 1:
            n = int((duration + SEGMENT_SECONDS - 1) // SEGMENT_SECONDS)
            self.log_cb(f"视频共 {duration:.0f} 秒, 将切成 {n} 段 (每段≤{SEGMENT_SECONDS}秒)")
            for i in range(n):
                start = i * SEGMENT_SECONDS
                length = min(SEGMENT_SECONDS, duration - start)
                dst = os.path.join(folder, f"{stem}_forX_part{i + 1}.mp4")
                self._run_one(src, dst, i / n, 1 / n,
                              start=start, length=length, seg_total=length)
                outputs.append(dst)
        else:
            dst = os.path.join(folder, f"{stem}_forX.mp4")
            self._run_one(src, dst, 0.0, 1.0, seg_total=duration)
            outputs.append(dst)
        return outputs


# ---------------------------------------------------------------- 链接下载
def find_tool(name):
    """定位内嵌工具 (yt-dlp.exe / gallery-dl.exe / qjs.exe)。"""
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(os.path.join(meipass, name))
        candidates.append(os.path.join(meipass, "dlbin", name))
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
    else:
        exe_dir = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(exe_dir, name))
    candidates.append(os.path.join(exe_dir, "dlbin", name))
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


class Downloader:
    """粘贴链接下载无水印视频/图文: yt-dlp 为主引擎, gallery-dl 兜底图文。
    progress_cb(frac, text), log_cb(text)。"""

    def __init__(self, progress_cb=None, log_cb=None):
        self.progress_cb = progress_cb or (lambda f, t: None)
        self.log_cb = _tee_log(log_cb)
        self._proc = None
        self._cancelled = False

    def cancel(self):
        self._cancelled = True
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:
                pass

    def _pump(self, proc, parse_line):
        tail = []
        for raw in proc.stdout:
            if self._cancelled:
                break
            line = raw.decode("utf-8", "ignore").strip()
            if line:
                tail.append(line)
                parse_line(line)
        proc.wait()
        return tail

    def _run_ytdlp(self, url, outdir, quality, cookies):
        ytdlp = find_tool("yt-dlp.exe")
        if not ytdlp:
            raise RuntimeError("缺少 yt-dlp 引擎文件")
        cmd = [ytdlp, "--newline", "--no-playlist", "--no-mtime",
               "--socket-timeout", "30", "--retries", "5",
               "--ffmpeg-location", FFMPEG,
               "-o", os.path.join(outdir, "%(title).80B [%(id)s].%(ext)s")]
        qjs = find_tool("qjs.exe")
        if qjs:
            cmd += ["--js-runtimes", f"quickjs:{qjs}"]
        if quality == "audio":
            cmd += ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
        elif quality == "1080":
            cmd += ["-f", "bv*[height<=1080]+ba/b[height<=1080]",
                    "--merge-output-format", "mp4"]
        elif quality == "720":
            cmd += ["-f", "bv*[height<=720]+ba/b[height<=720]",
                    "--merge-output-format", "mp4"]
        else:
            cmd += ["-f", "bv*+ba/b", "--merge-output-format", "mp4"]
        if cookies and cookies != "none":
            cmd += ["--cookies-from-browser", cookies]
        cmd.append(url)
        self.log_cb("开始下载 (yt-dlp)…")
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW, bufsize=1)

        def parse(line):
            m = re.search(r"\[download\]\s+(\d+(?:\.\d+)?)%", line)
            if m:
                pct = float(m.group(1))
                self.progress_cb(pct / 100.0, f"下载中 {pct:.0f}%")
            elif "Downloading" in line or "[download] Destination" in line:
                self.log_cb(line[:110])

        tail = self._pump(self._proc, parse)
        if self._cancelled:
            raise RuntimeError("已取消")
        if self._proc.returncode != 0:
            err = "\n".join(tail[-8:])
            raise RuntimeError("yt-dlp 下载失败:\n" + err)
        self.progress_cb(1.0, "完成")

    def _run_gallery(self, url, outdir):
        gdl = find_tool("gallery-dl.exe")
        if not gdl:
            raise RuntimeError("缺少 gallery-dl 引擎文件")
        cmd = [gdl, "-d", outdir, url]
        self.log_cb("改用图文引擎 (gallery-dl) 重试…")
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW, bufsize=1)
        count = [0]

        def parse(line):
            if line.startswith("#") or ".jpg" in line or ".png" in line \
                    or ".mp4" in line or ".webp" in line:
                count[0] += 1
                self.log_cb(f"已保存 {count[0]} 个文件…")

        tail = self._pump(self._proc, parse)
        if self._cancelled:
            raise RuntimeError("已取消")
        if self._proc.returncode != 0:
            err = "\n".join(tail[-8:])
            raise RuntimeError("gallery-dl 下载失败:\n" + err)
        self.progress_cb(1.0, "完成")

    # 这些站点 gallery-dl 没有解析器, 失败就直接报错, 不做无谓兜底
    VIDEO_ONLY_HOSTS = ("bilibili.com", "youtube.com", "youtu.be", "vimeo.com",
                        "youku.com", "iqiyi.com", "twitch.tv", "douyin.com",
                        "tiktok.com")

    # ---------------------------------------------------------- 快手自研解析
    KS_HOSTS = ("kuaishou.com", "chenzhongtech.com", "gifshow.com")

    @staticmethod
    def _ks_find_video(html):
        """从快手分享页/作品页 HTML 里找无水印视频直链, 找不到返回 None。"""
        import json as _json
        if not html:
            return None
        m = re.search(r'"mainMvUrls"\s*:\s*(\[.*?\])\s*[,}]', html, re.S)
        if m:
            try:
                arr = _json.loads(m.group(1))
                for it in arr:
                    if isinstance(it, dict) and it.get("url"):
                        return it["url"]
            except Exception:
                pass
        cands = re.findall(r'https?://[^"\']+?\.mp4[^"\']*', html)
        cands = [c.replace("\\u002F", "/").replace("\\/", "/")
                 for c in cands]
        cands = [c for c in cands if "kwimgs" in c or "kwaicdn" in c]
        if cands:
            return cands[0]
        mv = re.search(r'<video[^>]+src="(https?://[^"]+)"', html)
        if mv:
            return mv.group(1)
        return None

    def _run_kuaishou(self, url, outdir):
        """快手分享页自研解析: 分享页的 INIT_STATE 里带 mainMvUrls
        直链 (kwimgs CDN, 无水印原文件), yt-dlp 无此站解析器, 故自研。
        多 UA + cookie 会话 + 桌面页兜底, 提高在真实网络下的成功率。"""
        import http.cookiejar
        import json as _json
        import urllib.request

        uas = [
            ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
             "AppleWebKit/605.1.15 Mobile/15E148"),
            ("Mozilla/5.0 (Linux; Android 13; Pixel 7) "
             "AppleWebKit/537.36 Chrome/120.0 Mobile Safari/537.36"),
            ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
             "AppleWebKit/537.36 Chrome/120.0 Safari/537.36"),
        ]
        self.log_cb("快手链接, 走自研解析…")
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar))
        html = None
        final_url = url
        last_err = ""
        pages = [url]
        for ua in uas:
            for page in list(pages):
                try:
                    req = urllib.request.Request(
                        page, headers={"User-Agent": ua})
                    with opener.open(req, timeout=40) as resp:
                        final_url = resp.geturl()
                        html = resp.read().decode("utf-8", "ignore")
                except Exception as e:
                    last_err = str(e)
                    html = None
                if html and self._ks_find_video(html):
                    break
                # 拿到 photoId 后补一个桌面作品页作为兜底来源
                mm = re.search(r"photoId=([A-Za-z0-9]+)", final_url)
                if mm:
                    desk = ("https://www.kuaishou.com/short-video/" +
                            mm.group(1))
                    if desk not in pages:
                        pages.append(desk)
            if html and self._ks_find_video(html):
                break
        video_url = self._ks_find_video(html) if html else None
        if not video_url:
            raise RuntimeError(
                "快手解析失败: 分享页里没找到视频直链(可能该作品需要登录、"
                "已被删除/限制, 或快手改版)。最后错误: " +
                (last_err or "页面无视频数据") + "; 落地页: " + final_url)
        pid = ""
        for pat in (r"photoId=([A-Za-z0-9]+)", r"/fw/photo/([A-Za-z0-9]+)",
                    r"/short-video/([A-Za-z0-9]+)"):
            mm = re.search(pat, final_url) or re.search(pat, url)
            if mm:
                pid = mm.group(1)
                break
        ua = uas[0]
        caption = ""
        mc = re.search(r'"caption"\s*:\s*"([^"]{0,60})"', html)
        if mc:
            caption = re.sub(r'[\\/:*?"<>|\r\n]+', "_", mc.group(1)).strip()
        name = (caption + "_" if caption else "") + \
            ("kuaishou_" + pid if pid else "kuaishou_video") + ".mp4"
        dst = self._safe_dst(
            outdir, name,
            ("kuaishou_" + pid if pid else "kuaishou_video") + ".mp4")
        self.log_cb("已解析到视频直链, 开始下载…")
        req2 = urllib.request.Request(
            video_url, headers={"User-Agent": ua,
                                "Referer": "https://v.m.chenzhongtech.com/"})
        with urllib.request.urlopen(req2, timeout=60) as resp, \
                open(dst, "wb") as f:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while True:
                if self._cancelled:
                    f.close()
                    try:
                        os.remove(dst)
                    except Exception:
                        pass
                    raise RuntimeError("已取消")
                chunk = resp.read(1 << 18)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total:
                    self.progress_cb(done / total, f"下载中 {done / total * 100:.0f}%")
        self.progress_cb(1.0, "完成")
        return dst

    # ---------------------------------------------------------- B 站自研解析
    BL_HOSTS = ("bilibili.com", "b23.tv", "bili2233.cn")

    _BL_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/120.0.0.0 Safari/537.36")

    def _bl_headers(self, referer=None, dest="empty"):
        """仿真 Chrome 请求头 + buvid3 设备 cookie。
        B 站风控认设备: 裸 Python 请求没有 buvid3, 会被直接 412
        (浏览器能打开是因为它自带这些)。buvid3 只生成一次、存在
        EXE 旁的 .xconv_buvid 里, 以后一直用同一个, 像台固定设备。"""
        import json as _json
        import time as _time
        import uuid as _uuid
        buvid_file = os.path.join(_app_dir(), ".xconv_buvid")
        data = {}
        try:
            with open(buvid_file, "r", encoding="utf-8") as f:
                data = _json.load(f)
        except Exception:
            data = {}
        if not data.get("buvid3"):
            data = {"buvid3": str(_uuid.uuid4()).upper(),
                    "b_nut": int(_time.time())}
            try:
                with open(buvid_file, "w", encoding="utf-8") as f:
                    _json.dump(data, f)
            except Exception:
                pass
        h = {
            "User-Agent": self._BL_UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Cookie": "buvid3=%s; b_nut=%s" % (data["buvid3"],
                                               data["b_nut"]),
            "Sec-Ch-Ua": '"Chromium";v="120", "Not_A Brand";v="99"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": dest,
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
        }
        if referer:
            h["Referer"] = referer
        return h

    def _bl_json(self, url):
        """B 站官方接口 (未登录可用, 大部分公开视频 720p/1080p)。
        返回解析后的 JSON dict, code 非 0 时抛 RuntimeError。"""
        import json as _json
        import urllib.request
        req = urllib.request.Request(
            url, headers=self._bl_headers(
                referer="https://www.bilibili.com/"))
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
        except Exception as e:
            raise RuntimeError("B站接口请求失败: " + str(e)[:160])
        try:
            data = _json.loads(raw.decode("utf-8", "ignore"))
        except Exception:
            raise RuntimeError("B站接口返回了非 JSON 内容 "
                               "(可能被风控拦截, HTTP 412)")
        if not isinstance(data, dict) or data.get("code") != 0:
            msg = data.get("message", "") if isinstance(data, dict) else ""
            raise RuntimeError("B站接口拒绝 (code %s: %s)" %
                               (data.get("code"), msg))
        return data["data"]

    def _bl_http_download(self, url, dst, referer, weight0, weight1):
        """带 Referer 的分段下载 (B 站 CDN 要求 Referer)。
        weight0..weight1 为本段在总进度中的占比。"""
        import urllib.request
        req = urllib.request.Request(
            url, headers=self._bl_headers(referer=referer, dest="video"))
        try:
            resp = urllib.request.urlopen(req, timeout=60)
        except Exception as e:
            raise RuntimeError("B站流下载失败: " + str(e)[:160])
        with resp, open(dst, "wb") as f:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while True:
                if self._cancelled:
                    raise RuntimeError("已取消")
                chunk = resp.read(1 << 18)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total:
                    frac = weight0 + (weight1 - weight0) * done / total
                    self.progress_cb(frac,
                                     f"下载中 {frac * 100:.0f}%")
        if os.path.getsize(dst) == 0:
            raise RuntimeError("B站流下载为空, CDN 可能拒绝了请求")

    @staticmethod
    def _safe_dst(outdir, name, fallback):
        """输出文件名含中文时, 在英文系统/Wine 下可能因 ANSI 代码页
        写不进文件; 先探一下, 不行就退回纯 ASCII 名, 别让整个
        下载失败。"""
        dst = os.path.join(outdir, name)
        existed = os.path.exists(dst)
        try:
            with open(dst, "ab"):
                pass
            if not existed:
                os.remove(dst)
            return dst
        except Exception:
            return os.path.join(outdir, fallback)

    def _run_bilibili(self, url, outdir, quality="best"):
        """B 站官方 API 自研解析: view 接口取 cid/title, playurl
        接口取 DASH 音视频流, ffmpeg 合并。失败时抛错, 由上层
        改走 yt-dlp 兜底。返回最终文件路径。"""
        import urllib.request
        self.log_cb("B站链接, 走自研解析…")
        # b23.tv 等短链先跟随跳转到真实视频页 (HEAD 不行时改 GET)
        # 注意: 最终页请求失败 (如 412 风控) 时 urllib 抛 HTTPError,
        # 但错误对象仍带最终 URL (含 BV 号), 一定要先取出来再继续
        for _method in ("HEAD", "GET"):
            try:
                req = urllib.request.Request(
                    url, headers=self._bl_headers(),
                    method=_method)
                with urllib.request.urlopen(req, timeout=20) as resp:
                    url = resp.geturl()
                break
            except Exception as e:
                final = getattr(e, "geturl", None)
                if callable(final):
                    try:
                        url = final()
                    except Exception:
                        pass
                if re.search(r"BV[0-9A-Za-z]{10}", url):
                    break
                continue
        m = re.search(r"BV[0-9A-Za-z]{10}", url)
        page = 1
        pm = re.search(r"[?&]p=(\d+)", url)
        if pm:
            page = max(1, int(pm.group(1)))
        bvid = m.group(0) if m else None
        aid = None
        if not bvid:
            am = re.search(r"/av(\d+)", url)
            if am:
                aid = am.group(1)
        if not bvid and not aid:
            raise RuntimeError("B站解析失败: 链接里找不到 BV 号或 av 号")
        self.log_cb("B站: 解析到 %s (第 %d P)" % (bvid or ("av" + aid), page))
        view_url = ("https://api.bilibili.com/x/web-interface/view?"
                    + ("bvid=" + bvid if bvid else "aid=" + aid))
        info = self._bl_json(view_url)
        title = re.sub(r'[\\/:*?"<>|\r\n]+', "_",
                       str(info.get("title") or "bilibili_video")).strip()
        title = title[:60] or "bilibili_video"
        pages = info.get("pages") or []
        cid = None
        for p in pages:
            if p.get("page") == page:
                cid = p.get("cid")
                break
        cid = cid or info.get("cid")
        if not cid:
            raise RuntimeError("B站解析失败: 未取到视频 cid")
        self.log_cb("B站: 《%s》 cid=%s" % (title, cid))
        referer = "https://www.bilibili.com/video/" + \
            (bvid or ("av" + str(aid)))
        qn = {"best": 80, "1080": 80, "720": 64, "audio": 0}.get(quality, 80)
        play_url = ("https://api.bilibili.com/x/player/playurl?"
                    "fnver=0&fnval=16&fourk=1"
                    + ("&bvid=" + bvid if bvid else "&aid=" + aid)
                    + "&cid=%s&qn=%d" % (cid, qn))
        play = self._bl_json(play_url)
        dash = play.get("dash") or {}
        videos = dash.get("video") or []
        audios = dash.get("audio") or []
        self.log_cb("B站: 取到 %d 条视频流 / %d 条音频流" %
                    (len(videos), len(audios)))
        cap = {"best": 1080, "1080": 1080, "720": 720}.get(quality, 1080)
        name = "%s_bilibili_%s%s" % (
            title, bvid or ("av" + str(aid)),
            ("_p%d" % page) if page > 1 else "")
        ext = ".mp3" if quality == "audio" else ".mp4"
        dst = self._safe_dst(
            outdir, name + ext,
            "bilibili_" + (bvid or ("av" + str(aid))) + ext)
        tmpdir = os.path.join(outdir, ".xconv_tmp")
        os.makedirs(tmpdir, exist_ok=True)
        try:
            if quality == "audio":
                if not audios:
                    raise RuntimeError("B站解析失败: 该视频无可用音频流")
                a = max(audios, key=lambda s: s.get("bandwidth", 0))
                a_path = os.path.join(tmpdir, "audio.m4s")
                self._bl_http_download(a["baseUrl"], a_path, referer, 0, 0.8)
                self._ffmpeg_merge_audio(a_path, dst)
                self.log_cb("B站: 音频已转 MP3 %s (%.1f MB)" %
                            (os.path.basename(dst),
                             os.path.getsize(dst) / 1e6))
                self.progress_cb(1.0, "完成")
                return dst
            if not videos:
                durl = play.get("durl") or []
                if durl and durl[0].get("url"):
                    self._bl_http_download(durl[0]["url"], dst, referer,
                                           0, 1.0)
                    self.log_cb("B站: 已下载 %s (%.1f MB)" %
                                (os.path.basename(dst),
                                 os.path.getsize(dst) / 1e6))
                    self.progress_cb(1.0, "完成")
                    return dst
                raise RuntimeError("B站解析失败: 未取到视频流 "
                                   "(可能需登录/大会员, 或被风控拦截)")
            # 无登录时 DASH 常被压到 480p；此时试 html5 MP4 直链，
            # 不登录常能拿到 720p（单文件含音画，不用合并）。只有当直链
            # 画质确实高于 DASH 时才改走直链，否则继续用 DASH。
            dash_max = max((v.get("height", 0) for v in videos),
                           default=0)
            if quality != "audio" and dash_max < 720:
                try:
                    h5_url = ("https://api.bilibili.com/x/player/playurl?"
                              "fnver=0&fnval=1&fourk=1"
                              + ("&bvid=" + bvid if bvid else "&aid=" + aid)
                              + "&cid=%s&qn=80&platform=html5&high_quality=1"
                              % cid)
                    h5 = self._bl_json(h5_url)
                    h5_durl = h5.get("durl") or []
                    h5_qn = h5.get("quality") or 0
                    h5_h = {112: 1080, 80: 1080, 64: 720, 32: 480,
                            16: 360, 6: 240}.get(h5_qn, 0)
                    if h5_durl and h5_durl[0].get("url") and h5_h > dash_max:
                        self.log_cb("B站: html5 直链 %dp，高于 DASH %dp，"
                                    "改走直链…" % (h5_h, dash_max))
                        segs = []
                        for i, s in enumerate(h5_durl):
                            p = os.path.join(tmpdir, "seg%d.mp4" % i)
                            self._bl_http_download(
                                s["url"], p, referer, 0, 1.0)
                            segs.append(p)
                        if len(segs) == 1:
                            os.replace(segs[0], dst)
                        else:
                            self._ffmpeg_concat(segs, dst)
                        self.log_cb("B站: 已下载 %s (%.1f MB)" %
                                    (os.path.basename(dst),
                                     os.path.getsize(dst) / 1e6))
                        self.progress_cb(1.0, "完成")
                        return dst
                except Exception as e:
                    self.log_cb("B站: html5 直链不可用 (%s)，继续用 DASH…"
                                % str(e)[:100])
            ok_v = [v for v in videos
                    if v.get("height", 9999) <= cap and v.get("baseUrl")]
            # 同分辨率下优先 avc (H.264): 下出来的文件 Windows/X
            # 能直接播; hevc (hvc1) 还得再转一次, 只当没得选时用
            def _vkey(s):
                codecs = str(s.get("codecs", ""))
                return (s.get("height", 0),
                        1 if codecs.startswith("avc") else 0,
                        s.get("bandwidth", 0))
            v = max(ok_v or videos, key=_vkey)
            self.log_cb("B站: 选流 id=%s %s %sx%s 带宽 %d" %
                        (v.get("id"), v.get("codecs"),
                         v.get("width"), v.get("height"),
                         v.get("bandwidth", 0)))
            v_path = os.path.join(tmpdir, "video.m4s")
            self._bl_http_download(v["baseUrl"], v_path, referer, 0, 0.7)
            self.log_cb("B站: 视频流已下 %.1f MB" %
                        (os.path.getsize(v_path) / 1e6))
            if audios:
                a = max(audios, key=lambda s: s.get("bandwidth", 0))
                a_path = os.path.join(tmpdir, "audio.m4s")
                self._bl_http_download(a["baseUrl"], a_path, referer,
                                       0.7, 0.95)
                self.log_cb("B站: 音频流已下 %.1f MB" %
                            (os.path.getsize(a_path) / 1e6))
                self._ffmpeg_merge(v_path, a_path, dst)
                self.log_cb("B站: 合并完成 %s (%.1f MB)" %
                            (os.path.basename(dst),
                             os.path.getsize(dst) / 1e6))
            else:
                os.replace(v_path, dst)
                self.log_cb("B站: 无音频, 仅保存视频流 %s" %
                            os.path.basename(dst))
            self.progress_cb(1.0, "完成")
            return dst
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def _ffmpeg_merge(self, video, audio, dst):
        import subprocess
        cmd = [FFMPEG, "-y", "-i", video, "-i", audio,
               "-c", "copy", dst]
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW)
        _, err = self._proc.communicate()
        if self._proc.returncode != 0:
            raise RuntimeError("ffmpeg 合并失败: "
                               + err.decode("utf-8", "ignore")[-200:])

    def _ffmpeg_concat(self, files, dst):
        """多段 MP4/FLV 首尾拼成一个文件 (html5 直链长视频可能分段)。"""
        import subprocess
        lst = os.path.join(os.path.dirname(files[0]), "concat.txt")
        with open(lst, "w", encoding="utf-8") as f:
            for p in files:
                f.write("file '%s'\n" % p.replace("'", "'\\''"))
        cmd = [FFMPEG, "-y", "-f", "concat", "-safe", "0",
               "-i", lst, "-c", "copy", dst]
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW)
        _, err = self._proc.communicate()
        if self._proc.returncode != 0:
            raise RuntimeError("ffmpeg 拼接失败: "
                               + err.decode("utf-8", "ignore")[-200:])

    def _ffmpeg_merge_audio(self, audio, dst):
        import subprocess
        cmd = [FFMPEG, "-y", "-i", audio, "-vn",
               "-c:a", "libmp3lame", "-q:a", "2", dst]
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW)
        _, err = self._proc.communicate()
        if self._proc.returncode != 0:
            raise RuntimeError("ffmpeg 转音频失败: "
                               + err.decode("utf-8", "ignore")[-200:])

    @staticmethod
    def _snapshot(outdir):
        found = set()
        for root, _dirs, files in os.walk(outdir):
            for fn in files:
                found.add(os.path.join(root, fn))
        return found

    def download(self, url, outdir, quality="best", cookies="none"):
        """快手走自研解析; 其余先 yt-dlp, 失败且不是纯视频站时 gallery-dl 兜底图文。
        返回 (engine, [本次新增的文件路径])。"""
        self._cancelled = False
        url = (url or "").strip()
        # 用户常整段粘贴 App 的分享文案(带文字/口令), 自动摘出链接
        m = re.search(r"https?://[^\s\"'<>，。；、）】]+", url)
        if m:
            url = m.group(0)
        if not url or not re.match(r"https?://", url):
            raise RuntimeError("请先粘贴一个有效的 http(s) 链接")
        os.makedirs(outdir, exist_ok=True)
        before = self._snapshot(outdir)
        host0 = re.sub(r"^https?://", "", url).split("/")[0].lower()
        if any(host0 == h or host0.endswith("." + h) for h in self.KS_HOSTS):
            self._run_kuaishou(url, outdir)
            return "kuaishou", sorted(self._snapshot(outdir) - before)
        bl_hint = ""
        if any(host0 == h or host0.endswith("." + h) for h in self.BL_HOSTS):
            if cookies and cookies != "none":
                # 登录态交给 yt-dlp：它带真实 SESSDATA，能拿到 1080p；
                # 自研解析无登录态，部分视频（如影视剪辑）只能到 480p，
                # 先跑自研解析反而会把画质锁死在 480p
                self.log_cb("已选浏览器登录态，改走 yt-dlp（可拿满画质）…")
            else:
                try:
                    dst = self._run_bilibili(url, outdir, quality)
                    return "bilibili", [dst]
                except RuntimeError as e:
                    # 自研解析失败 (风控/登录可见/接口改版) 时改走 yt-dlp,
                    # yt-dlp 可配合浏览器 cookies 选项处理登录可见视频
                    # 完整错误先写进诊断日志 (界面上只显示截短版)
                    self.log_cb("B站自研解析失败详情: " + str(e))
                    self.log_cb("B站自研解析失败, 改走 yt-dlp… ("
                                + str(e)[:120] + ")")
                    if "412" in str(e):
                        # B 站对这台电脑的裸请求做了风控拦截: 浏览器能开是
                        # 因为它带登录态, 下面的 yt-dlp 兜底也一样会被拦,
                        # 只有借用浏览器登录态才绕得过去
                        bl_hint = ("\n\n提示: B站对裸请求做了风控拦截 (412)。"
                                   "请在下载页打开「借用浏览器登录态」选 "
                                   "chrome（需先在 Chrome 里登录过 B站）"
                                   "再重试，或过段时间再试。")
        try:
            self._run_ytdlp(url, outdir, quality, cookies)
            return "yt-dlp", sorted(self._snapshot(outdir) - before)
        except RuntimeError as e:
            if self._cancelled or str(e) == "已取消" or quality == "audio":
                raise
            host = re.sub(r"^https?://", "", url).split("/")[0].lower()
            if any(host == h or host.endswith("." + h)
                   for h in self.VIDEO_ONLY_HOSTS):
                if bl_hint:
                    raise RuntimeError(str(e) + bl_hint)
                raise
            self.log_cb("视频引擎没下成, 试试图文引擎…")
            try:
                self._run_gallery(url, outdir)
            except RuntimeError:
                raise RuntimeError(str(e) + "\n(图文引擎也未能处理这个链接)"
                                   + bl_hint)
            return "gallery-dl", sorted(self._snapshot(outdir) - before)


# ---------------------------------------------------------------- 文字提取
class Extractor:
    """从视频提取文字: whisper.cpp 语音转文字 + RapidOCR 画面字幕。
    语音模型 (ggml-small-q5_1, 182MB) 不打包进 EXE: 首次使用时自动从
    hf-mirror 下载到 EXE 同目录的 models 文件夹, 之后离线可用;
    也可手动把模型文件放到该位置。"""

    MODEL_NAME = "ggml-small-q5_1.bin"
    MODEL_URL = ("https://hf-mirror.com/ggerganov/whisper.cpp"
                 "/resolve/main/ggml-small-q5_1.bin")
    VIDEO_EXTS = (".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".ts")

    def __init__(self, progress_cb=None, log_cb=None):
        self.progress_cb = progress_cb or (lambda f, t: None)
        self.log_cb = _tee_log(log_cb)
        self._proc = None
        self._cancelled = False
        self._ocr_engine = None

    def cancel(self):
        self._cancelled = True
        if self._proc:
            try:
                self._proc.kill()
            except Exception:
                pass

    # ------------------------------------------------------------ 模型管理
    def _app_dir(self):
        if getattr(sys, "frozen", False):
            return os.path.dirname(sys.executable)
        return os.path.dirname(os.path.abspath(__file__))

    def model_path(self):
        for cand in (os.path.join(self._app_dir(), "models", self.MODEL_NAME),
                     os.path.join(self._app_dir(), self.MODEL_NAME)):
            if os.path.isfile(cand) and os.path.getsize(cand) > 100_000_000:
                return cand
        return None

    def ensure_model(self):
        found = self.model_path()
        if found:
            return found
        import urllib.request
        target_dir = os.path.join(self._app_dir(), "models")
        os.makedirs(target_dir, exist_ok=True)
        target = os.path.join(target_dir, self.MODEL_NAME)
        tmp = target + ".part"
        self.log_cb("首次使用语音转文字, 正在下载识别模型 (182MB, 只需一次)…")
        req = urllib.request.Request(self.MODEL_URL,
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as resp, \
                open(tmp, "wb") as f:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while True:
                if self._cancelled:
                    raise RuntimeError("已取消")
                chunk = resp.read(1 << 18)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total:
                    self.progress_cb(done / total,
                                     f"下载识别模型 {done / total * 100:.0f}%")
        os.replace(tmp, target)
        return target

    # ------------------------------------------------------------ 语音转写
    def transcribe(self, video):
        cli = find_tool("whisper-cli.exe")
        if not cli:
            raise RuntimeError("缺少 whisper-cli 引擎文件")
        model = self.ensure_model()
        ffmpeg = find_ffmpeg()
        import tempfile
        tmpdir = tempfile.mkdtemp(prefix="xconv_asr_")
        wav = os.path.join(tmpdir, "audio.wav")
        self.log_cb("正在提取音频…")
        subprocess.run([ffmpeg, "-y", "-v", "error", "-i", video,
                        "-vn", "-ac", "1", "-ar", "16000", wav],
                       check=True, stdin=subprocess.DEVNULL)
        outbase = os.path.join(tmpdir, "out")
        cmd = [cli, "-m", model, "-f", wav, "-l", "auto",
               "-t", str(os.cpu_count() or 4), "-pp",
               "-otxt", "-of", outbase, "-nt"]
        self.log_cb("正在语音转文字 (长视频需要几分钟)…")
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, text=True, errors="replace")
        for line in self._proc.stdout:
            if self._cancelled:
                self.cancel()
                raise RuntimeError("已取消")
            m = re.search(r"progress\s*=\s*(\d+)%", line)
            if m:
                pct = min(100, int(m.group(1)))
                self.progress_cb(pct / 100.0, f"语音转文字 {pct}%")
        self._proc.wait()
        self._proc = None
        txt_path = outbase + ".txt"
        if not os.path.isfile(txt_path):
            raise RuntimeError("语音转写没有产出结果")
        with open(txt_path, encoding="utf-8", errors="replace") as f:
            text = f.read().strip()
        return text

    # ------------------------------------------------------------ 画面 OCR
    def _find_ocr_worker(self):
        """OCR 在独立的 AI 增强包里 (ocr_pack/ocr_worker.exe), 与主程序同级
        放置; 主程序本身不带 OCR 库, 保持体积小。"""
        app_dir = self._app_dir()
        candidates = [
            os.path.join(app_dir, "ocr_pack", "ocr_worker.exe"),
            os.path.join(app_dir, "ocr_worker.exe"),
        ]
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(os.path.join(meipass, "ocr_worker.exe"))
        for cand in candidates:
            if os.path.isfile(cand):
                return cand
        return None

    def ocr(self, video):
        worker = self._find_ocr_worker()
        if not worker:
            raise RuntimeError("未安装 AI 增强包 (ocr_pack), 已跳过画面文字。"
                               "把 ocr_pack 文件夹放在 XConverter.exe 旁边即可启用")
        ffmpeg = find_ffmpeg()
        import json as _json
        import tempfile
        tmpdir = tempfile.mkdtemp(prefix="xconv_ocr_")
        pattern = os.path.join(tmpdir, "f%05d.jpg")
        self.log_cb("正在抽帧识别画面文字…")
        subprocess.run([ffmpeg, "-y", "-v", "error", "-i", video,
                        "-vf", "fps=1/2,scale=min(960\\,iw):-2", pattern],
                       check=True, stdin=subprocess.DEVNULL)
        out_json = os.path.join(tmpdir, "ocr.json")
        self._proc = subprocess.Popen(
            [worker, tmpdir, out_json],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, text=True, errors="replace")
        for line in self._proc.stdout:
            if self._cancelled:
                self.cancel()
                raise RuntimeError("已取消")
            m = re.search(r"progress\s+(\d+)/(\d+)", line)
            if m:
                done, total = int(m.group(1)), int(m.group(2))
                if total:
                    self.progress_cb(done / total,
                                     f"画面文字识别 {done}/{total} 帧")
        self._proc.wait()
        self._proc = None
        if not os.path.isfile(out_json):
            raise RuntimeError("OCR 增强包没有产出结果")
        with open(out_json, encoding="utf-8") as f:
            results = _json.load(f)
        events = []
        last_seen = {}
        for i, item in enumerate(results):
            ts = i * 2
            for line, conf in item.get("lines", []):
                line = (line or "").strip()
                try:
                    conf = float(conf)
                except (TypeError, ValueError):
                    continue
                if conf < 0.6 or len(line) < 2:
                    continue
                if ts - last_seen.get(line, -10 ** 9) >= 8:
                    events.append((ts, line))
                last_seen[line] = ts
        return events

    # ------------------------------------------------------------ 汇总
    @staticmethod
    def _fmt_ts(sec):
        return f"{sec // 60:02d}:{sec % 60:02d}"

    def extract(self, video):
        """返回格式化全文, 并写到视频同目录的 <名>_文字.txt。
        任何一节失败都只写进文本, 绝不向上抛(取消除外)——提取失败
        绝不能连累已经下好的视频被误报成下载失败。"""
        parts = []
        try:
            speech = self.transcribe(video)
            parts.append("【语音转文字】\n" + (speech or "(未识别到语音)"))
        except Exception as e:
            if str(e) == "已取消":
                raise RuntimeError("已取消")
            parts.append("【语音转文字】\n(失败: " + str(e) + ")")
        try:
            events = self.ocr(video)
            lines = [f"[{self._fmt_ts(ts)}] {line}" for ts, line in events]
            parts.append("【画面文字】\n" + ("\n".join(lines) if lines
                                             else "(未识别到画面文字)"))
        except Exception as e:
            if str(e) == "已取消":
                raise RuntimeError("已取消")
            if str(e).startswith("未安装"):
                parts.append("【画面文字】\n(" + str(e) + ")")
            else:
                parts.append("【画面文字】\n(失败: " + str(e) + ")")
        full = "\n\n".join(parts)
        out_txt = os.path.splitext(video)[0] + "_文字.txt"
        try:
            with open(out_txt, "w", encoding="utf-8") as f:
                f.write(full)
        except Exception:
            pass
        self.progress_cb(1.0, "文字提取完成")
        return full


# ---------------------------------------------------------------- CLI 模式
# 旧日志名 XConverter_cli.log 已统一为顶部的 XConverter.log (LOG_PATH),
# 界面和命令行共用同一份诊断日志


def _out(msg):
    """CLI 输出: 先试 stdout(窗口版 EXE 无控制台时 stdout 可能为 None),
    避免中文在 cp1252 控制台下崩溃。
    注意: 经 log_cb 发出的内容已由 _tee_log 写进诊断日志, 这里只打印
    不再重复写; 会话开头/结尾与完整 traceback 由 cli() 直接记日志。"""
    try:
        print(msg, flush=True)
    except Exception:
        pass


def cli(argv):
    import traceback
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    _file_log("===== CLI 启动: " + " ".join(argv) + " =====")
    args = [a for a in argv if not a.startswith("--")]
    split = "--split" in argv
    if "--download" in argv:
        if not args:
            _out("usage: XConverter --download URL [OUTDIR] [--quality=best|1080|720|audio] [--cookies=chrome|edge|firefox]")
            return 2
        url = args[0]
        outdir = args[1] if len(args) > 1 else os.getcwd()
        quality, cookies = "best", "none"
        for a in argv:
            if a.startswith("--quality="):
                quality = a.split("=", 1)[1]
            elif a.startswith("--cookies="):
                cookies = a.split("=", 1)[1]
        dl = Downloader(progress_cb=lambda f, t: _out(f"progress {f:.3f}"),
                        log_cb=lambda t: _out(t))
        try:
            engine, _files = dl.download(url, outdir, quality, cookies)
            _out(f"download ok via {engine} -> {outdir}")
            _file_log("CLI 下载成功 via " + engine)
            return 0
        except Exception as e:
            _out("download failed: " + str(e))
            _file_log("CLI 下载失败: " + str(e))
            _file_log(traceback.format_exc())
            return 1
    if "--extract" in argv:
        if not args:
            _out("usage: XConverter --extract VIDEO")
            return 2
        ext = Extractor(progress_cb=lambda f, t: _out(f"progress {f:.3f}"),
                        log_cb=lambda t: _out(t))
        try:
            text = ext.extract(args[0])
            _out("----- extracted text -----")
            _out(text)
            return 0
        except Exception as e:
            _out("extract failed: " + str(e))
            _file_log("CLI 提取失败: " + str(e))
            _file_log(traceback.format_exc())
            return 1
    if len(args) < 2:
        _out("usage: XConverter input output [--split]")
        return 2
    src, dst = args[0], args[1]
    conv = Converter(progress_cb=lambda f, t: _out(f"progress {f:.3f}"),
                     log_cb=lambda t: _out(t))
    dur = get_duration(src)
    _out(f"duration={dur:.2f} ffmpeg={FFMPEG}")
    if split and dur > SEGMENT_SECONDS + 1:
        n = int((dur + SEGMENT_SECONDS - 1) // SEGMENT_SECONDS)
        folder = os.path.dirname(os.path.abspath(dst))
        stem = os.path.splitext(os.path.basename(dst))[0]
        for i in range(n):
            start = i * SEGMENT_SECONDS
            length = min(SEGMENT_SECONDS, dur - start)
            out = os.path.join(folder, f"{stem}_part{i + 1}.mp4")
            conv._run_one(src, out, i / n, 1 / n, start=start,
                          length=length, seg_total=length)
            _out("wrote " + out)
    else:
        conv._run_one(src, dst, 0.0, 1.0, seg_total=dur)
        _out("wrote " + dst)
    return 0


# ---------------------------------------------------------------- GUI 模式
def _hide_console():
    """打包为控制台子系统(兼容性最好)时, GUI 启动后隐藏跟随的控制台窗口。"""
    if os.name == "nt":
        try:
            import ctypes
            hwnd = ctypes.windll.kernel32.GetConsoleWindow()
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
        except Exception:
            pass


def gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    _hide_console()
    root = tk.Tk()
    root.title("X 视频转换工具")
    root.geometry("560x800")
    root.resizable(False, False)

    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True, padx=6, pady=6)
    tab_conv = tk.Frame(nb)
    tab_dl = tk.Frame(nb)
    nb.add(tab_conv, text="  视频转换  ")
    nb.add(tab_dl, text="  链接下载  ")

    # ---------------------------------------------------------- 页签1: 转换
    state = {"path": None, "conv": None, "running": False}

    tk.Label(tab_conv, text="X / Twitter 视频转换工具",
             font=("Microsoft YaHei", 15, "bold")).pack(pady=(14, 4))
    tk.Label(tab_conv, text="HEVC 等格式 → H.264 + AAC, 电脑可播、X 可传, 长视频也支持",
             font=("Microsoft YaHei", 9), fg="#555").pack()

    file_label = tk.Label(tab_conv, text="尚未选择视频",
                          font=("Microsoft YaHei", 10), fg="#888", wraplength=480)
    file_label.pack(pady=(16, 6))

    def choose():
        p = filedialog.askopenfilename(
            title="选择视频文件",
            filetypes=[("视频文件", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.ts"),
                       ("所有文件", "*.*")])
        if p:
            state["path"] = p
            dur = get_duration(p)
            extra = f"  (时长 {dur / 60:.0f}分{dur % 60:.0f}秒)" if dur else ""
            file_label.config(text=os.path.basename(p) + extra, fg="#222")
            status.config(text="已选择文件, 选好模式后点开始转换")

    tk.Button(tab_conv, text="选择视频", font=("Microsoft YaHei", 11), width=20,
              command=choose).pack(pady=2)

    mode = tk.StringVar(value="full")
    frame = tk.Frame(tab_conv)
    frame.pack(pady=(10, 2))
    tk.Radiobutton(frame, text="完整版 (不限时长, Premium/电脑播放)",
                   variable=mode, value="full",
                   font=("Microsoft YaHei", 10)).pack(anchor="w")
    tk.Radiobutton(frame, text="分段版 (自动切成每段≤140秒, 普通账号)",
                   variable=mode, value="split",
                   font=("Microsoft YaHei", 10)).pack(anchor="w")

    bar = ttk.Progressbar(tab_conv, length=440, mode="determinate", maximum=1000)
    bar.pack(pady=(12, 4))
    status = tk.Label(tab_conv, text="请先选择需要转换的视频",
                      font=("Microsoft YaHei", 9), fg="#666", wraplength=480)
    status.pack(pady=2)

    btns = tk.Frame(tab_conv)
    btns.pack(pady=8)
    start_btn = tk.Button(btns, text="开始转换", font=("Microsoft YaHei", 12, "bold"),
                          bg="#1D9BF0", fg="white", width=16, height=1)
    start_btn.pack(side="left", padx=6)
    cancel_btn = tk.Button(btns, text="取消", font=("Microsoft YaHei", 11),
                           width=8, state="disabled")
    cancel_btn.pack(side="left", padx=6)

    def set_status(text):
        root.after(0, lambda: status.config(text=text))

    def set_progress(frac, text):
        def upd():
            bar["value"] = int(frac * 1000)
            if text:
                status.config(text=f"转换中… {text}")
        root.after(0, upd)

    def work():
        conv = Converter(progress_cb=set_progress, log_cb=set_status)
        state["conv"] = conv
        try:
            outs = conv.convert(state["path"], mode.get())
            def done():
                state["running"] = False
                start_btn.config(state="normal")
                cancel_btn.config(state="disabled")
                bar["value"] = 1000
                status.config(text="转换完成!")
                messagebox.showinfo("完成", "已生成:\n" + "\n".join(outs))
            root.after(0, done)
        except Exception as e:
            # 注意: except 块结束时会删掉 e, 闭包必须用默认参数先接住,
            # 否则 root.after 延后执行时报 NameError, 错误框弹不出来
            def fail(err=e):
                state["running"] = False
                start_btn.config(state="normal")
                cancel_btn.config(state="disabled")
                status.config(text="转换失败或已取消")
                if str(err) != "已取消":
                    messagebox.showerror("错误", str(err))
            root.after(0, fail)

    def start():
        if not state["path"]:
            messagebox.showwarning("提示", "请先选择视频文件")
            return
        if state["running"]:
            return
        state["running"] = True
        start_btn.config(state="disabled")
        cancel_btn.config(state="normal")
        bar["value"] = 0
        threading.Thread(target=work, daemon=True).start()

    def cancel():
        if state["conv"]:
            state["conv"].cancel()

    start_btn.config(command=start)
    cancel_btn.config(command=cancel)

    # ---------------------------------------------------------- 页签2: 下载
    dstate = {"dl": None, "ext": None, "running": False}
    tk.Label(tab_dl, text="无水印链接下载",
             font=("Microsoft YaHei", 15, "bold")).pack(pady=(14, 4))
    tk.Label(tab_dl,
             text="粘贴视频/图文链接即可下载原画质无水印文件\n支持 B站、抖音、快手、微博、小红书、YouTube、X、TikTok、Instagram 等 1000+ 站点",
             font=("Microsoft YaHei", 9), fg="#555", justify="center").pack()

    tk.Label(tab_dl, text="链接:", font=("Microsoft YaHei", 10)).pack(
        anchor="w", padx=30, pady=(14, 2))
    url_row = tk.Frame(tab_dl)
    url_row.pack(padx=30, fill="x")
    url_var = tk.StringVar()
    url_entry = tk.Entry(url_row, textvariable=url_var, font=("Microsoft YaHei", 10))
    url_entry.pack(side="left", fill="x", expand=True)

    def paste_url():
        try:
            url_var.set(root.clipboard_get().strip())
        except Exception:
            pass

    tk.Button(url_row, text="粘贴", font=("Microsoft YaHei", 10),
              command=paste_url).pack(side="left", padx=(6, 0))

    tk.Label(tab_dl, text="保存到:", font=("Microsoft YaHei", 10)).pack(
        anchor="w", padx=30, pady=(10, 2))
    dir_row = tk.Frame(tab_dl)
    dir_row.pack(padx=30, fill="x")
    default_dir = os.path.join(os.path.expanduser("~"), "Downloads")
    dir_var = tk.StringVar(value=default_dir)
    tk.Entry(dir_row, textvariable=dir_var, font=("Microsoft YaHei", 10)).pack(
        side="left", fill="x", expand=True)

    def choose_dir():
        p = filedialog.askdirectory(title="选择保存文件夹")
        if p:
            dir_var.set(p)

    tk.Button(dir_row, text="选择文件夹", font=("Microsoft YaHei", 10),
              command=choose_dir).pack(side="left", padx=(6, 0))

    q_frame = tk.Frame(tab_dl)
    q_frame.pack(pady=(12, 0))
    tk.Label(q_frame, text="画质:", font=("Microsoft YaHei", 10)).pack(side="left")
    quality = tk.StringVar(value="best")
    for txt, val in [("最佳", "best"), ("≤1080p", "1080"),
                     ("≤720p", "720"), ("仅音频 MP3", "audio")]:
        tk.Radiobutton(q_frame, text=txt, variable=quality, value=val,
                       font=("Microsoft YaHei", 10)).pack(side="left")

    c_frame = tk.Frame(tab_dl)
    c_frame.pack(pady=(6, 0))
    tk.Label(c_frame, text="借用浏览器登录态:", font=("Microsoft YaHei", 10)).pack(side="left")
    cookies = tk.StringVar(value="none")
    cookie_menu = tk.OptionMenu(c_frame, cookies, "none", "chrome", "edge", "firefox")
    cookie_menu.config(font=("Microsoft YaHei", 10))
    cookie_menu.pack(side="left")
    tk.Label(tab_dl, text="一般不用开; 抖音/小红书/Instagram 下载失败时, 先在对应浏览器里登录该网站再选它重试",
             font=("Microsoft YaHei", 8), fg="#888", wraplength=480).pack(pady=(2, 0))

    extract_var = tk.BooleanVar(value=True)
    tk.Checkbutton(tab_dl, text="下载后自动提取文字 (语音转文字 + 画面字幕)",
                   variable=extract_var, font=("Microsoft YaHei", 10)).pack(
                       pady=(8, 0))
    tk.Label(tab_dl, text="语音模型首次使用需联网下载 182MB (只下一次); 画面文字需要 AI 增强包 ocr_pack 放在 EXE 旁边; 结果显示在下方并存为 _文字.txt",
             font=("Microsoft YaHei", 8), fg="#888", wraplength=480).pack(pady=(2, 0))

    dbar = ttk.Progressbar(tab_dl, length=440, mode="determinate", maximum=1000)
    dbar.pack(pady=(12, 4))
    dstatus = tk.Label(tab_dl, text="粘贴链接后点开始下载",
                       font=("Microsoft YaHei", 9), fg="#666", wraplength=480)
    dstatus.pack(pady=2)

    dbtns = tk.Frame(tab_dl)
    dbtns.pack(pady=8)
    dstart_btn = tk.Button(dbtns, text="开始下载", font=("Microsoft YaHei", 12, "bold"),
                           bg="#1D9BF0", fg="white", width=16, height=1)
    dstart_btn.pack(side="left", padx=6)
    dcancel_btn = tk.Button(dbtns, text="取消", font=("Microsoft YaHei", 11),
                            width=8, state="disabled")
    dcancel_btn.pack(side="left", padx=6)

    def open_log():
        try:
            if os.path.exists(LOG_PATH):
                os.startfile(LOG_PATH)
            else:
                d_set_status("暂无日志文件 (还未跑过下载/转换)")
        except Exception as e:
            d_set_status("打不开日志: " + str(e)[:100])

    tk.Button(dbtns, text="查看日志", font=("Microsoft YaHei", 11),
              width=8, command=open_log).pack(side="left", padx=6)

    from tkinter import scrolledtext
    tk.Label(tab_dl, text="提取的文字:", font=("Microsoft YaHei", 10)).pack(
        anchor="w", padx=30, pady=(4, 2))
    text_box = scrolledtext.ScrolledText(tab_dl, height=10, width=62,
                                         font=("Microsoft YaHei", 9),
                                         wrap="word")
    text_box.pack(padx=30, fill="x")

    def copy_text():
        content = text_box.get("1.0", "end").strip()
        if content:
            root.clipboard_clear()
            root.clipboard_append(content)
            d_set_status("文字已复制到剪贴板")

    tk.Button(tab_dl, text="复制文字", font=("Microsoft YaHei", 10),
              command=copy_text).pack(pady=(4, 0))

    def set_text_box(content):
        def upd():
            text_box.delete("1.0", "end")
            text_box.insert("1.0", content)
        root.after(0, upd)

    def d_set_status(text):
        root.after(0, lambda: dstatus.config(text=text))

    def d_set_progress(frac, text):
        def upd():
            dbar["value"] = int(frac * 1000)
            if text:
                dstatus.config(text=text)
        root.after(0, upd)

    def d_work():
        _file_log("===== GUI 下载: %s 画质=%s 登录态=%s =====" % (
            url_var.get(), quality.get(), cookies.get()))
        dl = Downloader(progress_cb=d_set_progress, log_cb=d_set_status)
        dstate["dl"] = dl
        try:
            engine, new_files = dl.download(url_var.get(), dir_var.get(),
                                            quality.get(), cookies.get())
            note = ""
            if extract_var.get():
                videos = [f for f in new_files
                          if f.lower().endswith(Extractor.VIDEO_EXTS)]
                if videos:
                    # 提取是附加步骤: 失败只提示, 绝不把下载报成失败
                    try:
                        ext = Extractor(progress_cb=d_set_progress,
                                        log_cb=d_set_status)
                        dstate["ext"] = ext
                        chunks = []
                        for vf in videos:
                            d_set_status("正在提取文字: " +
                                         os.path.basename(vf))
                            chunks.append("===== " + os.path.basename(vf) +
                                          " =====\n" + ext.extract(vf))
                        set_text_box("\n\n".join(chunks))
                        note = "\n文字已提取 (见下方文本框, 视频旁有 _文字.txt)"
                    except Exception as e:
                        if str(e) == "已取消":
                            raise
                        note = "\n(视频已下载成功; 文字提取失败: " + \
                            str(e)[:200] + ")"
            def done():
                dstate["running"] = False
                dstart_btn.config(state="normal")
                dcancel_btn.config(state="disabled")
                dbar["value"] = 1000
                dstatus.config(text="下载完成!")
                messagebox.showinfo("完成", f"已下载到:\n{dir_var.get()}" + note)
            root.after(0, done)
        except Exception as e:
            # 注意: except 块结束时会删掉 e, 闭包必须用默认参数先接住,
            # 否则 root.after 延后执行时报 NameError, 错误框弹不出来
            def fail(err=e):
                dstate["running"] = False
                dstart_btn.config(state="normal")
                dcancel_btn.config(state="disabled")
                dstatus.config(text="下载失败或已取消")
                if str(err) != "已取消":
                    messagebox.showerror(
                        "错误", str(err)[:800] +
                        "\n\n详细步骤见 EXE 旁的 XConverter.log\n"
                        "(点「查看日志」可打开, 发给作者定位问题)")
            root.after(0, fail)

    def d_start():
        if dstate["running"]:
            return
        if not url_var.get().strip():
            messagebox.showwarning("提示", "请先粘贴链接")
            return
        dstate["running"] = True
        dstart_btn.config(state="disabled")
        dcancel_btn.config(state="normal")
        dbar["value"] = 0
        threading.Thread(target=d_work, daemon=True).start()

    def d_cancel():
        if dstate["dl"]:
            dstate["dl"].cancel()
        if dstate.get("ext"):
            dstate["ext"].cancel()

    dstart_btn.config(command=d_start)
    dcancel_btn.config(command=d_cancel)

    if os.environ.get("XCONV_TAB") == "2":
        nb.select(tab_dl)
    root.mainloop()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        sys.exit(cli(sys.argv[1:]))
    gui()
