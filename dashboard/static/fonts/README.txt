Hermes Skill 控制台 — 本地字体
================================

这里的 woff2 是从 Fontsource（Google Fonts 的静态打包）下载的 Latin 子集，
由 style.css 顶部的 @font-face 直接引用，不再请求 fonts.googleapis.com。

文件
- inter-400/500/600/700.woff2         Inter（正文）
- pixelify-sans-400/600/700.woff2     Pixelify Sans（标题/像素展示）
- vt323-400.woff2                     VT323（HUD 数字/代码）
- LICENSE-*.txt                       SIL Open Font License 1.1 原文

来源
https://cdn.jsdelivr.net/fontsource/fonts/inter@latest/latin-400-normal.woff2
https://cdn.jsdelivr.net/fontsource/fonts/pixelify-sans@latest/latin-400-normal.woff2
https://cdn.jsdelivr.net/fontsource/fonts/vt323@latest/latin-400-normal.woff2
（其余字重同理，包名 @fontsource/<family>）

中文
上述字体只覆盖 Latin。中文按 style.css 的字体栈回退到系统字体：
Noto Sans SC → Microsoft YaHei / PingFang SC，不产生网络请求。
如需完全一致的跨平台中文观感，可另行自托管 Noto Sans SC 子集，
但全量中文字体体积较大，未纳入默认包。
