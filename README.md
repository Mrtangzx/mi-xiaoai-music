# 小爱音乐本地服务

由 Songloft 2.13.8 和 Python 音乐助手组成。当前 Windows 本地部署只监听回环地址。

## 在已部署的电脑上使用

1. 打开 Docker Desktop，等待引擎就绪。
2. 双击 `start.bat`，启动并检查服务。
3. 主界面：http://127.0.0.1:58091 。双击 `open-songloft.bat` 复制管理员密码；用户名 `admin`，粘贴密码并勾选协议后登录。八个灰色圆点是占位符，表示尚未填写密码。
4. 音乐助手：http://127.0.0.1:58092 。双击 `open-music.bat` 复制访问密钥，粘贴后点“连接”。Token 是访问密钥，不是要输入的文字。
5. 双击 `stop.bat` 停止音乐服务，不删除数据。

## 从源码初始化

Windows 需要 Python 3.10+、FFmpeg/FFprobe 和 Docker Desktop。必要时设置 `HOME_MUSIC_PYTHON` 为 Python 可执行文件的完整路径，再运行 `start.bat`。首次启动会在本机 `runtime/` 生成随机凭据。Git 仓库不包含密钥、数据库、音频、第三方二进制或账号状态。

Linux 家庭服务器部署参考 [原始部署文档](docs/linux-deployment.md) 与 `setup.py`。两种部署分别使用自己的运行时配置。

## 数据与限制

- `runtime/`：本地凭据及进程记录；不要上传或分享。
- `local-data/`：音乐助手数据库、音频及来源记录。
- `songloft-local-data/`：Songloft 数据与登录状态。
- `songloft-local-music/`：Songloft 曲库。
- 以上目录应一起备份；迁移时先停止两个音乐进程。
- 当前未完成小米账号登录、MIoT 插件绑定、真实音箱播放及音乐助手到主曲库的自动同步。回环地址暂时不供局域网音箱访问。
- Songloft 镜像固定到摘要，容器内将该版本已知统计域名映射到回环地址，并清空代理。此措施只约束已知域名，不代表移除了全部遥测或验证所有插件出站行为。不要替换为未受该约束的原生二进制。

## 验证

运行 `python -X utf8 local_runner.py verify` 检查网页、本地管理员登录、曲库和音频哈希；运行 `python -X utf8 -m unittest discover -s tests -v` 执行离线测试。Windows 的符号链接测试可能需要系统权限；缺少分发样例时相关测试会跳过。浏览器登录已单独回归验证，不等于真实音箱验收。

## 上游与许可

本仓库不复制或分发 Songloft/MIoT 源码、镜像内容或音乐文件，仅引用上游镜像与接口；使用时请遵守 [Songloft](https://github.com/songloft-org/songloft) 及 [MIoT 插件](https://github.com/songloft-org/songloft-plugin-miot) 的相应许可证。音乐助手仅检索明确标注开放授权的 Wikimedia Commons 录音，保留作者和授权信息；录音的许可独立于程序代码。
