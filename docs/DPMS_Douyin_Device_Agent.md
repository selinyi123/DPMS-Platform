# DPMS 抖音设备代理最小闭环

抖音真实路径为 `douyin_device_v1`。Windows 主机运行纯 Python
`device_agent`，Docker 内的 `worker-douyin` 通过
`http://host.docker.internal:8765` 调用。服务只监听 `127.0.0.1`，全部
端点使用同一个 Bearer Token；Token、ADB 序列号和设备账号原文都不会写入
DPMS 证据。

## 1. 准备校准文件

复制 `device_agent/examples/calibration.example.json`，用目标抖音版本的只读
UI dump 审核并替换全部占位选择器。必须配置：

- `com.ss.android.ugc.aweme` 包名；
- 关注、点赞、评论、收藏四类动作的精确触发与完成态；
- CAPTCHA、人脸验证、操作频繁、账号异常等阻断文本；
- 每个候选目标的 `target_hash`、原帖唯一标记、作者 handle 和作者唯一标记。

固定坐标、模糊匹配和验证码绕过均不受支持。占位 Manifest 会失败关闭。

## 2. 启动 Windows 主机服务

在仓库根目录执行；Token 使用 32 至 512 字节随机值，不要写进命令行或仓库：

```powershell
$env:DPMS_DEVICE_AGENT_BEARER_TOKEN = '<32-512-byte-random-token>'
python -m device_agent serve `
  --manifest D:\secure\douyin-calibration.json `
  --adb-path C:\Android\platform-tools\adb.exe `
  --serial DEVICE_SERIAL `
  --account-id local-device-account `
  --state-dir D:\secure\dpms-device-state `
  --port 8765 `
  --adb-timeout-seconds 15 `
  --operation-timeout-seconds 60
```

启动只建立 HTTP 服务，不读取屏幕、不点击设备。它会输出 `agent_id`、
`manifest_sha256`、`device_serial_sha256` 和 `account_id_sha256`；只保存这些
公开哈希到 DPMS。

只读健康检查：

```powershell
$headers = @{ Authorization = "Bearer $env:DPMS_DEVICE_AGENT_BEARER_TOKEN" }
Invoke-RestMethod http://127.0.0.1:8765/health -Headers $headers
```

`status` 必须为 `ok` 且 `ready` 必须为 `true`。没有 ADB 设备或设备不可用时，
服务仍可启动，但健康结果为 `ready: false`，不会执行动作。

## 3. 配置 DPMS

在 `.env` 设置与主机进程相同的 Token：

```dotenv
DOUYIN_DEVICE_AGENT_URL=http://host.docker.internal:8765
DOUYIN_DEVICE_AGENT_TOKEN=<same-token>
DOUYIN_DEVICE_AGENT_TIMEOUT_SECONDS=45
```

通过运维界面或 `PUT /api/lotteries/adapters/config` 保存以下配置；四个值必须
与主机启动输出完全一致：

```json
{
  "config": {
    "douyin": {
      "device_agent": {
        "agent_id": "<sha256>",
        "manifest_sha256": "<sha256>",
        "device_serial_sha256": "<sha256>",
        "account_id_sha256": "<sha256>"
      }
    }
  }
}
```

创建或更新抖音设备账号时，在账号 API 的 `encrypted_credential` 输入框提交下列
非秘密身份信封（Core 会使用账号凭据 AAD 加密后存库）。这里绝不能放 Bearer
Token；四个哈希同样必须与主机输出一致：

```json
{
  "contract_version": 1,
  "credential_kind": "device_agent",
  "device_agent": {
    "agent_id": "<sha256>",
    "manifest_sha256": "<sha256>",
    "device_serial_sha256": "<sha256>",
    "account_id_sha256": "<sha256>"
  }
}
```

该账号会进入 `device_agent` 校准队列；Worker 只调用 `/health`，身份完全匹配且
设备 ready 后才把账号置为 ready，不会在校准阶段执行动作。

启动 Docker 侧服务：

```powershell
docker compose up -d --build core-douyin-runner worker-douyin
docker compose ps core-douyin-runner worker-douyin
```

Worker 容器健康只表示任务循环、MySQL 和 Redis 正常，不把真实设备可用性
伪装成容器健康。设备健康在每次 Probe、Shadow 和 real-run 前单独校验。

## 4. 执行门禁与结果

真实任务必须依次具备：已审核规则快照、可执行 Action Plan、ready 抖音账号、
精确设备配置、同一账号/目标/规则/计划/配置绑定的 Probe 与 Shadow 证据。每个
真实动作开始前先持久化 external-action intent；成功或明确拒绝会结算，超时、
传输中断、回执损坏或提交后状态不明会标记 `unknown` 并隔离账号与任务，禁止
盲目重试。

无设备时的精确阻断码：

- 主机服务未运行或容器不可达：`douyin_device_health_unreachable`；
- 服务运行但 ADB/设备未就绪：`douyin_device_health_not_ready`；
- 尚未生成同一绑定的 Probe + Shadow：Core 返回
  `exact_execution_evidence_required`；
- Manifest 与当前目标/UI 不匹配：`douyin_device_snapshot_not_ready` 或
  `douyin_device_action_not_calibrated:<action>`；
- 动作调用超时或结果不明：external intent 进入 `unknown`，任务进入
  reconciliation quarantine。

这些阻断都发生在未确认安全的动作之前；系统不解决或绕过任何平台验证。
