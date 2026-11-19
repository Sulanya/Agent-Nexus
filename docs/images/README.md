# AN 界面截图

三张图均运行完整的 ui/index.html 前端，由独立演示后端提供虚构项目、历史消息和 WebSocket 连接。原有页面渲染、会话加载、连接指示与品牌动画实际运行；没有重拼聊天 DOM、覆盖连接状态或另造静态页面。

## 查看图片

- [AN 原版 Logo 与字标](logo.svg)：从前端原有字标路径导出，README 顶部使用；可运行 `python tools/export_brand.py` 重新生成。

- [三设备总览：1800 × 1120](overview.png)：由下列实际渲染截图缩放合成，额外添加说明与设备边框。
- [电脑端：2560 × 1800](desktop.png)，1280 × 900 CSS 视口，2 倍像素。
- [平板端：2048 × 2000](tablet.png)，1024 × 1000 CSS 视口，2 倍像素。
- [手机端：860 × 1864](mobile.png)，430 × 932 CSS 视口，2 倍像素。

截图前自动检查绿色“在线”、会话渲染完成与页面无横向溢出，并等待品牌动画稳定。图中对话、项目和构建结果全部虚构；在线表示前端已连接隔离演示后端，不代表连接了正式 Agent 或发生真实模型执行。

## 重新生成

安装源码依赖和 Node.js 22 后执行：

```bash
python tools/render_demo_screenshots.py
python tools/compose_demo.py
```

默认使用 Windows Edge，可通过 --browser /path/to/chromium 指定兼容浏览器。demo_backend.py 只在临时 loopback 端口运行，所有写操作返回 403，不接入任何 Agent、用户聊天库或认证文件。浏览器使用临时独立配置，不读取个人浏览器档案；capture_demo.cjs 用 CDP 设置真实视口并截图。临时进程、配置目录和 HTTP 服务在退出时清理。

总览使用 Pillow 生成，默认读取本机 Microsoft YaHei 字体；其他平台可自行调整字体路径。原图不加拼接布局，保留完整前端原生控件与样式。截图前扫描可见 DOM 中的私人用户名、旧域名、疑似密钥和单位标识。演示数据保存在 demo_backend.py，未读取任何正式会话。

图片随项目 MIT 许可发布。这是浏览器模拟视口截图，不等于平板、手机实机兼容性认证。
