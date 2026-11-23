# XConverter — X 视频转换 & 无水印下载工具

一个单文件、免安装的 Windows 小工具，双击即用：

1. **视频转换**：把 Telegram、iPhone 等来源的 HEVC(H.265) 视频转成 Windows 能直接播放、X(Twitter) 能上传的格式（H.264 + AAC）。超过 X 免费版 140 秒限制的视频可自动分段。
2. **链接下载**：粘贴链接即可下载原画质无水印文件。支持 B站、抖音、快手、微博、小红书、YouTube、X、TikTok、Instagram 等 1000+ 站点；快手走自研解析，图文帖自动切换图文引擎。
3. **文字提取**（可选）：下载后自动提取视频里的文字——语音转文字（口播/台词）+ 画面字幕 OCR，结果显示在界面下方并存为 `视频名_文字.txt`。

## 下载与使用

到本仓库的 **Releases** 页面下载：

| 文件 | 说明 |
|---|---|
| `XConverter.exe` | 主程序（约 95MB），单文件免安装 |
| `ocr_pack.zip` | AI 增强包（可选）：解压后把 `ocr_pack` 文件夹放到 `XConverter.exe` 旁边，启用画面字幕 OCR |

- 首次使用语音转文字时，工具会自动下载一次语音识别模型（约 182MB，存放在 EXE 旁的 `models` 文件夹），之后离线可用。
- 本工具没有数字签名，首次运行时 Windows SmartScreen 可能拦截，点「更多信息」→「仍要运行」即可；浏览器下载大文件时提示「不常见下载」同理，点「保留」。

## 本地构建

```
pip install pyinstaller
# 将 ffmpeg.exe、yt-dlp.exe、gallery-dl.exe、qjs.exe、whisper-cli.exe 及相关 DLL
# 放到 dlbin/ 目录（ffmpeg.exe 放源码同级），然后：
pyinstaller --onefile --console --name XConverter \
  --add-binary "ffmpeg.exe;." --add-binary "dlbin/*;dlbin" x_converter.py
```

## 致谢 / 第三方组件

本工具打包了以下开源组件，各自遵循其原始许可证：

- [ffmpeg](https://ffmpeg.org/)（GPL/LGPL）
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)（Unlicense）
- [gallery-dl](https://github.com/mikf/gallery-dl)（GPL-2.0）
- [whisper.cpp](https://github.com/ggml-org/whisper.cpp)（MIT）及 Whisper small 模型
- [RapidOCR](https://github.com/RapidAI/RapidOCR)（Apache-2.0，增强包内）
- [QuickJS](https://github.com/quickjs-ng/quickjs-ng)

## 免责声明

请仅下载你有权保存的内容自用，尊重版权与各平台服务条款。
