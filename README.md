# Pixiv Auto Downloader NAS

基于 `pixivpy3 + ffmpeg` 的 Pixiv 收藏自动下载器，目标是在 NAS Docker 上低频运行。

## 功能

- 下载自己账号的公开收藏和私密收藏。
- 包含 R-18，只要 refresh-token 对应账号能看到就会下载。
- 普通单图、多图漫画、ugoira 都归为图片体系。
- ugoira 下载 zip 帧后默认转换成 GIF。
- SQLite 记录作品，避免重复下载。
- 支持停止标记和连续已下载停止。
- 网页端保存 refresh-token、测试 token、立即运行、手动下载单个作品。
- 日志和进度自动刷新。

## 下载目录结构

Pixiv 只有图片和 GIF，所以全部放在 `/downloads` 下，并按作品号分文件夹：

```text
/downloads/144905997/144905997_p00_星✨.png
/downloads/134550014/134550014_p00_黍与小男孩.png
/downloads/134550014/134550014_p01_黍与小男孩.png
/downloads/123456789/123456789_ugoira_标题.gif
/downloads/123456789/123456789.info.json
```

## Refresh Token

下载自己的公开收藏、私密收藏和 R-18 内容，需要使用你自己的 Pixiv `refresh-token`。

在电脑上先安装并运行：

```bash
python -m pip install gallery-dl
gallery-dl oauth:pixiv
```

命令会打印一个 Pixiv 登录链接，获取流程是：

```text
1. 复制命令行里给出的 Pixiv 登录链接，用浏览器打开。
2. 打开浏览器开发者工具 F12，切到 Network/网络 标签。
3. 登录 Pixiv。
4. 在 Network 里找到最后一个类似 callback?state=... 的请求。
5. 点开这个请求，复制 URL 里的 code 参数。
6. 回到命令行，把 code 粘贴到 gallery-dl 的 code: 提示后按回车。
```

注意：这个 `code` 大约 30 秒就会过期，所以要快一点；复制整个 callback URL 通常也可以，`gallery-dl` 会自己取里面的 code。

成功后，命令行会显示：

```text
Your 'refresh-token' is
```

下面那一行就是要保存的 `refresh-token`。NAS 上可以直接在网页端粘贴，也可以保存到：

```text
/volume2/docker/pixiv-auto-download/config/pixiv_refresh_token.txt
```

容器内路径是：

```text
/config/pixiv_refresh_token.txt
```

## 停止逻辑

默认停止标记：

```text
https://www.pixiv.net/artworks/119175141
```

拉取收藏时遇到这个作品 ID 就停止，并且不把这个作品作为本次下载对象。

日常增量同步还会使用：

```json
"stop_after_consecutive_done": 5
```

也就是连续遇到 5 个数据库里已成功下载且本地文件存在的作品，就停止继续翻页。

## NAS 部署

先创建目录：

```bash
mkdir -p /volume2/docker/pixiv-auto-download/config
mkdir -p /volume2/docker/pixiv-auto-download/state
mkdir -p /volume2/docker/pixiv-auto-download/downloads
mkdir -p /volume2/docker/pixiv-auto-download-app
```

把 `docker-compose.yml` 放到：

```text
/volume2/docker/pixiv-auto-download-app/docker-compose.yml
```

启动：

```bash
cd /volume2/docker/pixiv-auto-download-app
docker compose pull
docker compose up -d
```

网页端：

```text
http://NAS_IP:13004
```

## Docker 镜像

镜像固定版本，不使用 `latest`：

```text
ghcr.io/ccawmiku/pixiv-auto-download-nas:1.0.0
```

以后升级时：

```bash
git tag -a v1.0.1 -m "v1.0.1"
git push --follow-tags origin main
```

然后把 compose 里的镜像版本改到对应版本。

## 本地测试

安装依赖：

```bash
python -m pip install -r requirements.txt
```

先运行网页端：

```bash
python pixiv_auto_worker.py --config config.local.json
```

单次运行：

```bash
python pixiv_auto_worker.py --config config.local.json --run-once
```

调试收藏列表和少量原图时，也可以用探针脚本：

```bash
python pixiv_bookmark_list.py --config config.local.json --restrict public --max-pages 1 --download-images --download-limit 5 --pages-per-artwork 1 --print-summary
```
