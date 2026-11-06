# railway-env-report

可在 Railway 运行的只读环境报告与深色中文仪表盘，参考 [botkeep-env-report](https://github.com/ishaasu7/botkeep-env-report)。仅使用 Python 标准库，无第三方 Python 依赖。

本仓库只包含脱敏源码、测试和部署说明，不包含任何凭据、私有配置、真实运行报告或已部署实例。

## 功能

- 展示操作系统、内核、CPU、内存、磁盘、负载和运行时间。
- 分开显示 cgroup 容器配额与主机可见资源，避免把主机数据当成 Railway 分配额度。
- 展示网络接口、DNS、路由、本地候选监听器及常见 CLI 的路径与版本。
- 每 60 秒重新采样并刷新页面；首轮 CPU/网络速率显示为待采样。
- 提供 `/` 仪表盘、`/report.json` JSON 报告、`/healthz` 健康检查。
- 容量按 1024 进制换算，时间以北京时间（UTC+08:00）展示。

## 先了解公开风险

**应用默认没有身份认证。** 如果给它生成公网域名，仪表盘和 JSON 接口会公开运行环境中的主机名、运行用户、资源信息、网络接口/路由/DNS、本地监听器，以及查询成功时的公网出口 IP。不要把它部署到不适合公开环境库存的生产系统。

采集器不枚举环境变量、进程参数、密钥或私有配置文件。它只读取 `PORT` / `SERVER_PORT` 作为监听端口；启动时向 `https://api.ipify.org?format=json` 发起一次带短超时的 HTTPS 出口 IP 查询，失败不影响其他报告字段。它不会扫描外部主机、安装软件或修改系统设置。

如需限制访问，请不要生成公网域名，或在网关/网络层增加认证。不要把密钥放进 URL，也不要把真实报告提交到源码仓库。本项目本身不提供认证层。

## 本地运行

需要 Python 3.10 或更新版本；推荐 Python 3.12。

```bash
python3 web_service.py
```

默认监听 `0.0.0.0:8080`。可覆盖端口：

```bash
PORT=8080 python3 web_service.py
```

优先使用合法的 `PORT`，其次使用 `SERVER_PORT`，最后回退到 `8080`。只接受 1–65535 的数字端口。

一次性输出报告（只在你确认输出位置安全时使用）：

```bash
python3 inspect_environment.py
```

不要把输出或导出的真实 JSON 上传到这个公开仓库。

## 部署到 Railway

以下是部署者自行执行的说明；本项目发布源码不等于已经创建或部署 Railway 服务。

1. 在 Railway 新建项目，选择从此 GitHub 仓库部署。
2. 根目录的 `Dockerfile` 会被 Railway 自动识别。无需安装 Python 依赖，也无需覆盖启动命令；默认命令为 `python3 web_service.py`。
3. 应用监听 `0.0.0.0`，并优先使用 Railway 提供的 `PORT`。不要为了公开服务而硬编码平台分配端口。
4. 在服务的部署设置中把健康检查路径设为 `/healthz`。
5. 仅在接受上述公开风险后，才为服务生成公网域名。源码中没有预设域名，也不会自动验证公网入站可达性。

本项目不使用旧式 `railway.json` / `railway.toml` Config as Code；新服务可直接依赖 Dockerfile 构建，并在 Railway 面板配置健康检查。

官方参考：[Dockerfile 自动识别](https://docs.railway.com/guides/dockerfiles) · [公开网络与端口](https://docs.railway.com/guides/public-networking) · [健康检查](https://docs.railway.com/guides/healthchecks) · [旧 Config as Code 状态](https://docs.railway.com/config-as-code)。

## 使用 Docker

```bash
docker build -t railway-env-report .
docker run --rm -e PORT=8080 -p 127.0.0.1:8080:8080 railway-env-report
```

容器默认以非 root 用户运行。Dockerfile 只复制两个运行文件；`.dockerignore` 也使用允许清单，不把环境文件、工作日志或报告纳入构建上下文。

## 验证

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile web_service.py inspect_environment.py
```

测试在导入前阻断公网 IP 查询，HTTP 测试仅绑定本机回环地址并使用合成报告，不对外发布环境信息。测试结果不代表已经在真实 Railway 实例上完成部署验证。

## 数据解释

容器内的主机 CPU、负载、运行时间、文件系统容量可能来自共享宿主机；它们不是 Railway 套餐或配额承诺。优先参考 cgroup 配额，但不可用的字段会显示为空或不可用。本地监听和出口 IP 不能证明公网入站可达。

界面沿用 `KB/MB/GB/TB` 标签，按 `1 KB = 1024 B`、`1 GB = 1024 MB` 换算。这里的标签约定与 IEC 的 KiB/MiB/GiB 所表示的数值相同。

## 许可证

[MIT](LICENSE)。本项目不是 Railway 官方产品，也不包含任何 Railway 账号或部署凭据。
