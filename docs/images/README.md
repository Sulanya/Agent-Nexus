# AN 界面截图

三张图均运行完整的 ui/index.html 前端，由独立演示后端提供虚构项目、历史消息和 WebSocket 连接。原有页面渲染、会话加载、连接指示与品牌动画实际运行；没有重拼聊天 DOM、覆盖连接状态或另造静态页面。

## 查看图片

- [电脑端：1440 × 960](desktop.png)
- [平板端：1024 × 1100](tablet.png)
- [手机端：430 × 932](mobile.png)

截图前自动检查绿色“在线”、会话渲染完成与页面无横向溢出，并等待品牌动画稳定。图中对话、项目和构建结果全部虚构；在线表示前端已连接隔离演示后端，不代表连接了正式 Agent 或发生真实模型执行。

## 重新生成

安装源码依赖和 Node.js 22 后执行：

```bash
python tools/render_demo_screenshots.py
```

默认使用 Windows Edge，可通过 --browser /path/to/chromium 指定兼容浏览器。demo_backend.py 只在临时 loopback 端口运行，所有写操作返回 403，不接入任何 Agent、用户聊天库或认证文件。浏览器使用临时独立配置，不读取个人浏览器档案；capture_demo.cjs 用 CDP 设置真实视口并截图。临时进程、配置目录和 HTTP 服务在退出时清理。

图片随项目 MIT 许可发布。这是浏览器模拟视口截图，不等于平板、手机实机兼容性认证。
