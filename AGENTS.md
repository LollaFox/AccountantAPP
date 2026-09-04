# Receipt Sync Agent Handoff

本文档供接手本项目的编码 agent 使用，尤其是将在 Mac 上继续开发 iPhone 客户端和电脑端的 agent。它记录了截至 2026-09-04 的需求对话、技术选择、完成状态、运行方法、安全边界和下一步。面向普通用户的简明说明仍以 `README.md` 为准。

## 1. 工作目录和事实来源

- 当前 Mac 工作副本的源码根目录就是本文件所在目录。这是 Git 仓库。
- Windows 上的原始工作目录曾是 `work/receipt-sync-app/`；`outputs/receipt-sync-app/` 是较早的导出副本，不是继续开发的事实来源，不要在其中修改代码。
- Windows 上已有用户测试数据位于 `work/pc-local-test/`。未经用户明确同意，不要删除、清空、迁移或覆盖这些记录，也不要把它们拷到 Mac。
- 正式运行数据默认位于 `%LOCALAPPDATA%\ReceiptSync`（Windows）或 `~/Library/Application Support/ReceiptSync`（macOS），也不得在测试时随意改动。
- 测试用的数据库、证书、图片和模型目录属于运行产物，不应复制到 Mac 工程、提交或分发。
- 配对令牌、TLS 私钥和当前证书指纹属于敏感信息，不要写入源码、文档、日志样例或聊天回复。
- macOS OCR 虚拟环境是 `pc/.venv`（gitignored）。不要提交该目录，也不要提交 `*.pem`、`config.json` 或指纹文件。

## 2. 用户目标和完整需求脉络

以下是本项目对话中形成的需求和修正，按发生顺序整理。这里记录的是需求含义和决策，不是逐字聊天转录。

1. 最初目标是制作一份用于日常开支统计的会计财务表格，全部使用港币 HKD。已生成过独立工作簿 `outputs/daily-expense-tracker-hkd/日常开支统计表_HKD.xlsx`；当前工作的重点已经转为自动小票录入与手机同步应用。
2. 用户电脑已安装 PaddleOCR，希望用它识别小票并自动录入。曾用网上的小票示例以及用户提供的多张香港小票图片测试。
3. 报表既要直观，也要体现每个商品的花费。曾考虑用 DeepSeek 做商品分类，但当前版本没有接入 DeepSeek，也不能把敏感小票内容默认发送给第三方。
4. 总账必须同时覆盖购物、通勤、吃饭及收入，电脑负责把总收入、总支出和大类汇总返回手机。
5. 独立商品明细被确认可行：总收支流水和商品明细分层保存，避免主流水过度膨胀。
6. 餐饮小票必须列出每道具体菜品和有价饮品。只有真正的非菜品费用才合并，不是把饮料等消费合并。
7. “非菜品”特指服务费、茶芥、附加费和收款舍入等费用。这些费用统一成一条 `餐饮非菜品（合并）`，无需再分项计算。
8. HK$0 的 `Welcome`、`不用加配`、免费项目等不应录入明细。
9. 商店小票可能先印价格、下一行才印商品名；解析器必须支持这种版式。例如用户的小票中纸袋价格是 HK$2，而不是旁边或下一行的其他数字。
10. 餐饮小票的价格可能位于商品同一视觉行的右侧。曾发生拉面被误识别为 HK$1，原因是按 OCR 文本顺序抓到了数量/序号，而没有利用坐标选择同行最右侧价格。当前解析器用 OCR 坐标聚合视觉行并取最右金额，同时保留具体菜名。
11. 审核界面中不恰当的“（待确认）”展示已调整；确认小票后，大类、流水和汇总必须立即读取已确认数据并更新。
12. 单笔收入/支出录入曾出现不显示、未计入总收入的问题。当前已恢复，并统一按香港时区月份统计；保存后界面切换到该记录所在月份。用户确认相关记录确实在九月，因此月度切换和流水列表很重要。
13. 电脑审核页需要完整流水列表，展示日期、类型、大类、内容、金额和来源。
14. 支持删除：可删除整张小票及其关联流水、商品明细和原图，也可删除手工单笔录入；删除前确认，删除后重算汇总并原子重导出 CSV。
15. 手机负责拍照、离线排队、同步和看汇总；电脑（Windows 或 macOS）负责 PaddleOCR、人工审核、SQLite 存储和大类汇总。电脑删除一张小票后，手机下次查询该已知 UUID 收到 404，应清除对应本地队列项。
16. 暂不打包，不需要 `.ipa` 或安装包；先在本机与真机开发环境测试。
17. 暂不设计外出访问、Tailscale 或 VPN。手机与电脑只考虑同一局域网；大学公共 Wi-Fi 可能有客户端隔离，必要时使用 iPhone 个人热点做直连测试。
18. 公共 Wi-Fi 上必须有安全措施。当前使用 HTTPS、自签名证书指纹固定和受限随机设备令牌；电脑审核管理面仅监听回环地址。
19. 用户提出可以尝试 Passkey。结论是当前不实现：变化的局域网 IP 缺少稳定的 WebAuthn relying-party 域名，且后台同步不应每次要求 Face ID。若以后有稳定 HTTPS 域名或中转服务，可用 Passkey 做首次配对或管理授权，再签发受限设备凭证。
20. Paddle 模型不能每次测试都下载到新缓存。正式服务和临时测试必须复用 Windows 的 `%LOCALAPPDATA%\ReceiptSync\paddle_models` 或 macOS 的 `~/Library/Application Support/ReceiptSync/paddle_models`；只有显式升级模型或手工删除缓存时才重新下载。
21. 2026-09-04：用户要求在 Mac 上做一份电脑端（原 Windows PC part）的副本，以便同一台 Mac 既能跑审核/OCR 服务，又能用 Xcode 装 iPhone 客户端。结论是不复制 Python 账务/API 代码，只为 macOS 增加启动、证书和 Paddle 安装脚本，并与 Windows 共用 `pc/receipt_sync_server.py`、`pc/receipt_parser.py` 和 `pc/web/index.html`。换电脑或换系统后必须重新配对 iPhone，不要迁移 Windows 的 `config.json`、TLS 私钥或令牌。
22. 根目录曾同时有 `start-service.command` 和 `start-service.sh`。二者都只是调用 `pc/start.sh`。用户确认 `.command` 留给 Finder 双击，`.sh` 与 `./pc/start.sh` 重复，已删除 `start-service.sh`。

## 3. 当前产品行为

### Windows 电脑端

- 一个 Python 进程同时提供两个监听器和一个共享 OCR worker。
- `http://127.0.0.1:8764` 是仅限本机的审核管理页。
- `https://0.0.0.0:8765` 是 iPhone 加密同步接口；实际配对地址使用审核页检测到的局域网 IP。
- PaddleOCR 异步识别，结果在电脑端人工确认后才进入总收支。
- SQLite 保存小票、总流水和商品明细；原图保存在数据目录。
- 已确认数据导出为 UTF-8 CSV：`exports/transactions.csv` 和 `exports/line_items.csv`。
- 月度边界按 `Asia/Hong_Kong`/UTC+8 计算，而不是按服务器 UTC 日期直接截字符串。
- 金额以整数分存储，币种固定为 HKD，避免浮点误差。

### macOS 电脑端

- 与 Windows 共用 `pc/receipt_sync_server.py`、`pc/receipt_parser.py` 和 `pc/web/index.html`，行为与安全边界相同。
- 启动入口是 `pc/start.sh` / `start-service.command`；证书由 `pc/generate_certificate.sh` 用 OpenSSL 生成 3072 位 RSA PKCS#1 私钥。
- 正式数据目录是 `~/Library/Application Support/ReceiptSync`；模型缓存是其中的 `paddle_models`。
- OCR 虚拟环境在 `pc/.venv`，由 `pc/setup_macos.sh` 创建；不要使用 Windows 的 Python 路径或复制 Windows 的 `config.json`、TLS 私钥、配对令牌。
- Apple Silicon 上禁用 Paddle PIR executor，并保持 `enable_mkldnn=False`。

### iPhone 端

- SwiftUI 应用，最低 iOS 17，项目为 `ios/ReceiptSync.xcodeproj`。
- 拍照后 JPEG 压缩并写入 app 本地目录，立即尝试上传；电脑不可达时保留队列并重试。
- 后台刷新只是 iOS 提供的机会性重试，不能承诺固定同步周期。
- 手机显示所选月份的总收入、总支出、结余、收入大类、支出大类和待处理小票数。
- 汇总由电脑数据库计算，手机只展示，不自行重算总账。
- 手机可以新增手工收入/支出；金额必须大于零并按 HKD 分四舍五入。
- 配对令牌保存在 Keychain，使用 `kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`。
- TLS 客户端以 SHA-256 固定服务器叶证书（`SecTrustCopyCertificateChain` 取叶证书）。证书不匹配时必须拒绝连接。
- 月份选择、汇总查询 `YYYY-MM`、手工流水 `occurred_at` 和小票 `captured_at` 均按 `Asia/Hong_Kong` 编码，ISO 时间必须带 `+08:00` 时区，电脑才会入账到正确月份。
- 无相机时（模拟器）回退到照片图库；`Info.plist` 含 `NSPhotoLibraryUsageDescription`。只有相机可用时才设置 `cameraCaptureMode`。

## 4. 技术栈和关键文件

### 电脑端

- Python 标准库 HTTP 服务：`http.server.ThreadingHTTPServer`。
- TLS：Python `ssl`，最低 TLS 1.2。Windows 用 PowerShell 生成 3072 位 RSA 自签名证书；macOS 用 OpenSSL 生成同样规格的 PKCS#1 私钥和 PEM 证书。
- 数据库：SQLite 3，表为 `receipts`、`transactions`、`line_items`。
- OCR：PaddleOCR 3.7.0，当前固定 `PP-OCRv6_medium_det` 与 `PP-OCRv6_medium_rec`。
- 前端：单文件原生 HTML/CSS/JavaScript，无 Node 构建步骤。
- 测试：Python `unittest`，不依赖 pytest。

关键文件：

- `pc/receipt_sync_server.py`：HTTP/HTTPS API、鉴权、SQLite、OCR worker、汇总、删除和 CSV 导出。
- `pc/receipt_parser.py`：OCR 行、视觉坐标、总计/小计、商品和餐饮分项解析规则。
- `pc/web/index.html`：电脑审核、配对、流水、手工录入和删除界面。
- `pc/test_receipt_sync.py`：24 个回归测试（含 macOS 默认模型缓存路径和 OpenSSL 证书指纹）。
- `pc/start.ps1`：Windows 正式双端口单进程启动。
- `pc/generate_certificate.ps1`：兼容 Windows PowerShell 5.1 的证书和 PKCS#1 私钥生成。
- `pc/start.sh`：macOS 正式双端口单进程启动；终端入口，对应 `pc/start.ps1`。
- `pc/generate_certificate.sh`：macOS OpenSSL 证书和 PKCS#1 私钥生成。
- `pc/macos_paths.sh`：macOS 数据目录、模型缓存和 Python 查找；由启动脚本 source，不要单独执行。
- `pc/setup_macos.sh`：创建 `pc/.venv` 并安装 PaddlePaddle CPU 3.2.1 与 PaddleOCR 3.7.0。
- `pc/test-local.sh`：macOS 回环 HTTP 测试入口。
- `start-service.cmd`：Windows 供用户双击的正式启动入口。
- `start-service.command`：macOS Finder 双击入口，内部调用 `pc/start.sh`。不要再添加根目录 `start-service.sh`。
- `test-local.cmd`：只启动回环 HTTP 的本地 PC 测试入口，不能用于 iPhone。
- `test-local.command`：macOS 对应的本机测试入口，内部调用 `pc/test-local.sh`。

### 手机端

- Swift 5、SwiftUI、UIKit 相机桥接。
- `URLSession` + `CryptoKit` 做 HTTPS 请求和证书 SHA-256 固定。
- Security.framework Keychain 保存同步令牌。
- BackgroundTasks/后台 fetch 做机会性队列重试。

关键文件：

- `ios/ReceiptSync/ContentView.swift`：汇总、月份切换、相机、设置和手工收支界面。
- `ios/ReceiptSync/HongKongDate.swift`：香港时区月份键、月份标题和带偏移的 ISO 时间。
- `ios/ReceiptSync/ReceiptStore.swift`：离线队列、持久化、同步状态和月份刷新。
- `ios/ReceiptSync/SyncClient.swift`：受限 API 客户端和证书固定。
- `ios/ReceiptSync/Models.swift`：网络及本地数据模型。
- `ios/ReceiptSync/KeychainStore.swift`：令牌安全存储。
- `ios/ReceiptSync/AppDelegate.swift`：后台刷新注册与调度。
- `ios/ReceiptSync/CameraPicker.swift`：系统相机封装；模拟器回退相册。
- `ios/ReceiptSync/Info.plist`：相机、相册、本地网络和后台模式声明。
- `ios/ReceiptSyncTests/HongKongDateTests.swift`：香港月份边界、ISO 偏移和 HKD 格式。
- `ios/ReceiptSyncUITests/ReceiptSyncUITests.swift`：模拟器界面与可选的热点实机配对测试（无配对环境则 skip）。
- `ios/ReceiptSync.xcodeproj/xcshareddata/xcschemes/ReceiptSync.xcscheme`：共享 scheme，含 unit/UI test。

## 5. 解析和账务规则，不得回退

- 小票在 `confirmed` 之前绝不计入月度汇总或 CSV 正式流水。
- 餐饮：每个菜品/有价饮品一条 `餐饮菜品`；非菜品差额最多一条 `餐饮非菜品（合并）`。
- 餐饮非菜品金额优先按 `实付总额 - 小计` 得出，因此自然包含服务费和最终收款舍入；不要另建“舍入”条目。
- HK$0 欢迎项、免费项或无需加配项不进入明细。
- 有坐标时，同行金额取视觉上最右侧金额，避免把数量 `1` 当作菜价。
- 对价格先于商品名的超市版式，缓存价格行并绑定下一条有效商品名；折扣作用于前一个商品。
- 分项不可靠时保留警告供人工审核，不要为了让合计相等而虚构商品名或静默丢弃具体菜名。
- 手工流水 ID 使用 `manual-<UUID>`，不得覆盖小票流水 ID；小票流水不能通过“删除手工流水”接口删除。
- 删除小票必须一并删除其流水、明细和受管图片，但只能删除数据目录 `images` 内的文件。
- CSV 写入必须保持原子替换，避免同步或异常退出留下半份文件。

## 6. API 与安全边界

手机请求使用 `X-Sync-Token`。远程设备令牌只允许：

- `POST /api/v1/receipts`：上传 JPEG/PNG 小票，单张 32 字节至 12 MB。
- `GET /api/v1/receipts/<已知 UUID>`：查看这张手机已知小票的状态；不得带 `raw=1`。
- `GET /api/v1/summary?month=YYYY-MM`：读取大类汇总。
- `POST /api/v1/transactions`：新增手工收入或支出。

远程令牌不得访问：

- 全部小票列表、全部流水列表、配对信息或 OCR 原文。
- 确认、重新识别、删除小票或删除流水。
- 电脑审核页面。

只有本机回环连接且 Host 为 `localhost` 或回环 IP 时才能使用管理能力。不要为了方便 iPhone 调试而放宽这些边界。鉴权连续失败有速率限制；输入文本、金额、条目数和请求体大小均有限制。小票 ID 必须是规范 UUID，路径必须经过受管目录校验。

注意：`/api/v1/health` 当前可匿名访问，用于连通性判断，但不得返回配置、版本细节、令牌、账目或个人数据。

## 7. 如何在 Windows 运行

当前默认 PaddleOCR Python 路径为：

```text
C:\Users\15610\Documents\ChatGPT\DesktopAssistant\.venv-paddle\Scripts\python.exe
```

正式服务最简单的启动方法：

1. 在 Windows 打开 `work/receipt-sync-app/`。
2. 双击 `start-service.cmd`；也可在 PowerShell 中进入该目录后运行 `./pc/start.ps1`。
3. 保持终端窗口开启。
4. 浏览器打开 `http://127.0.0.1:8764`。
5. 在“手机配对”中读取本次可用的 HTTPS 地址、同步密钥和证书 SHA-256 指纹。
6. 停止服务时在终端按 `Ctrl+C`。

正式默认路径：

- 数据：`%LOCALAPPDATA%\ReceiptSync`
- 模型：`%LOCALAPPDATA%\ReceiptSync\paddle_models`
- CSV：`%LOCALAPPDATA%\ReceiptSync\exports`
- TLS：`%LOCALAPPDATA%\ReceiptSync\tls`

只测试电脑页面和 OCR 时，可双击 `test-local.cmd`。它使用 `work/pc-local-test/` 并只开放 `http://127.0.0.1:8764`。它没有 HTTPS 端口，iPhone 客户端会拒绝使用，不能拿它做真机同步测试。

Windows 防火墙只应按需要放行入站 TCP 8765。不要开放 8764。校园网 IP 会变化，不要把过去测试时出现过的 IP 写死到代码或设置说明中；总是使用当前配对页显示的地址。

## 8. 如何在 macOS 运行电脑端

1. 在 Mac 打开本仓库。
2. 若 `pc/.venv` 不存在，运行 `./pc/setup_macos.sh`。此 Mac 已于 2026-09-04 安装：PaddlePaddle CPU 3.2.1、PaddleOCR 3.7.0、Xcode 自带 Python 3.9。
3. 运行 `./pc/start.sh`，或双击 `start-service.command`。不要恢复已删除的根目录 `start-service.sh`。
4. 保持终端窗口开启。
5. 浏览器打开 `http://127.0.0.1:8764`。
6. 在“手机配对”中读取本次可用的 HTTPS 地址、同步密钥和证书 SHA-256 指纹。
7. 停止服务时在终端按 `Ctrl+C`。

正式默认路径：

- 数据：`~/Library/Application Support/ReceiptSync`
- 模型：`~/Library/Application Support/ReceiptSync/paddle_models`
- CSV：`~/Library/Application Support/ReceiptSync/exports`
- TLS：`~/Library/Application Support/ReceiptSync/tls`

只测试电脑页面时，运行 `./pc/test-local.sh`。它使用 `~/Library/Application Support/ReceiptSync-LocalTest`，只开放 `http://127.0.0.1:8764`，没有 HTTPS，不能用于 iPhone。模型仍复用上面的持久缓存。

不要把 Windows 的 `%LOCALAPPDATA%\ReceiptSync` 或 `work/pc-local-test` 拷到 Mac。换电脑后需要重新配对 iPhone。macOS 防火墙只应按需要放行入站 TCP 8765。

## 9. 如何在 Mac 安装并测试 iPhone 端

前提：Mac 安装 Xcode 16 或更高版本；真机运行 iOS 17 或更高版本。Windows 无法签名 iPhone 应用。

1. 当前 Mac 仓库已包含 `ios/ReceiptSync.xcodeproj`。不要从 Windows 拷贝运行数据、TLS 私钥或配对令牌。
2. 在 Mac 用 Xcode 打开 `ios/ReceiptSync.xcodeproj`。
3. 选择 `ReceiptSync` target，在 Signing & Capabilities 中选择用户自己的 Apple Team。
4. 把默认 `com.local.receiptsync` 改成用户账户下唯一的 Bundle Identifier。
5. 连接并信任 iPhone，选择该真机作为 Run Destination，然后 Build & Run。
6. 若电脑端跑在同一台 Mac 上，启动 `./pc/start.sh`；若仍使用 Windows，则在 Windows 上启动 `start-service.cmd`。确保手机与电脑位于同一可互访局域网。
7. 在 iPhone 应用“同步设置”中填写电脑配对页当时显示的完整 `https://<IP>:8765`、同步密钥和 64 位十六进制证书 SHA-256 指纹。

建议按以下顺序验收：

1. 下拉刷新，确认手机能读取当月汇总。
2. 手机新增一笔手工收入，在电脑九月/相应月份流水中确认出现且总收入增加。
3. 手机新增一笔手工支出，确认支出大类与结余更新。
4. 手机拍摄并上传小票，观察 `waiting/uploading/processing/review` 状态流转。
5. 在电脑审核具体菜品/商品、总额和大类并确认，确认手机汇总随刷新更新。
6. 在电脑删除该小票，手机下次同步应收到 404 并清除本地对应队列项。
7. 暂停电脑服务后再拍一张，确认手机保留失败队列；恢复服务后下拉刷新，确认可重试。
8. 校园 Wi-Fi 无法直连时，让电脑连接 iPhone 个人热点重新测试。这是网络客户端隔离问题，不应通过关闭 TLS、取消证书固定或扩大远程权限绕过。

免费 Apple ID 的开发签名通常需要定期重新安装；长期自用或分发需要合适的 Apple Developer 账号。用户已明确暂时不要打包，因此不要在未被要求时创建 `.ipa`、TestFlight 流程或发布配置。

## 10. 验证基线

截至 2026-09-04：

- 24 个 Python 自动化测试通过，含 macOS 默认模型缓存路径和 OpenSSL 证书指纹回归。
- Python 语法检查通过。
- `start.ps1` 和 `generate_certificate.ps1` 可在 Windows PowerShell 5.1 解析。
- PowerShell 5.1 与 macOS OpenSSL 生成的证书和 RSA 私钥可被 Python TLS 加载，计算出的证书 SHA-256 与保存指纹一致。
- 单 Python 进程双端口启动已在 Windows 和 macOS 验证。
- 真实局域网接口测试结果：带令牌读汇总为 200、手机手工记账为 201、带手机令牌读全量流水为 403、删除为 403、无令牌为 401、远程打开审核页为 404。
- 2026-09-04 同一套权限检查已在 iPhone 个人热点上，从这台 Mac 对 Windows HTTPS `8765` 复测通过；活证书 SHA-256 与配对页指纹一致。校园网客户端隔离下 ARP 邻居看不到 `8765`，不要靠扫描网段找电脑，只使用配对页当前地址。
- macOS 本机冒烟：审核页 200、配对 200、HTTPS health 200、手工收入 201 后当月总收入更新、伪造 Host 打开审核页为 404。冒烟数据已删除，未写入用户正式 `~/Library/Application Support/ReceiptSync`。
- 此 Mac 已运行 `./pc/setup_macos.sh`，`pc/.venv` 中 `import paddleocr` 成功（paddle 3.2.1）。首次真实识别仍会下载 PP-OCRv6 模型。
- iOS：Xcode 26.6 / iPhone 17 模拟器（iOS 26.5）Debug 构建成功，无新增编译警告。4 个 unit test 与 7 个离线 UI test 通过。热点实连时模拟器保存配对后显示「已同步」，并能读到当月汇总。
- 热点实连时从 Mac 向 Windows 正式账本写入过一笔 HK$0.01 测试收入（大类 `Mac同步测试`）。若仍在九月流水中，请在 Windows 审核页删除；不要把令牌、指纹或当时热点 IP 写进仓库。
- 相机、真机 Keychain、真机本地网络权限、后台刷新和完整小票拍照上传仍需按第 9 节用 iPhone 真机验收。

在 Windows 修改电脑端后至少运行：

```powershell
& "C:\Users\15610\Documents\ChatGPT\DesktopAssistant\.venv-paddle\Scripts\python.exe" -m unittest discover -s pc -p "test_*.py" -v
```

在 macOS 修改电脑端后至少运行：

```bash
python3 -m unittest discover -s pc -p "test_*.py" -v
bash pc/generate_certificate.sh /tmp/receipt-sync-tls-test
```

在 Mac 修改 iOS 端后至少完成：

```bash
xcodebuild test -project ios/ReceiptSync.xcodeproj -scheme ReceiptSync -configuration Debug \
  -destination 'platform=iOS Simulator,name=iPhone 17' -derivedDataPath ios/build CODE_SIGNING_ALLOWED=NO
```

- Xcode Debug build 无错误和新增警告。
- 上述命令应跑过 `ReceiptSyncTests` 与离线 `ReceiptSyncUITests`（`testLiveHotspotPairingAndSummary` 无配对环境时应 skip，不算失败）。
- 相机和局域网同步必须用真机检查；不要把配对令牌写进测试源码。
- 端到端验收序列仍以第 9 节为准，尤其是证书不匹配时拒绝连接、离线队列恢复和电脑删除后的 404 清理。

不要用正式 `%LOCALAPPDATA%\ReceiptSync`、`~/Library/Application Support/ReceiptSync` 或现有 `work/pc-local-test` 做破坏性自动化测试。新测试使用临时数据目录，但模型缓存仍指向持久共享缓存。

## 11. 已修复的高风险问题

继续开发时应保留相应回归测试：

- 小票 ID 路径穿越和越界图片删除。
- 手机令牌权限过大，可读取 OCR 原文、全量账目或执行管理操作。
- 手工流水 ID 覆盖小票流水。
- 删除小票与 OCR worker 完成之间的竞态导致记录复活。
- 畸形 HTTP body 影响持久连接中的后续请求。
- CSV 非原子写入。
- 首次启动时配置/令牌初始化竞态。
- Windows PowerShell 5.1 证书私钥导出不兼容。
- 双进程启动方式异常退出后留下后台服务；当前正式架构为单进程双监听器。
- 本地未知商户旧值的兼容迁移。
- iOS 在较新 Swift 上 `Task<Void, Never>.isCancelled` 无法编译；改为 `Task.isCancelled`。
- iOS 证书固定改用 `SecTrustCopyCertificateChain`，避免 `SecTrustGetCertificateAtIndex` 弃用警告。
- 模拟器无相机时若仍设置 `cameraCaptureMode` 会崩溃；仅在相机可用时设置，并声明相册用途。
- iOS 月份和 ISO 时间未带香港时区/偏移时，电脑会拒收或记到错误月份。

## 12. 尚未完成和建议下一步

当前最重要的下一步是 iPhone 真机验收（相机、Keychain、本地网络、后台刷新、拍小票上传），而不是继续扩展功能。模拟器与 Windows 电脑在 iPhone 热点上的配对/汇总/手工记账权限边界已经跑通。

尚未完成：

- iOS 工程尚未在真机测试相机、Keychain、证书固定、本地网络权限和后台刷新。
- 模拟器热点测试没有覆盖拍小票上传、电脑确认后汇总更新、电脑删除后 404 清队列、断线重试。
- macOS 电脑端尚未用真实小票跑完第一次 PaddleOCR（模型会在首次识别时下载到 `~/Library/Application Support/ReceiptSync/paddle_models`）。
- 尚未接入 DeepSeek 或其他第三方商品分类服务。
- 尚未实现 Passkey、外出同步、云中转、Tailscale 或 VPN。
- 尚未打包或发布。

后续若要接入 AI 分类，先明确数据是否允许离开电脑、供应商密钥如何保存、失败时的本地回退、费用和可审计性。默认应保持本地 PaddleOCR 和人工审核；不要让模型分类结果绕过人工确认，也不要把服务费或舍入重新拆成虚假商品。

后续若要接入 Passkey，优先限定为“首次配对/管理授权”，并保留短权限设备凭证执行后台同步。只有在有稳定、可信的 HTTPS 域名和正确的 Associated Domains/WebAuthn RP 配置后再实现。

## 13. Agent 工作约束

- 先读本文件和 `README.md`，再改代码。
- 保持 HKD、香港月份、人工确认后入账、电脑计算汇总这四个核心口径。
- 不要在 UI 中泄露同步令牌、私钥、OCR 原文或全量账目给远程调用方。
- 不要因为调试方便而允许 HTTP 局域网同步、跳过证书固定或开放电脑审核端口。
- 不要覆盖用户现有数据库或小票图片；数据库 schema 变更必须向后兼容并有测试。
- 修改解析器时添加真实版式的最小化 OCR 行回归测试，覆盖坐标关系和金额合计。
- 修改 API 权限时更新 `test_phone_token_has_only_required_endpoints` 一类安全边界测试。
- 修改 iOS 同步状态时同时检查离线重试、重复上传幂等、远端删除 404 和本地图片生命周期。
- 除非用户明确要求，暂不打包、发布、上传云端或引入第三方数据处理。
- 不要复制一份独立的 macOS Python 服务器；电脑端逻辑以 `pc/` 下的共享文件为准，只用 shell 脚本处理路径、证书和启动。
- 不要重新添加根目录 `start-service.sh`。终端启动用 `./pc/start.sh`，Finder 用 `start-service.command`。

## 14. 2026-09-04 进度（macOS 电脑端）

用户要求做一份 Mac 上的电脑端副本。已完成，且没有另起一套账务/API 实现。

做了什么：

- 共享现有 `pc/receipt_sync_server.py`、`pc/receipt_parser.py`、`pc/web/index.html`。Windows 与 macOS 行为、权限和账务口径相同。
- `AppConfig` 在未设置 `LOCALAPPDATA` 时，macOS 默认数据/模型目录为 `~/Library/Application Support/ReceiptSync`。设置了 `LOCALAPPDATA` 的旧测试仍走 Windows 路径。
- Apple Silicon 上 OCR 加载前设置 `FLAGS_enable_pir_in_executor=0` 和 `FLAGS_enable_pir_api=0`，并保持 `enable_mkldnn=False`。
- 新增 macOS 脚本（均在仓库内，不是单独 `mac/` 目录）：
  - `pc/start.sh`：正式双端口启动（审核 8764、HTTPS 8765）
  - `pc/generate_certificate.sh`：OpenSSL 3072 位 RSA PKCS#1 证书
  - `pc/macos_paths.sh`：路径和 Python 查找
  - `pc/setup_macos.sh`：创建 `pc/.venv`
  - `pc/test-local.sh`：仅本机 HTTP 测试
  - `start-service.command`：Finder 双击，调用 `pc/start.sh`
  - `test-local.command`：Finder 双击，调用 `pc/test-local.sh`
- 曾添加根目录 `start-service.sh`，与 `./pc/start.sh` 重复。用户要求删除后已删，文档同步更新。
- 回归测试增至 24 个：macOS Application Support 模型缓存、OpenSSL 证书可被 Python TLS 加载且指纹一致。
- 此 Mac 已执行 `./pc/setup_macos.sh`：PaddlePaddle 3.2.1 CPU + PaddleOCR 3.7.0，Python 3.9（Xcode）。
- 用临时目录做过双端口冒烟（审核页、配对、health、手工收入、伪造 Host），随后停止进程并删除临时数据，没有写入正式 Application Support 目录。

未做：

- 没有用真实小票跑第一次 OCR（模型尚未下载到正式缓存）。
- 没有做 iPhone 真机与电脑端的端到端同步验收。
- 没有打包、没有接入 DeepSeek、没有 Passkey/云中转。

## 15. 2026-09-04 进度（Mac 上的 iOS 客户端）

在这台 Mac 上第一次用 Xcode 编译并测试 iPhone 客户端。Windows 电脑当时在跑正式 `start-service.cmd`（HTTPS 8765）。不要把本次配对令牌、证书指纹或热点 IP 写入仓库。

构建与模拟器：

- 本机 Xcode 26.6，iPhone 17 模拟器 iOS 26.5。工程最低版本仍是 iOS 17。
- 首次 Debug 构建失败：`AppDelegate` 里 `Task<Void, Never>.isCancelled` 在当前 Swift 不能编译。已改为 `Task.isCancelled`。
- 去掉 `SecTrustGetCertificateAtIndex` 弃用警告，改为 `SecTrustCopyCertificateChain` 取叶证书再算 SHA-256。
- 模拟器没有相机：`CameraPicker` 在 `photoLibrary` 上回退，且不再对非相机 source 设置 `cameraCaptureMode`；`Info.plist` 增加相册用途说明。
- 月份和手工/小票时间改为 `HongKongDate`（`Asia/Hong_Kong` + 带冒号的 `+08:00`），与电脑 `_iso_datetime` / 香港月份边界一致。界面月份标题用简体中文。
- 新增 `ReceiptSyncTests`（4）和 `ReceiptSyncUITests`（7 个离线用例：首页未配对、连接电脑、设置校验、手工表单、相册回退、月份翻页）。共享 scheme 已加入工程。
- `ios/build/` 为本地 DerivedData，已 gitignore。

网络与配对：

- 校园网上看不到 Windows 的 8765，符合客户端隔离。不要全网扫描找电脑。
- 用户把 Mac 与 Windows 都连到 iPhone 个人热点后，从 Mac 可访问 Windows HTTPS 8765：匿名 `/api/v1/health` 为 200；审核口 8764 从 Mac 连不上；无令牌读汇总为 401；远程打开审核页为 404。
- 使用配对页令牌后：活证书指纹匹配、九月汇总 200、全量流水/小票列表/配对/确认/删除均为 403、未知 UUID 小票 404、手工收入 201。这与第 6 节远程令牌边界一致。
- iPhone 17 模拟器填入配对并保存后显示「已同步」，能读取当月汇总。
- 测试写入过一笔 HK$0.01 收入，大类 `Mac同步测试`。若还在 Windows 正式账本里，请在审核页删除。

未做：

- 没有接 iPhone 真机，没有测相机、真机 Keychain、真机本地网络弹窗、后台刷新。
- 没有在模拟器上测拍小票上传、电脑确认入账、电脑删除后 404 清队列、电脑停机后的离线队列。
- 没有改 Bundle Identifier / Apple Team；真机运行前仍需用户自己签名。

