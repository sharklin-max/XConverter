# XConverter 更新日志

记录每个版本修掉的 Bug、原因和解决办法。

## v4.7（2026-11-27）—— B站免登录 720p

**问题**：不登录时，B站自研解析（DASH）对影视剪辑等受限视频只给到 480p，
用户要求“任何能装 EXE 的电脑都能顺利下 B站”，不能依赖登录态。

**排查**：
- 老加密接口 `interface.bilibili.com/v2/playurl`（2019 年的 appkey + MD5 签名
  方案）实测已返回 403，接口废弃。
- App 端 `app.bilibili.com/x/v2/playurl` 实测 404，路径废弃（但 App 端
  `/x/v2/view` 仍可用，说明签名方式本身没错）。
- 官方文档（bilibili-API-collect）提到 `platform=html5` + `high_quality=1`
  可解锁更高画质；实测该组合返回 MP4 直链（durl），不登录即 720p。

**修复**：DASH 最高流低于 720p 时，自动改走 html5 MP4 直链
（`playurl?fnval=1&platform=html5&high_quality=1`），不登录拿到 720p 单文件
（实测 1280×720 H.264 + AAC），不用合并；多段时用 ffmpeg concat 拼接。
只有当直链画质确实高于 DASH 时才切换——普通视频 DASH 仍给 1080p，不受影响。

**已知限制**：受限视频的 1080p 是 B站服务器端规则，只给登录账号，任何
下载器不靠账号都拿不到；v4.7 免登录上限为 720p。想要 1080p 请开
「借用浏览器登录态」选 chrome，程序会自动改走 yt-dlp 拿满画质。

## v4.6（2026-11-27）—— 修 B站 HTTP 412

**问题**：用户电脑上自研解析和 yt-dlp 都被 B站报 HTTP 412（Precondition
Failed），而同一台电脑的浏览器能正常打开同一接口。

**原因**：浏览器自带 buvid3 设备 cookie + 完整请求头，裸 Python 请求没有，
被 B站风控拦掉。

**修复**：
- 自研解析带 buvid3（大写 UUID，只生成一次，存在 EXE 旁的 `.xconv_buvid`
  里）+ b_nut + 完整 Chrome 请求头（Sec-Ch-Ua / Sec-Fetch-*）。
- 修复后 view / playurl 接口立即恢复，取到 4 条视频流 / 3 条音频流。
- 412 时最终报错附带提示“开借用浏览器登录态选 chrome 再试”。
- 开了登录态时跳过自研解析、直接走 yt-dlp（带真实 SESSDATA 能拿 1080p），
  否则先跑自研解析会把画质锁死在 480p。

## v4.5（2026-11-27）—— 报错弹窗本身崩溃 + 诊断日志

**问题**：下载失败时用户永远只看到“下载失败”四个字，看不到原因；
用户贴来的 cmd 日志揭出：

```
File "x_converter.py", line 1301, in fail
NameError: cannot access free variable 'e' ...
```

**原因**：两个页签的 `except Exception as e:` 里都写了 `def fail():` 引用 `e`，
再用 `root.after(0, fail)` 延后执行。Python 在 except 块结束时删除 `e`
（PEP 3110），延后执行的 `fail()` 永远报 NameError。**这个 bug 从 v1 就在，
错误框从来没弹出来过**，所以之前所有 GUI 失败都无法定位。

**修复**：
- 两个 `fail()` 闭包都改成 `def fail(err=e):` 先把异常接住。
- 新增诊断日志：所有状态信息带时间戳写进 EXE 旁的 `XConverter.log`
  （B站记下解析到的 BV 号、标题、cid、取到的流、选流详情、每段大小、合并结果；
  失败时记完整报错 + 命令行记完整 traceback），下载页新增「查看日志」按钮，
  失败弹窗提示看日志。

**打包插曲**：Wine 突然不透传 HOME / USERPROFILE，PyInstaller 报
`Could not determine home directory`。改走 `build.bat`，经
`wine cmd /c "set USERPROFILE=C:\users\root && <bat 路径>"` 调用（cmd 内
set 变量后再跑 PyInstaller）。以后打包都走 `build.bat`。

## v4.4（2026-11-27）—— B站自研解析

**问题**：B站视频下载从 v2 到 v4.2 一直失败。B站当时只走 yt-dlp，且被列在
`VIDEO_ONLY_HOSTS` 里、无任何兜底；沙箱里 B站接口直接报 412。

**修复**：仿照快手写了 B站自研解析——解析短链拿 BV 号 → view 接口取标题/cid
→ playurl 接口取 DASH 流（同分辨率优先 avc/H.264，保证下出来的文件
Windows / X 能直接播）→ 下载音视频流 → ffmpeg 合并；失败时自动改走
yt-dlp（可配合浏览器登录态）。

**顺带修掉的 bug**：
- 短链解析：最终页请求失败（412）时 urllib 先抛 HTTPError，`resp.geturl()`
  没机会执行，BV 号丢失。改成异常里取 `e.geturl()` 再匹配 BV。
- 流下载的 `urlopen` 没包异常，HTTPError 直接冲出 `download()`，走不到
  yt-dlp 兜底。包成 RuntimeError 再抛出。
- 中文文件名在 Wine / 英文 Windows 下建不出来（ANSI 代码页 cp1252），
  加 `_safe_dst`：先探针写文件，写不出来就退回 ASCII 名
  （`bilibili_<BV>.mp4`）。

## v4.2（2026-11-23）—— 下载与提取解耦

**问题**：用户反馈快手“还是失败”，但视频其实已经下下来了。

**原因**：`extract()` 只捕获 RuntimeError；首次使用时 182MB 语音模型下载的
网络错误（URLError）逃了出来，GUI 把整件事报成“下载失败”。

**修复**：提取与下载彻底解耦——提取失败只提示、不翻转下载结果；快手解析
加多 UA + cookie + 桌面页兜底、报错带详情；整段 App 分享文案粘贴自动摘链接。

## v4.1（2026-11-23）—— 体积瘦身

**问题**：v4.0 单文件 189MB，超过聊天附件和远程存储上限，用户下不到。

**修复**：拆成主程序（95MB，含语音转文字）+ 可选的 `ocr_pack.zip`（99MB，
画面字幕 OCR）。无增强包时语音转写照常，OCR 栏友好提示未安装。

## v3.1（2026-11-23）—— X 下载回归

**问题**：X（Twitter）链接下载失败。

**原因**：`find_tool()` 没去 `_MEIPASS/dlbin/` 里找 yt-dlp，打包后找不到引擎。

**教训**：交付前必须在只有单个 EXE 的干净目录里测下载功能。
