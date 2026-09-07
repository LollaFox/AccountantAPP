# 小票同步

这是一个本地优先的小票录入应用：iPhone 负责拍照、离线排队和查看汇总；Windows 或 macOS 电脑负责 PaddleOCR、人工审核、账目保存及按大类汇总。

## 当前功能

- iPhone 拍摄小票并保存到本地队列。
- 拍照后立即同步；电脑不可达时自动保留，回到同一局域网后重试。
- iOS 后台刷新会机会性重试，但实际时间由系统决定，不能保证精确间隔。
- Windows 或 macOS 使用 PaddleOCR 3.7.0 识别，原图、OCR 原文和结构化结果保存在本机。
- 小票必须在电脑审核页确认后，才会计入手机汇总。
- 手机显示月度总收入、总支出、结余、收入大类、支出大类和待处理小票数。
- 电脑审核页按香港月份显示收支流水；流水标明日期、类型、大类、内容、金额和来源。
- 电脑审核页可以删除整张小票（连同流水、商品明细和原始图片）或删除手工单笔录入；删除前会二次确认，并自动重导出 CSV。
- 电脑删除小票后，iPhone 会在下次同步时移除对应的本地队列记录。
- 手机和电脑均可手工登记收入或支出。
- 一台电脑保存共享账本，多台手机以各自独立凭证连接；可分别设置「可录入」或「只读汇总」，并单独撤销。
- 本机管理页使用 Passkey 解锁；为手机签发三分钟、单次使用的二维码邀请。
- 手工收支离线持久排队；明确被服务器拒绝的条目可修改，不阻塞其他记录和汇总。重复提交使用同一请求 ID，避免重复入账。
- 服务重启自动恢复尚未完成的 OCR 队列；慢连接有超时和并发上限。
- 确认后的数据自动导出为 UTF-8 CSV，便于 Excel 导入。

餐饮口径：有价菜品和饮品逐项记录；服务费、茶芥、附加费和收款舍入合并成一条“餐饮非菜品（合并）”；`Welcome`、“不用加配”等 HK$0 项目不录入。

## Windows 电脑端

要求：Windows 10/11，以及已经安装 PaddleOCR 的 Python。本机默认使用：

```text
C:\Users\15610\Documents\ChatGPT\DesktopAssistant\.venv-paddle\Scripts\python.exe
```

在 PowerShell 中运行：

```powershell
& "C:\Users\15610\Documents\ChatGPT\DesktopAssistant\.venv-paddle\Scripts\python.exe" -m pip install -r pc/requirements-server.txt
.\pc\start.ps1
```

也可以直接双击项目根目录的 `start-service.cmd`。服务运行期间请保持窗口开启；按 `Ctrl+C` 可停止服务。

启动后：

- 电脑审核页：`http://localhost:8764`（必须在服务器本机打开）
- iPhone 加密同步端口：`8765`
- 数据目录：`%LOCALAPPDATA%\ReceiptSync`
- Paddle 模型：`%LOCALAPPDATA%\ReceiptSync\paddle_models`
- CSV：`%LOCALAPPDATA%\ReceiptSync\exports`

首次识别会下载固定的 `PP-OCRv6_medium_det` 和 `PP-OCRv6_medium_rec` 模型。正式服务和临时测试的数据目录彼此独立，但统一复用上述持久、可写的模型缓存；删除测试数据不会删除模型。只有手工删除模型目录或以后明确升级模型版本时才会重新下载。

在电脑审核页先创建或验证管理员 Passkey，然后按下文「安全配对与恢复」为每台设备单独授权。

## macOS 电脑端

与 Windows 共用同一套 Python 服务、审核页、账务规则和 iPhone API；不要复制 Windows 的数据库、证书或配对令牌。要求 macOS 14 或更高版本，以及 Python 3.9+（建议 3.10–3.12）。首次在本机安装 OCR 环境：

```bash
chmod +x pc/setup_macos.sh pc/start.sh pc/generate_certificate.sh start-service.command
./pc/setup_macos.sh
```

之后在终端运行：

```bash
# 已有旧版环境时，先安装新增的 Passkey 与本地二维码依赖：
pc/.venv/bin/python -m pip install -r pc/requirements-server.txt
./pc/start.sh
```

也可以双击 `start-service.command`。若系统提示无法打开，可在 Finder 中右键该文件并选择打开。服务运行期间请保持窗口开启；按 `Ctrl+C` 可停止服务。首次绑定 `8765` 时，系统可能弹出防火墙询问，只允许 Python 接受入站连接即可，不要开放审核端口 `8764`。

启动后：

- 电脑审核页：`http://localhost:8764`（`127.0.0.1` 会跳转至此）
- iPhone 加密同步端口：`8765`
- 数据目录：`~/Library/Application Support/ReceiptSync`
- Paddle 模型：`~/Library/Application Support/ReceiptSync/paddle_models`
- CSV：`~/Library/Application Support/ReceiptSync/exports`

只测试电脑页面时，可运行 `./pc/test-local.sh` 或双击 `test-local.command`。它使用独立的 `ReceiptSync-LocalTest` 数据目录，只开放 `http://127.0.0.1:8764`，没有 HTTPS，不能用于 iPhone 同步。

换服务器时，在 iPhone「同步设置」中先忘记原电脑，再配对新电脑。旧队列保留原服务器身份，不会被发送到新账本。仅 IP 改变时可修改地址并测试保存，无需忘记服务器；不会自动扫描整个局域网。

## 安全配对与恢复

1. 在这台 Mac 的浏览器打开 `http://localhost:8764`，创建管理员 Passkey。以后用 Touch ID、设备解锁或安全密钥解锁管理页。此操作需要支持 WebAuthn 的浏览器与验证器；真实 Safari / Touch ID 行为仍需本机验收。
2. 在「Passkey 与恢复」中生成离线恢复代码，并妥善保存到独立、安全的位置。代码只显示一次；重新生成后旧代码立即失效。可用另一验证器添加备用 Passkey。
3. 在「手机配对」中为设备命名、选择权限，生成一次性二维码。此操作会要求再次验证 Passkey。
4. 在 iPhone「同步设置」中扫描这个二维码，核对它来自自己的 Mac，再点确认连接。不支持扫码时，可复制邀请内容粘贴到应用；也可使用手动创建设备密钥的备用方式。
5. 每个二维码三分钟有效、只能授权一台设备；关闭窗口会取消未用邀请。长期设备密钥由手机随机生成，先保存到 Keychain 再发送 HTTPS 配对请求；二维码不包含长期密钥。若响应丢失，在应用中重试保存的配对请求，不要立即生成另一把密钥。

二维码与恢复代码都是敏感授权材料，不要截图分享、上传或写入日志。管理会话只保留在页面内存中，十分钟后锁定；新标签页不会继承解锁状态。设备授权、添加/撤销 Passkey、生成恢复代码均需要一次新的验证。丢失设备可在设备列表撤销，只切断该设备访问，不删除账目。

丢失 Passkey 时，在本机锁定页输入离线恢复代码，再创建替代 Passkey。恢复授权本身不能读账本或添加设备；只有替代 Passkey 成功保存后代码才消耗，取消创建可重试。随后撤销丢失的旧 Passkey 并生成新恢复代码。最后一个有效 Passkey 不能直接删除；所有 Passkey 与恢复代码都丢失时，没有无认证清空/重置入口。

升级时原共享密钥仅作为标记为旧版的设备保留，以便逐台迁移；为每台手机建立新凭证并验证后，可撤销旧版凭证。撤销不会因重启而恢复。只读设备不能上传或手工记账。

## iPhone 端

必须在 Mac 上用 USB 线安装。不需要付费 Apple Developer Program；用 iPhone / App Store 同一个免费 Apple ID 即可。Windows 无法签名或生成可直接安装的 `.ipa`。

1. 用数据线连接 iPhone，解锁，并点“信任此电脑”。
2. 第一次安装前，打开 Xcode → Settings → Accounts，点 `+`，登录上述免费 Apple ID。
3. 在 Xcode 打开 Window → Devices and Simulators，等到列表里出现这台 iPhone。开发者模式在 Xcode 认出手机之前是隐藏的，不要在「通用」里找。
4. 双击 `install-iphone.command`，或在终端运行 `./ios/install-iphone.sh`。若安装时才出现「开发者模式」，到设置 → 隐私与安全性最底部打开，重启后再跑一次安装器。
5. 若 App 打不开：设置 → 通用 → VPN与设备管理，信任该 Apple ID。
6. 在应用「同步设置」中扫描本机管理页的单次二维码并确认连接；手动方式仍可填写 HTTPS 地址、独立设备密钥和证书指纹。

安装器会把本机签名信息写到 `ios/install-local.env`（不要提交）。免费 Apple ID 的开发签名大约 7 天过期，用同一条线再跑一次安装器即可，不必为此购买开发者账号。也可以继续用 Xcode 打开 `ios/ReceiptSync.xcodeproj` 后按 Run。长期对外分发才需要付费 Apple Developer 账号；当前不做 TestFlight 或 App Store 上架。

## 大学公共 Wi-Fi

公共 Wi-Fi 上只有 HTTPS 同步端口对局域网开放；电脑审核页只绑定 `127.0.0.1`，其他设备无法访问。

安全措施：

- 电脑首次启动生成 3072 位 RSA 本地证书。
- iPhone 固定核对证书 SHA-256 指纹，不接受其他证书，避免中间人冒充电脑。
- 新设备使用 256 位随机独立同步密钥，保存在 iPhone Keychain，服务器仅保存密钥哈希。
- 未带正确密钥的远程请求会被拒绝；连续失败会触发速率限制。
- 可录入设备密钥只能查询连接身份、上传小票、查询已知小票状态、读取月度汇总和手工记账，不能列出全部流水、读取 OCR 原文、审核、重新识别或删除记录。
- 审核数据、配对管理和删除接口要求本机回环连接与 Passkey 会话，拒绝伪造 Host 与跨来源管理请求。
- TLS 握手在有上限的工作线程中完成；慢握手、请求头和请求体均有超时，单个未完成握手不再阻塞其他设备。
- 单张图片限制为 12 MB，只接受 JPEG/PNG；汇总仅统计人工确认记录。
- Windows 防火墙只需要放行 TCP `8765`，不要开放审核端口 `8764` 或其他端口。macOS 同样只允许入站 TCP `8765`。

Passkey 仅用于服务器本机管理：浏览器以稳定的 `localhost` 为 WebAuthn RP，验证完整来源（包括端口），不把变化的局域网 IP 当作 RP。手机后台同步仍使用可撤销的设备凭证，不会每次要求 Face ID。此方案不需要外部域名、云中转或 iPhone Associated Domains；也不提供远程管理页。Passkey 不等于磁盘加密，不能防御已控制 Mac 账户或服务文件的攻击者。

部分校园网启用了客户端隔离，即使手机和电脑连接同一 Wi-Fi 也不能互相访问。此时应用会保留手机队列，但无法直接同步。在暂不使用云中转或 VPN 的前提下，可让电脑连接 iPhone 个人热点，形成只属于这两台设备的局域网。

## 数据口径

- `总收入`：选定月份内所有已确认收入之和。
- `总支出`：选定月份内所有已确认支出之和。
- `结余`：总收入减总支出。
- 大类汇总：由电脑数据库计算并返回手机，手机不自行重算。
- 待审核或识别失败的小票不进入总收支。

## 目录

```text
pc/
  receipt_sync_server.py   HTTPS、SQLite、汇总和审核服务
  receipt_passkeys.py      localhost Passkey、短时管理会话与离线恢复
  receipt_pairing.py       单次邀请与可重试的原子设备授权
  requirements-server.txt Passkey 与本地二维码依赖
  receipt_parser.py        小票金额与餐饮分项规则
  start.ps1                Windows：同时启动本机审核页和手机 HTTPS 接口
  start.sh                 macOS：同上
  generate_certificate.ps1 Windows 证书生成
  generate_certificate.sh  macOS OpenSSL 证书生成
  setup_macos.sh           创建 macOS PaddleOCR 虚拟环境
  test-local.sh            macOS 本机 HTTP 测试入口
  web/index.html           电脑审核页面
ios/
  ReceiptSync.xcodeproj
  ReceiptSync/             SwiftUI iPhone 客户端
  install-iphone.sh        USB 安装到已连接的 iPhone（免费 Apple ID）
install-iphone.command     Finder 双击入口，调用上面的安装脚本
```

当前版本未接入 DeepSeek，也未实现外出访问、云端中转或 VPN。OCR 结果会保留 AI 分类字段的扩展空间，但敏感小票内容不会自动发送给第三方。

自动化验证范围、重跑命令与尚需真机完成的检查见 [连接验证记录](docs/connection-verification.md)。明确拒绝的手工记录可在手机编辑；冲突记录请先在 Mac 核对，再决定是否仅从本机队列移除。认证、证书或存储故障仍会停止整轮同步，不会作为普通条目错误跳过。
